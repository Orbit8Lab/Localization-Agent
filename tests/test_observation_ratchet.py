"""Wiring the observation layer into the Stage 4 ratchet (PLAN §3).

The ratchet (`graphs/translate.py::gate`) is the pipeline's most important
decision: a candidate replaces the incumbent only when strictly better.
Observing it is what makes the promotion question answerable later — and it
is also the worst possible place for a logging bug, which is why the
"never breaks a run" test below is the one that matters most.

The observations are WRITE-ONLY by design. No node reads them; nothing
routes on them. That is what makes Phase 1 risk-free, and a test asserts it
stays that way.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from orbit8.gate_checks import GateConfig
from orbit8.graphs.translate import (StageContext, TranslateConfig,
                                     build_translate_graph)
from orbit8.llm import EchoProvider
from orbit8.memory import RunDB
from orbit8.observation import ACCEPTED, FIRST, REJECTED, ObservationLog
from orbit8.schemas import UniqueString


@pytest.fixture()
def ctx(tmp_path: Path):
    run_db = RunDB(tmp_path / "run.db")
    run_db.seed([UniqueString(uid="u1", text="开始游戏", keys=["UI_START"])])
    cfg = TranslateConfig(
        game="ExampleGame", source_lang="zh", locale="en",
        critic_mode="off", dry_run=True,
        gate=GateConfig(source_lang="zh", target_lang="en"))
    return StageContext(
        provider=EchoProvider("en"), cfg=cfg, run_db=run_db,
        observations=ObservationLog(tmp_path / "obs.db"), attempt=2)


def _run(ctx: StageContext) -> None:
    compiled = build_translate_graph(ctx).compile()
    compiled.invoke({
        "job_id": "job-1", "locale": "en", "batch_id": "b001",
        "segments": [{"uid": "u1", "domain": "ui"}],
        "iteration": 0, "findings": []})


def test_a_translate_run_writes_observations(ctx: StageContext):
    _run(ctx)
    rows = ctx.observations.all_rows()
    assert rows, "the ratchet ran but nothing was observed"
    assert {r["uid"] for r in rows} == {"u1"}


def test_the_attempt_number_reaches_the_row(ctx: StageContext):
    """PLAN §5.7 — the coordinate that makes a row auditable. The
    Controller computes the s4 attempt; it has to survive the trip."""
    _run(ctx)
    assert {r["attempt"] for r in ctx.observations.all_rows()} == {2}


def test_the_batch_and_job_are_recorded(ctx: StageContext):
    _run(ctx)
    row = ctx.observations.all_rows()[0]
    assert row["job_id"] == "job-1" and row["batch_id"] == "b001"


def test_the_first_candidate_is_marked_first_not_accepted(ctx: StageContext):
    """A candidate with no incumbent did not beat anything. Calling that an
    accept would inflate every accept rate computed later."""
    _run(ctx)
    assert ctx.observations.all_rows()[0]["verdict"] == FIRST


def test_the_incumbent_rescore_is_not_logged_as_an_attempt(ctx: StageContext):
    """`gate` re-scores the surviving incumbent on every pass. It is not a
    new attempt at anything, so counting it would manufacture rows that
    never corresponded to a model call."""
    _run(ctx)
    rows = ctx.observations.all_rows()
    assert all(r["strategy"] != "incumbent" for r in rows)
    # exactly one candidate was produced for one string in a dry run
    assert len(rows) == 1


def test_observation_failure_never_breaks_the_run(ctx: StageContext,
                                                 monkeypatch):
    """The guarantee that makes Phase 1 safe. This layer hangs off the
    ratchet; if a logging error could fail a batch, the 'no risk' phase
    would be the most expensive thing in the plan."""
    def boom(*_args, **_kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ctx.observations, "record", boom)
    _run(ctx)                                     # must not raise
    assert ctx.run_db.get("u1")["status"] in ("accepted", "mtpe")


def test_a_run_without_a_log_behaves_identically(tmp_path: Path):
    """Observation is opt-in: every existing caller passes no log, and
    those runs must be untouched."""
    run_db = RunDB(tmp_path / "run.db")
    run_db.seed([UniqueString(uid="u1", text="开始游戏", keys=["UI_START"])])
    cfg = TranslateConfig(game="G", source_lang="zh", locale="en",
                          critic_mode="off", dry_run=True)
    ctx = StageContext(provider=EchoProvider("en"), cfg=cfg, run_db=run_db)
    compiled = build_translate_graph(ctx).compile()
    compiled.invoke({"job_id": "j", "locale": "en", "batch_id": "b",
                     "segments": [{"uid": "u1", "domain": "ui"}],
                     "iteration": 0, "findings": []})
    assert run_db.get("u1")["status"] in ("accepted", "mtpe")


# ------------------------------------- the ratchet's own verdicts, directly

def _gate_only(ctx: StageContext, staged: dict) -> dict:
    """Drive the gate node alone, so accept/reject can be forced without
    depending on what a provider happens to return."""
    graph = build_translate_graph(ctx)
    gate = graph.nodes["gate"].runnable
    return gate.invoke({"job_id": "job-1", "locale": "en", "batch_id": "b001",
                        "segments": [{"uid": "u1", "domain": "ui"}],
                        "iteration": 1, "best": staged, "tokens_start": 0.0})


def test_a_worse_repair_is_recorded_as_rejected(ctx: StageContext):
    """PLAN §4.3: the negative example. A repair the ratchet rolled back is
    the evidence that later bounds a skill's applicability, so it must
    reach the log as a rejection rather than vanishing."""
    ctx.cfg.dry_run = False
    ctx.cfg.gate = GateConfig(source_lang="zh", target_lang="en")
    incumbent = {"target": "Start Game", "findings": [],
                 "term_decisions": {}}
    # an empty target scores OMISSION/HIGH — strictly worse than clean
    worse = {"target": "", "findings": [], "term_decisions": {}}
    _gate_only(ctx, {"u1": incumbent, "__sample__r1__u1": worse})

    rows = ctx.observations.all_rows()
    assert [r["verdict"] for r in rows] == [REJECTED]
    assert rows[0]["strategy"] == "repair"
    assert rows[0]["badness_before"] == 0
    assert rows[0]["badness_after"] > 0


def test_a_better_repair_is_recorded_as_accepted(ctx: StageContext):
    ctx.cfg.dry_run = False
    ctx.cfg.gate = GateConfig(source_lang="zh", target_lang="en")
    broken = {"target": "", "findings": [], "term_decisions": {}}
    fixed = {"target": "Start Game", "findings": [], "term_decisions": {}}
    _gate_only(ctx, {"u1": broken, "__sample__r1__u1": fixed})

    row, = ctx.observations.all_rows()
    assert row["verdict"] == ACCEPTED and row["strategy"] == "repair"
    assert row["badness_after"] < row["badness_before"]


def test_the_signatures_describe_what_is_still_wrong(ctx: StageContext):
    """The log records the defect classes a NEXT repair would target, which
    is what makes a signature actionable rather than historical."""
    ctx.cfg.dry_run = False
    ctx.cfg.gate = GateConfig(source_lang="zh", target_lang="en")
    _gate_only(ctx, {"__sample__r1__u1": {"target": "", "findings": [],
                                          "term_decisions": {}}})
    row, = ctx.observations.all_rows()
    assert row["signatures"] == ["omission"]


def test_the_candidate_text_is_kept_for_the_g3_diff(ctx: StageContext):
    """Without the target there is nothing to diff a human's approved
    string against, and accepted-vs-edited (PLAN §5.6) is unrecoverable."""
    ctx.cfg.dry_run = False
    _gate_only(ctx, {"__sample__r1__u1": {"target": "Start Game",
                                          "findings": [],
                                          "term_decisions": {}}})
    assert ctx.observations.all_rows()[0]["target"] == "Start Game"


def test_the_incumbent_is_scored_before_any_repair(ctx: StageContext):
    """Ordering guard, and this one protects the RATCHET, not just the log:
    if a repair candidate is scored before the incumbent it is compared
    against nothing, so a worse repair would win outright."""
    ctx.cfg.dry_run = False
    ctx.cfg.gate = GateConfig(source_lang="zh", target_lang="en")
    worse = {"target": "", "findings": [], "term_decisions": {}}
    good = {"target": "Start Game", "findings": [], "term_decisions": {}}
    # repair candidate FIRST in insertion order — the hostile ordering
    out = _gate_only(ctx, {"__sample__r1__u1": worse, "u1": good})

    assert out["best"]["u1"]["target"] == "Start Game"
    assert ctx.observations.all_rows()[0]["verdict"] == REJECTED
