"""Coverage bugs found by replaying a real 1,349-string client corpus.

Three defects, all silent, all found the same way: the numbers in a
stored ledger did not match what today's code produced, and the reason
turned out to be real every time.

Each is a COVERAGE bug rather than a wrong answer, which is why none
surfaced as a failing test. A string removed from the pipeline produces
no error — it produces a clean report that checked less than it claimed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbit8.graphs.lqa import verify_cascade
from orbit8.memory import RunDB
from orbit8.po_scan import _seed, scan_po
from orbit8.schemas import BugType, LQAReport
from orbit8.style_defaults import ZH_EN
from orbit8.style_guide import StyleRule, _run_check


# ------------------------------------- 1. the parity rule was symmetric

PARITY = StyleRule(id="ZH-EN-03", text="x", enforcement="mechanical",
                   check="require_source_parity", value=".!?。！？")


def test_english_may_add_a_period_the_chinese_omitted():
    """The regression: this fired 213 times on one real corpus and 212 of
    those were CORRECT translations. Chinese item descriptions omit the
    trailing 。; English sentences require a period."""
    hit, _ = _run_check(PARITY, "动物皮背包，耐用容量大",
                        "A durable leather backpack with plenty of space.")
    assert not hit


def test_a_dropped_sentence_ending_is_still_caught():
    """The direction the rule was actually written for must keep working
    — the fix is narrowing, not disabling."""
    hit, _ = _run_check(PARITY, "这是一句话。", "This is a sentence")
    assert hit


@pytest.mark.parametrize("source,target", [
    ("这是一句话。", "This is a sentence."),      # both end
    ("背包", "Backpack"),                        # neither ends
])
def test_matching_ends_are_never_flagged(source, target):
    assert not _run_check(PARITY, source, target)[0]


def test_the_shipped_zh_en_guide_uses_the_fixed_rule():
    """The default guide is what every zh→en job loads; a fix that lived
    only in the checker would not reach it."""
    rule = next(r for r in ZH_EN.rules if r.id == "ZH-EN-03")
    assert rule.check == "require_source_parity"
    assert not _run_check(rule, "耐用容量大", "Durable and roomy.")[0]


# ------------------------- 2. po_scan deduped by source, losing targets

def test_one_source_two_renderings_yields_two_rows(tmp_path: Path):
    """Deduping by SOURCE alone kept only the first target and attached it
    to every occurrence — so a bug row could pair a source with a
    rendering from a different key, and T2's inconsistency rule became
    unreachable because no source ever appeared twice.

    `external_lqa.seed_audit_db` was fixed for this; `po_scan._seed` was
    not, so the two entry points disagreed.
    """
    db = RunDB(tmp_path / "run.db")
    entries = [("k1", "确定", "Confirm", ""),
               ("k2", "确定", "OK", ""),
               ("k3", "取消", "Cancel", "")]
    uid_map, inconsistent = _seed(db, entries)

    assert len(uid_map) == 3, "a second rendering was silently dropped"
    targets = {row["target"] for row in uid_map.values()
               if row["source"] == "确定"}
    assert targets == {"Confirm", "OK"}
    assert len(inconsistent) == 1 and inconsistent[0]["source"] == "确定"


def test_identical_pairs_still_collapse(tmp_path: Path):
    """The saving the dedup exists for is not given up."""
    db = RunDB(tmp_path / "run.db")
    uid_map, inconsistent = _seed(db, [("k1", "确定", "OK", ""),
                                       ("k2", "确定", "OK", ""),
                                       ("k3", "确定", "OK", "")])
    assert len(uid_map) == 1
    assert sorted(next(iter(uid_map.values()))["keys"]) == ["k1", "k2", "k3"]
    assert inconsistent == []


def test_t2_can_see_an_inconsistency_end_to_end(tmp_path: Path):
    """The whole point of the dedup fix: T2 rule (a) needs two rows that
    share a source. With the old dedup it could never fire."""
    po = tmp_path / "g.po"
    po.write_text(
        'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgctxt ",k1"\nmsgid "确定"\nmsgstr "Confirm"\n\n'
        'msgctxt ",k2"\nmsgid "确定"\nmsgstr "OK"\n\n',
        encoding="utf-8")
    result = scan_po(po, None, tmp_path / "out", game="G", locale="en",
                     deterministic_only=True, suggestions=False)
    consistency = [f for item in result.report.items for f in item.findings
                   if f.finding.bug_type is BugType.CONSISTENCY]
    assert consistency, "T2 never saw the inconsistency"
    assert len(result.inconsistent) == 1


# --------------------------- 3. full scan: every tier sees every string

def _po(tmp_path: Path) -> Path:
    """A string carrying BOTH a mechanical defect and an inconsistency."""
    po = tmp_path / "g.po"
    po.write_text(
        'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\n'
        'msgctxt ",k1"\nmsgid "确定"\nmsgstr "Confirm  now"\n\n'
        'msgctxt ",k2"\nmsgid "确定"\nmsgstr "OK"\n\n',
        encoding="utf-8")
    return po


def test_full_scan_reports_every_defect_on_one_string(tmp_path: Path):
    """A string whose OTHER problems waited for the next round trip is a
    second delivery the client did not need. One scan that finds
    everything beats three that each find one thing."""
    result = scan_po(_po(tmp_path), None, tmp_path / "full", game="G",
                     locale="en", deterministic_only=True,
                     suggestions=False, full_scan=True)
    led = result.report.cascade_ledger
    assert led["t2_input"] == led["accepted"]
    assert led["t3_input"] == led["accepted"]


def test_ladder_mode_still_short_circuits(tmp_path: Path):
    """The cost ladder remains available — the change is the default, not
    the removal of a mode."""
    result = scan_po(_po(tmp_path), None, tmp_path / "ladder", game="G",
                     locale="en", deterministic_only=True,
                     suggestions=False, full_scan=False)
    led = result.report.cascade_ledger
    assert led["t2_input"] == led["accepted"] - led["t1_flagged"]


def test_full_scan_is_a_superset_of_ladder(tmp_path: Path):
    """A stronger claim, never a weaker one: full scan must not LOSE a
    finding the ladder would have reported."""
    def types(full):
        result = scan_po(_po(tmp_path), None, tmp_path / f"s{full}",
                         game="G", locale="en", deterministic_only=True,
                         suggestions=False, full_scan=full)
        return {(item.uid, f.finding.bug_type.value)
                for item in result.report.items for f in item.findings}
    assert types(False) <= types(True)


def test_the_ledger_records_which_mode_ran(tmp_path: Path):
    """The report is what a reader gets six weeks later; "did every tier
    see every string?" must be answerable from it alone."""
    full = scan_po(_po(tmp_path), None, tmp_path / "a", game="G",
                   locale="en", deterministic_only=True, suggestions=False,
                   full_scan=True)
    ladder = scan_po(_po(tmp_path), None, tmp_path / "b", game="G",
                     locale="en", deterministic_only=True,
                     suggestions=False, full_scan=False)
    assert full.report.cascade_ledger["full_scan"] == 1
    assert ladder.report.cascade_ledger["full_scan"] == 0


def test_verify_cascade_accepts_both_shapes():
    """The audit must not reject a full scan for failing to telescope —
    nor accept a run where a tier silently skipped strings."""
    def report(ledger):
        return LQAReport(
            job_id="j", locale="en", checked=ledger["accepted"],
            flagged_strings=0, findings_total=0, confirmed=0, overturned=0,
            uncertain=0, block_ship=False, items=[], cascade_ledger=ledger)

    full = {"accepted": 100, "t1_flagged": 10, "t2_input": 100,
            "t2_flagged": 5, "t3_input": 100, "t3_ran": 0,
            "second_layer": 0, "t3_raw": 0, "t3_kept": 0, "full_scan": 1}
    assert verify_cascade(report(full)) == []

    ladder = {**full, "full_scan": 0, "t2_input": 90, "t3_input": 85}
    assert verify_cascade(report(ladder)) == []

    # A full scan whose T3 skipped strings must NOT pass.
    broken = {**full, "t3_input": 60}
    assert verify_cascade(report(broken))


def test_both_tiers_findings_survive_on_one_string():
    """`{**t1, **t2}` keys on uid, so a string flagged by both tiers kept
    only T2's findings. Invisible in ladder mode (the overlap was always
    empty); every overlapping string hits it under full scan."""
    import orbit8.graphs.lqa as lqa_module
    source = __import__("inspect").getsource(lqa_module.build_lqa_graph)
    assert "{**state.get(\"findings_t1\"" not in source, (
        "tiers are dict-merged; T1 findings are lost on strings T2 also "
        "flags")
