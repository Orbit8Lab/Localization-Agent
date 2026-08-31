"""Pre-flight before a large batch.

Two modes already existed and neither answers "is this batch configured
correctly?":

- `dry_run` swaps in `EchoProvider`, so it exercises the plumbing and
  never sends a prompt. Every wrong-configuration bug this repo has hit
  was invisible to it — most recently an audit that reviewed Japanese
  under Simplified Chinese rules because the locale came from the intake
  order rather than the input. 400 strings later that returns 400
  confident findings, which reads as a quality catastrophe rather than a
  config error.
- `pilot` is 30 strings WITH client sign-off at G2 — a lifecycle phase,
  not a pre-flight.

So: real provider, tiny sample, nothing written. The last clause is the
load-bearing one and most of these tests are about it — a pre-flight that
consumes pending segments or advances the stage is a trap, because it
looks like a safety measure while shrinking the batch it was meant to
protect.
"""
from __future__ import annotations

import json

import pytest

from orbit8.controller import Job
from orbit8.memory import RunDB
from orbit8.schemas import IntakeBrief, UniqueString

INTAKE = IntakeBrief(game="G", source_lang="en",
                     target_locales=["zh-CN", "ja"])


@pytest.fixture
def job(tmp_path):
    j = Job.init(tmp_path / "jobs", "j", intake=INTAKE, source_files=[])
    for locale in INTAKE.target_locales:
        RunDB(j.store.run_db_path(locale)).seed(
            [UniqueString(uid=f"u{i:03d}", text=f"Start {i}", keys=[f"k{i}"])
             for i in range(20)])
    return j


# --------------------------------------------------------- it stays out

def test_a_smoke_run_does_not_consume_pending_segments(job):
    """The trap. `run_translate_stage` marks what it translates, so a
    smoke run against the live DB would silently shrink the real batch."""
    job.smoke(size=3)
    for locale in INTAKE.target_locales:
        live = RunDB(job.store.run_db_path(locale))
        assert len(live.refs("pending")) == 20


def test_a_smoke_run_cannot_advance_the_stage(job):
    """`derive()` reads artifacts, so writing one would move the job. A
    pre-flight must be repeatable at any point without side effects."""
    before = job.derive()
    job.smoke(size=3)
    after = job.derive()
    assert (before.phase, before.gate) == (after.phase, after.gate)


def test_a_smoke_run_writes_no_stage_artifacts(job):
    job.smoke(size=3)
    for stage in (4, 5):
        assert not (job.store.job_dir / f"s{stage}").exists()


def test_the_scratch_db_lives_outside_runs(job):
    """Everything under runs/ is real translation state."""
    job.smoke(size=3)
    smoke_dbs = list((job.store.job_dir / "smoke").glob("*.db"))
    assert smoke_dbs
    runs = {p.name for p in (job.store.job_dir / "runs").glob("*.db")}
    assert runs == {"zh-CN.db", "ja.db"}


def test_each_smoke_run_starts_clean(job):
    """A stale scratch DB would report the previous run's samples."""
    first = job.smoke(size=3, locales=["ja"])[0]
    second = job.smoke(size=2, locales=["ja"])[0]
    assert first.sampled == 3 and second.sampled == 2


# ------------------------------------------------------- what it reports

def test_it_samples_only_the_requested_size(job):
    result = job.smoke(size=3, locales=["ja"])[0]
    assert result.sampled == 3
    assert result.pending_total == 20
    assert len(result.samples) == 3


def test_it_reports_every_locale_not_just_the_first(job):
    """A multi-locale batch is exactly when a pre-flight earns its keep."""
    assert [r.locale for r in job.smoke(size=2)] == ["zh-CN", "ja"]


def test_a_locale_subset_is_honoured(job):
    assert [r.locale for r in job.smoke(size=2, locales=["ja"])] == ["ja"]


def test_an_unknown_locale_is_refused(job):
    with pytest.raises(ValueError, match="not target locales"):
        job.smoke(size=2, locales=["fr"])


def test_the_samples_carry_real_source_and_target(job):
    """Counts do not answer "is this the right language?" — only the text
    does, so the samples are the actual deliverable."""
    sample = job.smoke(size=1, locales=["ja"])[0].samples[0]
    assert sample["source"] == "Start 0"
    assert sample["target"]                       # EchoProvider renders it


def test_echo_mode_says_it_proved_nothing_about_prompts(job):
    """A green pre-flight that never sent a prompt is worse than none if
    it does not say so."""
    result = job.smoke(size=2, locales=["ja"])[0]
    assert any("NOT the prompts" in w for w in result.warnings)


def test_it_reports_the_resolved_config_not_just_a_verdict(job):
    result = job.smoke(size=2, locales=["ja"])[0]
    assert (result.source_lang, result.target_lang) == ("en", "ja")
    assert result.glossary_source == "none"       # nothing staged here
    assert result.style_brief is False


def test_no_pending_segments_is_reported_not_crashed(tmp_path):
    """Before INGEST there is nothing to sample; that is a finding about
    the job, not a failure of the pre-flight."""
    j = Job.init(tmp_path / "jobs", "empty", intake=INTAKE, source_files=[])
    result = j.smoke(size=3, locales=["ja"])[0]
    assert result.ok and result.sampled == 0
    assert any("no pending segments" in w for w in result.warnings)


# --------------------------------------------------- per-locale isolation

def test_one_broken_locale_does_not_hide_the_others(job, monkeypatch):
    """The whole reason to smoke a multi-locale batch: a raise on locale 1
    must not cost the verdict on locale 2."""
    real = Job._smoke_translate

    def boom(self, locale, provider, glossary, scratch, *, live):
        if locale == "zh-CN":
            raise RuntimeError("provider exploded")
        return real(self, locale, provider, glossary, scratch, live=live)

    monkeypatch.setattr(Job, "_smoke_translate", boom)
    results = {r.locale: r for r in job.smoke(size=2)}
    assert results["zh-CN"].ok is False
    assert "provider exploded" in results["zh-CN"].error
    assert results["ja"].ok is True


# --------------------------------------------------------- cost forecast

def test_the_projection_scales_the_sample_to_the_batch():
    from orbit8.schemas import SmokeResult
    r = SmokeResult(locale="ja", kind="translate", sampled=5,
                    pending_total=400, ok=True, tokens_spent=1000.0)
    assert r.cost_projection() == pytest.approx(80000.0)


def test_the_projection_is_zero_without_measurements():
    """Never invent a forecast: a dry run spends no tokens."""
    from orbit8.schemas import SmokeResult
    r = SmokeResult(locale="ja", kind="translate", sampled=5,
                    pending_total=400, ok=True, tokens_spent=0.0)
    assert r.cost_projection() == 0.0


# ------------------------------------------------------- the audit variant

def _pairs(tmp_path, locale, rows):
    path = tmp_path / f"{locale}.jsonl"
    path.write_text("\n".join(json.dumps(
        {"key": k, "source_language": "en", "target_language": locale,
         "source_text": s, "target_text": t}, ensure_ascii=False)
        for k, s, t in rows) + "\n", encoding="utf-8")
    return path


def test_the_audit_preflight_resolves_the_locale_from_the_input(job,
                                                               tmp_path):
    """The regression this whole feature is aimed at: intake[0] is zh-CN
    and the pairs file is ja."""
    from orbit8.external_lqa import smoke_audit
    pairs = _pairs(tmp_path, "ja", [("a", "Start Game", "ゲーム開始")])
    result = smoke_audit(job, None, pairs, size=1)
    assert result.locale == "ja"
    assert result.ok


def test_the_audit_preflight_reports_a_locale_conflict_first(job, tmp_path):
    """Cheapest possible failure, and the most valuable."""
    from orbit8.external_lqa import smoke_audit
    pairs = _pairs(tmp_path, "ja", [("a", "Start", "開始")])
    result = smoke_audit(job, None, pairs, size=1, locale="zh-CN")
    assert result.ok is False
    assert "LocaleConflict" in result.error


def test_the_audit_preflight_writes_no_report(job, tmp_path):
    """`run_external_lqa` opens an s5 attempt; the pre-flight must not,
    or it leaves a half-finished audit behind."""
    from orbit8.external_lqa import smoke_audit
    pairs = _pairs(tmp_path, "ja", [("a", "Start", "開始")])
    smoke_audit(job, None, pairs, size=1)
    assert not (job.store.job_dir / "s5").exists()


def test_the_audit_preflight_warns_when_everything_is_flagged(job,
                                                              tmp_path):
    """The signature of a wrong-language or wrong-glossary audit. Under a
    glossary demanding Chinese renderings, correct Japanese fails every
    term — which is precisely what the real bug produced, 450 times."""
    from orbit8.external_lqa import smoke_audit
    staged = job.store.stage_dir(3) / "t1.ja.staged.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(
        {"metadata": {"game": "G", "locale": "ja"},
         "terms": {"Game": {"translation": "游戏", "locked": True}}},
        ensure_ascii=False), encoding="utf-8")
    pairs = _pairs(tmp_path, "ja", [("a", "Game", "ゲーム"),
                                    ("b", "New Game", "ニューゲーム")])
    result = smoke_audit(job, None, pairs, size=2)
    assert result.glossary_terms == 1
    assert any("EVERY sampled string was flagged" in w
               for w in result.warnings)


def test_the_audit_preflight_names_a_missing_glossary(job, tmp_path):
    """"No terminology findings" with an empty termbase reads as a clean
    bill of health."""
    from orbit8.external_lqa import smoke_audit
    pairs = _pairs(tmp_path, "ja", [("a", "Start", "開始")])
    result = smoke_audit(job, None, pairs, size=1)
    assert result.glossary_source == "none"
    assert any("no glossary resolved" in w for w in result.warnings)


def test_the_audit_preflight_findings_carry_their_string(job, tmp_path):
    """A finding without its source is unverifiable by a human."""
    from orbit8.external_lqa import smoke_audit
    staged = job.store.stage_dir(3) / "t1.ja.staged.json"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text(json.dumps(
        {"metadata": {"game": "G", "locale": "ja"},
         "terms": {"Game": {"translation": "游戏", "locked": True}}},
        ensure_ascii=False), encoding="utf-8")
    pairs = _pairs(tmp_path, "ja", [("a", "Game", "ゲーム")])
    result = smoke_audit(job, None, pairs, size=1)
    assert result.findings
    assert result.findings[0]["source"] == "Game"
    assert result.findings[0]["target"] == "ゲーム"


def test_a_bad_pairs_file_is_reported_not_raised(job, tmp_path):
    from orbit8.external_lqa import smoke_audit
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"key": "a"}\n', encoding="utf-8")
    result = smoke_audit(job, None, bad, size=1)
    assert result.ok is False and result.error


# ---------------------------------------------------------- agent access

def test_the_preflight_is_in_the_tool_set():
    """A pre-flight only the CLI can reach is a pre-flight the agent will
    skip — and the agent is how the operator drives an audit."""
    from orbit8.orchestrator import ChatOrchestrator
    assert "lqa_smoke" in ChatOrchestrator.tool_names()


def test_the_agent_tool_reports_the_resolved_locale(tmp_path):
    from orbit8.llm import EchoProvider
    from orbit8.orchestrator import ChatOrchestrator
    j = Job.init(tmp_path / "proj" / "jobs", "j", intake=INTAKE,
                 source_files=[])
    chat = ChatOrchestrator(j, EchoProvider("ja"), operator="t",
                            dry_run=True)
    pairs = _pairs(tmp_path / "proj", "ja", [("a", "Start", "開始")])
    out = json.loads(chat._t_lqa_smoke({"pairs": str(pairs), "size": 1}))
    assert out["locale"] == "ja"          # NOT intake[0], which is zh-CN
    assert out["ok"] is True


def test_the_agent_tool_surfaces_a_conflict(tmp_path):
    from orbit8.llm import EchoProvider
    from orbit8.orchestrator import ChatOrchestrator
    j = Job.init(tmp_path / "proj" / "jobs", "j", intake=INTAKE,
                 source_files=[])
    chat = ChatOrchestrator(j, EchoProvider("ja"), operator="t",
                            dry_run=True)
    pairs = _pairs(tmp_path / "proj", "ja", [("a", "Start", "開始")])
    out = json.loads(chat._t_lqa_smoke(
        {"pairs": str(pairs), "locale": "zh-CN", "size": 1}))
    assert out["ok"] is False and "LocaleConflict" in out["error"]
