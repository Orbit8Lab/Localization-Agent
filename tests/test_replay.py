"""Replaying a past run against today's code.

The rest of the suite proves each piece behaves on inputs a developer
invented. This proves the cascade still behaves on inputs a CLIENT sent.

The harness has one job that is easy to get wrong: it must distinguish
"the code changed" from "I compared against something that never stored
what I am comparing". Historical `scan_report.json` files record COUNTS
only — `findings: 681`, not the findings — so an item-level comparison
against them would silently compare nothing and report a clean pass.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.replay import (compare, fingerprint_report, fingerprint_stored,
                           find_runs, read_run, write_baseline)
from orbit8.schemas import (BugType, Finding, LQAItem, LQAReport, Severity,
                            VerifiedFinding)


def _report(items, ledger=None) -> LQAReport:
    return LQAReport(
        job_id="j", locale="en", checked=len(items),
        flagged_strings=len(items), findings_total=len(items),
        confirmed=0, overturned=0, uncertain=0, block_ship=False,
        by_severity={}, by_bug_type={}, items=items,
        cascade_ledger=ledger or {})


def _item(uid, source, findings=()) -> LQAItem:
    return LQAItem(
        uid=uid, game_keys=[uid], source=source, target="t",
        findings=[VerifiedFinding(finding=Finding(
            key=uid, bug_type=bt, severity=sv, tier=tier,
            message="m", evidence="e"))
            for bt, sv, tier in findings])


# ------------------------------------------------------------- baselines

def test_baseline_round_trips(tmp_path: Path):
    report = _report([_item("u1", "确定",
                            [(BugType.TERMINOLOGY, Severity.HIGH, 1)])])
    path = write_baseline(report, tmp_path / "gold.json")
    assert fingerprint_stored(path) == fingerprint_report(report)


def test_baseline_records_the_code_version(tmp_path: Path):
    """The most misleading diff is one where the code moved and nobody
    wrote down that it did."""
    path = write_baseline(_report([]), tmp_path / "gold.json", note="pre")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["note"] == "pre"
    assert "commit" in data


def test_fingerprint_ignores_message_wording():
    """Rewording a finding's message is not a behaviour change. A harness
    that failed on it would be switched off within a week."""
    a = _report([_item("u1", "s", [(BugType.TERMINOLOGY, Severity.HIGH, 1)])])
    b = _report([_item("u1", "s", [(BugType.TERMINOLOGY, Severity.HIGH, 1)])])
    b.items[0].findings[0].finding.message = "completely different wording"
    assert fingerprint_report(a) == fingerprint_report(b)


def test_fingerprint_is_order_independent():
    """Batch composition changed in this session; item ORDER moving is not
    a regression, item CONTENT moving is."""
    one = _item("u1", "a", [(BugType.TERMINOLOGY, Severity.HIGH, 1)])
    two = _item("u2", "b", [(BugType.CONSISTENCY, Severity.LOW, 2)])
    assert fingerprint_report(_report([one, two])) == fingerprint_report(
        _report([two, one]))


# ------------------------------------------------------------ comparison

def test_a_severity_downgrade_is_caught():
    """The exact regression shape this exists for: one string of 400
    quietly changing severity."""
    old = _report([_item("u1", "s", [(BugType.PLACEHOLDER, Severity.HIGH, 1)])])
    new = _report([_item("u1", "s",
                         [(BugType.PLACEHOLDER, Severity.MEDIUM, 1)])])
    result = compare(fingerprint_report(old), fingerprint_report(new))
    assert not result.identical
    assert len(result.changed) == 1
    assert result.changed[0][0] == "u1"


def test_gained_and_lost_are_reported_separately():
    """A net-zero change of "10 gained, 10 lost" is not "no change", and
    only the identities can tell them apart."""
    old = _report([_item("u1", "a", [(BugType.TERMINOLOGY, Severity.HIGH, 1)])])
    new = _report([_item("u2", "b", [(BugType.TERMINOLOGY, Severity.HIGH, 1)])])
    result = compare(fingerprint_report(old), fingerprint_report(new))
    assert len(result.only_old) == 1 and len(result.only_new) == 1
    assert result.same == 0


def test_identical_runs_compare_clean():
    report = _report([_item("u1", "s",
                            [(BugType.TERMINOLOGY, Severity.HIGH, 1)])])
    result = compare(fingerprint_report(report), fingerprint_report(report))
    assert result.identical and result.same == 1


def test_ledger_drift_is_surfaced():
    result = compare([], [], ledger_old={"t2_flagged": 137},
                     ledger_new={"t2_flagged": 4})
    assert "137" in result.summary() and "differs" in result.summary()


# ------------------------------------------------------- reading a run

def test_a_missing_po_is_refused_not_silently_scanned(tmp_path: Path):
    """Scanning a DIFFERENT file than the one recorded would produce a
    diff that means nothing."""
    report = tmp_path / "scan_report.json"
    report.write_text(json.dumps({"po": str(tmp_path / "gone.po")}),
                      encoding="utf-8")
    run = read_run(report)
    assert not run.replayable
    assert "gone.po" in run.missing[0]


def test_a_run_with_intact_inputs_is_replayable(tmp_path: Path):
    po = tmp_path / "Game.po"
    po.write_text("", encoding="utf-8")
    report = tmp_path / "scan_report.json"
    report.write_text(json.dumps(
        {"po": str(po), "cascade_ledger": {"accepted": 5},
         "flagged_strings": 2}), encoding="utf-8")
    run = read_run(report)
    assert run.replayable
    assert run.ledger == {"accepted": 5} and run.flagged == 2


def test_replay_refuses_an_unreplayable_run(tmp_path: Path):
    from orbit8.replay import replay
    report = tmp_path / "scan_report.json"
    report.write_text(json.dumps({"po": str(tmp_path / "gone.po")}),
                      encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        replay(read_run(report), tmp_path / "out")


def test_find_runs_locates_stored_reports(tmp_path: Path):
    for name in ("run-a", "run-b"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "scan_report.json").write_text("{}", encoding="utf-8")
    assert len(find_runs(tmp_path)) == 2


def test_historical_reports_carry_no_item_detail(tmp_path: Path):
    """Documents WHY baselines must be taken deliberately: a real stored
    report has `findings: 681` — an integer, not a list — and the run DB
    beside it persists an empty findings_json. Item-level comparison
    against pre-existing runs is impossible, and the harness must not
    pretend otherwise by reporting a clean pass over zero items."""
    stored = tmp_path / "scan_report.json"
    stored.write_text(json.dumps(
        {"po": "x.po", "findings": 681, "bug_rows": 681,
         "cascade_ledger": {"accepted": 1094}}), encoding="utf-8")
    assert fingerprint_stored(stored) == []
