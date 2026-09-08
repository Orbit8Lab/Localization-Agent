"""T3 batches must be coherent and non-redundant.

Measured on project002 before this fix, over 174 strings in 9 batches:

- **9 of 9 batches mixed domains** (ui + system + item_desc), so
  `tier3` passed `domain=None` and the domain-specific rubric
  (`DOMAIN_EMPHASIS`) was skipped for EVERY batch. The batch-split spec
  promises a domain-aware prompt; nothing delivered it.
- **9% of batch slots were redundant** — batch 0 spent 7 of 19 slots on
  the identical source `任务八：护送马车`. Upstream dedup is by
  (source, target), but distinct game keys sharing one source each still
  occupied a slot, so the Critic paid for the same judgment repeatedly
  and saw less of the corpus per call.

Both are pure waste: the fix costs nothing and buys back rubric
coverage and context.
"""
from __future__ import annotations

from orbit8.grouping import plan_similarity_batches


def _seg(uid, text, domain, target=""):
    return {"uid": uid, "text": text, "domain": domain, "target": target}


# ----------------------------------------------------- C: no redundancy

def test_identical_source_target_pairs_collapse_within_a_batch():
    """Seven keys sharing one (source, target) must occupy ONE slot."""
    segs = [_seg(f"k{i}", "任务八：护送马车", "system", "Mission 8: Escort")
            for i in range(7)]
    segs.append(_seg("kx", "完全不同的字符串内容", "system", "Totally other"))
    batches = plan_similarity_batches(segs, max_size=20, collapse=True)
    slots = [s for b in batches for s in b]
    assert len(slots) == 2, f"expected 2 distinct slots, got {len(slots)}"


def test_collapsed_rows_keep_every_game_key():
    """Collapsing must not lose keys — the report has to attribute the
    finding back to all of them."""
    segs = [_seg("k1", "确定", "ui", "OK"), _seg("k2", "确定", "ui", "OK")]
    batches = plan_similarity_batches(segs, max_size=20, collapse=True)
    row = batches[0][0]
    assert sorted(row.get("collapsed_uids", [row["uid"]])) == ["k1", "k2"]


def test_different_targets_for_one_source_do_NOT_collapse():
    """The inconsistency case: one source, two renderings. Collapsing
    these would destroy the very defect T3 is meant to see."""
    segs = [_seg("k1", "确定", "ui", "OK"), _seg("k2", "确定", "ui", "Confirm")]
    batches = plan_similarity_batches(segs, max_size=20, collapse=True)
    slots = [s for b in batches for s in b]
    assert len(slots) == 2, "differing targets must both be shown"


def test_collapse_is_opt_in():
    """Default behaviour is unchanged, so existing callers and the
    translate path are unaffected."""
    segs = [_seg("k1", "确定", "ui", "OK"), _seg("k2", "确定", "ui", "OK")]
    slots = [s for b in plan_similarity_batches(segs, max_size=20) for s in b]
    assert len(slots) == 2


# ------------------------------------------------ B: domain coherence

def test_batches_are_homogeneous_by_domain():
    """A batch must carry ONE domain, so tier3 can pass the rubric."""
    segs = ([_seg(f"u{i}", f"界面文本{i}", "ui") for i in range(25)]
            + [_seg(f"s{i}", f"系统消息{i}", "system") for i in range(25)]
            + [_seg(f"i{i}", f"道具说明{i}", "item_desc") for i in range(8)])
    batches = plan_similarity_batches(segs, max_size=20, by_domain=True)
    for b in batches:
        assert len({s["domain"] for s in b}) == 1, \
            f"mixed domains in one batch: {[s['domain'] for s in b]}"


def test_domain_split_still_packs_to_the_window():
    """Homogeneity must not degenerate into one call per string: 25 ui
    rows at max 20 is 2 batches, not 25."""
    segs = [_seg(f"u{i}", f"界面文本{i}", "ui") for i in range(25)]
    batches = plan_similarity_batches(segs, max_size=20, by_domain=True)
    assert len(batches) == 2
    assert sum(len(b) for b in batches) == 25


def test_every_row_survives_both_transformations():
    """Nothing may be dropped when collapse and by_domain combine."""
    segs = ([_seg(f"u{i}", f"界面文本{i}", "ui") for i in range(10)]
            + [_seg(f"d{i}", "重复的字符串", "system", "Dup") for i in range(5)])
    batches = plan_similarity_batches(segs, max_size=20,
                                      by_domain=True, collapse=True)
    covered = {u for b in batches for s in b
               for u in s.get("collapsed_uids", [s["uid"]])}
    assert covered == {s["uid"] for s in segs}
