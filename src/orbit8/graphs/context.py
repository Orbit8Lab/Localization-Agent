"""Stage 2 — Context: two agents, map-parallel (design §2).

The Style branch and the Classify branch fan out from START and join at the
end of the graph — neither depends on the other.

Domain classification ships as a rules-based v0 (design §8): domain labels
for game strings are substantially predictable from key naming conventions
(`UI_`, `ITEM_`, `DLG_`, …), which delivers most of the routing value with
the LLM classifier upgrading precision later. Safety property: the
classifier fails EXPENSIVE — a low-confidence label routes to MTPE, never
away from it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import agents
from ..llm import Provider
from ..memory import RunDB
from ..schemas import (Domain, DomainLabelItem, DomainLabels, StyleBrief,
                       UniqueString)

# Key-prefix conventions → (domain, confidence).
KEY_RULES: List[Tuple[str, Domain, float]] = [
    (r"^(UI|BTN|MENU|HUD|OPT)[_.]", Domain.UI, 0.9),
    (r"^(DLG|DIALOG|STORY|CHAT|NPC)[_.]", Domain.DIALOGUE, 0.9),
    (r"^(MAP|LOC|ZONE|AREA)[_.]", Domain.MAP, 0.9),
    (r"^(ITEM|SKILL|WEAPON|DESC|CARD)[_.]", Domain.ITEM_DESC, 0.9),
    (r"^(SYS|ERR|MAIL|NOTIF|MSG)[_.]", Domain.SYSTEM, 0.9),
    (r"^(MKT|STORE|PROMO|AD)[_.]", Domain.MARKETING, 0.9),
]
FALLBACK = (Domain.UI, 0.5)   # below the MTPE threshold on purpose (§8)


def classify_rules_v0(uniques: List[UniqueString]) -> DomainLabels:
    items = []
    for unique in uniques:
        key = unique.keys[0] if unique.keys else ""
        domain, confidence = FALLBACK
        for pattern, rule_domain, rule_conf in KEY_RULES:
            if re.match(pattern, key, flags=re.I):
                domain, confidence = rule_domain, rule_conf
                break
        items.append(DomainLabelItem(key=unique.uid, domain=domain,
                                     confidence=confidence))
    return DomainLabels(items=items)


class ContextState(TypedDict, total=False):
    style_brief: Optional[dict]
    style_fingerprint: Optional[str]
    labels: Optional[dict]
    labels_fingerprint: Optional[str]


@dataclass
class ContextConfig:
    game: str
    source_lang: str
    target_locales: List[str]
    sample_size: int = 40
    llm_classifier: bool = False       # M6 upgrade path
    client_notes: Optional[str] = None
    dry_run: bool = False


def build_context_graph(provider: Provider, cfg: ContextConfig,
                        uniques: List[UniqueString]):
    def style(_: ContextState) -> dict:
        if cfg.dry_run:
            brief = StyleBrief(genre=["unknown"], sample_size=0,
                               per_locale_notes={l: "" for l in cfg.target_locales})
            return {"style_brief": brief.model_dump(mode="json"),
                    "style_fingerprint": None}
        samples = sorted((u.text for u in uniques), key=len)[:cfg.sample_size]
        brief, fingerprint = agents.analyze_style(
            provider, samples, game=cfg.game, source_lang=cfg.source_lang,
            target_locales=cfg.target_locales, client_notes=cfg.client_notes)
        return {"style_brief": brief.model_dump(mode="json"),
                "style_fingerprint": fingerprint}

    def classify(_: ContextState) -> dict:
        labels = classify_rules_v0(uniques)
        fingerprint = None
        if cfg.llm_classifier and not cfg.dry_run:
            llm_labels, fingerprint = agents.classify_domains(
                provider, [(u.uid, u.text) for u in uniques])
            labels = llm_labels
        return {"labels": labels.model_dump(mode="json"),
                "labels_fingerprint": fingerprint}

    graph = StateGraph(ContextState)
    graph.add_node("style", style)
    graph.add_node("classify", classify)
    graph.add_edge(START, "style")       # parallel branches:
    graph.add_edge(START, "classify")    # neither depends on the other
    graph.add_edge("style", END)
    graph.add_edge("classify", END)
    return graph


def run_context_stage(provider: Provider, cfg: ContextConfig,
                      uniques: List[UniqueString], run_db: RunDB
                      ) -> Tuple[StyleBrief, DomainLabels, Dict[str, Optional[str]]]:
    compiled = build_context_graph(provider, cfg, uniques).compile()
    state = compiled.invoke({})
    brief = StyleBrief.model_validate(state["style_brief"])
    labels = DomainLabels.model_validate(state["labels"])
    for item in labels.items:           # labels land in the run DB, not state
        run_db.label(item.key, item.domain, item.confidence)
    return brief, labels, {"style": state.get("style_fingerprint"),
                           "classify": state.get("labels_fingerprint")}
