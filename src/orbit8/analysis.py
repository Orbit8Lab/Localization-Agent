"""Corpus text analysis: strings, words, and the story/instruction split.

Deterministic counting; classification is REUSED from run DBs where the
Domain Classifier already labeled these exact source texts (zero LLM
cost), with an optional LLM pass for still-unlabeled strings.

Counting rules (localization-industry quoting conventions):
- Latin/ASCII words: whitespace-delimited tokens.
- CJK: one character = one word (standard for zh/ja quoting).
- Placeholders/markup are counted separately, not as words.
- Word totals are reported on BOTH bases: all records (what the file
  ships) and unique strings (what actually gets translated — dedup is
  the quoting basis).

Rollup (docs/skills/lqa-batch-split.md taxonomy):
- story lines  = dialogue + marketing
- instructions = ui + system (labels, controls, system messages)
- other        = map + item_desc + unlabeled
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import Field

from .gate_checks import MARKUP_PATTERN, PLACEHOLDER_PATTERNS
from .ingest import dedup
from .llm import Provider
from .schemas import SourceString, Strict

WORD_RE = re.compile(r"[A-Za-z0-9']+")
CJK_RE = re.compile(r"[一-鿿㐀-䶿぀-ヿ가-힯]")
_STRIP_RE = re.compile("|".join(PLACEHOLDER_PATTERNS + [MARKUP_PATTERN]))

STORY_DOMAINS = {"dialogue", "marketing"}
INSTRUCTION_DOMAINS = {"ui", "system"}


class CorpusReport(Strict):
    files: List[str] = Field(default_factory=list)
    total_strings: int = 0            # records as shipped (incl. dupes)
    unique_strings: int = 0           # dedup — the translation basis
    words_all_records: int = 0
    words_unique: int = 0             # quoting basis
    chars_unique: int = 0
    placeholders: int = 0
    avg_words_per_string: float = 0.0
    by_domain: Dict[str, int] = Field(default_factory=dict)
    story_lines: int = 0
    instructions: int = 0
    other: int = 0
    unlabeled: int = 0


def count_words(text: str) -> int:
    stripped = _STRIP_RE.sub(" ", text)
    return len(WORD_RE.findall(stripped)) + len(CJK_RE.findall(stripped))


def count_placeholders(text: str) -> int:
    return len(_STRIP_RE.findall(text))


def labels_from_run_dbs(runs_dir: Path) -> Dict[str, str]:
    """Harvest content classifications from every run DB in the job —
    source text -> domain. Confident labels win over rules fallbacks."""
    from .memory import RunDB
    best: Dict[str, tuple] = {}       # text -> (confidence, domain)
    if not runs_dir.exists():
        return {}
    for db_path in sorted(runs_dir.glob("*.db")):
        for row in RunDB(db_path).all_segments():
            confidence = row["confidence"] or 0.0
            if (row["text"] not in best
                    or confidence > best[row["text"]][0]):
                best[row["text"]] = (confidence, row["domain"])
    return {text: domain for text, (conf, domain) in best.items()
            if conf >= 0.6}


def analyze_corpus(records: List[SourceString], *,
                   labels: Optional[Dict[str, str]] = None,
                   provider: Optional[Provider] = None,
                   classify_batch: int = 40) -> CorpusReport:
    uniques = dedup(records)
    labels = dict(labels or {})

    # Optional LLM pass for texts no run DB has labeled yet.
    unlabeled = [u for u in uniques if u.text not in labels]
    if provider is not None and unlabeled:
        from . import agents
        for start in range(0, len(unlabeled), classify_batch):
            batch = unlabeled[start:start + classify_batch]
            got, _fp = agents.classify_domains(
                provider, [(u.uid, u.text) for u in batch])
            by_uid = {u.uid: u for u in batch}
            for item in got.items:
                labels[by_uid[item.key].text] = item.domain.value

    by_domain: Dict[str, int] = {}
    story = instructions = other = missing = 0
    for unique in uniques:
        domain = labels.get(unique.text)
        if domain is None:
            missing += 1
            other += 1
            continue
        by_domain[domain] = by_domain.get(domain, 0) + 1
        if domain in STORY_DOMAINS:
            story += 1
        elif domain in INSTRUCTION_DOMAINS:
            instructions += 1
        else:
            other += 1

    words_unique = sum(count_words(u.text) for u in uniques)
    return CorpusReport(
        files=sorted({r.file_ref for r in records if r.file_ref}),
        total_strings=len(records),
        unique_strings=len(uniques),
        words_all_records=sum(count_words(r.text) for r in records),
        words_unique=words_unique,
        chars_unique=sum(len(u.text) for u in uniques),
        placeholders=sum(count_placeholders(u.text) for u in uniques),
        avg_words_per_string=round(words_unique / max(1, len(uniques)), 1),
        by_domain=dict(sorted(by_domain.items())),
        story_lines=story, instructions=instructions, other=other,
        unlabeled=missing)
