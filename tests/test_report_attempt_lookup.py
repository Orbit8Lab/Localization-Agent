"""Finding a stored LQA report across attempts.

Every `orbit8 lqa run` opens its OWN s5 attempt, so auditing four locales
leaves four attempts each holding exactly one locale's report. `lqa
report` defaulted to `latest_attempt(5)` — "what ran most recently" —
which is a different question from "where is the report I named". After a
four-locale audit, three of the four reports were unreadable:

    ArtifactError: missing artifact .../s5/attempt-05/lqa_report.lqa-ja-*.json

The file existed the whole time, in attempt-02. The `--attempt` flag
could have recovered it, but nothing told the operator which number to
pass, and the error named only the path it had guessed.
"""
from __future__ import annotations

import json

import pytest

from orbit8.controller import Job
from orbit8.external_lqa import run_external_lqa
from orbit8.schemas import IntakeBrief, LQAReport

LOCALES = ["zh-CN", "ja", "ko", "zh-Hant"]
INTAKE = IntakeBrief(game="Nomori", source_lang="en",
                     target_locales=LOCALES)


@pytest.fixture
def audited(tmp_path):
    """A job after the real workflow: one audit per locale."""
    job = Job.init(tmp_path / "jobs", "nomori-lqa", intake=INTAKE,
                   source_files=[])
    for locale in LOCALES:
        pairs = tmp_path / f"{locale}.jsonl"
        pairs.write_text(json.dumps(
            {"key": "a", "source_language": "en",
             "target_language": locale, "source_text": "Start",
             "target_text": "x"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        run_external_lqa(job, None, pairs, name=f"lqa-{locale}",
                         deterministic_only=True)
    return job


def test_each_audit_opens_its_own_attempt(audited):
    """The premise. If this ever stops being true the lookup below is
    merely redundant rather than wrong — but it is true today."""
    attempts = sorted(p.name for p in
                      (audited.store.job_dir / "s5").iterdir()
                      if p.name.startswith("attempt-"))
    assert attempts == ["attempt-01", "attempt-02", "attempt-03",
                        "attempt-04"]


def test_the_latest_attempt_holds_only_the_last_locale(audited):
    """Why the default was wrong: attempt-04 has zh-Hant and nothing
    else."""
    latest = audited.store.latest_attempt(5)
    assert audited.store.find_attempt(5, "lqa_report.lqa-zh-Hant") == latest
    assert audited.store.find_attempt(5, "lqa_report.lqa-ja") != latest


@pytest.mark.parametrize("locale", LOCALES)
def test_every_locale_report_is_findable(audited, locale):
    """The regression: three of these four raised ArtifactError."""
    name = f"lqa_report.lqa-{locale}"
    attempt = audited.store.find_attempt(5, name)
    assert attempt is not None
    report = audited.store.read(5, name, LQAReport, attempt=attempt)
    assert report.locale == locale


def test_a_name_that_was_never_audited_returns_none(audited):
    assert audited.store.find_attempt(5, "lqa_report.lqa-fr") is None


def test_find_attempt_prefers_the_newest_holder(tmp_path):
    """A re-audit under the same name must resolve to the RE-RUN, not the
    stale first attempt — otherwise a fix would never show up in the
    report."""
    job = Job.init(tmp_path / "jobs", "j", intake=INTAKE, source_files=[])
    pairs = tmp_path / "ja.jsonl"
    pairs.write_text(json.dumps(
        {"key": "a", "source_language": "en", "target_language": "ja",
         "source_text": "Start", "target_text": "開始"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    run_external_lqa(job, None, pairs, name="ja", deterministic_only=True)
    run_external_lqa(job, None, pairs, name="ja", deterministic_only=True)
    assert job.store.find_attempt(5, "lqa_report.ja") == 2


def test_a_missing_stage_is_not_an_error(tmp_path):
    job = Job.init(tmp_path / "jobs", "j", intake=INTAKE, source_files=[])
    assert job.store.find_attempt(5, "lqa_report.anything") is None
    assert job.store.artifact_names(5) == {}


# ------------------------------------------------------- the recovery path

def test_artifact_names_lists_every_report_and_its_attempts(audited):
    """"missing artifact <path>" tells the operator nothing they can act
    on. The names ARE the recovery."""
    names = audited.store.artifact_names(5)
    reports = {n for n in names if n.startswith("lqa_report.")}
    assert reports == {f"lqa_report.lqa-{locale}" for locale in LOCALES}
    assert names["lqa_report.lqa-ja"] == [2]


def test_the_cli_reports_the_available_names(audited, capsys):
    """A wrong --name must print what the operator could have typed."""
    from orbit8.cli import main
    code = main(["lqa", "report", str(audited.store.root),
                 "nomori-lqa", "--name", "lqa-fr", "--no-suggestions"])
    assert code == 1
    err = capsys.readouterr().err
    assert "no LQA report named 'lqa-fr'" in err
    for locale in LOCALES:
        assert f"--name lqa-{locale}" in err


def test_the_cli_builds_a_report_for_a_non_latest_attempt(audited, capsys):
    """End to end: the exact command that raised ArtifactError.

    `--in-place` keeps the output beside the artifact, which is what makes
    the attempt number observable; the default now writes to
    30-deliverables and deliberately does not encode an attempt."""
    from orbit8.cli import main
    code = main(["lqa", "report", str(audited.store.root), "nomori-lqa",
                 "--name", "lqa-ja", "--no-suggestions", "--in-place"])
    assert code == 0
    assert "attempt-02" in capsys.readouterr().out


def test_an_explicit_attempt_still_wins(audited):
    """`--attempt` stays an override, not a suggestion."""
    from orbit8.cli import main
    with pytest.raises(Exception):
        main(["lqa", "report", str(audited.store.root), "nomori-lqa",
              "--name", "lqa-ja", "--attempt", "4", "--no-suggestions"])


# ------------------------------------------------------ where it is written

def test_the_deliverable_goes_to_30_deliverables(audited):
    """An s5 attempt folder is INTERNAL run state: its name encodes
    nothing a client understands, and finding the right one meant knowing
    which attempt an audit happened to open."""
    from orbit8.cli import main
    main(["lqa", "report", str(audited.store.root), "nomori-lqa",
          "--name", "lqa-ja", "--no-suggestions"])
    project = audited.store.root.parent
    assert list((project / "30-deliverables").glob("*/*Bug_Report_ja.xlsx"))


def test_the_folder_is_dated_so_audits_do_not_overwrite(audited):
    """Same layout as `lqa deliver`: successive audits of one game sit
    side by side rather than clobbering each other."""
    from datetime import date
    from orbit8.cli import main
    main(["lqa", "report", str(audited.store.root), "nomori-lqa",
          "--name", "lqa-ja", "--no-suggestions"])
    project = audited.store.root.parent
    stamp = date.today().strftime("%Y%m%d")
    assert (project / "30-deliverables" / f"{stamp}-lqa-report").is_dir()


def test_the_folder_is_created_when_absent(tmp_path):
    """A fresh project has no 30-deliverables yet; the tool makes it
    rather than failing."""
    from orbit8.cli import main
    job = Job.init(tmp_path / "proj" / "jobs", "j", intake=INTAKE,
                   source_files=[])
    pairs = tmp_path / "ja.jsonl"
    pairs.write_text(json.dumps(
        {"key": "a", "source_language": "en", "target_language": "ja",
         "source_text": "Start", "target_text": "開始"},
        ensure_ascii=False) + "\n", encoding="utf-8")
    run_external_lqa(job, None, pairs, name="ja", deterministic_only=True)
    assert not (tmp_path / "proj" / "30-deliverables").exists()
    assert main(["lqa", "report", str(tmp_path / "proj" / "jobs"), "j",
                 "--name", "ja", "--no-suggestions"]) == 0
    assert (tmp_path / "proj" / "30-deliverables").is_dir()


def test_all_locales_share_one_dated_folder(audited):
    """One folder per audit DATE, not per locale — the client receives the
    set together."""
    from orbit8.cli import main
    for locale in ("ja", "ko"):
        main(["lqa", "report", str(audited.store.root), "nomori-lqa",
              "--name", f"lqa-{locale}", "--no-suggestions"])
    project = audited.store.root.parent
    folders = list((project / "30-deliverables").iterdir())
    assert len(folders) == 1
    names = sorted(p.name for p in folders[0].glob("*Bug_Report*.xlsx"))
    assert names == ["Nomori_Bug_Report_ja.xlsx",
                     "Nomori_Bug_Report_ko.xlsx"]


def test_in_place_still_writes_beside_the_artifact(audited):
    """The old behaviour stays reachable for an internal rebuild."""
    from orbit8.cli import main
    main(["lqa", "report", str(audited.store.root), "nomori-lqa",
          "--name", "lqa-ja", "--no-suggestions", "--in-place"])
    attempt = audited.store.find_attempt(5, "lqa_report.lqa-ja")
    assert list(audited.store.stage_dir(5, attempt).glob("*.xlsx"))
    assert not (audited.store.root.parent / "30-deliverables").exists()


def test_an_explicit_out_still_wins(audited, tmp_path):
    from orbit8.cli import main
    out = tmp_path / "elsewhere"
    main(["lqa", "report", str(audited.store.root), "nomori-lqa",
          "--name", "lqa-ja", "--no-suggestions", "--out", str(out)])
    assert list(out.glob("*Bug_Report_ja.xlsx"))


def test_the_timestamp_can_be_pinned(audited):
    """A re-delivery must be able to land in the original dated folder."""
    from orbit8.cli import main
    main(["lqa", "report", str(audited.store.root), "nomori-lqa",
          "--name", "lqa-ja", "--no-suggestions",
          "--timestamp", "20260830"])
    project = audited.store.root.parent
    assert (project / "30-deliverables" / "20260830-lqa-report"
            / "Nomori_Bug_Report_ja.xlsx").exists()


def test_the_summary_lands_beside_the_xlsx(audited):
    """The tech summary is part of the same deliverable."""
    from orbit8.cli import main
    main(["lqa", "report", str(audited.store.root), "nomori-lqa",
          "--name", "lqa-ja", "--no-suggestions"])
    project = audited.store.root.parent
    folder = next((project / "30-deliverables").iterdir())
    assert (folder / "Nomori_LQA_Summary_ja.xlsx".replace(".xlsx", ".md")
            ).exists()


# ------------------------------------------------ concurrent attempts

def test_two_callers_never_claim_the_same_attempt(tmp_path):
    """`new_attempt` was read-then-create with exist_ok=True, so two
    processes could compute the same number and both "succeed" — the
    second then wrote its report into the first's directory. Auditing
    four locales in parallel is the obvious way to use this tool, and it
    would have silently lost a locale's findings."""
    from concurrent.futures import ThreadPoolExecutor
    job = Job.init(tmp_path / "jobs", "j", intake=INTAKE, source_files=[])
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: job.store.new_attempt(5),
                                range(8)))
    assert len(set(claimed)) == 8, f"duplicate attempts: {sorted(claimed)}"


def test_a_claimed_attempt_directory_exists(tmp_path):
    job = Job.init(tmp_path / "jobs", "j", intake=INTAKE, source_files=[])
    n = job.store.new_attempt(5)
    assert job.store.stage_dir(5, n).is_dir()


def test_attempts_stay_sequential_when_uncontended(tmp_path):
    job = Job.init(tmp_path / "jobs", "j", intake=INTAKE, source_files=[])
    assert [job.store.new_attempt(5) for _ in range(3)] == [1, 2, 3]
