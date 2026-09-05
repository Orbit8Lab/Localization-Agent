"""Conversation grouping — deriving WHICH strings belong together.

Batching decides how many strings share one API call. Grouping decides
*which* ones, and for story text that is the difference between a coherent
scene and five lines torn out of three conversations.

Two axes, because the two consumers need different things:

- **Conversation** (`group_id` + `seq`) — derived from the game key's own
  structure, because a key like ``dlg.ch01.scene03.007`` already encodes
  the scene and the position within it. A translator holding a whole
  exchange in one call keeps register, pronouns and callbacks consistent;
  the same lines split across three batches cannot.
- **Template family** (templates.py) — strings sharing a structure, so
  ONE reviewer call sees them side by side. Inconsistency is invisible
  unless the conflicting renderings are in the same context window, and a
  blind slice almost guarantees they are not. Grouping is structural
  (`+10 HP` and `+20 HP` share `<NUM> HP`), never a similarity threshold:
  a threshold can only trade "group them" against "keep them apart",
  while a template gives both.

Both are deterministic. Grouping steers cost and attention, so an LLM
deciding it would put a spend decision inside a prompt (design §7) — and
a scene boundary that moves between runs would make batches
irreproducible, which the model fingerprints are supposed to prevent.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

# Key separators seen across UE/Unity/gettext exports. A key is split on
# any of them, so `dlg/ch01/scene03:007` and `dlg.ch01.scene03.007` group
# identically.
_KEY_SEP = re.compile(r"[./:|\\_\-#]+")

# A trailing counter is what makes the LAST segment a position rather than
# part of the scene name: `007`, `line_12`, `L003`, `(4)`.
_TRAILING_NUM = re.compile(r"^(?P<stem>.*?)(?P<num>\d+)\s*$")

# Opaque ids — UE exports routinely key by GUID, and the batch-split doc
# calls this out. A GUID carries NO grouping information, so pretending it
# does would silently produce one-string "conversations".
_OPAQUE = re.compile(
    r"^(?:[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}"
    r"|[0-9A-F]{16,}|[0-9a-f]{32,})$", re.I)


def _segments(key: str) -> List[str]:
    return [s for s in _KEY_SEP.split(key.strip()) if s]


def is_opaque_key(key: str) -> bool:
    """True when a key carries no derivable structure (GUID exports).

    Reported rather than guessed at: `derive_groups` falls back to file
    order for these, and the caller deserves to know that happened.
    """
    # Test the WHOLE key before splitting: a dashed GUID would otherwise
    # be torn into segments that each look innocuous, and the last one
    # ("446655440000") would be read as a sequence number — inventing a
    # scene out of an id that has none.
    stripped = key.strip()
    if _OPAQUE.match(stripped) or _OPAQUE.match(stripped.replace("-", "")):
        return True
    segs = _segments(key)
    if not segs:
        return True
    # A single segment with no trailing counter tells us nothing either.
    if len(segs) == 1 and not _TRAILING_NUM.match(segs[0]):
        return True
    return any(_OPAQUE.match(s) for s in segs)


def parse_key(key: str) -> Tuple[Optional[str], Optional[int]]:
    """``dlg.ch01.scene03.007`` → ``("dlg.ch01.scene03", 7)``.

    Returns ``(None, None)`` when the key has no derivable structure, so
    the caller can fall back rather than invent a group.
    """
    if is_opaque_key(key):
        return None, None
    segs = _segments(key)
    match = _TRAILING_NUM.match(segs[-1])
    if match is None:
        # No counter: the whole key names a group of one. Useful for UI
        # keys, useless for dialogue — the caller decides which it wants.
        return ".".join(segs), None
    stem, num = match.group("stem"), int(match.group("num"))
    # `line_12` keeps `line` inside the group name; a bare `007` does not
    # contribute a name at all.
    parts = segs[:-1] + ([stem] if stem else [])
    if not parts:
        return None, num
    return ".".join(parts), num


def derive_groups(keys: Sequence[str]) -> Dict[str, Tuple[str, int]]:
    """Map each key to ``(group_id, seq)``.

    Keys that parse contribute their own group and position. Keys that do
    not — GUID exports — fall back to ``file`` order under a single
    ``_ungrouped`` bucket, preserving the order they arrived in, which is
    the only signal an opaque export offers.
    """
    out: Dict[str, Tuple[str, int]] = {}
    fallback_seq = 0
    for key in keys:
        group, seq = parse_key(key)
        if group is None:
            out[key] = ("_ungrouped", fallback_seq)
            fallback_seq += 1
            continue
        if seq is None:
            # Structured but unpositioned: keep the key's own order within
            # its group so a later sort is still stable.
            seq = fallback_seq
            fallback_seq += 1
        out[key] = (group, seq)
    return out


def group_stats(keys: Sequence[str]) -> dict:
    """What grouping actually achieved — for the operator, not the model.

    A derivation that silently produced 400 groups of one is a failure
    that looks like success; this is what makes it visible.
    """
    groups = derive_groups(keys)
    sizes: Dict[str, int] = {}
    for group, _seq in groups.values():
        sizes[group] = sizes.get(group, 0) + 1
    grouped = sum(n for g, n in sizes.items() if g != "_ungrouped" and n > 1)
    return {
        "keys": len(keys),
        "groups": len([g for g in sizes if g != "_ungrouped"]),
        "ungrouped": sizes.get("_ungrouped", 0),
        "singletons": len([g for g, n in sizes.items()
                           if g != "_ungrouped" and n == 1]),
        "grouped_keys": grouped,
        "largest": max(sizes.values(), default=0),
    }


# --------------------------------------------------------------- batching

def plan_story_batches(segments: Sequence[dict], *, max_size: int,
                       key: str = "uid") -> List[List[dict]]:
    """Batch story text so a CONVERSATION stays in one call.

    A blind ``range(0, n, size)`` slice tears a 12-line exchange across
    three calls, and no amount of prompt wording recovers what the model
    was never shown: the line it is translating had a question before it
    and an answer after it, in another batch.

    Conversations are emitted whole, in sequence order, smallest-first
    packing so short scenes share a call rather than each burning one.
    A scene longer than ``max_size`` splits at ``max_size`` — but along
    its own sequence, so each part is still a contiguous stretch of
    dialogue rather than an arbitrary sample of it.

    Segments with no ``group_id`` (opaque keys, or a job seeded before
    grouping) fall back to the old contiguous slice, which is exactly the
    previous behaviour.
    """
    grouped: Dict[str, List[dict]] = {}
    ungrouped: List[dict] = []
    for seg in segments:
        gid = seg.get("group_id")
        if gid:
            grouped.setdefault(gid, []).append(seg)
        else:
            ungrouped.append(seg)

    batches: List[List[dict]] = []
    # Deterministic order: by group id, and by seq within a group. Batch
    # layout has to be reproducible or two runs of the same job produce
    # different fingerprints for reasons nobody can reconstruct.
    oversized: List[List[dict]] = []
    intact: List[List[dict]] = []
    for gid in sorted(grouped):
        convo = sorted(grouped[gid],
                       key=lambda s: (s.get("seq") if s.get("seq") is not None
                                      else 0, str(s.get(key, ""))))
        if len(convo) > max_size:
            for i in range(0, len(convo), max_size):
                oversized.append(convo[i:i + max_size])
        else:
            intact.append(convo)

    batches.extend(oversized)          # already exactly max_size-shaped
    # Pack whole conversations together without ever splitting one.
    current: List[dict] = []
    for convo in sorted(intact, key=len, reverse=True):
        if current and len(current) + len(convo) > max_size:
            batches.append(current)
            current = []
        if len(convo) > max_size:      # unreachable: oversized handled above
            batches.append(convo)
            continue
        current.extend(convo)
    if current:
        batches.append(current)

    for i in range(0, len(ungrouped), max_size):
        batches.append(ungrouped[i:i + max_size])
    return [b for b in batches if b]


def plan_similarity_batches(segments: Sequence[dict], *, max_size: int,
                            threshold: float = 0.8,
                            key: str = "uid") -> List[List[dict]]:
    """Batch pure strings so members of a TEMPLATE FAMILY share a call.

    Inconsistency is only visible when the conflicting renderings sit in
    the same context window. Under a blind slice two near-duplicates land
    in different batches almost every time, so no single Tier-3 call can
    see the conflict — the audit reports two separately-plausible
    translations and never notices they disagree.

    Grouping is by template (templates.build_families), NOT by a
    similarity threshold over raw text. `+10 HP` and `+20 HP` share the
    template `<NUM> HP`, so they batch together with no threshold
    involved — and they stay distinct instances, which a threshold could
    never give at the same time. `threshold` now applies only to the
    second pass that merges near-identical TEMPLATES.
    """
    from .templates import build_families

    by_id = {str(s.get(key)): s for s in segments}
    clusters = [[m.id for m in family.members]
                for family in build_families(
                    [(str(s.get(key)), s.get("text") or "")
                     for s in segments], threshold=threshold)]
    # A family larger than the window still has to fit one.
    clusters = [c[i:i + max_size]
                for c in clusters for i in range(0, len(c), max_size)]

    # Clustering alone yields many singletons — most strings resemble
    # nothing else — and one API call per singleton would cost far more
    # than the blind slice it replaced. So clusters are PACKED back up to
    # max_size. What matters is that members of a cluster stay adjacent
    # inside one call, not that a call holds only one cluster.
    batches: List[List[dict]] = []
    current: List[dict] = []
    for cluster in sorted(clusters, key=len, reverse=True):
        rows = [by_id[i] for i in cluster if i in by_id]
        if not rows:
            continue
        if current and len(current) + len(rows) > max_size:
            batches.append(current)
            current = []
        current.extend(rows)
        if len(current) >= max_size:
            batches.append(current)
            current = []
    if current:
        batches.append(current)
    return batches
