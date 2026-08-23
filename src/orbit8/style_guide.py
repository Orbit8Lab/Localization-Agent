"""Style guides as DATA, per language pair (docs/STANDARDS.md §2.2).

A style guide nobody can verify is a guide nobody follows. So every rule
declares how it is ENFORCED, and the enforcement bin decides where it
goes in the pipeline:

  mechanical — deterministic check in the T1 gate. Absolute, free, no
               LLM. (punctuation width, spacing, forbidden strings,
               length caps, required/forbidden patterns)
  llm        — a rubric line in the T3 review prompt. The reviewer must
               cite the rule id in its finding, so "sounds unnatural"
               becomes "violates ZH-EN-04".
  advisory   — steering only: goes in the translator prompt, is never
               checked. Deliberately rare; an unenforceable rule that
               nobody marks as advisory is a rule that silently rots.

Rules are scoped: a guide has GLOBAL rules plus per-string-type
overrides, because "casual register" is right for dialogue and wrong for
a system error. The applicable slice for a batch = global + that batch's
domain.

Storage: ``40-reference/style/<source>-<target>.json`` beside the
glossary. Human-readable markdown is RENDERED from the json, never
maintained separately.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

ENFORCEMENT = ("mechanical", "llm", "advisory")

# Mechanical check kinds — the vocabulary a deterministic checker
# understands. Anything else must be an llm or advisory rule.
CHECKS = (
    "forbid_pattern",     # regex must NOT appear in the target
    "require_pattern",    # regex MUST appear when the rule applies
    "max_chars",          # target length ceiling
    "max_words",
    "forbid_chars",       # literal characters that must not appear
    "require_source_parity",   # e.g. trailing punctuation must match
)


@dataclass
class StyleRule:
    id: str
    text: str                      # the rule, as a human reads it
    enforcement: str = "advisory"
    check: Optional[str] = None    # required when enforcement=mechanical
    value: Optional[object] = None  # pattern / limit / char set
    domains: List[str] = field(default_factory=list)  # [] = all
    severity: str = "medium"
    rationale: str = ""
    examples: Dict[str, str] = field(default_factory=dict)  # good/bad

    def applies_to(self, domain: Optional[str]) -> bool:
        return not self.domains or (domain or "") in self.domains

    def to_dict(self) -> dict:
        out = {"id": self.id, "text": self.text,
               "enforcement": self.enforcement, "severity": self.severity}
        for name in ("check", "value", "rationale"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        if self.domains:
            out["domains"] = self.domains
        if self.examples:
            out["examples"] = self.examples
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "StyleRule":
        return cls(id=raw["id"], text=raw["text"],
                   enforcement=raw.get("enforcement", "advisory"),
                   check=raw.get("check"), value=raw.get("value"),
                   domains=list(raw.get("domains", [])),
                   severity=raw.get("severity", "medium"),
                   rationale=raw.get("rationale", ""),
                   examples=dict(raw.get("examples", {})))


@dataclass
class Morphology:
    """How the TARGET language varies the surface form of a term.

    This is the data that lets glossary compliance tolerate legitimate
    inflection without weakening enforcement into fuzzy matching. Each
    strategy is declarative, so adding a language means authoring a
    profile — never editing the gate.

      suffix   — regular affixes: a locked term matches when the target
                 carries one of ``suffixes`` on its last word
                 (English -s/-es/-ies; German -n/-en …)
      stem     — truncate the last word by ``stem_cut`` characters and
                 match the stem (Russian/Polish case endings)
      none     — the surface form never varies (CJK): exact match only

    ``case_sensitive`` says whether capitalization is part of term
    IDENTITY. It almost never is (that is a style rule, ZH-EN-11), so
    matching lowercases by default.

    ``variants_note`` documents, for humans, that alternate wordings
    ("Source of Plague" vs "Plague Source") are a GLOSSARY decision —
    recorded per term, never guessed by the matcher.
    """
    strategy: str = "none"                     # suffix | stem | none
    suffixes: List[str] = field(default_factory=list)
    irregular: Dict[str, List[str]] = field(default_factory=dict)
    stem_cut: List[int] = field(default_factory=list)
    min_stem: int = 4
    case_sensitive: bool = False
    # Verbal affixes the language adds to a base form (English -ing/-ed).
    # A glossary stores ONE surface string, but a source term like 合成 is
    # both a verb and a noun: without this, "Used to craft" is judged a
    # violation of "Crafting" and the fix corrupts the sentence.
    # Cross-POS identity is still a per-term DECISION (`forms` on the
    # entry); this only covers regular inflection of one base.
    verb_suffixes: List[str] = field(default_factory=list)
    variants_note: str = ""

    def to_dict(self) -> dict:
        out: Dict[str, object] = {"strategy": self.strategy}
        for name in ("suffixes", "irregular", "stem_cut", "verb_suffixes",
                     "variants_note"):
            if getattr(self, name):
                out[name] = getattr(self, name)
        if self.case_sensitive:
            out["case_sensitive"] = True
        if self.strategy == "stem":
            out["min_stem"] = self.min_stem
        return out

    @classmethod
    def from_dict(cls, raw: dict) -> "Morphology":
        return cls(strategy=raw.get("strategy", "none"),
                   suffixes=list(raw.get("suffixes", [])),
                   irregular={k: list(v) for k, v
                              in (raw.get("irregular") or {}).items()},
                   stem_cut=list(raw.get("stem_cut", [])),
                   min_stem=int(raw.get("min_stem", 4)),
                   case_sensitive=bool(raw.get("case_sensitive", False)),
                   verb_suffixes=list(raw.get("verb_suffixes", [])),
                   variants_note=raw.get("variants_note", ""))

    # ------------------------------------------------------------ matching

    def forms(self, term: str) -> List[str]:
        """Every surface form of ``term`` this language may legitimately
        produce — the locked form first."""
        out = [term]
        words = term.split()
        if not words:
            return out
        last = words[-1]
        head = words[:-1]

        for base, alts in self.irregular.items():
            if last.lower() == base.lower():
                out += [" ".join(head + [alt]) for alt in alts]
            elif last.lower() in {a.lower() for a in alts}:
                out.append(" ".join(head + [base]))

        if self.strategy == "suffix":
            for suffix in self.suffixes:
                # add the affix …
                out.append(" ".join(head + [last + suffix]))
                # … and accept a locked form that already carries it
                if last.lower().endswith(suffix.lower()) and \
                        len(last) > len(suffix) + 1:
                    out.append(" ".join(head + [last[:-len(suffix)]]))
            # -y → -ies and its inverse, when declared
            if "es" in self.suffixes and last.lower().endswith("y"):
                out.append(" ".join(head + [last[:-1] + "ies"]))
            if last.lower().endswith("ies"):
                out.append(" ".join(head + [last[:-3] + "y"]))

        # Verbal inflection, both directions: a glossary that stores the
        # gerund ("Crafting") must accept the base ("craft"), and one that
        # stores the base must accept the gerund.
        for suffix in self.verb_suffixes:
            low = last.lower()
            if low.endswith(suffix) and len(last) > len(suffix) + 2:
                base = last[:-len(suffix)]
                out.append(" ".join(head + [base]))
                # doubled final consonant: "running" → "run"
                if len(base) > 2 and base[-1].lower() == base[-2].lower():
                    out.append(" ".join(head + [base[:-1]]))
                # dropped silent -e: "crafting" → "craft" needs no -e, but
                # "moving" → "move" does
                out.append(" ".join(head + [base + "e"]))
            else:
                out.append(" ".join(head + [last + suffix]))
                if low.endswith("e") and len(last) > 2:
                    out.append(" ".join(head + [last[:-1] + suffix]))
        return list(dict.fromkeys(out))

    def matches(self, locked: str, target: str) -> bool:
        """Does ``target`` contain ``locked`` in any legitimate form?"""
        from .gate_checks import term_in_text
        for form in self.forms(locked):
            if self.case_sensitive:
                if form in target:
                    return True
            elif term_in_text(form, target):
                return True
        if self.strategy != "stem":
            return False
        words = locked.lower().split()
        if not words or len(words[-1]) < self.min_stem + 1:
            return False
        haystack = target if self.case_sensitive else target.lower()
        for cut in (self.stem_cut or [2, 3]):
            stem = words[-1][:-cut]
            if len(stem) >= self.min_stem and \
                    " ".join(words[:-1] + [stem]) in haystack:
                return True
        return False


@dataclass
class StyleGuide:
    source_lang: str
    target_lang: str
    version: str = "1"
    notes: str = ""
    rules: List[StyleRule] = field(default_factory=list)
    # how the target language inflects — used by glossary compliance
    morphology: Morphology = field(default_factory=Morphology)

    # ------------------------------------------------------------ loading

    @classmethod
    def load(cls, path: Path) -> "StyleGuide":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        meta = raw.get("metadata", {})
        return cls(source_lang=meta.get("source_lang", ""),
                   target_lang=meta.get("target_lang", ""),
                   version=str(meta.get("version", "1")),
                   notes=meta.get("notes", ""),
                   rules=[StyleRule.from_dict(r)
                          for r in raw.get("rules", [])],
                   morphology=Morphology.from_dict(
                       raw.get("morphology", {})))

    def validate(self) -> List[str]:
        """Format conformance (docs/STANDARDS.md §2.2). A rule that
        cannot run is worse than no rule: it looks like coverage and
        provides none, so these problems are surfaced, never tolerated."""
        problems: List[str] = []
        seen: Dict[str, int] = {}
        for rule in self.rules:
            if not rule.id:
                problems.append("a rule has no id")
                continue
            seen[rule.id] = seen.get(rule.id, 0) + 1
            if not rule.text.strip():
                problems.append(f"{rule.id}: empty rule text")
            if rule.enforcement not in ENFORCEMENT:
                problems.append(
                    f"{rule.id}: unknown enforcement "
                    f"{rule.enforcement!r} (use {'/'.join(ENFORCEMENT)})")
            if rule.severity not in ("high", "medium", "low"):
                problems.append(f"{rule.id}: unknown severity "
                                f"{rule.severity!r}")
            if rule.enforcement == "mechanical":
                if not rule.check:
                    problems.append(
                        f"{rule.id}: mechanical rules need a 'check' — "
                        f"otherwise nothing enforces them")
                elif rule.check not in CHECKS:
                    problems.append(
                        f"{rule.id}: unknown check {rule.check!r} "
                        f"(known: {', '.join(CHECKS)}) — it will never "
                        f"fire")
                elif rule.value in (None, ""):
                    problems.append(f"{rule.id}: check {rule.check!r} "
                                    f"needs a 'value'")
                elif rule.check in ("forbid_pattern", "require_pattern"):
                    try:
                        re.compile(str(rule.value))
                    except re.error as err:
                        problems.append(f"{rule.id}: invalid regex "
                                        f"{rule.value!r} ({err})")
                elif rule.check in ("max_chars", "max_words"):
                    try:
                        int(rule.value)
                    except (TypeError, ValueError):
                        problems.append(f"{rule.id}: {rule.check} needs "
                                        f"an integer value")
            elif rule.check:
                problems.append(
                    f"{rule.id}: has a 'check' but enforcement is "
                    f"{rule.enforcement!r} — it will never run")
        problems += [f"duplicate rule id {rid!r} ({n} times)"
                     for rid, n in sorted(seen.items()) if n > 1]
        if self.morphology.strategy not in ("suffix", "stem", "none"):
            problems.append(f"unknown morphology strategy "
                            f"{self.morphology.strategy!r}")
        return problems

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"metadata": {"source_lang": self.source_lang,
                          "target_lang": self.target_lang,
                          "version": self.version, "notes": self.notes,
                          "rules_total": len(self.rules),
                          "by_enforcement": self.counts()},
             "morphology": self.morphology.to_dict(),
             "rules": [r.to_dict() for r in self.rules]},
            ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # ------------------------------------------------------------ queries

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for rule in self.rules:
            out[rule.enforcement] = out.get(rule.enforcement, 0) + 1
        return out

    def for_domain(self, domain: Optional[str]) -> List[StyleRule]:
        """Global rules + the overrides that apply to this string type."""
        return [r for r in self.rules if r.applies_to(domain)]

    def mechanical(self, domain: Optional[str] = None) -> List[StyleRule]:
        return [r for r in self.for_domain(domain)
                if r.enforcement == "mechanical" and r.check]

    def rubric(self, domain: Optional[str] = None) -> List[StyleRule]:
        return [r for r in self.for_domain(domain)
                if r.enforcement == "llm"]

    # ------------------------------------------------------------ prompts

    def render_prompt(self, domain: Optional[str] = None, *,
                      include_advisory: bool = True) -> str:
        """The ONE way a style guide enters a prompt (STANDARDS §2.2).

        Emits the ``llm`` and ``advisory`` rules that apply to this
        string type, each prefixed with its id so a finding can cite it.
        ``mechanical`` rules are deliberately EXCLUDED: code already
        enforces them, so repeating them here only spends tokens and
        invites the model to duplicate findings the gate will raise
        anyway.
        """
        bins = ("llm", "advisory") if include_advisory else ("llm",)
        rules = [r for r in self.for_domain(domain)
                 if r.enforcement in bins]
        if not rules:
            return ""
        scope = f" · {domain}" if domain else ""
        lines = [f"**STYLE GUIDE — {self.source_lang} → "
                 f"{self.target_lang} v{self.version}{scope}**",
                 "Follow every rule below. When reporting a violation, "
                 "START the message with the rule id in brackets, e.g. "
                 f"\"[{rules[0].id}] …\"."]
        for rule in rules:
            line = f"  [{rule.id}] {rule.text}"
            if rule.severity != "medium":
                line += f" ({rule.severity})"
            if rule.examples.get("good"):
                line += f"\n      ✓ {rule.examples['good']}"
            if rule.examples.get("bad"):
                line += f"\n      ✗ {rule.examples['bad']}"
            lines.append(line)
        return "\n".join(lines)


# ----------------------------------------------------------- mechanical run

@dataclass
class StyleViolation:
    rule_id: str
    message: str
    severity: str
    evidence: str


def check_mechanical(guide: StyleGuide, source: str, target: str, *,
                     domain: Optional[str] = None) -> List[StyleViolation]:
    """Run every mechanical rule for this string type. Pure, deterministic
    and cheap — this is what makes a style guide enforceable rather than
    aspirational."""
    out: List[StyleViolation] = []
    if not target.strip():
        return out
    for rule in guide.mechanical(domain):
        violated, evidence = _run_check(rule, source, target)
        if violated:
            out.append(StyleViolation(
                rule_id=rule.id, message=rule.text,
                severity=rule.severity, evidence=evidence))
    return out


def _run_check(rule: StyleRule, source: str, target: str):
    value = rule.value
    if rule.check == "forbid_pattern":
        match = re.search(str(value), target)
        return (bool(match), match.group(0) if match else "")
    if rule.check == "require_pattern":
        return (re.search(str(value), target) is None, target[:60])
    if rule.check == "forbid_chars":
        hits = [ch for ch in str(value) if ch in target]
        return (bool(hits), "".join(hits))
    if rule.check == "max_chars":
        return (len(target) > int(value), f"{len(target)} chars")
    if rule.check == "max_words":
        count = len(target.split())
        return (count > int(value), f"{count} words")
    if rule.check == "require_source_parity":
        # trailing punctuation parity: if the source ends with one of the
        # listed marks, the target must end with a mark too (any of them)
        marks = str(value)
        src_end = source.rstrip()[-1:] in marks
        tgt_end = target.rstrip()[-1:] in marks
        return (src_end != tgt_end,
                f"source ends {source.rstrip()[-1:]!r}, "
                f"target ends {target.rstrip()[-1:]!r}")
    return (False, "")


# --------------------------------------------------------------- rendering

def render_markdown(guide: StyleGuide) -> str:
    """Human-readable guide, generated from the data — so the doc and the
    enforced rules can never drift apart."""
    lines = [f"# Style guide — {guide.source_lang} → {guide.target_lang}",
             f"version {guide.version}", ""]
    if guide.notes:
        lines += [guide.notes, ""]
    counts = guide.counts()
    morph = guide.morphology
    lines += [f"{len(guide.rules)} rules — "
              + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())),
              "",
              "## Morphology (glossary compliance)", "",
              f"- strategy: `{morph.strategy}`"
              + (f" — suffixes {morph.suffixes}" if morph.suffixes else "")
              + (f", stem cuts {morph.stem_cut}" if morph.stem_cut else ""),
              f"- capitalization is "
              + ("part of term identity"
                 if morph.case_sensitive else
                 "NOT part of term identity (see the style rules)"),
              *([f"- irregular forms: "
                 f"{', '.join(sorted(morph.irregular))}"]
                if morph.irregular else []),
              *([f"- {morph.variants_note}"]
                if morph.variants_note else []),
              "",]
    lines += [
              "| id | rule | applies to | enforcement | severity |",
              "|---|---|---|---|---|"]
    for rule in guide.rules:
        scope = ", ".join(rule.domains) if rule.domains else "all"
        lines.append(f"| `{rule.id}` | {rule.text} | {scope} | "
                     f"{rule.enforcement} | {rule.severity} |")
    detailed = [r for r in guide.rules if r.rationale or r.examples]
    if detailed:
        lines += ["", "## Notes and examples", ""]
        for rule in detailed:
            lines.append(f"**{rule.id}** — {rule.text}")
            if rule.rationale:
                lines.append(f"  - why: {rule.rationale}")
            for kind in ("good", "bad"):
                if rule.examples.get(kind):
                    mark = "✓" if kind == "good" else "✗"
                    lines.append(f"  - {mark} {rule.examples[kind]}")
            lines.append("")
    return "\n".join(lines) + "\n"
