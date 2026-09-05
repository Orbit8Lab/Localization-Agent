"""Conversation grouping and the two batching axes.

Batching is not only about cost. WHICH strings share a call decides what
the model can see, and both failures here are silent: a conversation torn
across three calls still produces fluent output, and two inconsistent
renderings in different batches are each individually plausible. Neither
shows up as an error — only as a worse deliverable.
"""
from __future__ import annotations

from orbit8.grouping import (derive_groups, group_stats, is_opaque_key,
                             parse_key, plan_similarity_batches,
                             plan_story_batches)


# ------------------------------------------------------- key derivation

def test_parses_scene_and_position():
    assert parse_key("dlg.ch01.scene03.007") == ("dlg.ch01.scene03", 7)


def test_separator_style_does_not_change_the_group():
    """UE, Unity and gettext exports punctuate keys differently; the same
    conversation must group the same way in all of them."""
    for key in ("dlg.ch01.scene03.007", "dlg/ch01/scene03:007",
                "dlg-ch01-scene03-007", "dlg\\ch01\\scene03\\007"):
        assert parse_key(key) == ("dlg.ch01.scene03", 7)


def test_named_counter_keeps_its_name_in_the_group():
    assert parse_key("Dialogue_Line_12") == ("Dialogue.Line", 12)


def test_guids_are_refused_not_guessed_at():
    """The batch-split doc calls out GUID-keyed UE exports. Splitting one
    on its dashes would read `446655440000` as a line number and invent a
    scene that does not exist."""
    for key in ("550e8400-e29b-41d4-a716-446655440000",
                "550e8400e29b41d4a716446655440000",
                "7F3A9B2C1D4E5F6A"):
        assert is_opaque_key(key), key
        assert parse_key(key) == (None, None)


def test_opaque_keys_fall_back_to_arrival_order():
    groups = derive_groups(["550e8400-e29b-41d4-a716-446655440000",
                            "7f3a9b2c1d4e5f6a7b8c9d0e1f2a3b4c"])
    assert [g for g, _ in groups.values()] == ["_ungrouped", "_ungrouped"]
    assert [s for _, s in groups.values()] == [0, 1]


def test_group_stats_makes_a_useless_derivation_visible():
    """400 conversations of one is a failure that looks like success."""
    stats = group_stats(["dlg.s1.001", "dlg.s1.002", "dlg.s1.003",
                         "ui.ok", "550e8400-e29b-41d4-a716-446655440000"])
    assert stats["grouped_keys"] == 3
    assert stats["ungrouped"] == 1
    assert stats["singletons"] == 1
    assert stats["largest"] == 3


# ------------------------------------------------- story batch planning

def _convo(gid, n, start=1):
    return [{"uid": f"{gid}-{i}", "group_id": gid, "seq": i,
             "text": f"{gid} line {i}"} for i in range(start, start + n)]


def test_a_conversation_is_never_split_when_it_fits():
    """The regression this exists for: `range(0, n, size)` tore a 4-line
    exchange across two calls whenever it straddled a boundary."""
    segs = _convo("s1", 3) + _convo("s2", 2)
    batches = plan_story_batches(segs, max_size=5)
    for batch in batches:
        assert len({s["group_id"] for s in batch}) <= 2
    # s1's three lines must all be in ONE batch
    home = [b for b in batches if any(s["group_id"] == "s1" for s in b)]
    assert len(home) == 1
    assert sum(1 for s in home[0] if s["group_id"] == "s1") == 3


def test_lines_arrive_in_sequence_order():
    """The prompt tells the model these are consecutive lines. If they are
    shuffled, that instruction actively misleads it."""
    segs = list(reversed(_convo("s1", 5)))
    batch = plan_story_batches(segs, max_size=5)[0]
    assert [s["seq"] for s in batch] == [1, 2, 3, 4, 5]


def test_an_oversized_conversation_splits_into_contiguous_stretches():
    """A 12-line scene cannot fit a 5-line window, but each part must
    still be consecutive dialogue rather than an arbitrary sample."""
    batches = plan_story_batches(_convo("s1", 12), max_size=5)
    seqs = [[s["seq"] for s in b] for b in batches]
    assert seqs == [[1, 2, 3, 4, 5], [6, 7, 8, 9, 10], [11, 12]]


def test_short_conversations_share_a_call():
    """One call per two-line scene would cost more than the blind slice
    it replaced."""
    segs = _convo("a", 2) + _convo("b", 2) + _convo("c", 1)
    assert len(plan_story_batches(segs, max_size=5)) == 1


def test_no_batch_exceeds_the_window():
    segs = sum((_convo(f"g{i}", 3) for i in range(7)), [])
    for batch in plan_story_batches(segs, max_size=5):
        assert len(batch) <= 5


def test_ungrouped_segments_keep_the_old_slice():
    """A job seeded before grouping existed must keep working, batching
    exactly as it did before."""
    segs = [{"uid": f"u{i}", "group_id": None, "seq": None, "text": "x"}
            for i in range(7)]
    batches = plan_story_batches(segs, max_size=5)
    assert [len(b) for b in batches] == [5, 2]


def test_planning_is_deterministic():
    """Batch layout rides on every model fingerprint; two runs of one job
    must lay out identically or the fingerprints are unreproducible."""
    segs = _convo("b", 3) + _convo("a", 4) + _convo("c", 2)
    first = plan_story_batches(segs, max_size=5)
    second = plan_story_batches(list(reversed(segs)), max_size=5)
    assert ([[s["uid"] for s in b] for b in first]
            == [[s["uid"] for s in b] for b in second])


# ------------------------------------------- template-family batching

def test_number_variants_share_a_reviewer_call():
    """The LQA failure this fixes: two near-identical sources rendered
    differently are each plausible alone. The conflict is only visible
    when both are in the same call.

    No threshold decides this any more — `+10 HP` and `+20 HP` share the
    template `<NUM> HP` (templates.py), so they group structurally.
    """
    segs = [{"uid": "a1", "text": "恢复5点生命"},
            {"uid": "a2", "text": "恢复10点生命"},
            {"uid": "z", "text": "退出游戏"}]
    batches = plan_similarity_batches(segs, max_size=20)
    home = [b for b in batches if any(s["uid"] == "a1" for s in b)][0]
    assert {"a1", "a2"} <= {s["uid"] for s in home}


def test_similarity_batches_are_packed_not_exploded():
    """Most strings belong to a family of one. A call per singleton would
    cost far more than the blind slice it replaced."""
    segs = [{"uid": f"u{i}", "text": f"unrelated string number {i} " + "x" * i}
            for i in range(20)]
    assert len(plan_similarity_batches(segs, max_size=20)) <= 2


def test_similarity_respects_the_window():
    segs = [{"uid": f"u{i}", "text": "恢复%d点生命" % i} for i in range(50)]
    for batch in plan_similarity_batches(segs, max_size=20):
        assert len(batch) <= 20


def test_every_segment_survives_batching():
    """Whatever the axis, nothing may be silently dropped — an unbatched
    string is an untranslated or unreviewed one."""
    segs = (_convo("s1", 7) + _convo("s2", 2)
            + [{"uid": "x", "group_id": None, "seq": None, "text": "solo"}])
    planned = {s["uid"] for b in plan_story_batches(segs, max_size=5)
               for s in b}
    assert planned == {s["uid"] for s in segs}

    flat = [{"uid": s["uid"], "text": s["text"]} for s in segs]
    planned = {s["uid"] for b in plan_similarity_batches(flat, max_size=5)
               for s in b}
    assert planned == {s["uid"] for s in flat}
