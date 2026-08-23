"""PO drop comparison: what changed between the previous .po and a newly
received one?

Every incoming drop (a dev update, a returned post-edit, a new export)
gets diffed against its predecessor BEFORE any pipeline work: the delta —
not the file — is what drives re-translation scope, LQA re-runs and
quoting. Keys are matched by msgctxt (the stable Unreal identity);
entries land in exactly one bucket:

    added                key only in the new file
    removed              key only in the old file
    source_changed       msgid differs (translation must be re-reviewed;
                         if the translation did NOT change too it is
                         additionally flagged STALE)
    translation_modified msgstr differs, both non-empty
    newly_translated     old msgstr empty → new filled
    translation_lost     old msgstr filled → new empty (regression!)
    unchanged            byte-identical msgid and msgstr

``translation_lost`` and stale translations are the red flags a human
must see — they are listed first in the report.

Word accounting happens at two levels:

- **entry words** (word_count of the whole string) — the re-review scope;
- **delta words** (word_diff: tokens actually added/removed inside a
  changed string) — the minimal edit; a 2-word fix in a 100-word
  paragraph counts as 2, not 100.

``work_summary()`` ties the two languages together: source words
added/edited in this drop, how much of the target was ALREADY updated in
the same drop, and the outstanding target work (new untranslated strings,
stale translations whose source moved, lost translations).
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from .exports import read_po_entries
from .po_sanity import CJK_RE, LATIN_WORD_RE, word_count

# Tokenization identical to po_sanity.word_count: each CJK character is
# one token, each Latin/digit run is one token.
_WORD_TOKEN_RE = re.compile(f"{CJK_RE.pattern}|{LATIN_WORD_RE.pattern}")


def word_diff(old: str, new: str) -> Dict[str, int]:
    """Words actually added/removed between two strings (CJK-aware).
    A replace counts on both sides."""
    a = _WORD_TOKEN_RE.findall(old)
    b = _WORD_TOKEN_RE.findall(new)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return {"added": added, "removed": removed}


@dataclass
class PoComparison:
    old_file: str
    new_file: str
    added: List[dict] = field(default_factory=list)
    removed: List[dict] = field(default_factory=list)
    source_changed: List[dict] = field(default_factory=list)
    translation_modified: List[dict] = field(default_factory=list)
    newly_translated: List[dict] = field(default_factory=list)
    translation_lost: List[dict] = field(default_factory=list)
    unchanged: int = 0
    duplicate_keys: List[str] = field(default_factory=list)

    BUCKETS = ("added", "removed", "source_changed",
               "translation_modified", "newly_translated",
               "translation_lost")

    def counts(self) -> Dict[str, int]:
        counts = {name: len(getattr(self, name)) for name in self.BUCKETS}
        counts["unchanged"] = self.unchanged
        counts["stale_translations"] = sum(
            1 for e in self.source_changed if e.get("stale"))
        return counts

    def word_counts(self) -> Dict[str, Dict[str, int]]:
        """Source/translation words per bucket — the quoting basis for
        the incremental work a new drop creates. Counted on the FULL
        strings at compare time (never the clipped display texts)."""
        out: Dict[str, Dict[str, int]] = {}
        for name in self.BUCKETS:
            entries = getattr(self, name)
            out[name] = {
                "entries": len(entries),
                "source_words": sum(e.get("source_words", 0)
                                    for e in entries),
                "translation_words": sum(e.get("target_words", 0)
                                         for e in entries)}
        return out

    def work_summary(self) -> Dict[str, dict]:
        """The source→target correspondence, in words.

        source              what the drop changed in the source language
        target_updated      target edits that arrived IN THIS SAME DROP
        target_outstanding  target work the drop creates but does not
                            contain: new untranslated strings, stale
                            translations (source moved, translation did
                            not — full words = re-review scope, edited
                            words = the minimal source delta), and lost
                            translations
        correspondence      of the entries whose source was edited, how
                            many had their translation updated too
        """
        stale = [e for e in self.source_changed if e.get("stale")]
        retranslated = [e for e in self.source_changed
                        if not e.get("stale")]
        new_untranslated = [e for e in self.added if e["untranslated"]]
        new_translated = [e for e in self.added if not e["untranslated"]]
        updated = retranslated + self.translation_modified
        return {
            "source": {
                "new": {"entries": len(self.added),
                        "words": sum(e["source_words"]
                                     for e in self.added)},
                "removed": {"entries": len(self.removed),
                            "words": sum(e["source_words"]
                                         for e in self.removed)},
                "edited": {
                    "entries": len(self.source_changed),
                    "words_added": sum(e["source_delta"]["added"]
                                       for e in self.source_changed),
                    "words_removed": sum(e["source_delta"]["removed"]
                                         for e in self.source_changed)},
            },
            "target_updated": {
                "entries": (len(updated) + len(self.newly_translated)
                            + len(new_translated)),
                "words_added": (
                    sum(e["target_delta"]["added"] for e in updated)
                    + sum(e["target_words"]
                          for e in self.newly_translated)
                    + sum(e["target_words"] for e in new_translated)),
                "words_removed": sum(e["target_delta"]["removed"]
                                     for e in updated),
            },
            "target_outstanding": {
                "new_strings": {
                    "entries": len(new_untranslated),
                    "source_words": sum(e["source_words"]
                                        for e in new_untranslated)},
                "stale_translations": {
                    "entries": len(stale),
                    "source_words_full": sum(e["source_words"]
                                             for e in stale),
                    "source_words_edited": sum(e["source_delta"]["added"]
                                               for e in stale)},
                "lost_translations": {
                    "entries": len(self.translation_lost),
                    "source_words": sum(e["source_words"]
                                        for e in self.translation_lost)},
            },
            "correspondence": {
                "source_edited_entries": len(self.source_changed),
                "target_also_updated": len(retranslated),
                "target_stale": len(stale),
            },
        }

    @property
    def needs_attention(self) -> bool:
        return bool(self.translation_lost
                    or any(e.get("stale") for e in self.source_changed))


def _index(path: Path) -> Tuple[Dict[str, dict], List[str]]:
    """key -> {source, target, location}; also reports duplicate keys
    (last occurrence wins, matching engine import behavior)."""
    index: Dict[str, dict] = {}
    duplicates: List[str] = []
    for key, source, target, location in read_po_entries(Path(path)):
        if key in index:
            duplicates.append(key)
        index[key] = {"source": source, "target": target,
                      "location": location}
    return index, duplicates


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def compare_po(old: Path, new: Path) -> PoComparison:
    old_idx, old_dup = _index(old)
    new_idx, new_dup = _index(new)
    result = PoComparison(old_file=str(old), new_file=str(new),
                          duplicate_keys=sorted(set(old_dup + new_dup)))

    for key in new_idx.keys() - old_idx.keys():
        e = new_idx[key]
        result.added.append({
            "key": key, "location": e["location"],
            "source": _clip(e["source"]), "new_target": _clip(e["target"]),
            "untranslated": not e["target"].strip(),
            "source_words": word_count(e["source"]),
            "target_words": word_count(e["target"])})
    for key in old_idx.keys() - new_idx.keys():
        e = old_idx[key]
        result.removed.append({
            "key": key, "location": e["location"],
            "source": _clip(e["source"]), "old_target": _clip(e["target"]),
            "source_words": word_count(e["source"])})

    for key in old_idx.keys() & new_idx.keys():
        o, n = old_idx[key], new_idx[key]
        entry = {"key": key, "location": n["location"],
                 "source_words": word_count(n["source"]),
                 "target_words": word_count(n["target"])}
        if o["source"] != n["source"]:
            entry.update(
                old_source=_clip(o["source"]), new_source=_clip(n["source"]),
                old_target=_clip(o["target"]), new_target=_clip(n["target"]),
                source_delta=word_diff(o["source"], n["source"]),
                target_delta=word_diff(o["target"], n["target"]),
                # source moved but the old translation shipped unchanged —
                # it now translates text that no longer exists
                stale=(o["target"].strip() != ""
                       and o["target"] == n["target"]))
            result.source_changed.append(entry)
        elif o["target"] != n["target"]:
            entry.update(source=_clip(n["source"]),
                         old_target=_clip(o["target"]),
                         new_target=_clip(n["target"]),
                         target_delta=word_diff(o["target"], n["target"]))
            if not o["target"].strip():
                result.newly_translated.append(entry)
            elif not n["target"].strip():
                result.translation_lost.append(entry)
            else:
                result.translation_modified.append(entry)
        else:
            result.unchanged += 1
    for bucket in PoComparison.BUCKETS:
        getattr(result, bucket).sort(key=lambda e: e["key"])
    return result


def write_compare_report(result: PoComparison, out_dir: Path) -> Path:
    """md + json under ``out_dir``, named after the new file's stem."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(result.new_file).stem
    (out_dir / f"{stem}_compare.json").write_text(json.dumps(
        {"old": result.old_file, "new": result.new_file,
         "counts": result.counts(), "word_counts": result.word_counts(),
         "work_summary": result.work_summary(),
         "needs_attention": result.needs_attention,
         **{name: getattr(result, name) for name in PoComparison.BUCKETS},
         "duplicate_keys": result.duplicate_keys},
        ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# PO comparison — `{Path(result.old_file).name}` → "
             f"`{Path(result.new_file).name}`", ""]
    if result.needs_attention:
        lines += ["> ⚠ **Needs attention** — lost or stale translations "
                  "below.", ""]
    lines += ["| Bucket | Entries |", "|---|---|"]
    lines += [f"| {name} | {count} |"
              for name, count in result.counts().items()]
    if result.translation_lost:
        lines += ["", "## ⛔ Translations LOST (filled before, empty now)"]
        lines += [f"- `{e['key']}` {e['source']!r} — was: {e['old_target']}"
                  for e in result.translation_lost]
    stale = [e for e in result.source_changed if e.get("stale")]
    if stale:
        lines += ["", "## ⚠ Stale translations (source changed, "
                  "translation did not)"]
        lines += [f"- `{e['key']}`: {e['old_source']!r} → "
                  f"{e['new_source']!r} (still: {e['new_target']})"
                  for e in stale]
    for name, title in (("added", "Added"), ("removed", "Removed"),
                        ("source_changed", "Source changed"),
                        ("translation_modified", "Translation modified"),
                        ("newly_translated", "Newly translated")):
        entries = getattr(result, name)
        if entries:
            lines += ["", f"## {title} ({len(entries)})"]
            lines += [f"- `{e['key']}` {(_clip(e.get('source') or e.get('new_source') or '', 50))!r}"
                      for e in entries[:30]]
            if len(entries) > 30:
                lines.append(f"- … {len(entries) - 30} more (see json)")
    if result.duplicate_keys:
        lines += ["", "## Duplicate keys (last occurrence wins on import)"]
        lines += [f"- `{k}`" for k in result.duplicate_keys[:20]]
    wc = result.word_counts()
    lines += ["", "## Word counts (incremental work basis)",
              "| Bucket | Entries | Source words | Translation words |",
              "|---|---|---|---|"]
    lines += [f"| {name} | {b['entries']} | {b['source_words']} | "
              f"{b['translation_words']} |" for name, b in wc.items()]

    ws = result.work_summary()
    src, upd, out = (ws["source"], ws["target_updated"],
                     ws["target_outstanding"])
    corr = ws["correspondence"]
    lines += [
        "", "## Source → target correspondence",
        f"- **Source side of this drop**: "
        f"{src['new']['entries']} new strings "
        f"({src['new']['words']} words) · "
        f"{src['edited']['entries']} edited strings "
        f"(+{src['edited']['words_added']}/"
        f"-{src['edited']['words_removed']} words) · "
        f"{src['removed']['entries']} removed "
        f"({src['removed']['words']} words)",
        f"- **Target already updated in this drop**: "
        f"{upd['entries']} strings "
        f"(+{upd['words_added']}/-{upd['words_removed']} words)",
        f"- **Target work still outstanding**: "
        f"{out['new_strings']['entries']} new untranslated "
        f"({out['new_strings']['source_words']} source words) · "
        f"{out['stale_translations']['entries']} stale "
        f"({out['stale_translations']['source_words_full']} words "
        f"re-review scope, "
        f"{out['stale_translations']['source_words_edited']} words "
        f"minimal edit) · "
        f"{out['lost_translations']['entries']} lost "
        f"({out['lost_translations']['source_words']} words)",
        f"- **Correspondence**: of {corr['source_edited_entries']} "
        f"source-edited strings, {corr['target_also_updated']} had their "
        f"translation updated in the same drop; "
        f"{corr['target_stale']} did not (stale).",
    ]
    md = out_dir / f"{stem}_compare.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    return md
