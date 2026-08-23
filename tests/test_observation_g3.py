"""The G3 verdict reaching the observation log (PLAN §3, §5.6).

This is the load-bearing half of Phase 1 and the only part that was
genuinely missing from the system: `_absorb_flagged` bulk-marked every
flagged row `accepted` on approval, so there was no way to tell "the
reviewer agreed with this string" from "the reviewer approved the gate".

Without that distinction every later utility estimate is `_badness()`
grading its own homework — our gate scoring the repairs our gate asked
for. The verdict here is DERIVED from whether the approved target differs
from what S4 produced, so it costs the operator no extra workflow, and it
stays SILENT when it cannot honestly tell, because a fabricated
`accepted` would read as human endorsement forever.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.controller import Job
from orbit8.observation import (G3_ACCEPTED, G3_EDITED, G3_PENDING,
                                ObservationLog)
from orbit8.schemas import IntakeBrief

SOURCE = {
    "UI_START": "开始游戏",
    "UI_QUIT": "退出",
    "DLG_WOLF_01": "狼人在黑夜中睁开了眼睛。",
    "ITEM_SWORD": "传说之剑：攻击力+10",
}


@pytest.fixture()
def job(tmp_path: Path) -> Job:
    source = tmp_path / "strings.json"
    source.write_text(json.dumps(SOURCE, ensure_ascii=False),
                      encoding="utf-8")
    intake = IntakeBrief(game="ExampleGame", source_lang="zh",
                         target_locales=["ko"], genre=["werewolf"],
                         client_lang="zh-CN")
    return Job.init(tmp_path / "jobs", "obs-ko", intake=intake,
                    source_files=[str(source)], pilot_size=2)


def _walk_to_g3(job: Job) -> None:
    for gate, by in (("G0", "tian"), ("G1", "dev"), ("G2", "client")):
        while job.derive().gate != gate:
            job.next_step(dry_run=True)
        job.approve(gate, by=by)
    while job.derive().gate != "G3":
        job.next_step(dry_run=True)


def test_the_log_exists_and_is_populated_by_production(job: Job):
    """Phase 1 is wired into the real Controller, not only the graph."""
    _walk_to_g3(job)
    log = ObservationLog(job.store.observations_path())
    assert log.all_rows(), "S4 ran through the Controller but observed nothing"


def test_rows_carry_the_s4_attempt_number(job: Job):
    """PLAN §5.7: the attempt the Controller actually opened, not a
    hardcoded 1 — otherwise cross-attempt comparison is meaningless."""
    _walk_to_g3(job)
    log = ObservationLog(job.store.observations_path())
    attempt = job.store.latest_attempt(4)
    assert {r["attempt"] for r in log.all_rows()} == {attempt}


def test_rows_are_pending_until_the_human_rules(job: Job):
    _walk_to_g3(job)
    log = ObservationLog(job.store.observations_path())
    assert {r["g3_verdict"] for r in log.all_rows()} == {G3_PENDING}


def test_approving_g3_records_agreement_for_untouched_strings(job: Job):
    """The operator approved without changing the target: our gate and the
    human agree, which is the positive evidence promotion would need."""
    _walk_to_g3(job)
    job.approve("G3", by="pm")

    log = ObservationLog(job.store.observations_path())
    ruled = [r for r in log.all_rows() if r["g3_verdict"] != G3_PENDING]
    assert ruled, "G3 was approved but no verdict reached the log"
    assert {r["g3_verdict"] for r in ruled} == {G3_ACCEPTED}


def test_a_post_edited_string_is_recorded_as_an_overturn(job: Job):
    """The signal that matters most (PLAN §5.6): the gate was satisfied and
    a human changed the string anyway. Those rows are the evidence that a
    gate check is miscalibrated, so they must not read as agreement."""
    _walk_to_g3(job)
    run_db = job._run_db("ko")
    flagged = run_db.by_status("flagged", "mtpe")
    assert flagged, "fixture produced no reviewable strings"
    edited_uid = flagged[0]["uid"]
    run_db.record(edited_uid, status=flagged[0]["status"],
                  target="사람이 고친 번역")          # the human rewrote it

    job.approve("G3", by="pm")

    log = ObservationLog(job.store.observations_path())
    rows = log.for_uid(edited_uid)
    assert rows, "no observation for the edited string"
    assert {r["g3_verdict"] for r in rows} == {G3_EDITED}
    assert {r["g3_text"] for r in rows} == {"사람이 고친 번역"}


def test_the_verdict_reaches_every_observation_of_that_string(job: Job):
    """A human ruled on the STRING, so each candidate that led there
    inherits it — including rejected repairs, which is what makes a
    rejection interpretable rather than just a failure count."""
    _walk_to_g3(job)
    log = ObservationLog(job.store.observations_path())
    multi = next((r["uid"] for r in log.all_rows()
                  if len(log.for_uid(r["uid"])) > 1), None)
    if multi is None:
        pytest.skip("dry run produced no multi-candidate string")
    job.approve("G3", by="pm")
    verdicts = {r["g3_verdict"] for r in
                ObservationLog(job.store.observations_path()).for_uid(multi)}
    assert len(verdicts) == 1 and G3_PENDING not in verdicts


def test_an_unobserved_string_is_not_given_a_fabricated_verdict(job: Job):
    """Silence is a valid third outcome. This v0 gate approves in bulk, so
    'the operator did not touch it' is NOT evidence of agreement when
    there is no observation to compare against — inventing one would be
    worse than having no data at all."""
    _walk_to_g3(job)
    log = ObservationLog(job.store.observations_path())
    observed = {r["uid"] for r in log.all_rows()}
    run_db = job._run_db("ko")
    # prefilled/reused strings never reach the ratchet, so they have no
    # observation and must acquire no verdict
    unobserved = [row["uid"] for row in run_db.all_segments()
                  if row["uid"] not in observed]

    job.approve("G3", by="pm")

    after = ObservationLog(job.store.observations_path())
    for uid in unobserved:
        assert after.for_uid(uid) == []


def test_g3_approval_still_absorbs_the_queue(job: Job):
    """The observation write is additive: the pre-existing G3 behavior —
    flagged rows accepted, pairs written back to the TM — is unchanged."""
    _walk_to_g3(job)
    run_db = job._run_db("ko")
    assert run_db.by_status("flagged", "mtpe")
    job.approve("G3", by="pm")
    assert job._run_db("ko").by_status("flagged", "mtpe") == []
