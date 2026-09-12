"""Coverage must catch the bug that motivated it.

A locked term that matches nothing produces no finding, no warning and
no log line, so silence looked exactly like compliance. These tests pin
the one surface that can tell the difference.
"""
from __future__ import annotations

from orbit8 import glossary_coverage as gc


def test_a_term_that_matches_nothing_is_reported_unmatched():
    report = gc.measure([("恢复生命值", "Restores Health")],
                        {"瘟疫点": "Plague Node"})
    assert report.unmatched == ["瘟疫点"]
    assert report.matched == 0


def test_the_boss_regression_would_now_be_visible():
    """The actual failure: a Latin term inside CJK. Before the
    `term_spans` fix this scored 0 matches and the operator was told
    nothing at all."""
    pairs = [("摧毁BOSS能降低世界感染值", "Destroying the BOSS can reduce it"),
             ("前往BOSS区域", "Go to the BOSS Area")]
    report = gc.measure(pairs, {"BOSS": "Boss"}, term_case={"BOSS": "exact"})
    assert report.matched == 1, "the term must be reachable in CJK source"
    assert report.never_applied == ["BOSS"], \
        "it occurs twice and is never rendered as locked — say so"


def test_zero_reach_is_called_a_matcher_fault_not_a_terminology_gap():
    """The distinction that matters operationally: a glossary is never
    0% relevant to its own project, so 0% reach is a bug."""
    report = gc.measure([("恢复生命值", "Restores Health")],
                        {"瘟疫点": "Plague Node", "撤离": "Extraction"})
    warnings = report.warnings()
    assert warnings and "matcher or encoding fault" in warnings[0]
    assert report.reach == 0.0


def test_a_correctly_applied_term_produces_no_warning():
    """Silence has to be earned, or operators learn to ignore the
    channel."""
    report = gc.measure([("净化瘟疫点", "Purify the Plague Node")],
                        {"瘟疫点": "Plague Node"})
    assert report.matched == 1
    assert report.never_applied == []
    assert report.warnings() == []


def test_declared_forms_count_as_applied():
    """合成 is locked as the noun "Crafting" but declares verb "craft";
    honouring the ruling must not be reported as a violation."""
    report = gc.measure([("通过材料合成", "Craft using materials")],
                        {"合成": "Crafting"},
                        term_forms={"合成": {"verb": "craft",
                                             "noun": "crafting"}})
    assert report.never_applied == []


def test_case_context_makes_the_check_vacuous_and_that_is_visible():
    """With case='context' any casing satisfies the term, so a casing
    defect is NOT reported here. The report should therefore show the
    term as applied — the gap is in the glossary metadata, and
    `never_applied` must not silently absorb it."""
    pairs = [("摧毁BOSS", "Destroy the BOSS")]
    lax = gc.measure(pairs, {"BOSS": "Boss"}, term_case={"BOSS": "context"})
    strict = gc.measure(pairs, {"BOSS": "Boss"}, term_case={"BOSS": "exact"})
    assert lax.never_applied == []
    assert strict.never_applied == ["BOSS"]


def test_reach_is_one_when_every_term_is_exercised():
    report = gc.measure([("净化瘟疫点并撤离", "Purify the Plague Node and Extraction")],
                        {"瘟疫点": "Plague Node", "撤离": "Extraction"})
    assert report.reach == 1.0


# ------------------------------------------------ the detection that works

def test_reach_alone_would_have_missed_the_bug():
    """Documents WHY per-term occurrences exist.

    On the real corpus, aggregate reach barely moved across the matcher
    fix (90.2% -> 92.7%) because BOSS still matched at string edges,
    while its detected occurrences went 2 -> 28. A summary number would
    have shown a healthy glossary. This asserts the shape of that trap
    on a miniature corpus.
    """
    # 'BOSS' is reachable in one string (edge) and unreachable in the
    # rest (Han-flanked) under the old matcher.
    pairs = [("BOSS", "The Boss")] + [(f"摧毁BOSS第{i}关", "Destroy the Boss")
                                      for i in range(10)]
    locked = {"BOSS": "Boss"}
    full = gc.measure(pairs, locked, term_case={"BOSS": "exact"})
    # Post-fix the term is seen in every string, not just the bare one.
    assert full.per_term["BOSS"].sources == 11
    assert full.reach == 1.0, "reach is 100% either way — hence the trap"


def test_compare_isolates_a_matcher_change():
    """`compare` is the check that catches a partly-failing matcher."""
    pairs = [("摧毁BOSS", "Destroy the Boss"), ("前往BOSS区域", "Go to the Boss Area")]
    locked = {"BOSS": "Boss"}
    after = gc.measure(pairs, locked)

    # Simulate the pre-fix matcher: Han-flanked Latin matches nothing.
    import re
    def broken(term, text):
        tl, xl = term.lower(), text.lower()
        left = r"\b" if (tl[:1].isascii() and tl[:1].isalnum()) else ""
        right = r"\b" if (tl[-1:].isascii() and tl[-1:].isalnum()) else ""
        return [(m.start(), m.end())
                for m in re.finditer(left + re.escape(tl) + right, xl)]
    original = gc.term_spans
    gc.term_spans = broken
    try:
        before = gc.measure(pairs, locked)
    finally:
        gc.term_spans = original

    assert before.per_term["BOSS"].sources == 0
    assert gc.compare(before, after) == ["BOSS: 0 -> 2 source matches"]


def test_thin_match_warning_needs_a_corpus_worth_judging():
    """On a 3-string corpus, "matched once" is meaningless — the warning
    must not fire, or it becomes noise on every small scan."""
    pairs = [("净化瘟疫点", "Purify the Plague Node")]
    report = gc.measure(pairs, {"瘟疫点": "Plague Node"})
    assert report.corpus_size == 1
    assert report.warnings() == []
