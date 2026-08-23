"""Direct operator edits to a T1 glossary (docs/STANDARDS.md §2.1).

The everyday "just add these terms" path — an operator ruling is a
first-class decision, so an added term lands LOCKED with provenance, the
same status a PE-review decision earns. Rules:

- never mutate in place blindly: the previous file is kept as
  ``<name>.bak-<stamp>.json`` and every edit is reported;
- an existing UNLOCKED entry is overwritten (operator outranks mining) —
  the old rendering is recorded as ``supersedes_rendering``;
- an existing LOCKED entry is NOT silently overwritten: the edit is
  reported as a conflict unless ``force=True``, because superseding a
  ruling has propagation cost;
- ``zh_old`` (alias/rename source) is retired from the term list and
  recorded in the surviving entry's ``supersedes``;
- family rules (族) are never shadowed by a term of the same base.

After editing, the caller re-renders the xlsx view so JSON and workbook
never drift.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .glossary_update import _en_has, write_term_glossary


@dataclass
class TermEdit:
    """One operator instruction. ``zh`` may carry alias forms separated
    by '/' — the first is canonical, the rest become aliases."""
    zh: str
    en: str
    note: str = ""
    zh_old: Optional[str] = None


@dataclass
class EditResult:
    added: List[dict] = field(default_factory=list)
    overwritten: List[dict] = field(default_factory=list)
    aliased: List[dict] = field(default_factory=list)
    retired: List[dict] = field(default_factory=list)
    conflicts: List[dict] = field(default_factory=list)
    unchanged: List[dict] = field(default_factory=list)
    flagged: List[dict] = field(default_factory=list)

    @property
    def wrote(self) -> bool:
        return bool(self.added or self.overwritten or self.aliased
                    or self.retired)


def parse_term_arg(raw: str) -> TermEdit:
    """``zh=EN`` or ``zh1/zh2=EN`` or ``old>new=EN`` (rename)."""
    if "=" not in raw:
        raise ValueError(f"--term needs zh=EN, got {raw!r}")
    zh, en = raw.split("=", 1)
    zh, en = zh.strip(), en.strip()
    zh_old = None
    if ">" in zh:
        zh_old, zh = [p.strip() for p in zh.split(">", 1)]
    return TermEdit(zh=zh, en=en, zh_old=zh_old or None)


def add_variants(glossary: dict, zh: str, variants: List[str], *,
                 origin: str) -> dict:
    """Record operator-approved ALTERNATE renderings on a term.

    A variant satisfies glossary compliance without being the preferred
    form — it is a decision ("Source of Plague is acceptable for Plague
    Source"), never a fuzzy match the checker guessed. Morphological
    forms (plurals, cases) do NOT belong here: those come from the
    language profile in the style guide.
    """
    entry = glossary.setdefault("terms", {}).get(zh)
    if entry is None:
        raise KeyError(f"{zh!r} is not in the glossary — add the term "
                       f"first")
    existing = list(entry.get("variants", []))
    for variant in variants:
        variant = variant.strip()
        if variant and variant != entry["translation"] \
                and variant not in existing:
            existing.append(variant)
    entry["variants"] = existing
    note = f"variants recorded {origin}"
    entry["evidence"] = ((entry.get("evidence", "") + "; " + note)
                         .lstrip("; "))
    return entry


def unify_terms(glossary: dict, canonical: str, variants: List[str], *,
                translation: Optional[str] = None, origin: str,
                keep: bool = True) -> dict:
    """Declare a SOURCE-variant group: several spellings of one concept
    that must all render as the same target term.

    Game source text is rarely consistent — a dev writes 马车 in some
    strings and 车队 in others. Normalizing them to one target is the
    glossary's job; this records that decision explicitly:

    - the canonical entry is locked with the agreed rendering;
    - each variant keeps its own entry (so the LQA gate still matches
      those source strings) but is marked ``variant_of`` the canonical
      one, and is never treated as a competing ruling;
    - with ``keep=False`` the variants are retired instead, for cases
      where the source spelling is genuinely obsolete.

    Returns a summary dict.
    """
    terms = glossary.setdefault("terms", {})
    head = terms.get(canonical)
    if head is None and translation is None:
        raise KeyError(f"{canonical!r} is not in the glossary — pass a "
                       f"translation to create it")
    rendering = translation or head["translation"]
    terms[canonical] = {**(head or {}), "translation": rendering,
                        "locked": True, "type": "decision",
                        "evidence": ((head or {}).get("evidence", "")
                                     + f"; canonical {origin}").lstrip("; ")}
    terms[canonical].pop("variant_of", None)

    applied, retired, missing = [], [], []
    for variant in variants:
        if variant == canonical:
            continue
        entry = terms.get(variant)
        if entry is None:
            missing.append(variant)
            continue
        if keep:
            terms[variant] = {
                **entry, "translation": rendering, "variant_of": canonical,
                "locked": True, "type": "source_variant",
                "evidence": (entry.get("evidence", "")
                             + f"; source variant of {canonical} "
                               f"{origin}").lstrip("; ")}
            applied.append(variant)
        else:
            terms.pop(variant)
            retired.append(variant)
    if retired:
        terms[canonical]["supersedes"] = ", ".join(retired)

    meta = glossary.setdefault("metadata", {})
    meta["locked_terms"] = sum(1 for t in terms.values()
                               if t.get("locked"))
    meta["mined_terms"] = sum(1 for t in terms.values()
                              if not t.get("locked"))
    glossary["terms"] = dict(sorted(
        terms.items(), key=lambda kv: (not kv[1].get("locked"), kv[0])))
    return {"canonical": canonical, "translation": rendering,
            "variants": applied, "retired": retired, "missing": missing}


def apply_edits(glossary: dict, edits: List[TermEdit], *, origin: str,
                force: bool = False) -> EditResult:
    """Apply operator edits to a loaded T1 glossary (mutated in place)."""
    terms = glossary.setdefault("terms", {})
    families = glossary.get("metadata", {}).get("family_rules") or {}
    result = EditResult()

    for edit in edits:
        forms = [f.strip() for f in edit.zh.split("/") if f.strip()]
        canonical, aliases = forms[0], forms[1:]
        for i, zh in enumerate(forms):
            existing = terms.get(zh)
            record = {"zh": zh, "en": edit.en,
                      **({"alias_of": canonical} if i else {})}
            if existing and existing.get("locked"):
                if existing["translation"] == edit.en:
                    result.unchanged.append(record)
                    continue
                if not force:
                    result.conflicts.append(
                        {**record, "current": existing["translation"],
                         "evidence": existing.get("evidence", ""),
                         "why": "locked ruling — pass force to supersede"})
                    continue
            entry = {"translation": edit.en, "type": "operator",
                     "locked": True, "evidence": origin}
            if existing:
                if not existing.get("locked"):
                    entry["supersedes_rendering"] = existing["translation"]
                for carry in ("category", "supersedes"):
                    if existing.get(carry):
                        entry.setdefault(carry, existing[carry])
                result.overwritten.append(
                    {**record, "was": existing["translation"]})
            else:
                (result.aliased if i else result.added).append(record)
            if i:
                entry["alias_of"] = canonical
            if edit.note:
                entry["note"] = edit.note
            if edit.zh_old:
                entry["supersedes"] = edit.zh_old
            terms[zh] = entry

        if edit.zh_old and edit.zh_old in terms:
            retired = terms.pop(edit.zh_old)
            result.retired.append({"zh": edit.zh_old,
                                   "was": retired["translation"],
                                   "by": canonical})

    # family-rule consistency: any term containing a family base must
    # render it — re-checked after every edit round.
    for zh, entry in terms.items():
        entry.pop("check", None)
        if entry.get("locked"):
            continue
        for base, rule in families.items():
            if base != zh and base in zh and not _en_has(
                    rule["translation"], entry["translation"]):
                entry["check"] = (f"contains family rule {base}="
                                  f"{rule['translation']}")
                result.flagged.append({"zh": zh, "rule": base})
                break

    meta = glossary.setdefault("metadata", {})
    meta["locked_terms"] = sum(1 for t in terms.values()
                               if t.get("locked"))
    meta["mined_terms"] = sum(1 for t in terms.values()
                              if not t.get("locked"))
    glossary["terms"] = dict(sorted(
        terms.items(), key=lambda kv: (not kv[1].get("locked"), kv[0])))
    return result


def edit_glossary_file(path: Path, edits: List[TermEdit], *, origin: str,
                       force: bool = False,
                       backup_stamp: str = "") -> Tuple[dict, EditResult,
                                                        Optional[Path]]:
    """Load, edit, back up, write, and re-render the xlsx view."""
    path = Path(path)
    glossary = json.loads(path.read_text(encoding="utf-8"))
    result = apply_edits(glossary, edits, origin=origin, force=force)
    backup = None
    if result.wrote:
        backup = path.with_suffix(f".bak-{backup_stamp}.json") \
            if backup_stamp else path.with_suffix(".bak.json")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        write_term_glossary(glossary, [], path.parent)
    return glossary, result, backup
