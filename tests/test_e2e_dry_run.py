"""End-to-end wire test: a job walks INTAKE → RELEASE → INCREMENTAL through
the Controller with ZERO LLM calls (echo provider + deterministic
fallbacks). Verifies the design's core invariants:

- artifacts are authoritative (stage derivation from the tree)
- gates hard-stop until a human approves
- attempt-versioned S4/S5 artifacts
- dry-run stubs never poison the TM
"""
import json
from pathlib import Path

import pytest

from orbit8.controller import Job
from orbit8.memory import TranslationMemory
from orbit8.schemas import IntakeBrief, MTPEQueue, TranslateRunSummary

SOURCE = {
    "UI_START": "开始游戏",
    "UI_QUIT": "退出",
    "DLG_WOLF_01": "狼人在黑夜中睁开了眼睛。",
    "ITEM_SWORD": "传说之剑：攻击力+10",
    "SYS_ERR_NET": "网络连接失败，请重试。",
    "MAP_VILLAGE": "月影村",
    "UI_START_COPY": "开始游戏",          # dedup: same text as UI_START
}


@pytest.fixture()
def job(tmp_path: Path) -> Job:
    source = tmp_path / "strings.json"
    source.write_text(json.dumps(SOURCE, ensure_ascii=False),
                      encoding="utf-8")
    intake = IntakeBrief(game="ExampleGame", source_lang="zh",
                         target_locales=["ko"], genre=["werewolf"],
                         client_lang="zh-CN")
    return Job.init(tmp_path / "jobs", "feiyue-ko", intake=intake,
                    source_files=[str(source)], pilot_size=3)


def step(job: Job, expect_phase: str):
    stage = job.next_step(dry_run=True)
    assert stage.phase == expect_phase, (
        f"expected {expect_phase}, got {stage.phase} ({stage.action})")
    return stage


def test_full_lifecycle(job: Job):
    # INTAKE: market analysis runs, then G0 blocks
    step(job, "INTAKE")
    assert job.derive().gate == "G0"
    # the gate is a hard stop: next_step must not advance anything
    assert job.next_step(dry_run=True).gate == "G0"
    with pytest.raises(ValueError):
        job.approve("G1", by="tian")           # only the pending gate opens
    job.approve("G0", by="tian")

    step(job, "INGEST")
    step(job, "CONTEXT")
    step(job, "ASSET")                          # extraction (stub: empty)
    step(job, "ASSET")                          # health check for ko
    assert job.derive().gate == "G1"
    job.approve("G1", by="dev-team")
    # G1 freeze: the versioned glossary exists and is the frozen artifact
    frozen = job.store.stage_dir(3) / "glossary.v1.ko.json"
    assert frozen.exists()
    assert json.loads(frozen.read_text())["frozen_at_gate"] == "G1"

    step(job, "PILOT")
    assert job.derive().gate == "G2"
    job.approve("G2", by="client")

    step(job, "PRODUCTION")
    summary = job.store.read(4, "run_summary.production.ko",
                             TranslateRunSummary)
    assert summary.segments_total == 6          # 7 records, 1 duplicate
    assert summary.accepted + summary.escalated + summary.mtpe_policy == 6
    # dialogue routes to MTPE by domain policy; unknown-prefix strings via
    # the fail-expensive low-confidence rule
    assert summary.mtpe_policy >= 1

    step(job, "LQA")
    step(job, "FLAGGED")
    queue = job.store.read(4, "mtpe_queue.ko", MTPEQueue)
    assert {i.reason.value for i in queue.items} <= {
        "domain_policy", "failure", "low_confidence"}
    assert job.derive().gate == "G3"
    job.approve("G3", by="pm")

    step(job, "TESTING")
    assert job.derive().gate == "G4"
    job.approve("G4", by="qa-lead")

    step(job, "RELEASE")                        # marketing kit for ko
    step(job, "RELEASE")                        # deliverables manifest
    assert job.derive().gate == "G5"
    job.approve("G5", by="client")
    assert job.derive().phase == "INCREMENTAL"

    # the emitted deliverable fans uids back out to ALL game keys
    lines = (job.store.stage_dir(7) / "translated.ko.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    keys = {json.loads(line)["key"] for line in lines}
    assert {"UI_START", "UI_START_COPY"} <= keys

    # dry-run stubs never enter the TM (poisoned-reuse guard)...
    tm = TranslationMemory(job.store.tm_path())
    assert tm.lookup("开始游戏", "ko") is None or not \
        tm.lookup("开始游戏", "ko").startswith("[ko]")


def test_artifacts_are_authoritative(job: Job):
    """Deleting an artifact rewinds the derived stage — no status field to
    disagree with the tree."""
    step(job, "INTAKE")
    job.approve("G0", by="tian")
    step(job, "INGEST")
    assert job.derive().phase == "CONTEXT"
    (job.store.stage_dir(1) / "strings.json").unlink()
    assert job.derive().phase == "INGEST"
