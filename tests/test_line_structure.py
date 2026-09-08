"""Line-break structure must be a MECHANICAL check, not a prompt rule.

The client's STY-08 says `\\r \\n \\` counts and positions must match the
source. It was classified `enforcement: "llm"` — a prompt suggestion —
and no code anywhere compared newline counts. Measured on the 1,233-row
round-1 corpus: batching a translation dropped embedded newlines on 32
strings that per-sentence translation preserved, taking STY-08
violations from 17 to 40. Multi-line recipe text ("Lv.1 Two-Handed
Hammer: Requires 4 Iron Ore\\nLv.2 …") arrived as a single flattened
line.

This is decidable by counting, so per design §7 it belongs in the gate:
a prompt instruction is a suggestion, a check is a guarantee. Detecting
it is also what makes a targeted repair possible — re-translating only
the affected strings rather than the batch.
"""
from __future__ import annotations

from orbit8.gate_checks import GateConfig, run_gate
from orbit8.schemas import BugType, Severity


def _findings(source: str, target: str):
    return [f for f in run_gate(key="k", source=source, target=target,
                                cfg=GateConfig(target_lang="en"))
            if f.bug_type is BugType.MARKUP]


def test_a_dropped_newline_is_flagged():
    """The exact regression batching introduced."""
    found = _findings("第一行\n第二行", "First line second line")
    assert found, "a lost line break must be a finding"
    assert found[0].severity is Severity.HIGH


def test_preserved_newlines_pass():
    assert _findings("第一行\n第二行", "First line\nSecond line") == []


def test_an_added_newline_is_NOT_flagged():
    """Adding structure is allowed, on the evidence.

    A first version of this test asserted the opposite, reasoning that
    inventing a break could overflow a fixed-height widget. Measured
    against the post-editor's own output, that formulation fired 16
    times on shipped text, and 5 of those were the editor deliberately
    adding breaks for readability where the source had none. No client
    rule forbids it, so the check does not.
    """
    assert _findings("单行文本", "One\nline") == []


def test_carriage_returns_are_counted_separately():
    r"""\r and \n are tracked independently, so swapping one for the
    other is caught as a lost \r."""
    assert _findings("第一行\r第二行", "First\nSecond") != []
    assert _findings("第一行\r第二行", "First\rSecond") == []


def test_a_continuation_marker_may_be_reflowed():
    r"""`\` + newline is a UE line-continuation marker, not a rendered
    break. The editor reflows those into prose 13 times in the corpus,
    so flagging them would be wrong."""
    assert _findings("第一行\\\n第二行", "First line second line") == []


def test_multiple_breaks_must_all_survive():
    """The real failure was 3 newlines collapsing to 0."""
    src = "一\n二\n三\n四"
    assert _findings(src, "One\nTwo\nThree\nFour") == []
    assert _findings(src, "One\nTwo Three Four") != []


def test_a_source_with_no_breaks_is_never_flagged():
    """Most strings are single-line; the check must be silent there."""
    assert _findings("确定", "Confirm") == []


def test_literal_backslash_n_is_counted():
    r"""PO files carry escaped \n as two characters in some exports; the
    check reads the unescaped text it is given, so a literal
    backslash-n in BOTH sides must not be read as a mismatch."""
    assert _findings(r"第一行\n第二行", r"First\nSecond") == []


def test_the_message_names_the_counts():
    """A finding a post-editor cannot act on is noise: say what was
    expected and what arrived."""
    found = _findings("一\n二\n三", "One two three")
    assert found
    assert "2" in found[0].message and "0" in found[0].message
