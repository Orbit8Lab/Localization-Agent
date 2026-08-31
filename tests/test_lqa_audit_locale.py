"""Which language an external audit actually reviews.

`run_external_lqa` took its locale from `intake.target_locales[0]` and
ignored the pairs file entirely. Under a multi-locale intake whose first
entry was zh-CN, EVERY audit — ja, ko, zh-Hant — was configured as a
Simplified Chinese review: the zh-CN reviewer prompt, the zh-CN glossary,
the zh-CN gate, the zh-CN translation memory, and `locale: zh-CN` stamped
on the resulting report. The Japanese audit therefore flagged correct
Japanese as "uses Japanese instead of Simplified Chinese" on nearly every
line — a wrong-configuration failure that presents as a flood of genuine
findings, which is the hardest kind to notice.

The `lqa_run` agent tool made it worse: it accepted a `locale`, validated
it against the intake, named the run after it and counted that locale's
glossary — then never passed it to the cascade, and reported the
ARGUMENT back as the audited locale. Caller and callee disagreed and the
result claimed the caller was right.

Locale now comes from the input: explicit argument > the pairs file's own
`target_language` > the intake brief as a last resort.
"""
from __future__ import annotations

import json

import pytest

from orbit8.controller import Job
from orbit8.external_lqa import (LocaleConflict, pairs_locale,
                                 resolve_locale, run_external_lqa)
from orbit8.schemas import IntakeBrief

INTAKE = IntakeBrief(game="G", source_lang="en",
                     target_locales=["zh-CN", "ja", "ko"])


def _pairs(locale, n=2):
    return [{"key": f"k{i}", "source_text": f"s{i}", "target_text": f"t{i}",
             "source_language": "en", "target_language": locale}
            for i in range(n)]


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in rows) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------ resolution

def test_the_pairs_file_decides_not_the_intake_order():
    """The regression. A ja pairs file under a zh-CN-first intake is a ja
    audit."""
    assert resolve_locale(INTAKE, _pairs("ja")) == "ja"


def test_an_explicit_locale_wins_when_the_file_declares_nothing():
    bare = [{"key": "k", "source_text": "s", "target_text": "t"}]
    assert resolve_locale(INTAKE, bare, "ko") == "ko"


def test_the_intake_is_the_last_resort_not_the_default():
    bare = [{"key": "k", "source_text": "s", "target_text": "t"}]
    assert resolve_locale(INTAKE, bare) == "zh-CN"


def test_an_explicit_locale_contradicting_the_file_is_refused():
    """Silently preferring either side is how the original bug shipped:
    one of the two is a mistake and guessing produces an audit scored
    against the wrong language."""
    with pytest.raises(LocaleConflict) as err:
        resolve_locale(INTAKE, _pairs("ja"), "zh-CN")
    assert "ja" in str(err.value) and "zh-CN" in str(err.value)


def test_an_explicit_locale_agreeing_with_the_file_is_fine():
    assert resolve_locale(INTAKE, _pairs("ja"), "ja") == "ja"


def test_a_file_mixing_target_languages_is_not_one_audit():
    """The cascade audits one language pair at a time; reviewing the
    first line's language would score the rest against the wrong rules."""
    mixed = _pairs("ja") + _pairs("ko")
    with pytest.raises(LocaleConflict) as err:
        pairs_locale(mixed)
    assert "ja" in str(err.value) and "ko" in str(err.value)


def test_no_locale_anywhere_asks_for_one_instead_of_guessing():
    empty = IntakeBrief(game="G", source_lang="en", target_locales=[])
    bare = [{"key": "k", "source_text": "s", "target_text": "t"}]
    with pytest.raises(ValueError, match="--locale"):
        resolve_locale(empty, bare)


def test_blank_target_languages_are_not_a_declaration():
    rows = [{"key": "k", "source_text": "s", "target_text": "t",
             "target_language": "  "}]
    assert pairs_locale(rows) is None


# ------------------------------------------------- through the real audit

@pytest.fixture
def job(tmp_path):
    return Job.init(tmp_path / "jobs", "audit", intake=INTAKE,
                    source_files=[])


def test_the_report_is_stamped_with_the_audited_language(job, tmp_path):
    """`report.locale` names the xlsx the client receives. A ja audit
    stamped zh-CN mislabels the deliverable as well as misgrading it."""
    pairs = _write(tmp_path / "ja.jsonl", _pairs("ja"))
    report = run_external_lqa(job, None, pairs, name="ja-audit",
                              deterministic_only=True)
    assert report.locale == "ja"


def test_an_explicit_locale_reaches_the_cascade(job, tmp_path):
    bare = [{"key": "k", "source_text": "s", "target_text": "t"}]
    pairs = _write(tmp_path / "bare.jsonl", bare)
    report = run_external_lqa(job, None, pairs, name="ko-audit",
                              deterministic_only=True, locale="ko")
    assert report.locale == "ko"


def test_a_contradiction_stops_the_audit_before_any_model_call(job,
                                                               tmp_path):
    pairs = _write(tmp_path / "ja.jsonl", _pairs("ja"))
    with pytest.raises(LocaleConflict):
        run_external_lqa(job, None, pairs, name="x",
                         deterministic_only=True, locale="ko")


# ------------------------------------------------------- the agent tool

def _chat(tmp_path):
    from orbit8.llm import EchoProvider
    from orbit8.orchestrator import ChatOrchestrator
    job = Job.init(tmp_path / "proj" / "jobs", "j", intake=INTAKE,
                   source_files=[])
    return ChatOrchestrator(job, EchoProvider("ja"), operator="t",
                            dry_run=True)


def test_the_tool_passes_its_locale_to_the_cascade(tmp_path):
    """The tool validated `locale`, named the run after it and counted its
    glossary, then dropped it on the floor — so it announced a ja audit
    that ran as zh-CN."""
    chat = _chat(tmp_path)
    pairs = _write(tmp_path / "proj" / "ja.jsonl", _pairs("ja"))
    out = json.loads(chat._t_lqa_run(
        {"pairs": str(pairs), "locale": "ja", "deterministic_only": True}))
    assert out["status"] == "complete"
    assert out["locale"] == "ja"


def test_the_tool_reports_the_locale_the_cascade_used(tmp_path):
    """With no `locale` argument the tool used to report "" while the
    cascade silently reviewed target_locales[0]. It now reports what
    actually ran."""
    chat = _chat(tmp_path)
    pairs = _write(tmp_path / "proj" / "ko.jsonl", _pairs("ko"))
    out = json.loads(chat._t_lqa_run(
        {"pairs": str(pairs), "deterministic_only": True}))
    assert out["locale"] == "ko"


def test_the_tool_surfaces_a_contradiction_as_an_error(tmp_path):
    chat = _chat(tmp_path)
    pairs = _write(tmp_path / "proj" / "ja.jsonl", _pairs("ja"))
    out = chat._t_lqa_run(
        {"pairs": str(pairs), "locale": "ko", "deterministic_only": True})
    assert out.startswith("error:") and "LocaleConflict" in out
