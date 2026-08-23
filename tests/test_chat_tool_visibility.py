"""What the chat tools actually SHOW the model.

Both regressions here were found by reading a real session trace where the
operator asked "which entry changed?" and the agent went hunting through
report files on disk, then made a claim about a JSON it had only read the
first 800 bytes of. The answer it landed on was correct; nothing in the
tool output could support it.

A tool that reports a COUNT without the corresponding IDENTIFIERS forces
exactly that guess, and a read that truncates SILENTLY makes the guess
look sourced. These tests pin both fixes.
"""
from __future__ import annotations

import json
from pathlib import Path

from orbit8.controller import Job
from orbit8.orchestrator import (INSPECT_TEXT_LIMIT, INSPECT_TEXT_MAX,
                                 ChatOrchestrator)
from orbit8.schemas import IntakeBrief


class _Provider:
    """Never consulted — these tests call the tool handlers directly."""
    name, model, tokens_spent = "stub", "test", 0.0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        raise AssertionError("no model call expected")


def _chat(tmp_path: Path) -> ChatOrchestrator:
    project = tmp_path / "projectX"
    (project / "10-received").mkdir(parents=True)
    src = project / "s.json"
    src.write_text(json.dumps({"K": "x"}), encoding="utf-8")
    job = Job.init(project / "20-work", "j1",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["en"]),
                   source_files=[str(src)])
    return ChatOrchestrator(job, _Provider(), operator="tian")


def _po(entries) -> str:
    out = ['msgid ""', 'msgstr "Content-Type: text/plain; charset=UTF-8\\n"',
           ""]
    for key, source, target in entries:
        out += [f'msgctxt ",{key}"', f'msgid "{source}"',
                f'msgstr "{target}"', ""]
    return "\n".join(out)


# ------------------------------------------- compare_po names the entries

def test_a_non_stale_source_edit_is_still_named(tmp_path: Path):
    """The regression: source_changed was surfaced ONLY when stale=True.
    An entry whose translation was updated in the same drop vanished from
    the payload, leaving a count the model could not resolve to a key."""
    chat = _chat(tmp_path)
    received = tmp_path / "projectX" / "10-received"
    old = received / "old.po"
    new = received / "new.po"
    old.write_text(_po([("AAA", "old source", "translated")]),
                   encoding="utf-8")
    new.write_text(_po([("AAA", "new source", "retranslated")]),
                   encoding="utf-8")

    payload = json.loads(chat._t_compare_po({"old": str(old),
                                             "new": str(new)}))

    assert payload["counts"]["source_changed"] == 1
    assert payload["stale_translations"] == []      # translation kept pace
    keys = [e["key"] for e in payload["source_changed"]]
    assert keys == [",AAA"]                        # ...and is STILL named


def test_the_named_entry_carries_both_source_versions(tmp_path: Path):
    """Naming the key is not enough to answer "what changed" — the
    operator needs the before/after text in the same payload."""
    chat = _chat(tmp_path)
    received = tmp_path / "projectX" / "10-received"
    old, new = received / "o.po", received / "n.po"
    old.write_text(_po([("BBB", "before text", "t")]), encoding="utf-8")
    new.write_text(_po([("BBB", "after text", "t2")]), encoding="utf-8")

    entry = json.loads(chat._t_compare_po(
        {"old": str(old), "new": str(new)}))["source_changed"][0]
    assert "before text" in entry["old_source"]
    assert "after text" in entry["new_source"]


def test_stale_entries_appear_in_both_lists(tmp_path: Path):
    """A stale edit is a source_changed entry too; the red-flag list is a
    filtered VIEW, not a different bucket."""
    chat = _chat(tmp_path)
    received = tmp_path / "projectX" / "10-received"
    old, new = received / "o.po", received / "n.po"
    old.write_text(_po([("CCC", "old", "shipped")]), encoding="utf-8")
    new.write_text(_po([("CCC", "changed", "shipped")]), encoding="utf-8")

    payload = json.loads(chat._t_compare_po({"old": str(old),
                                             "new": str(new)}))
    assert [e["key"] for e in payload["stale_translations"]] == [",CCC"]
    assert [e["key"] for e in payload["source_changed"]] == [",CCC"]


def test_an_unchanged_pair_names_nothing(tmp_path: Path):
    chat = _chat(tmp_path)
    received = tmp_path / "projectX" / "10-received"
    old, new = received / "o.po", received / "n.po"
    for path in (old, new):
        path.write_text(_po([("DDD", "same", "same-t")]), encoding="utf-8")
    payload = json.loads(chat._t_compare_po({"old": str(old),
                                             "new": str(new)}))
    assert payload["source_changed"] == []


# --------------------------------------- inspect_file pages, and says so

def test_a_long_report_is_no_longer_capped_at_800_bytes(tmp_path: Path):
    """A compare .md is ~5KB and its .json ~30KB; the old flat 800-byte
    head hid the section that answers the operator's question."""
    chat = _chat(tmp_path)
    report = tmp_path / "projectX" / "report.md"
    report.write_text("H" * 3000 + "THE-ANSWER", encoding="utf-8")

    info = json.loads(chat._t_inspect_file({"path": str(report)}))
    assert "THE-ANSWER" in info["head"]
    assert len(info["head"]) > 800


def test_truncation_is_announced_with_a_way_to_continue(tmp_path: Path):
    """Silent truncation is what let a partial read become a confident
    claim about content never seen."""
    chat = _chat(tmp_path)
    big = tmp_path / "projectX" / "big.json"
    big.write_text("x" * (INSPECT_TEXT_LIMIT + 500), encoding="utf-8")

    info = json.loads(chat._t_inspect_file({"path": str(big)}))
    assert info["truncated"] is True
    assert info["next_offset"] == INSPECT_TEXT_LIMIT
    assert info["remaining_chars"] == 500


def test_paging_with_offset_reaches_the_tail(tmp_path: Path):
    chat = _chat(tmp_path)
    big = tmp_path / "projectX" / "big.md"
    big.write_text("A" * INSPECT_TEXT_LIMIT + "TAIL-MARKER",
                   encoding="utf-8")

    first = json.loads(chat._t_inspect_file({"path": str(big)}))
    second = json.loads(chat._t_inspect_file(
        {"path": str(big), "offset": first["next_offset"]}))
    assert "TAIL-MARKER" in second["head"]
    assert "truncated" not in second


def test_a_short_file_is_not_marked_truncated(tmp_path: Path):
    chat = _chat(tmp_path)
    small = tmp_path / "projectX" / "small.md"
    small.write_text("all of it", encoding="utf-8")
    info = json.loads(chat._t_inspect_file({"path": str(small)}))
    assert info["head"] == "all of it"
    assert "truncated" not in info


def test_a_caller_cannot_request_an_unbounded_window(tmp_path: Path):
    """The window still has to fit the model's context."""
    chat = _chat(tmp_path)
    big = tmp_path / "projectX" / "big.md"
    big.write_text("y" * (INSPECT_TEXT_MAX + 5000), encoding="utf-8")
    info = json.loads(chat._t_inspect_file({"path": str(big),
                                            "limit": 10 ** 9}))
    assert len(info["head"]) == INSPECT_TEXT_MAX
    assert info["truncated"] is True


def test_a_bom_is_not_read_as_content(tmp_path: Path):
    chat = _chat(tmp_path)
    path = tmp_path / "projectX" / "bom.md"
    path.write_text("# Heading", encoding="utf-8-sig")
    info = json.loads(chat._t_inspect_file({"path": str(path)}))
    assert info["head"].startswith("# Heading")


def test_the_inner_window_fits_inside_the_outer_observation_cap():
    """Layering invariant. OBSERVATION_LIMIT clips EVERY tool result before
    the model sees it, so a window sized at or above that cap would be cut
    again one layer later — and the next_offset it reported would point
    past data the model never actually received."""
    from orbit8.orchestrator import OBSERVATION_LIMIT
    assert INSPECT_TEXT_MAX < OBSERVATION_LIMIT
    assert INSPECT_TEXT_LIMIT <= INSPECT_TEXT_MAX


def test_po_files_keep_their_structured_summary(tmp_path: Path):
    """The .po branch is unchanged — it reports entry counts, not a text
    window."""
    chat = _chat(tmp_path)
    path = tmp_path / "projectX" / "x.po"
    path.write_text(_po([("EEE", "src", "tgt")]), encoding="utf-8")
    info = json.loads(chat._t_inspect_file({"path": str(path)}))
    assert info["entries"] == 1 and info["msgstr_filled"] == 1
    assert "head" not in info
