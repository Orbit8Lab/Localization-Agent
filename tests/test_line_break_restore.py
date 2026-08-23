"""Line-break fidelity when a reviewed replacement re-enters a .po.

The received file's conventions are authoritative even where they look
inconsistent: one live UE asset writes ``\\`` before the newline in 77
of 95 multi-line entries and a bare newline in the other 18. Style is
therefore a property of the individual ENTRY, and a replacement must
inherit whichever marker the entry it replaces already used — never a
house style, and never the reviewer's incidental spelling.

Getting this wrong is invisible: the xlsx reads correctly, the diff looks
like a wording change, and the in-game line break silently disappears.
"""
from __future__ import annotations

from orbit8.po_patch import align_break_marker, normalize_break_notation

MARK = "\\\n"          # the asset's hard-break marker: backslash + newline


# ------------------------------------------------------- typed escapes

def test_literal_backslash_n_becomes_a_real_break():
    """PE row 52: the reviewer typed the escape instead of a break."""
    out = normalize_break_notation("Animal Sinew\\nplus a 200 fee")
    assert "\\n" not in out
    assert out == "Animal Sinew\nplus a 200 fee"


def test_literal_crlf_becomes_a_real_break():
    """PE row 410: the Windows spelling of the same mistake."""
    out = normalize_break_notation("Disconnected\\r\\nReturn to the lobby")
    assert out == "Disconnected\nReturn to the lobby"


def test_real_breaks_are_left_alone():
    text = "Disconnected\nReturn to the lobby"
    assert normalize_break_notation(text) == text


# ------------------------------------------------- per-entry style match

def test_replacement_inherits_the_entrys_marker():
    """The entry used '\\' before its break, so the rewrite must too."""
    original = f"Mana cost: 65 MP{MARK}Cooldown: 5s"
    out = align_break_marker(original, "Mana Cost: 65 MP\nCooldown: 5s")
    assert out == f"Mana Cost: 65 MP{MARK}Cooldown: 5s"


def test_plain_break_entry_stays_plain():
    """18 of 95 entries use a bare newline — do not add a marker."""
    original = "Disconnected\nReturn to the lobby to reconnect"
    out = align_break_marker(original, "Disconnected\nReturn to the lobby")
    assert "\\" not in out


def test_every_break_gets_the_marker():
    """A 3-line entry needs the marker on both breaks, not just the first."""
    out = align_break_marker(f"a{MARK}b{MARK}c", "x\ny\nz")
    assert out == f"x{MARK}y{MARK}z"
    assert out.count(MARK) == 2


def test_marker_is_not_doubled():
    """A reviewer who typed the marker correctly must not get '\\\\'."""
    out = align_break_marker(f"a{MARK}b", f"x{MARK}y")
    assert out == f"x{MARK}y"
    assert "\\\\" not in out


def test_single_line_replacement_untouched():
    assert align_break_marker("plain text", "rewritten") == "rewritten"


def test_crlf_entry_is_recognized_as_marked():
    """The marker must be detected regardless of CR in the stored text."""
    out = align_break_marker("a\\\r\nb", "x\ny")
    assert out == f"x{MARK}y"


# ----------------------------------------------------------- full chain

def test_typed_escape_then_marker_reproduces_source_style():
    """PE row 52 end to end: reviewer typed '\\n', entry uses the marker,
    result must match the file's own convention exactly."""
    original = f"Requires 1 Animal Tendon{MARK}plus a 200 crafting fee"
    typed = "Requires 1 Animal Sinew\\nplus a 200 crafting fee"
    out = align_break_marker(original, normalize_break_notation(typed))
    assert out == f"Requires 1 Animal Sinew{MARK}plus a 200 crafting fee"


def test_dangling_marker_regains_its_newline():
    """PE row 330: the modification reads '…destination.\\The goal…' — a
    marker whose newline was dropped upstream. The backslash only ever
    precedes a break in this asset, so it means the break is missing."""
    original = f"…escort the convoy to its destination.{MARK}The goal of B"
    typed = "…escort the Convoy to its destination.\\The goal of B"
    out = align_break_marker(original, typed)
    assert out == f"…escort the Convoy to its destination.{MARK}The goal of B"
    assert out.count(MARK) == 1


def test_dangling_marker_untouched_on_plain_break_entries():
    """Only entries that USE the marker get this repair."""
    out = align_break_marker("plain\nbreak entry", "a\\b")
    assert out == "a\\b"


def test_escape_sequences_are_not_mistaken_for_dangling_markers():
    """A real escape ('\\\\', '\\t', '\\\"') is content, not a lost break."""
    original = f"a{MARK}b"
    for text in ("keeps \\\\ intact", 'quote \\" here', "tab \\t here"):
        assert "\\\n" not in align_break_marker(original, text)


def test_empty_input_is_safe():
    assert normalize_break_notation("") == ""
    assert normalize_break_notation(None) == ""
    assert align_break_marker("a\nb", "") == ""


def test_trailing_marker_is_left_alone():
    """The source keeps a dangling marker at end-of-string; there is no
    following line for it to separate, so adding one appends a blank."""
    original = f"a{MARK}b\\"
    out = align_break_marker(original, "x\\")
    assert out == "x\\"
    assert not out.endswith("\n")
