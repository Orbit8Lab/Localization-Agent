"""A Latin-script glossary term embedded in CJK source.

`term_spans` guards ASCII terms with `\\b` so "spirit" does not match
inside "spirited". In a CJK source that guard silently fails: Python's
`\\w` includes Han characters, so there is no word boundary between 毁
and the B of BOSS, and the term matches NOTHING.

Consequence measured on project002 (1,349 strings, zh->en): the
glossary locks BOSS -> "Boss", but the term never reached the
translator's glossary brief and never raised a gate finding, so every
occurrence shipped as "BOSS". That was 11 of the corpus's style
violations — the single largest defect class — against a ruling the
glossary already contained.

Three of that glossary's locked terms start with an ASCII alnum
(BOSS, Boss玩法, 8分钟); all were unreachable in CJK context.
"""
from __future__ import annotations

from orbit8.gate_checks import applicable_terms, term_spans


def test_latin_term_matches_when_flanked_by_han():
    """The regression, minimal: BOSS inside a Chinese sentence."""
    assert term_spans("BOSS", "摧毁BOSS能降低世界感染值") != []


def test_latin_term_is_found_case_insensitively_in_cjk():
    """Source and glossary disagree on case constantly — the glossary
    holds BOSS, the script writes Boss, and both must match."""
    for source in ("摧毁BOSS能获胜", "摧毁Boss能获胜", "摧毁boss能获胜"):
        assert term_spans("BOSS", source) != [], source


def test_the_locked_rendering_becomes_applicable():
    """What actually matters: the term reaches `applicable_terms`, which
    is what feeds BOTH the translator's glossary brief and the gate."""
    got = applicable_terms("摧毁BOSS能降低世界感染值", {"BOSS": "Boss"})
    assert got == {"BOSS": "Boss"}


def test_mixed_term_with_han_and_latin_still_matches():
    """`Boss玩法` starts ASCII and ends Han — the boundary logic is
    per-edge, so this exercises the other side of the guard."""
    assert term_spans("Boss玩法", "进入Boss玩法界面") != []


def test_the_word_boundary_still_protects_latin_context():
    """The guard exists for a reason and must survive the fix: a term
    must NOT match inside a longer Latin word."""
    assert term_spans("spirit", "a spirited defence") == []
    assert term_spans("spirit", "restores spirit now") != []


def test_boundary_holds_for_the_trailing_edge_too():
    assert term_spans("boss", "bossy manager") == []
    assert term_spans("boss", "the boss said") != []


def test_digit_leading_term_in_cjk():
    """`8分钟` is locked in the same glossary and starts with a digit,
    so it hit the identical guard."""
    assert term_spans("8分钟", "还剩8分钟") != []


# --------------------------------------------- term_in_text, same guard

def test_term_in_text_shares_the_fix():
    """`term_in_text` carried the identical bug behind a docstring that
    claimed to be CJK-safe. Both functions must agree, or the gate and
    the brief disagree about which terms apply."""
    from orbit8.gate_checks import term_in_text
    assert term_in_text("BOSS", "摧毁BOSS能降低世界感染值")
    assert term_in_text("8分钟", "还剩8分钟")
    # and the Latin guard still holds
    assert not term_in_text("spirit", "a spirited defence")
    assert term_in_text("spirit", "restores spirit now")


def test_the_two_matchers_never_disagree():
    """Whatever the input, presence per `term_in_text` and a non-empty
    span list per `term_spans` are the same question."""
    from orbit8.gate_checks import term_in_text
    cases = [
        ("BOSS", "摧毁BOSS能获胜"), ("BOSS", "no boss here at all"),
        ("spirit", "a spirited defence"), ("spirit", "restores spirit"),
        ("boss", "bossy manager"), ("8分钟", "还剩8分钟"),
        ("Boss玩法", "进入Boss玩法界面"), ("Plague Node", "净化瘟疫点"),
    ]
    for term, text in cases:
        assert term_in_text(term, text) == bool(term_spans(term, text)), \
            f"{term!r} in {text!r}"
