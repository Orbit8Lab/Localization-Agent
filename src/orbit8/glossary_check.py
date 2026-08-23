"""Glossary integrity checks — the gate before a termbase is published.

A glossary is law for every downstream stage, so a defect in it is a
defect in every translation and every LQA verdict that follows. These
checks run before promotion to ``40-reference/`` and answer one question:
can this file be trusted as the single source of truth?

Severity contract:
  ERROR — the file is internally contradictory; promotion is blocked.
  WARN  — a real problem needing a human decision, but the file is still
          usable (an operator may knowingly ship it).
  INFO  — hygiene worth knowing about.

Every finding names the terms involved, because "17 collisions" is not
actionable and "'Mana' is claimed by 魔力值, 魔法值 and 法力值" is.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SEVERITIES = ("ERROR", "WARN", "INFO")


@dataclass
class GlossaryIssue:
    severity: str
    kind: str
    message: str
    terms: List[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"[{self.severity}] {self.kind}: {self.message}"

    def to_dict(self) -> dict:
        return {"severity": self.severity, "kind": self.kind,
                "message": self.message, "terms": self.terms}


@dataclass
class GlossaryReport:
    issues: List[GlossaryIssue] = field(default_factory=list)
    terms_total: int = 0
    locked_total: int = 0

    @property
    def errors(self) -> List[GlossaryIssue]:
        return [i for i in self.issues if i.severity == "ERROR"]

    @property
    def warnings(self) -> List[GlossaryIssue]:
        return [i for i in self.issues if i.severity == "WARN"]

    @property
    def blocked(self) -> bool:
        """An ERROR means the file contradicts itself — never publish."""
        return bool(self.errors)

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in SEVERITIES}
        for issue in self.issues:
            out[issue.severity] = out.get(issue.severity, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {"terms_total": self.terms_total,
                "locked_total": self.locked_total,
                "counts": self.counts(), "blocked": self.blocked,
                "issues": [i.to_dict() for i in self.issues]}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def check_glossary(glossary: dict, *,
                   morphology=None) -> GlossaryReport:
    """Structural + semantic audit of a T1 glossary."""
    terms: Dict[str, dict] = glossary.get("terms", {}) or {}
    families = (glossary.get("metadata", {}) or {}).get(
        "family_rules") or {}
    report = GlossaryReport(
        terms_total=len(terms),
        locked_total=sum(1 for e in terms.values() if e.get("locked")))
    add = report.issues.append

    # -- 1. structural validity ------------------------------------------
    for zh, entry in sorted(terms.items()):
        if not str(zh).strip():
            add(GlossaryIssue("ERROR", "empty_source",
                              "a term has an empty source key", [zh]))
        if not str(entry.get("translation", "")).strip():
            add(GlossaryIssue("ERROR", "empty_translation",
                              f"{zh!r} has no translation", [zh]))
        if "/" in zh or "／" in zh:
            add(GlossaryIssue(
                "WARN", "unsplit_alias",
                f"{zh!r} looks like two aliases in one key — split them "
                f"into separate entries so each can be matched",
                [zh]))
        if _norm(zh) == _norm(entry.get("translation", "")):
            add(GlossaryIssue("WARN", "untranslated",
                              f"{zh!r} maps to itself", [zh]))

    # -- 2. source variants sharing one rendering ------------------------
    # Game source text is NOT clean: a dev writes 马车 in 40 strings and
    # 车队 in 8 for the same thing. Mapping every spelling to one target
    # is the CORRECT outcome — it is how a glossary normalizes a messy
    # source. What must be declared is which spelling is CANONICAL, so
    # the group is a deliberate normalization and not an accident.
    by_target: Dict[str, List[str]] = defaultdict(list)
    for zh, entry in terms.items():
        by_target[_norm(entry.get("translation"))].append(zh)
    for target, sources in sorted(by_target.items()):
        if len(sources) < 2 or not target:
            continue
        group = sorted(sources)
        # a group is DECLARED when exactly one member is canonical and
        # every other member points at it via `variant_of`
        canonical = [z for z in group
                     if not terms[z].get("variant_of")]
        pointers = {z: terms[z].get("variant_of") for z in group
                    if terms[z].get("variant_of")}
        dangling = {z: v for z, v in pointers.items() if v not in terms}
        wrong_head = {z: v for z, v in pointers.items()
                      if v in terms and v not in group}
        if dangling:
            add(GlossaryIssue(
                "ERROR", "dangling_variant_of",
                f"{', '.join(sorted(dangling))} point at "
                f"{', '.join(sorted(set(dangling.values())))}, which is "
                f"not in the glossary", sorted(dangling)))
        if wrong_head:
            add(GlossaryIssue(
                "ERROR", "cross_group_variant_of",
                f"{', '.join(sorted(wrong_head))} point at a term that "
                f"renders differently — variant_of must name a member of "
                f"the same rendering group", sorted(wrong_head)))
        if len(canonical) == 1 and not dangling and not wrong_head:
            add(GlossaryIssue(
                "INFO", "source_variants",
                f"{len(group)} source spellings normalize to {target!r} "
                f"(canonical {canonical[0]!r}) — declared",
                group))
            continue
        locked = [z for z in canonical if terms[z].get("locked")]
        severity = "ERROR" if len(locked) > 1 else "WARN"
        add(GlossaryIssue(
            severity, "undeclared_variant_group",
            f"{len(group)} source terms render as {target!r} "
            f"({', '.join(group)}) with "
            + (f"{len(canonical)} of them unmarked"
               if canonical else "no canonical term")
            + (f" and {len(locked)} locked" if len(locked) > 1 else "")
            + ". Declare one canonical spelling and mark the rest "
              "`variant_of` it (orbit8 glossary unify).",
            group))

    # -- 3. rename chains ------------------------------------------------
    for zh, entry in sorted(terms.items()):
        old = entry.get("supersedes")
        if not old:
            continue
        if old in terms:
            add(GlossaryIssue(
                "WARN", "superseded_still_present",
                f"{zh!r} supersedes {old!r}, but {old!r} is still an "
                f"active term — retire it or drop the supersedes claim",
                [zh, old]))
        if _norm(old) == _norm(zh):
            add(GlossaryIssue("WARN", "self_supersede",
                              f"{zh!r} supersedes itself", [zh]))

    # -- 4. variants ------------------------------------------------------
    preferred = {_norm(e.get("translation")): zh
                 for zh, e in terms.items()}
    for zh, entry in sorted(terms.items()):
        seen = set()
        for variant in entry.get("variants", []) or []:
            key = _norm(variant)
            if not key:
                continue
            if key == _norm(entry.get("translation")):
                add(GlossaryIssue(
                    "INFO", "redundant_variant",
                    f"{zh!r} lists its own rendering as a variant", [zh]))
            if key in seen:
                add(GlossaryIssue("INFO", "duplicate_variant",
                                  f"{zh!r} repeats variant {variant!r}",
                                  [zh]))
            seen.add(key)
            owner = preferred.get(key)
            if owner and owner != zh:
                add(GlossaryIssue(
                    "ERROR", "variant_collision",
                    f"{zh!r} accepts {variant!r}, which is the preferred "
                    f"rendering of {owner!r} — the same English would "
                    f"satisfy two different source terms",
                    [zh, owner]))
            if morphology is not None and morphology.matches(
                    entry.get("translation", ""), variant):
                add(GlossaryIssue(
                    "INFO", "morphological_variant",
                    f"{zh!r}: variant {variant!r} is just an inflection "
                    f"of {entry.get('translation')!r} — the language "
                    f"profile already accepts it, so the variant is "
                    f"redundant", [zh]))

    # -- 5. family-rule compliance ---------------------------------------
    from .glossary_update import _en_base, _en_has
    for zh, entry in sorted(terms.items()):
        if entry.get("locked"):
            continue
        for base, rule in families.items():
            if base in zh and not _en_has(rule["translation"],
                                          entry.get("translation", "")):
                add(GlossaryIssue(
                    "WARN", "family_violation",
                    f"{zh!r} contains family term {base!r} but renders as "
                    f"{entry.get('translation')!r}, missing "
                    f"{_en_base(rule['translation'])!r}",
                    [zh]))
                break

    # -- 6. locked terms contained in other locked terms -----------------
    locked_terms = {zh: e["translation"] for zh, e in terms.items()
                    if e.get("locked")}
    for zh, rendering in sorted(locked_terms.items()):
        for other, other_rendering in locked_terms.items():
            if other == zh or other not in zh:
                continue
            if not _en_has(other_rendering, rendering):
                add(GlossaryIssue(
                    "WARN", "locked_inconsistency",
                    f"locked {zh!r}={rendering!r} contains locked "
                    f"{other!r}={other_rendering!r} but does not render "
                    f"it — one of the two rulings needs revisiting",
                    [zh, other]))
                break
    return report


def check_glossary_file(path: Path, *, morphology=None) -> GlossaryReport:
    glossary = json.loads(Path(path).read_text(encoding="utf-8"))
    return check_glossary(glossary, morphology=morphology)


def write_check_report(report: GlossaryReport, out_dir: Path) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "glossary_check.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=1),
        encoding="utf-8")
    lines = ["# Glossary check", "",
             f"- terms: {report.terms_total} "
             f"({report.locked_total} locked)",
             f"- {report.counts()}",
             f"- publishable: {'NO — fix the ERRORs' if report.blocked else 'yes'}",
             ""]
    for severity in SEVERITIES:
        issues = [i for i in report.issues if i.severity == severity]
        if not issues:
            continue
        lines += [f"## {severity} ({len(issues)})", ""]
        lines += [f"- **{i.kind}** — {i.message}" for i in issues]
        lines.append("")
    path = out_dir / "glossary_check.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
