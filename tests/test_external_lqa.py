"""External LQA per docs/skills/lqa-batch-split.md: seeding, split files,
and the story-n=5 / string-n=20 Tier-3 batch policy."""
import json
from pathlib import Path

import pytest

from orbit8.controller import Job
from orbit8.external_lqa import (load_pairs, run_external_lqa, seed_audit_db,
                                 split_files)
from orbit8.memory import RunDB
from orbit8.schemas import Domain, IntakeBrief


def _pairs_file(tmp_path: Path, n_story=7, n_string=45) -> Path:
    rows = []
    for i in range(n_story):
        rows.append({"key": f"S{i}", "source_language": "en",
                     "target_language": "zh-CN",
                     "source_text": f"The old king whispered his secret #{i}.",
                     "target_text": f"老国王低声说出了他的秘密{i}。"})
    for i in range(n_string):
        rows.append({"key": f"U{i}", "source_language": "en",
                     "target_language": "zh-CN",
                     "source_text": f"Button {i}", "target_text": f"按钮{i}"})
    path = tmp_path / "pairs.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in rows), encoding="utf-8")
    return path


class CountingProvider:
    """Classifies S* keys as dialogue, U* as ui; empty review findings.
    Records the item count of every Tier-3 review batch."""
    name, model, tokens_spent = "counting", "test", 0.0

    def __init__(self):
        self.review_batch_sizes = []

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        if "content classifier" in system:
            import re
            keys = re.findall(r"^### (\S+)$", user, flags=re.M)
            items = [{"key": k,
                      "domain": "dialogue" if "king" in user.split(
                          f"### {k}\n")[1][:80] else "ui",
                      "confidence": 1.0}
                     for k in keys if k != "END"]
            # classify by text content: S rows mention 'king'
            items = []
            blocks = re.findall(r"^### (\S+)\n(.*?)(?=\n### |\Z)", user,
                                flags=re.M | re.S)
            for k, text in blocks:
                if k == "END":
                    continue
                items.append({"key": k,
                              "domain": ("dialogue" if "king" in text
                                         else "ui"),
                              "confidence": 1.0})
            return json.dumps({"items": items})
        if "QA reviewer" in system:
            self.review_batch_sizes.append(user.count("### "))
            return json.dumps({"findings": []})
        return json.dumps({"findings": []})


@pytest.fixture()
def job(tmp_path: Path) -> Job:
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"K": "x"}), encoding="utf-8")
    intake = IntakeBrief(game="G", source_lang="en",
                         target_locales=["zh-CN"])
    job = Job.init(tmp_path / "20-work", "j1", intake=intake,
                   source_files=[str(src)])
    # minimal style brief artifact so job._style() resolves
    from orbit8.schemas import StyleBrief
    job.store.write(2, "style_brief", StyleBrief(),
                    produced_by="code:test@1")
    return job


def test_seed_dedup_and_split(tmp_path: Path, job: Job):
    pairs = load_pairs(_pairs_file(tmp_path, n_story=2, n_string=3))
    db = RunDB(tmp_path / "db.db")
    seed_audit_db(db, pairs)
    assert len(db.by_status("accepted")) == 5
    for row in db.by_status("accepted")[:2]:
        db.label(row["uid"], Domain.DIALOGUE)
    story, strings, counts = split_files(
        db, tmp_path, "t", source_lang="en", target_lang="zh-CN")
    assert counts == {"story": 2, "string": 3}
    story_rows = [json.loads(l) for l in
                  story.read_text().strip().splitlines()]
    assert all(r["domain"] == "dialogue" for r in story_rows)


def test_batch_policy_story5_string20(tmp_path: Path, job: Job):
    provider = CountingProvider()
    report = run_external_lqa(
        job, provider, _pairs_file(tmp_path, n_story=7, n_string=45),
        name="t", batch_story=5, batch_string=20)
    # 7 story -> [5, 2]; 45 strings -> [20, 20, 5]
    assert sorted(provider.review_batch_sizes, reverse=True) == \
        [20, 20, 5, 5, 2]
    assert report.checked == 52
    # split files landed in the attempt-versioned s5 dir
    s5 = job.store.stage_dir(5, 1)
    assert (s5 / "split_story.t.jsonl").exists()
    assert (s5 / "split_strings.t.jsonl").exists()
    assert job.store.exists(5, "lqa_report.t", attempt=1)


def test_t3_audit_trail_records_drops(tmp_path: Path, job: Job):
    """The filter must show what it filtered: overturned and
    below-threshold findings appear in t3_stats/t3_audit, and
    report.overturned counts them (it was silently 0 before)."""

    class FilteringProvider:
        name, model, tokens_spent = "f", "t", 0.0

        def complete(self, system, user, *, temperature=0.3,
                     max_tokens=2000):
            import re
            if "content classifier" in system:
                blocks = re.findall(r"^### (\S+)\n", user, flags=re.M)
                return json.dumps({"items": [
                    {"key": k, "domain": "ui", "confidence": 1.0}
                    for k in blocks if k != "END"]})
            if "second-layer" in system:
                # overturn findings on u0000; low-confidence on u0001
                if "u0000" in user:
                    return json.dumps({"decision": "overturn",
                                       "confidence": 0.9,
                                       "reasoning": "fp",
                                       "suggested_target": None})
                return json.dumps({"decision": "confirm",
                                   "confidence": 0.4, "reasoning": "weak",
                                   "suggested_target": None})
            if "QA reviewer" in system:
                keys = [k for k in re.findall(r"^### (\S+)$", user,
                                              flags=re.M) if k != "END"]
                return json.dumps({"findings": [
                    {"key": k, "bug_type": "mistranslation",
                     "severity": "medium", "message": f"issue {k}",
                     "evidence": "ev", "suggested_fix": None}
                    for k in keys[:2]]})
            return json.dumps({"findings": []})

    report = run_external_lqa(
        job, FilteringProvider(), _pairs_file(tmp_path, 0, 2), name="a")
    assert report.cascade_ledger["t3_ran"] == 1
    assert report.cascade_ledger["second_layer"] == 1
    assert report.cascade_ledger["t3_raw"] == 2
    assert report.cascade_ledger["t3_kept"] == 0
    assert report.t3_stats["raw"] == 2
    assert report.t3_stats["kept"] == 0
    assert report.t3_stats["overturned"] == 1
    assert report.t3_stats["below_threshold"] == 1
    assert report.overturned == 1                  # no longer stuck at 0
    reasons = {a["uid"]: a["reason"] for a in report.t3_audit}
    assert reasons["u0000"] == "overturned"
    assert reasons["u0001"].startswith("below_threshold")


def test_deterministic_only_zero_llm(tmp_path: Path, job: Job):
    class ExplodingProvider:
        name, model, tokens_spent = "x", "x", 0.0
        def complete(self, *a, **k):
            raise AssertionError("LLM called in deterministic-only mode")
    report = run_external_lqa(
        job, ExplodingProvider(), _pairs_file(tmp_path, 1, 2),
        name="d", deterministic_only=True)
    assert report.checked == 3


# ------------------------------------------------- cascade compliance audit

def _tampered_pairs(tmp_path: Path) -> Path:
    """One placeholder mismatch (T1) + a near-identical source pair rendered
    two ways (T2 consistency) + one clean row."""
    rows = [
        {"key": "P1", "source_text": "HP +{0}", "target_text": "生命值提升"},
        {"key": "C1", "source_text": "Attack", "target_text": "攻击"},
        {"key": "C2", "source_text": "attack ", "target_text": "进攻"},
        {"key": "OK", "source_text": "Settings", "target_text": "设置"},
    ]
    for row in rows:
        row.update(source_language="en", target_language="zh-CN")
    path = tmp_path / "tamper_pairs.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                              for r in rows), encoding="utf-8")
    return path


def test_cascade_ledger_telescopes_and_tiers_stamped(tmp_path: Path,
                                                     job: Job):
    from orbit8.graphs.lqa import verify_cascade
    report = run_external_lqa(
        job, None, _tampered_pairs(tmp_path), name="c",
        deterministic_only=True)
    led = report.cascade_ledger
    assert led["accepted"] == 4 and led["t1_flagged"] == 1     # P1
    assert led["t2_input"] == 3 and led["t2_flagged"] == 2     # C1+C2
    assert led["t3_input"] == 1 and led["t3_ran"] == 0         # OK survived
    assert led["t3_raw"] == 0 and led["second_layer"] == 0
    tiers = {vf.finding.tier
             for item in report.items for vf in item.findings}
    assert tiers == {1, 2}                 # every finding carries provenance
    assert verify_cascade(report) == []


def test_verify_cascade_catches_tampering(tmp_path: Path, job: Job):
    from orbit8.graphs.lqa import verify_cascade
    from orbit8.schemas import Verdict, VerdictDecision
    report = run_external_lqa(
        job, None, _tampered_pairs(tmp_path), name="v",
        deterministic_only=True)

    broken = report.model_copy(deep=True)   # ledger arithmetic broken
    broken.cascade_ledger["t1_flagged"] += 1
    assert any("T2 input" in v for v in verify_cascade(broken))

    unstamped = report.model_copy(deep=True)  # finding without provenance
    unstamped.items[0].findings[0].finding.tier = None
    assert any("no tier stamp" in v for v in verify_cascade(unstamped))

    faked = report.model_copy(deep=True)      # LLM verdict on a code tier
    faked.items[0].findings[0].verdict = Verdict(
        decision=VerdictDecision.CONFIRM, confidence=1.0, reasoning="x")
    assert any("T1/T2 are code" in v for v in verify_cascade(faked))

    bare = report.model_copy(deep=True)       # a tier never reported in
    bare.cascade_ledger = {}
    assert any("never ran" in v for v in verify_cascade(bare))
