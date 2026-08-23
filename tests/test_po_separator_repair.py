"""Inbound separator repair: a received target must decode to the same
layer-2 separator token as the msgid of its OWN entry.

A received UE .po drop came back escaped one level too many:
a source separator written ``\\n`` returned as ``\\\\\\r\\n``, which the
engine renders as a printed backslash instead of a line break. Invisible
in the xlsx, invisible in a wording diff, wrong on screen.

The asset uses three separator conventions and they are NOT
interchangeable, so the repair is per-entry, never a house style.
"""
from __future__ import annotations

from orbit8.po_patch import (audit_separators, match_source_format,
                             po_escape_raw, po_unescape_raw,
                             read_raw_entries, repair_po_separators,
                             repair_separator_raw, separator_convention,
                             separators)

# The three layer-2 conventions, as they appear in the FILE (layer 1).
RAW_N = r"\\n"          # decodes to  \ + n   — 157 msgids, the dominant one
RAW_CRLF = r"\r\n"      # decodes to  CR LF   —  29 msgids
RAW_BS = r"\\\\"        # decodes to  \ \     —  12 msgids


# ------------------------------------------------------- escape round trip

def test_unescape_consumes_each_pair_once():
    """``\\\\n`` is a backslash followed by 'n' — NOT a newline. A chained
    str.replace would decode the pair, then re-read its own output."""
    assert po_unescape_raw(RAW_N) == "\\n"
    assert "\n" not in po_unescape_raw(RAW_N)


def test_escape_is_the_exact_inverse():
    for raw in (RAW_N, RAW_CRLF, RAW_BS, r"plain", r"quote \" here",
                r"tab \t here", r"a\\nb\r\nc"):
        assert po_escape_raw(po_unescape_raw(raw)) == raw


def test_round_trip_is_lossless_so_untouched_entries_stay_identical():
    raw = r"Mixed \\n and \r\n and \\\\ tokens"
    assert po_escape_raw(po_unescape_raw(raw)) == raw


# ---------------------------------------------------- convention detection

def test_longest_token_wins_over_its_substring():
    """``\\\\`` must be consumed whole; reading the second backslash as the
    start of another token would split one separator into two."""
    assert separators(po_unescape_raw(RAW_BS)) == ["\\\\"]


def test_a_damaged_break_counts_as_one_separator_not_three():
    """``\\\\\\r\\n`` decodes to an adjacent run of three tokens. It is ONE
    corrupted break; counting the tokens would triple it on repair."""
    # decodes to the escape TEXT '\\' + '\r' + '\n' — three tokens, but
    # one damaged break, so it must come back as a single run
    assert separators(po_unescape_raw(r"a\\\\\\r\\nb")) == [r"\\\r\n"]


def test_a_msgid_whose_own_separator_is_damaged_donates_nothing():
    """No clean token to inherit ⇒ leave the entry to a human."""
    assert separator_convention(po_unescape_raw(r"a\\\\\\r\\nb")) is None


def test_each_convention_is_detected():
    assert separator_convention(po_unescape_raw(RAW_N)) == "\\n"
    assert separator_convention(po_unescape_raw(RAW_CRLF)) == "\r\n"
    assert separator_convention(po_unescape_raw(RAW_BS)) == "\\\\"


def test_single_line_string_has_no_convention():
    assert separator_convention("just text") is None


# --------------------------------------------------------------- the rule

def test_double_escaped_target_is_brought_back_to_the_source_token():
    """The drop's dominant defect: 71 entries arrived like this."""
    src = po_unescape_raw(r"包括变身后的冥界使徒\\n若发现")
    tgt = po_unescape_raw(r"Underworld Apostles.\\\\\\r\\nRelentlessly")
    out = match_source_format(src, tgt)
    assert separators(out) == ["\\n"]
    assert out == "Underworld Apostles.\\nRelentlessly"


def test_repair_is_byte_exact_at_the_file_level():
    assert repair_separator_raw(r"a\\nb",
                                r"x\\\\\\r\\ny") == r"x\\ny"


def test_crlf_entry_keeps_crlf_not_the_file_majority():
    """Convention is per-ENTRY: a CRLF source stays CRLF even though
    ``\\n`` dominates the file."""
    assert repair_separator_raw(r"a\r\nb", r"x\\ny") == r"x\r\ny"


def test_double_backslash_entry_keeps_double_backslash():
    assert repair_separator_raw(r"a\\\\b", r"x\\ny") == r"x\\\\y"


def test_already_correct_target_is_untouched():
    assert repair_separator_raw(r"a\\nb", r"x\\ny") == r"x\\ny"


def test_text_content_is_never_altered():
    out = po_unescape_raw(repair_separator_raw(
        r"a\\nb", r"Hello, world!\r\nSecond line"))
    assert "Hello, world!" in out and "Second line" in out


# ------------------------------------------- what the rule refuses to do

def test_lost_break_is_never_invented():
    """Source has a separator, target has none. Three such entries in the
    drop also dropped clauses — no format pass can restore that."""
    assert repair_separator_raw(r"需要5个草药\\n以及合成费用200",
                                r"Requires 5 herbs") == r"Requires 5 herbs"


def test_single_line_source_leaves_the_target_alone():
    assert repair_separator_raw(r"plain source", r"target\\nwith break") \
        == r"target\\nwith break"


def test_break_count_is_reported_not_reflowed():
    """A translator merging 11 lines into 4 is a content decision."""
    _fixes, audit = audit_separators(
        [("k", r"a\r\nb\r\nc", r"x\r\ny")])
    assert audit.count_drift and audit.count_drift[0]["source_breaks"] == 2
    assert audit.count_drift[0]["target_breaks"] == 1


def test_empty_and_untranslated_entries_are_skipped():
    fixes, audit = audit_separators([("k", r"a\\nb", ""),
                                     ("k2", r"a\\nb", "   ")])
    assert fixes == {} and audit.counts() == {
        "repaired": 0, "lost_break": 0, "count_drift": 0}


# --------------------------------------------------------------- the sweep

def test_audit_buckets_every_entry_exactly_once():
    fixes, audit = audit_separators([
        ("good", r"a\\nb", r"x\\ny"),               # already correct
        ("bad", r"a\\nb", r"x\\\\\\r\\ny"),         # repairable
        ("lost", r"a\\nb", r"x"),                   # needs a human
    ])
    assert list(fixes) == ["bad"]
    assert [r["key"] for r in audit.lost_break] == ["lost"]
    assert audit.counts()["repaired"] == 1


PO = (                       # the BOM comes from utf-8-sig on write, not here
    '# header\n'
    'msgctxt ",AAA"\n'
    'msgid "包括\\\\n若发现"\n'
    'msgstr "Apostles.\\\\\\\\\\\\r\\\\nRelentlessly"\n'
    '\n'
    'msgctxt ",BBB"\n'
    'msgid "plain"\n'
    'msgstr "untouched"\n'
)


def test_file_sweep_repairs_only_the_defective_entry(tmp_path):
    src = tmp_path / "Game.po"
    src.write_text(PO, encoding="utf-8-sig", newline="")
    dst = tmp_path / "Game.fixed.po"
    applied, audit = repair_po_separators(src, dst)

    assert applied == [",AAA"]
    out = dst.read_text(encoding="utf-8-sig")
    assert 'msgstr "Apostles.\\\\nRelentlessly"' in out
    # the untouched entry and the header survive byte-for-byte
    assert 'msgstr "untouched"' in out
    assert out.startswith("# header")
    assert audit.counts()["repaired"] == 1


def test_sweep_preserves_bom_and_leaves_other_bytes_alone(tmp_path):
    src = tmp_path / "Game.po"
    src.write_text(PO, encoding="utf-8-sig", newline="")
    dst = tmp_path / "out.po"
    repair_po_separators(src, dst)
    assert dst.read_bytes().startswith(b"\xef\xbb\xbf")
    # only the one msgstr line differs
    before = src.read_text(encoding="utf-8-sig").splitlines()
    after = dst.read_text(encoding="utf-8-sig").splitlines()
    assert len(before) == len(after)
    assert sum(1 for a, b in zip(before, after) if a != b) == 1


def test_repairing_an_already_repaired_file_is_a_no_op(tmp_path):
    """Idempotence: the sweep must converge, so it can be re-run safely."""
    src = tmp_path / "Game.po"
    src.write_text(PO, encoding="utf-8-sig", newline="")
    once, twice = tmp_path / "1.po", tmp_path / "2.po"
    repair_po_separators(src, once)
    applied, _audit = repair_po_separators(once, twice)
    assert applied == []
    assert twice.read_bytes() == once.read_bytes()


def test_read_raw_entries_keeps_the_escaping_intact(tmp_path):
    src = tmp_path / "Game.po"
    src.write_text(PO, encoding="utf-8-sig", newline="")
    entries = dict((k, (s, t)) for k, s, t in read_raw_entries(src))
    # still ESCAPED — decoding here would flatten the defect being audited
    assert entries[",AAA"][1] == r"Apostles.\\\\\\r\\nRelentlessly"
