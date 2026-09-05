"""The reviewer's own fix belongs in the deliverable.

`Finding.suggested_fix` is populated by the T3 reviewer and stored in the
LQA report artifact. The xlsx writer read only the Repair-agent
`suggestions` dict, so a report built with `--no-suggestions` shipped an
EMPTY "Expected Result / Suggested Translation" column — while dozens of
usable fixes sat unread in the JSON it was reading from. On the real
Nomori ja audit that was 48 fixes discarded across 349 rows, including
unclosed `<size>` tag repairs a developer could apply verbatim.

The two sources are not interchangeable and the workbook must not pretend
they are: a Repair-agent suggestion is re-gated against the glossary and
style rules before shipping, a reviewer's is not. So the fallback is
labelled in the Orbit8 Comment column.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbit8.bug_report import (CODE_TOKEN_NOTE, REVIEWER_FIX_NOTE,
                               write_bug_report_xlsx)
from orbit8.schemas import (BugType, Finding, LQAItem, LQAReport, Severity,
                            VerifiedFinding)


def _report(*findings_per_item, locale="ja"):
    items = []
    for n, findings in enumerate(findings_per_item):
        items.append(LQAItem(
            uid=f"u{n:04d}", game_keys=[f"line:{n}"],
            source=f"Source {n}", target=f"ターゲット {n}",
            findings=findings))
    return LQAReport(job_id="j", locale=locale, checked=len(items),
                     flagged_strings=len(items),
                     findings_total=sum(len(f) for f in findings_per_item),
                     confirmed=0, overturned=0, uncertain=0,
                     block_ship=False, items=items)


def _finding(*, fix=None, bug=BugType.MISTRANSLATION, key="u0000"):
    return VerifiedFinding(finding=Finding(
        key=key, bug_type=bug, severity=Severity.MEDIUM,
        message="something is wrong", evidence="ターゲット",
        suggested_fix=fix, tier=3))


def _cells(path: Path):
    """openpyxl reads an empty cell as None; normalise so a test can say
    "blank" without caring which."""
    import openpyxl
    sheet = openpyxl.load_workbook(path).active
    headers = [c.value for c in sheet[1]]
    return [{h: ("" if v is None else v) for h, v in zip(headers, row)}
            for row in sheet.iter_rows(min_row=2, values_only=True)]


def test_a_reviewer_fix_reaches_the_column(tmp_path):
    """The regression: this cell was empty."""
    out = tmp_path / "r.xlsx"
    write_bug_report_xlsx(_report([_finding(fix="正しい訳")]), out,
                          suggestions={}, game="G")
    row = _cells(out)[0]
    assert row["Expected Result / Suggested Translation"] == "正しい訳"


def test_a_reviewer_fix_is_labelled_as_unverified(tmp_path):
    """It was NOT re-gated against the glossary, so it must not read as a
    verified repair."""
    out = tmp_path / "r.xlsx"
    write_bug_report_xlsx(_report([_finding(fix="正しい訳")]), out,
                          suggestions={}, game="G")
    assert _cells(out)[0]["Orbit8 Comment"] == REVIEWER_FIX_NOTE


def test_the_repair_agent_wins_over_the_reviewer(tmp_path):
    """Precedence. The Repair agent's output passed the deterministic
    gate; the reviewer's did not, so it is the weaker claim."""
    out = tmp_path / "r.xlsx"
    write_bug_report_xlsx(_report([_finding(fix="reviewer")]), out,
                          suggestions={"u0000": "repaired"}, game="G")
    row = _cells(out)[0]
    assert row["Expected Result / Suggested Translation"] == "repaired"
    assert row["Orbit8 Comment"] != REVIEWER_FIX_NOTE


def test_a_finding_with_no_fix_stays_blank(tmp_path):
    """Never invent a suggestion."""
    out = tmp_path / "r.xlsx"
    write_bug_report_xlsx(_report([_finding(fix=None)]), out,
                          suggestions={}, game="G")
    assert _cells(out)[0]["Expected Result / Suggested Translation"] == ""


def test_an_empty_string_fix_is_treated_as_absent(tmp_path):
    out = tmp_path / "r.xlsx"
    write_bug_report_xlsx(_report([_finding(fix="")]), out,
                          suggestions={}, game="G")
    row = _cells(out)[0]
    assert row["Expected Result / Suggested Translation"] == ""
    assert row["Orbit8 Comment"] != REVIEWER_FIX_NOTE


def test_the_fallback_is_per_finding_not_per_item(tmp_path):
    """One string can carry several findings, each with its own fix.
    `suggestions` is keyed by uid and rewrites the whole string;
    `suggested_fix` addresses one defect, so the rows must differ."""
    out = tmp_path / "r.xlsx"
    write_bug_report_xlsx(
        _report([_finding(fix="fix A"),
                 _finding(fix="fix B", bug=BugType.PLACEHOLDER)]),
        out, suggestions={}, game="G")
    got = {r["Expected Result / Suggested Translation"] for r in _cells(out)}
    assert got == {"fix A", "fix B"}


def test_a_code_token_note_is_not_overwritten(tmp_path):
    """A blank suggestion must always be explained. Where there is no fix
    to fall back on, the existing explanation still stands."""
    out = tmp_path / "r.xlsx"
    report = _report([_finding(fix=None, bug=BugType.UNTRANSLATED)])
    report.items[0].source = "OK"          # a code/name token
    write_bug_report_xlsx(report, out, suggestions={}, game="G")
    row = _cells(out)[0]
    assert row["Expected Result / Suggested Translation"] == ""
    assert row["Orbit8 Comment"] == CODE_TOKEN_NOTE


def test_a_reviewer_fix_beats_the_code_token_note(tmp_path):
    """When a fix exists, showing it is more useful than explaining a
    blank that is no longer blank."""
    out = tmp_path / "r.xlsx"
    report = _report([_finding(fix="OK!", bug=BugType.UNTRANSLATED)])
    report.items[0].source = "OK"
    write_bug_report_xlsx(report, out, suggestions={}, game="G")
    row = _cells(out)[0]
    assert row["Expected Result / Suggested Translation"] == "OK!"
    assert row["Orbit8 Comment"] == REVIEWER_FIX_NOTE
