"""String classification — one authoritative label per string.

Classification is not cosmetic bookkeeping: it selects which style rules
apply, how strings are batched, and which review policy a string takes.
Before this module the label was re-derived in three places with three
different rules (context key-prefixes, external_lqa's LLM pass,
pe_form's location heuristic); now there is ONE decision, persisted with
its evidence.

Precedence (highest first) — the same shape as every other decision in
this system: a human ruling outranks a machine inference, and every
label records HOW it was decided.

  1. human      — an operator correction; never overwritten by a re-run
  2. location   — the engine path says it (``/Game/UI/…`` → ui). UE
                  msgctxt keys are opaque GUIDs, but the ``#:`` comment
                  is a reliable structural signal
  3. key_rule   — a key naming convention (``DLG_``, ``SYS_``)
  4. llm        — semantic classification of the source text
  5. fallback   — UI at low confidence (below the MTPE threshold on
                  purpose: an unclassified string routes TO human review,
                  never away from it)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .schemas import Domain

# Engine path fragments → domain. Checked case-insensitively against the
# whole location string; longest/most specific first.
LOCATION_RULES: List[Tuple[str, Domain]] = [
    (r"/dialog|/dlg|/story|/quest|/npc|/conversation|/subtitle", Domain.DIALOGUE),
    (r"/item|/weapon|/armor|/equip|/skill|/ability|/buff|/card", Domain.ITEM_DESC),
    (r"/map|/level|/zone|/area|/region|/location", Domain.MAP),
    (r"/error|/system|/notif|/mail|/tutorial|/tip|/log", Domain.SYSTEM),
    (r"/store|/shop|/promo|/marketing|/steam|/ad\b", Domain.MARKETING),
    (r"/ui|/widget|/wdg|/hud|/menu|/button|/panel|/popup", Domain.UI),
]

KEY_RULES: List[Tuple[str, Domain]] = [
    (r"^(DLG|DIALOG|STORY|CHAT|NPC)[_.]", Domain.DIALOGUE),
    (r"^(MAP|LOC|ZONE|AREA)[_.]", Domain.MAP),
    (r"^(ITEM|SKILL|WEAPON|DESC|CARD)[_.]", Domain.ITEM_DESC),
    (r"^(SYS|ERR|MAIL|NOTIF|MSG)[_.]", Domain.SYSTEM),
    (r"^(MKT|STORE|PROMO|AD)[_.]", Domain.MARKETING),
    (r"^(UI|BTN|MENU|HUD)[_.]", Domain.UI),
]

CONFIDENCE = {"human": 1.0, "location": 0.9, "key_rule": 0.85,
              "llm": 0.75, "fallback": 0.5}

# Below this, a string is treated as unclassified for style selection and
# routes to human review (design §8: uncertainty routes TO MTPE).
MIN_TRUSTED_CONFIDENCE = 0.7


@dataclass
class Label:
    key: str
    domain: Domain
    source: str                 # human | location | key_rule | llm | fallback
    confidence: float
    evidence: str = ""

    @property
    def trusted(self) -> bool:
        return self.confidence >= MIN_TRUSTED_CONFIDENCE

    def to_dict(self) -> dict:
        return {"key": self.key, "domain": self.domain.value,
                "source": self.source, "confidence": self.confidence,
                "evidence": self.evidence}

    @classmethod
    def from_dict(cls, raw: dict) -> "Label":
        return cls(key=raw["key"], domain=Domain(raw["domain"]),
                   source=raw.get("source", "llm"),
                   confidence=float(raw.get("confidence", 0.5)),
                   evidence=raw.get("evidence", ""))


def classify_by_location(location: str) -> Optional[Tuple[Domain, str]]:
    if not location:
        return None
    low = location.lower()
    for pattern, domain in LOCATION_RULES:
        match = re.search(pattern, low)
        if match:
            return domain, f"path matches {match.group(0)!r}"
    return None


def classify_by_key(key: str) -> Optional[Tuple[Domain, str]]:
    if not key:
        return None
    stripped = key.lstrip(",")
    for pattern, domain in KEY_RULES:
        if re.match(pattern, stripped, flags=re.I):
            return domain, f"key prefix {stripped.split('_')[0]!r}"
    return None


def classify_deterministic(key: str, location: str = "") -> Label:
    """Everything decidable without an LLM. Returns a fallback label
    (low confidence) when nothing structural matches — the caller can
    then send only those strings to the LLM pass."""
    hit = classify_by_location(location)
    if hit:
        return Label(key=key, domain=hit[0], source="location",
                     confidence=CONFIDENCE["location"], evidence=hit[1])
    hit = classify_by_key(key)
    if hit:
        return Label(key=key, domain=hit[0], source="key_rule",
                     confidence=CONFIDENCE["key_rule"], evidence=hit[1])
    return Label(key=key, domain=Domain.UI, source="fallback",
                 confidence=CONFIDENCE["fallback"],
                 evidence="no structural signal")


def classify_batch(entries: Iterable[Tuple[str, str, str]], *,
                   provider=None, batch_size: int = 40,
                   on_progress=None) -> Dict[str, Label]:
    """Classify ``(key, source_text, location)`` triples. Deterministic
    signals win; only the leftovers cost an LLM call."""
    labels: Dict[str, Label] = {}
    needs_llm: List[Tuple[str, str]] = []
    for key, text, location in entries:
        label = classify_deterministic(key, location)
        labels[key] = label
        if label.source == "fallback" and text.strip():
            needs_llm.append((key, text))

    if provider is None or not needs_llm:
        return labels

    from . import agents
    for start in range(0, len(needs_llm), batch_size):
        chunk = needs_llm[start:start + batch_size]
        try:
            result, _fp = agents.classify_domains(provider, chunk)
        except Exception as err:          # a failed batch keeps fallbacks
            if on_progress:
                on_progress("classify_failed",
                            {"size": len(chunk), "error": str(err)[:150]})
            continue
        for item in result.items:
            if item.key in labels:
                labels[item.key] = Label(
                    key=item.key, domain=item.domain, source="llm",
                    confidence=min(CONFIDENCE["llm"], item.confidence),
                    evidence="semantic classification")
        if on_progress:
            on_progress("classify", {"size": len(chunk)})
    return labels


# ------------------------------------------------------------- persistence

class LabelStore:
    """Labels live beside the corpus as a project asset, not inside a run
    folder: they are reused by translation, LQA and reporting, and human
    corrections must survive every re-run."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.labels: Dict[str, Label] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.labels = {k: Label.from_dict(v)
                           for k, v in raw.get("labels", {}).items()}

    def merge(self, fresh: Dict[str, Label]) -> Dict[str, int]:
        """Add newly classified strings. A stored HUMAN label always wins
        — re-running the classifier must never silently undo a
        correction."""
        counts = {"added": 0, "updated": 0, "human_kept": 0}
        for key, label in fresh.items():
            existing = self.labels.get(key)
            if existing is None:
                self.labels[key] = label
                counts["added"] += 1
            elif existing.source == "human":
                counts["human_kept"] += 1
            elif (label.confidence > existing.confidence
                  or label.source != existing.source):
                self.labels[key] = label
                counts["updated"] += 1
        return counts

    def correct(self, key: str, domain: Domain, *, by: str = "operator",
                note: str = "") -> Label:
        label = Label(key=key, domain=domain, source="human",
                      confidence=CONFIDENCE["human"],
                      evidence=f"{by}{': ' + note if note else ''}")
        self.labels[key] = label
        return label

    def domain_of(self, key: str) -> Optional[Domain]:
        label = self.labels.get(key)
        return label.domain if label and label.trusted else None

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for label in self.labels.values():
            out[label.domain.value] = out.get(label.domain.value, 0) + 1
        return out

    def by_source(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for label in self.labels.values():
            out[label.source] = out.get(label.source, 0) + 1
        return out

    def untrusted(self) -> List[Label]:
        return [l for l in self.labels.values() if not l.trusted]

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"metadata": {"total": len(self.labels),
                          "by_domain": self.counts(),
                          "by_source": self.by_source(),
                          "untrusted": len(self.untrusted())},
             "labels": {k: v.to_dict()
                        for k, v in sorted(self.labels.items())}},
            ensure_ascii=False, indent=1), encoding="utf-8")
        return self.path
