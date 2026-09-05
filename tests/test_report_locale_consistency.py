"""A deliverable must not be mislabelled.

`lqa report` names the client xlsx from `report.locale`, so a report
built with the wrong stored locale ships `Nomori_Bug_Report_zh-CN.xlsx`
containing a Japanese audit — or, worse, a Japanese audit that was
actually SCORED against Chinese rules. The command did this silently: it
read the artifact, took its locale, and wrote the file.

Three facts were available to catch it and none were consulted:

- the run NAME usually carries the locale (`lqa-ja-20260830`)
- the report carries its own stored `locale`
- `--locations-from` is a bilingual export stamped `target_language`

Any disagreement between them means at least one is wrong, and shipping
is the one recovery that cannot be undone — the client has the file. So
the command now refuses and explains, with `--force` for the operator who
knows better.
"""
from __future__ import annotations

import json

import pytest

from orbit8.cli import main
from orbit8.controller import Job
from orbit8.schemas import IntakeBrief, LQAReport

INTAKE = IntakeBrief(game="Nomori", source_lang="en",
                     target_locales=["zh-CN", "ja", "ko", "zh-Hant"])


@pytest.fixture
def job(tmp_path):
    return Job.init(tmp_path / "jobs", "nomori-lqa", intake=INTAKE,
                    source_files=[])


def _report(job, name, locale):
    """A stored report, as the buggy audit produced it."""
    attempt = job.store.new_attempt(5)
    job.store.write(5, f"lqa_report.{name}",
                    LQAReport(job_id="nomori-lqa", locale=locale, checked=1,
                              flagged_strings=0, findings_total=0,
                              confirmed=0, overturned=0, uncertain=0,
                              block_ship=False),
                    produced_by="test", attempt=attempt)
    return attempt


def _jsonl(tmp_path, locale):
    path = tmp_path / f"{locale}.jsonl"
    path.write_text(json.dumps(
        {"key": "a", "source_language": "en", "target_language": locale,
         "source_text": "Start", "target_text": "x"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)


def _run(job, *extra):
    return main(["lqa", "report", str(job.store.root), "nomori-lqa",
                 "--no-suggestions", *extra])


# ------------------------------------------------------- name vs. report

def test_a_ja_named_run_storing_zh_CN_is_refused(job, capsys):
    """The exact artifact on the server: audited as zh-CN, named ja."""
    _report(job, "lqa-ja-20260830", "zh-CN")
    assert _run(job, "--name", "lqa-ja-20260830") == 1
    err = capsys.readouterr().err
    assert "named 'lqa-ja-20260830'" in err and "locale='zh-CN'" in err
    assert "--locale ja" in err              # the recovery command


def test_the_refusal_says_the_findings_do_not_describe_the_locale(job,
                                                                  capsys):
    """The operator's real question is whether the report is salvageable.
    It is not: the findings were scored against the other language."""
    _report(job, "lqa-ja-20260830", "zh-CN")
    _run(job, "--name", "lqa-ja-20260830")
    assert "scored against zh-CN rules" in capsys.readouterr().err


def test_no_deliverable_is_written_on_a_refusal(job, tmp_path):
    """Refusing but writing the file anyway would be the worst outcome."""
    attempt = _report(job, "lqa-ja-20260830", "zh-CN")
    _run(job, "--name", "lqa-ja-20260830")
    assert not list(job.store.stage_dir(5, attempt).glob("*.xlsx"))


def test_an_agreeing_name_and_locale_builds_normally(job, capsys):
    _report(job, "lqa-ja-20260830", "ja")
    assert _run(job, "--name", "lqa-ja-20260830") == 0
    assert "Nomori_Bug_Report_ja.xlsx" in capsys.readouterr().out


def test_a_name_with_no_locale_in_it_is_not_second_guessed(job):
    """`--name dev-audit` says nothing about locale; inventing a
    complaint from it would block a legitimate run."""
    _report(job, "dev-audit", "ja")
    assert _run(job, "--name", "dev-audit") == 0


def test_zh_Hant_is_not_misread_as_another_locale(job):
    """Longest-match: with both zh-CN and zh-Hant configured, a zh-Hant
    run name must not resolve to a shorter sibling."""
    _report(job, "lqa-zh-Hant-20260830", "zh-Hant")
    assert _run(job, "--name", "lqa-zh-Hant-20260830") == 0


# -------------------------------------------------- locations vs. report

def test_a_locations_file_for_another_locale_is_refused(job, tmp_path,
                                                        capsys):
    """The second half of the reported command: a ja report with a ko
    locations file."""
    _report(job, "lqa-ja-20260830", "ja")
    code = _run(job, "--name", "lqa-ja-20260830",
                "--locations-from", _jsonl(tmp_path, "ko"))
    assert code == 1
    assert "--locations-from is a ko export" in capsys.readouterr().err


def test_a_matching_locations_file_is_accepted(job, tmp_path):
    _report(job, "lqa-ja-20260830", "ja")
    assert _run(job, "--name", "lqa-ja-20260830",
                "--locations-from", _jsonl(tmp_path, "ja")) == 0


def test_an_unstamped_locations_file_is_not_an_error(job, tmp_path):
    """Older exports carry no `target_language`; absence is not a
    mismatch, and refusing on it would break working workflows."""
    path = tmp_path / "bare.jsonl"
    path.write_text(json.dumps({"key": "a", "source_text": "S",
                                "target_text": "T"}) + "\n",
                    encoding="utf-8")
    _report(job, "lqa-ja-20260830", "ja")
    assert _run(job, "--name", "lqa-ja-20260830",
                "--locations-from", str(path)) == 0


def test_both_mismatches_are_reported_together(job, tmp_path, capsys):
    """One run, both problems — the operator should see the whole picture
    rather than fixing one and hitting the next."""
    _report(job, "lqa-ja-20260830", "zh-CN")
    _run(job, "--name", "lqa-ja-20260830",
         "--locations-from", _jsonl(tmp_path, "ko"))
    err = capsys.readouterr().err
    assert "locale='zh-CN'" in err and "is a ko export" in err


# --------------------------------------------------------------- override

def test_force_writes_the_deliverable_anyway(job, capsys):
    """The operator may know the stored locale is the correct one."""
    _report(job, "lqa-ja-20260830", "zh-CN")
    assert _run(job, "--name", "lqa-ja-20260830", "--force") == 0
    assert "Nomori_Bug_Report_zh-CN.xlsx" in capsys.readouterr().out


def test_force_still_prints_the_warnings(job, capsys):
    """Overriding is not silencing: the file is still mislabelled."""
    _report(job, "lqa-ja-20260830", "zh-CN")
    _run(job, "--name", "lqa-ja-20260830", "--force")
    assert "locale='zh-CN'" in capsys.readouterr().err


# -------------------------------------------- hyphenated locale names

def test_a_hyphenated_locale_is_found_in_a_run_name():
    """`locale_in_name` tokenized the name on non-alphanumerics, which
    shreds a hyphenated locale: "lqa-zh-CN-20260830" became
    {"lqa","zh","CN","20260830"} and "zh-CN" never matched. Worse than a
    miss — a configured bare "zh" DID match, so the mismatch guard
    compared the report against the WRONG locale instead of skipping."""
    from orbit8.bug_report import locale_in_name
    locales = ["zh-CN", "zh-Hant", "ja", "ko"]
    assert locale_in_name("lqa-zh-CN-20260830", locales) == "zh-CN"
    assert locale_in_name("lqa-zh-Hant-20260830-full",
                          locales) == "zh-Hant"


def test_a_bare_locale_does_not_win_over_its_hyphenated_sibling():
    """Longest-first must still hold once matching is by search: "zh"
    occurs INSIDE "zh-CN", so a naive scan would return it."""
    from orbit8.bug_report import locale_in_name
    locales = ["zh", "zh-CN", "zh-Hant"]
    assert locale_in_name("lqa-zh-CN-20260830", locales) == "zh-CN"
    assert locale_in_name("lqa-zh-Hant-x", locales) == "zh-Hant"
    assert locale_in_name("lqa-zh-20260830", locales) == "zh"


def test_a_un_m49_region_is_found():
    from orbit8.bug_report import locale_in_name
    assert locale_in_name("lqa-es-419-20260830", ["es-419", "es"]) == "es-419"


def test_a_locale_inside_a_word_is_not_a_match():
    """Bounded on both sides, so a name that merely CONTAINS the letters
    does not trip the guard."""
    from orbit8.bug_report import locale_in_name
    assert locale_in_name("my-zhang-report", ["zh", "ja"]) is None
    assert locale_in_name("jakarta-build", ["ja"]) is None


def test_a_name_with_no_locale_is_still_none():
    from orbit8.bug_report import locale_in_name
    assert locale_in_name("dev-audit", ["zh-CN", "ja"]) is None
