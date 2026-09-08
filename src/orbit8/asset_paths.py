"""Grouping derived from the UE asset path, not the game key.

`grouping.derive_groups` reads the game KEY, and for this client that
key is a bare GUID (`,FDBF56024F483C8AAAEED6A95744DFFF`) — correctly
reported opaque, and carrying nothing to group on. But every PO entry
also has a `#:` reference comment holding the full UE asset path, and
that path is richly structured:

    /Game/.../DT_CraftFormula_A.DT_CraftFormula_A.B_072.ItemName
    /Game/.../DT_CraftFormula_A.DT_CraftFormula_A.B_072.ItemCommit

Three axes fall out, in decreasing precision and increasing coverage.
Measured on the real 1,349-string project002 PO:

  entity   `(asset, row)` — 1,033 entities; 92 with 2+ strings (191
           rows); 57 carrying BOTH a name and a description (114 rows).
           An item's name and its blurb must agree, and template
           clustering can never pair them because a name shares no
           surface form with its own description.
  role     the field name with UE's column GUID stripped — `ItemName`,
           `ItemCommit`, `Conent`. All `.ItemName` rows share a register
           (short noun phrase, no terminal punctuation); all
           `.ItemCommit` share another (one-sentence flavour text).
  subsystem the asset directory — the only axis with 100% coverage, and
           the sole handle on rows whose field is just a column GUID.

Subtitles additionally carry real sequence numbers
(`DT_Subtitles.0.Character`, `.1.Character`), so a conversation is
reconstructible for those rows.

A caution worth keeping: an earlier analysis was run against the xlsx
test-kit, whose StringLocation column is truncated to 34 chars + "…" +
34 chars. It concluded paths were unusable for 84.7% of rows and that
conversations could not be reconstructed. Both were artifacts of
reading the wrong file. The PO is the source of truth, it carries full
paths, and `exports.read_po_entries` already captures them.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

# UE appends a column GUID to a struct field: `Conent_5_C6AE360C…`.
# Only 26 distinct GUIDs exist across 1,349 rows, because the GUID
# identifies a COLUMN, not a string — leaving it in shatters one role
# into hundreds of singletons. An optional numeric index (`Text_1_5_…`)
# is part of the same suffix, not part of the role.
_COLUMN_GUID = re.compile(r"_(?:\d+_)*\d+_[0-9A-F]{32}$", re.I)



def _split(location: str) -> Optional[Tuple[str, str, str]]:
    """``(asset, row, field)`` for a data-table reference, else None.

    Parsed from the right so a path containing dots in a directory name
    cannot confuse the row/field split.
    """
    loc = (location or "").strip()
    if not loc or "." not in loc:
        return None
    head, _dot, field = loc.rpartition(".")
    if not head or "." not in head:
        return None
    asset, _dot2, row = head.rpartition(".")
    if not asset or not row:
        return None
    return asset, row, field


def field_role(location: str) -> str:
    """The field's semantic role, with UE's column GUID removed."""
    parts = _split(location)
    if parts is None:
        return ""
    return _COLUMN_GUID.sub("", parts[2])


def asset_entity(location: str) -> str:
    """``asset::row`` — the thing a name and a description share.

    The asset MUST be part of the key: row ids are not globally unique
    (`B_006` exists under both DT_CraftFormula_B and
    DT_CraftFormula_Werewolf), so keying on the row alone silently
    merges unrelated items.
    """
    parts = _split(location)
    if parts is None:
        return ""
    asset, row, _field = parts
    return f"{asset}::{row}"


def subsystem(location: str, depth: int = 4) -> str:
    """The asset DIRECTORY, to `depth` segments. Coarse but universal.

    The fallback for rows whose field is only a column GUID: they have
    no per-string identity, but "these are Wiki entries" is still a
    real domain signal.

    The final path segment is dropped first — it is the asset filename
    (`DT_Subtitles.DT_Subtitles.0.Character`), not a directory, and
    keeping it made every data table its own "subsystem": 265 groups of
    median 6 on a 1,349-row corpus, which is not a grouping.

    depth=4 measured on that corpus: 22 groups, median 11, e.g.
    `Game/Program/Config/UIConfig` (579), `Game/Program/Item` (87),
    `Game/Program/Config/ObjectiveSystem` (65). Depth 3 collapses to 4
    groups (too coarse to mean anything); depth 5+ climbs back to 54+.
    """
    loc = (location or "").strip().strip("/")
    if not loc:
        return ""
    segs = loc.split("/")
    dirs = segs[:-1] if len(segs) > 1 else segs
    return "/".join(dirs[:depth])


# Conversation-bearing assets keep an integer row id that IS a position.
_CONVO_ASSET = re.compile(r"(Subtitle|Conversation|Dialog)", re.I)


def conversation_ref(location: str) -> Optional[Tuple[str, int]]:
    """``(asset, seq)`` when the path is a positioned conversation line.

    Only fires when the asset name says conversation AND the row id is
    an integer — an integer row on an item table is a formula index, not
    a dialogue position, and treating it as one would invent scenes.
    """
    parts = _split(location)
    if parts is None:
        return None
    asset, row, _field = parts
    if not _CONVO_ASSET.search(asset):
        return None
    if not row.isdigit():
        return None
    return asset, int(row)


def group_by_entity(segments: Sequence[dict], *,
                    key: str = "uid",
                    location_key: str = "location") -> List[List[dict]]:
    """Group rows describing ONE entity (its name beside its blurb).

    Rows with no parseable location each become their own group rather
    than being pooled: a shared "unknown" bucket would claim they belong
    together, which is a stronger statement than the data supports.
    Order is deterministic — batch layout rides on every model
    fingerprint.
    """
    groups: Dict[str, List[dict]] = {}
    loose: List[List[dict]] = []
    for seg in segments:
        entity = asset_entity(seg.get(location_key) or "")
        if not entity:
            loose.append([seg])
            continue
        groups.setdefault(entity, []).append(seg)
    ordered = [groups[e] for e in sorted(groups)]
    # Sort within an entity by role so a name precedes its description
    # consistently, whatever order the PO listed them in.
    for group in ordered:
        group.sort(key=lambda s: (field_role(s.get(location_key) or ""),
                                  str(s.get(key, ""))))
    return ordered + loose


def group_stats(locations: Sequence[str]) -> dict:
    """What the three axes actually achieve on a corpus — for the
    operator. An axis that resolves to all-singletons is a failure that
    looks like success unless it is counted."""
    ent: Dict[str, int] = {}
    roles: Dict[str, int] = {}
    subs: Dict[str, int] = {}
    convo = 0
    for loc in locations:
        e = asset_entity(loc)
        if e:
            ent[e] = ent.get(e, 0) + 1
        r = field_role(loc)
        if r:
            roles[r] = roles.get(r, 0) + 1
        s = subsystem(loc)
        if s:
            subs[s] = subs.get(s, 0) + 1
        if conversation_ref(loc):
            convo += 1
    multi = {e: n for e, n in ent.items() if n > 1}
    return {
        "rows": len(locations),
        "entities": len(ent),
        "entities_multi": len(multi),
        "rows_in_multi_entities": sum(multi.values()),
        "roles": len(roles),
        "subsystems": len(subs),
        "conversation_rows": convo,
        "unparseable": sum(1 for loc in locations if not asset_entity(loc)),
    }
