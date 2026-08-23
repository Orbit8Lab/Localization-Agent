"""Stage 3 — Localization Asset: Terminologist extraction + deterministic
health check, ending at gate G1 (asset lock).

Linear two-step pipeline, so a plain stage function — not a LangGraph graph
(same §2 reasoning as ingest: no control loop, no checkpoint value).

The Terminologist's output is STAGED (glossary_delta artifact + review
sheet); nothing enters the locked T1 glossary until a human approves G1.
After G1 the glossary file is frozen — no code path in this package writes
to it again (design §7).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .. import agents
from ..gate_checks import term_in_text
from ..llm import Provider
from ..schemas import (GlossaryDelta, HealthIssue, HealthReport,
                       UniqueString)


@dataclass
class AssetConfig:
    game: str
    source_lang: str
    target_locales: List[str]
    extract_batch: int = 30
    dry_run: bool = False


def run_asset_stage(provider: Provider, cfg: AssetConfig,
                    uniques: List[UniqueString],
                    known_terms: Optional[List[str]] = None
                    ) -> Tuple[GlossaryDelta, Optional[str]]:
    """Terminologist extraction over the unique strings, batched ~30."""
    if cfg.dry_run:
        return GlossaryDelta(), None
    merged = GlossaryDelta()
    fingerprint: Optional[str] = None
    texts = [u.text for u in uniques]
    seen = set(known_terms or [])
    for start in range(0, len(texts), cfg.extract_batch):
        delta, fingerprint = agents.extract_terms(
            provider, texts[start:start + cfg.extract_batch], game=cfg.game,
            source_lang=cfg.source_lang, target_locales=cfg.target_locales,
            known_terms=sorted(seen))
        for proposal in delta.new_terms:
            if proposal.term not in seen:
                seen.add(proposal.term)
                merged.new_terms.append(proposal)
        merged.conflicts.extend(delta.conflicts)
    return merged, fingerprint


def health_check(t1_terms: Dict[str, dict], locale: str,
                 corpus: List[str]) -> HealthReport:
    """Deterministic glossary health (blockers stop G1; agent sweeps may
    only ever add warnings)."""
    blockers: List[HealthIssue] = []
    warnings: List[HealthIssue] = []

    rendering_of: Dict[str, str] = {}
    for term, entry in t1_terms.items():
        translation = (entry.get("translation") or "").strip()
        if not translation:
            blockers.append(HealthIssue(
                check="empty_translation", term=term,
                message=f"term {term!r} has no {locale} rendering"))
            continue
        if translation in rendering_of.values():
            other = next(t for t, r in rendering_of.items()
                         if r == translation)
            warnings.append(HealthIssue(
                check="collision", term=term,
                message=f"{term!r} and {other!r} share the rendering "
                        f"{translation!r} — confusable in-game"))
        rendering_of[term] = translation

    hits = sum(1 for term in t1_terms
               if any(term_in_text(term, text) for text in corpus))
    for term in t1_terms:
        if corpus and not any(term_in_text(term, text) for text in corpus):
            warnings.append(HealthIssue(
                check="zero_coverage", term=term,
                message=f"{term!r} never occurs in the source corpus"))

    return HealthReport(
        blockers=blockers, warnings=warnings,
        stats={"terms": float(len(t1_terms)),
               "coverage_pct": round(100 * hits / len(t1_terms), 1)
               if t1_terms else 0.0})


def build_t1_from_delta(delta: GlossaryDelta, locale: str) -> Dict[str, dict]:
    """Assemble the staged T1 layer for one locale from extraction output.
    This is the artifact a human reviews and locks at G1."""
    terms: Dict[str, dict] = {}
    for proposal in delta.new_terms:
        rendering = proposal.proposed.get(locale, "")
        terms[proposal.term] = {
            "translation": rendering,
            "type": proposal.type,
            "context_sample": proposal.context_sample,
        }
    return terms
