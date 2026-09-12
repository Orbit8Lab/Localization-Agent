"""Background execution of job steps.

The runner is the piece that lets `POST /jobs` return in a second while
a two-hour job proceeds. What matters is that it stops where the
controller says to stop (a gate), that a crash is visible rather than
silent, and that polling clients cannot start two workers on one
artifact tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbit8.controller import Job, Stage
from orbit8.schemas import IntakeBrief
from orbit8.server.runner import JobRunner


def _job(root: Path, job_id: str = "j1") -> Job:
    src = root / "s.json"
    src.write_text('{"K": "v"}', encoding="utf-8")
    return Job.init(root / "jobs", job_id,
                    intake=IntakeBrief(game="G", source_lang="zh",
                                       target_locales=["en"]),
                    source_files=[str(src)])


def test_a_fresh_runner_reports_idle(tmp_path: Path):
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    assert runner.state("nobody").status == "idle"


def test_run_stops_at_a_gate_rather_than_pushing_through(tmp_path: Path,
                                                         monkeypatch):
    """A gate is a hard stop waiting on a human. A runner that advanced
    past one would translate against an unfrozen glossary."""
    _job(tmp_path)
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    monkeypatch.setattr(Job, "derive",
                        lambda self: Stage("ASSET", "lock glossary", gate="G1"))
    runner.start("j1")
    runner.join("j1", timeout=5)
    state = runner.state("j1")
    assert state.status == "waiting_gate"
    assert state.progress["gate"] == "G1"


def test_a_crash_is_reported_not_swallowed(tmp_path: Path, monkeypatch):
    """A job that dies silently looks identical to one still working."""
    _job(tmp_path)
    runner = JobRunner(tmp_path / "jobs", dry_run=True)

    def boom(self):
        raise RuntimeError("stage exploded")

    monkeypatch.setattr(Job, "derive", boom)
    runner.start("j1")
    runner.join("j1", timeout=5)
    state = runner.state("j1")
    assert state.status == "error"
    assert "stage exploded" in state.error


def test_starting_a_running_job_twice_does_not_double_run(tmp_path: Path,
                                                          monkeypatch):
    """A polling UI retries. Two workers on one artifact tree would race
    on attempt directories."""
    _job(tmp_path)
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    calls = []

    def slow(self):
        calls.append(1)
        import time
        time.sleep(0.3)
        return Stage("ASSET", "waiting", gate="G1")

    monkeypatch.setattr(Job, "derive", slow)
    runner.start("j1")
    runner.start("j1")              # must be a no-op while running
    runner.join("j1", timeout=5)
    assert len(calls) == 1


def test_a_stage_that_does_not_advance_stops_instead_of_spinning(
        tmp_path: Path, monkeypatch):
    """If `next_step` runs but the derivation is unchanged, looping would
    burn tokens forever."""
    _job(tmp_path)
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    monkeypatch.setattr(Job, "derive",
                        lambda self: Stage("INGEST", "ingest source files"))
    steps = []
    monkeypatch.setattr(Job, "next_step",
                        lambda self, f=None, **kw: steps.append(1))
    runner.start("j1")
    runner.join("j1", timeout=5)
    assert len(steps) == 1, "should stop once the stage stops moving"
    assert runner.state("j1").status == "idle"


def test_dry_run_asks_for_no_provider(tmp_path: Path):
    """A dry-run deployment must be runnable with no API key at all."""
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    assert runner._provider_factory() is None
