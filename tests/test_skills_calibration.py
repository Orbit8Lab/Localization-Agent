"""Threshold calibration by replay (PLAN open questions, §6.2).

PLAN deliberately leaves every promotion constant unset, on the grounds
that picking one now would repeat the guessed-threshold-of-3 mistake it
opens by criticizing. `replay()` is how they stop being guesses: the
observation log is fixed input, the policy is the variable, so sweeping the
space costs milliseconds instead of repeated Stage 4 runs.

The case that matters most here is the DECEPTIVE signature — perfect
badness delta, perfect ratchet accept rate, and humans overruling every
application. Under a badness-only policy it is the top candidate. It must
not promote, and the sweep must say why.
"""
from __future__ import annotations

from pathlib import Path

from orbit8.cli import main
from orbit8.controller import Job
from orbit8.observation import (ACCEPTED, FIRST, G3_ACCEPTED, G3_EDITED,
                                G3_PENDING, Observation, ObservationLog,
                                REJECTED)
from orbit8.schemas import IntakeBrief
from orbit8.skills import (PromotionPolicy, group_by_signature, replay)

LOOSE = PromotionPolicy(min_samples=3, min_g3_reviewed=3,
                        min_g3_agreement=0.75, min_utility=0.1)


def _row(uid, sig, verdict=ACCEPTED, before=100, after=0, g3=G3_PENDING):
    return {"uid": uid, "locale": "ko", "verdict": verdict,
            "badness_before": before, "badness_after": after,
            "g3_verdict": g3, "g3_text": None, "attempt": 1,
            "signatures": [sig]}


# --------------------------------------------------------------- replay

def test_a_human_backed_skill_would_promote():
    rows = [_row(f"u{n}", "t/x", g3=G3_ACCEPTED) for n in range(4)]
    result, = replay(group_by_signature(rows), LOOSE)
    assert result["would_promote"] and not result["blockers"]


def test_a_skill_humans_overrule_does_not_promote():
    """THE case (§5.6). Perfect badness delta, perfect ratchet accept rate,
    and every human overruled it. A badness-only policy ranks this first;
    this one must block it and name the reason."""
    rows = [_row(f"u{n}", "length/overflow", g3=G3_EDITED)
            for n in range(6)]
    result, = replay(group_by_signature(rows), LOOSE)
    assert result["mean_badness_delta"] == 100.0     # looks perfect...
    assert result["accept_rate"] == 1.0              # ...by our own scorer
    assert not result["would_promote"]
    assert "min_g3_agreement" in result["blockers"]


def test_an_unreviewed_skill_does_not_promote():
    """No human has ruled at all — the closed-loop case."""
    rows = [_row(f"u{n}", "t/x") for n in range(8)]
    result, = replay(group_by_signature(rows), LOOSE)
    assert not result["would_promote"]
    assert "min_g3_reviewed" in result["blockers"]


def test_a_rare_signature_is_blocked_on_samples_not_quality():
    """'Not yet' must be distinguishable from 'never': this one is only
    short of evidence."""
    rows = [_row("u0", "t/x", g3=G3_ACCEPTED)]
    result, = replay(group_by_signature(rows), LOOSE)
    assert result["blockers"][0] == "min_samples"


def test_a_fix_that_does_not_reduce_badness_is_blocked():
    rows = [_row(f"u{n}", "t/x", before=10, after=100, g3=G3_ACCEPTED)
            for n in range(4)]
    result, = replay(group_by_signature(rows), LOOSE)
    assert "no_badness_gain" in result["blockers"]


def test_counter_examples_are_counted_for_the_reviewer():
    """§6.3: the boundary travels with the candidate."""
    rows = ([_row(f"ok{n}", "t/x", g3=G3_ACCEPTED) for n in range(4)]
            + [_row("no1", "t/x", verdict=REJECTED, before=10, after=100)])
    result, = replay(group_by_signature(rows), LOOSE)
    assert result["counter_examples"] == 1


def test_tightening_a_threshold_only_ever_removes_promotions():
    """Monotonicity — the property that makes a sweep interpretable. If a
    tighter policy could promote MORE, the numbers would not be a
    threshold at all."""
    rows = [_row(f"u{n}", f"t/{n % 3}", g3=G3_ACCEPTED) for n in range(12)]
    grouped = group_by_signature(rows)
    loose = {r["signature"] for r in replay(grouped, LOOSE)
             if r["would_promote"]}
    tight = {r["signature"] for r in replay(
        grouped, PromotionPolicy(min_samples=3, min_g3_reviewed=3,
                                 min_g3_agreement=0.75,
                                 min_utility=0.99))
        if r["would_promote"]}
    assert tight <= loose


def test_replay_is_pure_and_repeatable():
    rows = [_row(f"u{n}", "t/x", g3=G3_ACCEPTED) for n in range(4)]
    grouped = group_by_signature(rows)
    assert replay(grouped, LOOSE) == replay(grouped, LOOSE)


def test_grouping_fans_a_multi_defect_candidate_into_each_class():
    """One candidate can carry several defect classes at once; each is
    evidence about its own signature."""
    row = _row("u0", "t/x")
    row["signatures"] = ["t/x", "t/y"]
    grouped = group_by_signature([row])
    assert set(grouped) == {"t/x", "t/y"}


def test_first_candidates_do_not_count_as_applications():
    rows = [_row(f"u{n}", "t/x", verdict=FIRST, before=None, g3=G3_ACCEPTED)
            for n in range(6)]
    result, = replay(group_by_signature(rows), LOOSE)
    assert result["distinct_strings"] == 0
    assert not result["would_promote"]


# ------------------------------------------------------------- the CLI

def _seeded_job(tmp_path: Path) -> Job:
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    job = Job.init(tmp_path / "jobs", "demo",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["ko"]),
                   source_files=[str(source)])
    log = ObservationLog(job.store.observations_path())
    for n in range(6):
        log.record(Observation(
            job_id="demo", locale="ko", attempt=1, uid=f"good{n}",
            signatures=["terminology/魂石"], strategy="repair", iteration=1,
            badness_before=100, badness_after=0, verdict=ACCEPTED,
            target=f"t{n}"))
        log.record_g3(f"good{n}", "ko", G3_ACCEPTED)
    for n in range(6):
        log.record(Observation(
            job_id="demo", locale="ko", attempt=1, uid=f"bad{n}",
            signatures=["length/overflow"], strategy="repair", iteration=1,
            badness_before=100, badness_after=0, verdict=ACCEPTED,
            target=f"b{n}"))
        log.record_g3(f"bad{n}", "ko", G3_EDITED, "human rewrote it")
    return job


def test_the_cli_reports_what_would_promote(tmp_path, capsys):
    job = _seeded_job(tmp_path)
    assert main(["calibrate", str(job.store.root), "demo",
                 "--min-samples", "3", "--min-g3-reviewed", "3",
                 "--min-utility", "0.1"]) == 0
    out = capsys.readouterr().out
    assert "would promote: 1/2" in out          # the human-backed one only
    assert "terminology/魂石" in out and "length/overflow" in out


def test_the_cli_names_the_binding_blocker(tmp_path, capsys):
    """The actual output of a sweep: WHICH floor is stopping things, since
    'waiting on reviewers' and 'the fix is wrong' need opposite responses."""
    job = _seeded_job(tmp_path)
    main(["calibrate", str(job.store.root), "demo",
          "--min-samples", "3", "--min-g3-reviewed", "3",
          "--min-utility", "0.1"])
    out = capsys.readouterr().out
    assert "binding blockers" in out
    assert "min_g3_agreement" in out


def test_the_cli_explains_a_dominant_agreement_blocker(tmp_path, capsys):
    """Reviewers overruling a fix is a miscalibrated CHECK, and the tool
    must not invite the user to lower the threshold instead."""
    job = _seeded_job(tmp_path)
    main(["calibrate", str(job.store.root), "demo",
          "--min-samples", "3", "--min-g3-reviewed", "3",
          "--min-utility", "0.01"])
    out = capsys.readouterr().out
    assert "miscalibrated gate check" in out


def test_the_cli_handles_an_empty_log(tmp_path, capsys):
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    job = Job.init(tmp_path / "jobs", "empty",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["ko"]),
                   source_files=[str(source)])
    assert main(["calibrate", str(job.store.root), "empty"]) == 0
    assert "no observations" in capsys.readouterr().out


def test_the_cli_distinguishes_no_signatures_from_no_rows(tmp_path, capsys):
    """A dry run logs candidates with no findings. That is not the same as
    an empty log, and saying 'no observations' would be wrong."""
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    job = Job.init(tmp_path / "jobs", "clean",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["ko"]),
                   source_files=[str(source)])
    ObservationLog(job.store.observations_path()).record(Observation(
        job_id="clean", locale="ko", attempt=1, uid="u0", signatures=[],
        strategy="translate", iteration=0, badness_before=None,
        badness_after=0, verdict=FIRST, target="x"))
    main(["calibrate", str(job.store.root), "clean"])
    out = capsys.readouterr().out
    assert "none carry a defect signature" in out
