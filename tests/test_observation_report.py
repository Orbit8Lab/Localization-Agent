"""`orbit8 observations` — the report Phase 1 exists to produce (PLAN §3).

A write-only log nobody can read is not an observation layer, it is a disk
leak. This report is what turns the log into the Phase 4 go/no-go decision
(PLAN §6.1), so the two conclusions it must never blur are asserted here:

- recurrence ACROSS strings vs. one string retried (PLAN §4.1/§4.2)
- our gate's opinion vs. the human's at G3 (PLAN §5.6)
"""
from __future__ import annotations

from pathlib import Path

from orbit8.cli import main
from orbit8.observation import (ACCEPTED, FIRST, G3_ACCEPTED, G3_EDITED,
                               Observation, ObservationLog, REJECTED)
from orbit8.controller import Job
from orbit8.schemas import IntakeBrief


def _job(tmp_path: Path) -> Job:
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    return Job.init(tmp_path / "jobs", "demo",
                    intake=IntakeBrief(game="G", source_lang="zh",
                                       target_locales=["ko"]),
                    source_files=[str(source)])


def _log(job: Job) -> ObservationLog:
    return ObservationLog(job.store.observations_path())


def _obs(uid: str, sig: str, **kw) -> Observation:
    base = dict(job_id="demo", locale="ko", attempt=1, uid=uid,
                signatures=[sig], strategy="translate", iteration=0,
                badness_before=None, badness_after=100, verdict=FIRST,
                target="x")
    base.update(kw)
    return Observation(**base)


def test_an_empty_log_says_so_without_failing(tmp_path, capsys):
    job = _job(tmp_path)
    assert main(["observations", str(job.store.root), "demo"]) == 0
    assert "no observations yet" in capsys.readouterr().out


def test_the_report_shows_recurrence_across_strings(tmp_path, capsys):
    job = _job(tmp_path)
    log = _log(job)
    for n in range(4):
        log.record(_obs(f"u{n}", "terminology/魂石"))

    main(["observations", str(job.store.root), "demo"])
    out = capsys.readouterr().out
    assert "terminology/魂石" in out
    assert "seen on more than one string" in out


def test_a_flapping_loop_is_not_reported_as_recurrence(tmp_path, capsys):
    """PLAN §4.1: five observations on ONE string is a repair loop retried,
    not a defect class. Reporting it as recurrence would green-light Phase 4
    on evidence that does not exist."""
    job = _job(tmp_path)
    log = _log(job)
    for _ in range(5):
        log.record(_obs("same-uid", "terminology/魂石"))

    main(["observations", str(job.store.root), "demo"])
    out = capsys.readouterr().out
    assert "nothing recurs across strings yet" in out


def test_the_report_surfaces_g3_disagreement(tmp_path, capsys):
    """The most valuable row in the log (PLAN §5.6): our gate was satisfied
    and the human overruled it. That is a miscalibrated check, and it must
    be called out rather than counted as a promotion candidate."""
    job = _job(tmp_path)
    log = _log(job)
    for n in range(3):
        log.record(_obs(f"u{n}", "length/overflow", verdict=ACCEPTED,
                        badness_before=100, badness_after=0))
    log.record_g3("u0", "ko", G3_EDITED, "shorter")
    log.record_g3("u1", "ko", G3_EDITED, "shorter")
    log.record_g3("u2", "ko", G3_ACCEPTED)

    main(["observations", str(job.store.root), "demo"])
    out = capsys.readouterr().out
    assert "overturned by G3 more often than upheld" in out
    assert "length/overflow" in out


def test_agreement_is_not_reported_as_disagreement(tmp_path, capsys):
    job = _job(tmp_path)
    log = _log(job)
    for n in range(3):
        log.record(_obs(f"u{n}", "terminology/魂石", verdict=ACCEPTED,
                        badness_before=100, badness_after=0))
        log.record_g3(f"u{n}", "ko", G3_ACCEPTED)

    main(["observations", str(job.store.root), "demo"])
    assert "overturned by G3 more often" not in capsys.readouterr().out


def test_pending_rows_are_flagged_as_unruled(tmp_path, capsys):
    """An unreviewed log must not look like an endorsed one."""
    job = _job(tmp_path)
    _log(job).record(_obs("u0", "terminology/魂石"))
    main(["observations", str(job.store.root), "demo"])
    assert "no human verdicts yet" in capsys.readouterr().out


def test_rejections_appear_in_the_report(tmp_path, capsys):
    job = _job(tmp_path)
    log = _log(job)
    log.record(_obs("u0", "terminology/魂石", verdict=REJECTED,
                    strategy="repair", badness_before=10, badness_after=100))
    main(["observations", str(job.store.root), "demo"])
    out = capsys.readouterr().out
    assert "rejected:1" in out


def test_the_signature_table_is_capped_and_says_so(tmp_path, capsys):
    job = _job(tmp_path)
    log = _log(job)
    for n in range(30):
        log.record(_obs(f"u{n}", f"terminology/term{n}"))
    main(["observations", str(job.store.root), "demo", "--limit", "5"])
    out = capsys.readouterr().out
    assert "25 more" in out          # 30 signatures, 5 shown
