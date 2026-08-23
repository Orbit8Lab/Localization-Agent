"""Merge-rule tests for glossary import — the rules ARE the review policy,
so each one is pinned: first-alternative wins, (verify) never silently
changes a translation, keep=N drops, unknown terms become conflicts,
audit notes are dated."""
import pytest

from orbit8.glossary_import import (ReviewRow, merge_review,
                                    validate_review_stdout)


def base(term, final, keep="Y", type_="ui", count=5.0):
    return ReviewRow(term=term, final=final, keep=keep, type=type_,
                     count=count)


def change(term, current, suggested, note=None):
    return ReviewRow(term=term, current=current, suggested=suggested,
                     note=note)


def test_change_applies_first_alternative_with_audit():
    terms, report = merge_review(
        [base("Journal", "日志"),
         change("Journal", "日志", "手记 / 笔记", "system-log connotation")],
        today="2026-07-30")
    entry = terms["Journal"]
    assert entry["translation"] == "手记"
    assert entry["alternatives"] == ["笔记"]
    assert entry["confidence"] == "reviewed_locked"
    assert "2026-07-30 review: 日志 → 手记" in entry["comment"]
    assert "system-log connotation" in entry["comment"]
    assert report.changes_applied == 1 and not report.conflicts


def test_verify_keeps_current_and_flags():
    terms, report = merge_review(
        [base("Note", "音符"),
         change("Note", "音符", "音符 (verify)", "split if written note")])
    entry = terms["Note"]
    assert entry["translation"] == "音符"          # NOT changed
    assert entry["confidence"] == "needs_review"
    assert "VERIFY" in entry["comment"]
    assert report.needs_review == 1 and report.changes_applied == 0


def test_keep_n_drops_and_unknown_term_conflicts():
    terms, report = merge_review(
        [base("Resume", "继续", keep="N"),
         base("Flute", "长笛"),
         change("Ghost", "鬼", "幽灵")])
    assert "Resume" not in terms and "Flute" in terms
    assert report.dropped_keep_n == 1
    assert len(report.conflicts) == 1 and "Ghost" in report.conflicts[0]


def test_case_insensitive_term_match_and_paren_cleanup():
    terms, _ = merge_review(
        [base("EFFECTS", "特效"),
         change("effects", "特效", "音效（音频总线）")])
    assert terms["EFFECTS"]["translation"] == "音效"


def test_undecided_sheet_rows_excluded_when_keep_exists():
    """Raw-extraction rows (final but no keep) must not contaminate a
    locked glossary that has keep decisions."""
    terms, report = merge_review(
        [base("Flute", "长笛", keep="Y"),
         ReviewRow(term="Back", final="返回"),        # raw sheet: no keep
         ReviewRow(term="Apply", final="应用")])
    assert set(terms) == {"Flute"}
    # but a keep-less import alone (plain csv hand-off) still works
    terms2, _ = merge_review([ReviewRow(term="Back", final="返回")])
    assert set(terms2) == {"Back"}


def test_parenthetical_qualifier_matches_base_term():
    terms, report = merge_review(
        [base("Note", "音符"),
         change("Note (mechanic)", "音符", "音符 (verify)", "split?")])
    assert terms["Note"]["confidence"] == "needs_review"
    assert not report.conflicts


def test_validator_rejects_wrong_mapping():
    with pytest.raises(ValueError, match="column mapping"):
        validate_review_stdout(
            '[{"term": "A", "final": null, "suggested": null}]', "f")
    rows = validate_review_stdout(
        '[{"term": "Term (EN)", "suggested": "x"}, '
        '{"term": "A", "final": "B"}]', "f")
    assert len(rows) == 1 and rows[0].term == "A"   # header row dropped
