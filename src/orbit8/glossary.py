"""Layered glossary: T1 game (locked at G1) > T2 genre (tenant library) >
T3 standard UI (cross-game library). Precedence is strict — the merged brief
a batch sees is the T1-wins view (LIFECYCLE stage 3).

This module has NO write path to a locked glossary. Post-G1 changes travel
exclusively through an `AuditedFixRequest` artifact that re-opens G1
(design §7: a prompt instruction is a suggestion; a missing tool is a
guarantee).

File format is shared with localization-pipeline `glossaries/`:
    {"metadata": {...}, "terms": {"<term>": {"translation": "...", ...}}}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .gate_checks import term_in_text
from .schemas import GlossaryBrief, TermBrief


class Glossary:
    def __init__(self, game: str, locale: str, asset_version: int = 1):
        self.game = game
        self.locale = locale
        self.asset_version = asset_version
        self.terms: Dict[str, TermBrief] = {}

    # ------------------------------------------------------------ loading

    @classmethod
    def from_layers(cls, game: str, locale: str, *,
                    t1: Optional[dict] = None,
                    t2: Optional[Dict[str, str]] = None,
                    t3: Optional[Dict[str, str]] = None,
                    asset_version: int = 1) -> "Glossary":
        """Merge with strict precedence: lower tiers first, T1 overwrites."""
        glossary = cls(game, locale, asset_version)
        for tier, layer in ((3, t3), (2, t2)):
            for term, translation in (layer or {}).items():
                glossary.terms[term.lower()] = TermBrief(
                    term=term, translation=translation, tier=tier)
        for term, entry in (t1 or {}).get("terms", {}).items():
            glossary.terms[term.lower()] = TermBrief(
                term=term, translation=entry["translation"],
                type=entry.get("type", "other"), tier=1,
                locked=bool(entry.get("locked")),
                forms=dict(entry.get("forms") or {}),
                case=entry.get("case", "context"),
                en_anchor=entry.get("en_anchor"),
                sense_note=entry.get("sense_note"),
                distinct_from=entry.get("distinct_from", []),
                examples=entry.get("examples", []))
        return glossary

    @classmethod
    def load_t1_file(cls, path: Path) -> dict:
        return json.loads(Path(path).read_text(encoding="utf-8"))

    # ------------------------------------------------------------ queries

    def locked_map(self, locked_only: bool = False) -> Dict[str, str]:
        """source term -> rendering, for the deterministic gate.
        Only T1 terms are hard gate constraints; T2/T3 steer prompts.

        ``locked_only`` restricts the map to entries a human actually
        ratified.

        The two callers differ ON PURPOSE, and the difference is easy to
        misread as a bug:

        - the **T1 term check** (controller, external_lqa) passes NOTHING,
          so every tier-1 term is enforced. A tier-1 entry got there by a
          deliberate act — a publisher's glossary, an operator's ruling —
          and is authoritative on arrival.
        - the **T2 consistency check** (graphs/lqa.py) passes
          ``locked_only=True``. It flags a term rendered inconsistently
          ACROSS the corpus, which is a much stronger claim, so it speaks
          only for renderings a human ratified.

        So ``locked`` is provenance — "a human signed off on this" — not a
        switch that turns enforcement on. An unlocked termbase still
        produces T1 terminology findings.
        """
        return {t.term: t.translation for t in self.terms.values()
                if t.tier == 1 and (t.locked or not locked_only)}

    def prefill(self, source: str) -> Optional[str]:
        """Whole-string exact term hit ⇒ translation with zero LLM cost."""
        entry = self.terms.get(source.strip().lower())
        return entry.translation if entry else None

    def brief_for(self, texts: List[str]) -> GlossaryBrief:
        """Per-batch slice: only terms matched in the batch source,
        longest-match-first so compound terms surface before their parts."""
        matched: List[TermBrief] = []
        for entry in sorted(self.terms.values(),
                            key=lambda t: len(t.term), reverse=True):
            if any(term_in_text(entry.term, text) for text in texts):
                matched.append(entry)
        return GlossaryBrief(game=self.game, locale=self.locale,
                             asset_version=self.asset_version, terms=matched)


def render_brief(brief: GlossaryBrief) -> str:
    """The glossary section of a Translator/Critic prompt.

    LOCKED and unlocked terms are rendered as separate blocks with
    different authority. A termbase is mostly mined guesses; labelling
    them all "locked" invites the reviewer to file a HIGH terminology
    defect against a preference no human ever ratified.
    """
    if not brief.terms:
        return ""
    locked = [t for t in brief.terms if t.locked]
    preferred = [t for t in brief.terms if not t.locked]

    def render(t: TermBrief) -> str:
        line = f'  • "{t.term}" → "{t.translation}"'
        if t.forms:
            shown = ", ".join(f"{pos}: {form}"
                              for pos, form in sorted(t.forms.items()))
            line += f" (forms — {shown})"
        if t.tier > 1:
            line += f" [T{t.tier}]"
        if t.en_anchor:
            line += f" (en: {t.en_anchor})"
        if t.sense_note:
            line += f" — sense: {t.sense_note}"
        if t.distinct_from:
            line += f" — distinct from: {', '.join(t.distinct_from)}"
        return line

    lines: List[str] = []
    if locked:
        lines.append("**CRITICAL — LOCKED glossary (mandatory renderings; "
                     "a deviation IS a terminology defect):**")
        lines += [render(t) for t in locked]
    if preferred:
        if lines:
            lines.append("")
        lines.append("**Preferred renderings (NOT locked — the termbase's "
                     "current best guess, never ratified). Use them when "
                     "translating, but do NOT report a deviation as a "
                     "terminology defect and never call these 'locked':**")
        lines += [render(t) for t in preferred]
    lines += [
        "",
        "**Capitalization is NOT term identity.** A glossary lists the "
        "WORD; whether it is capitalized in a given sentence is decided by "
        "the style rules (CAP-*) from the surrounding context. Never file "
        "a terminology defect whose only complaint is upper/lower case — "
        "if the casing genuinely breaks a style rule, cite that rule "
        "instead.",
        "**A term may inflect.** Where an entry lists `forms`, any listed "
        "form satisfies it; a verb use of a term stored as a noun (\"used "
        "to craft\" for \"Crafting\") is correct, not a defect."]
    return "\n".join(lines)
