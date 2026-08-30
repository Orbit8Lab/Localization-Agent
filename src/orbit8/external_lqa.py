"""External-translation LQA: audit translations we did NOT produce (a
developer's existing target text) through the same tier cascade.

Implements docs/skills/lqa-batch-split.md: pairs are content-classified
(keys are opaque GUIDs, so the rules classifier is useless here), split
into a story file and a pure-strings file, and Tier 3 reviews each class
with its own batch size (story n=5, strings n=20).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import agents
from .gate_checks import GateConfig
from .graphs.lqa import LQAConfig, LQAContext, run_lqa_stage
from .llm import Provider
from .memory import RunDB, TranslationMemory
from .schemas import (Domain, IntakeBrief, LQAReport, UniqueString)

# Skill: story class = narrative/persuasive domains (small batches).
STORY_DOMAINS = {Domain.DIALOGUE.value, Domain.MARKETING.value}
CLASSIFY_BATCH = 40


def load_pairs(path: Path) -> List[dict]:
    pairs = []
    for i, line in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        for field in ("key", "source_text", "target_text"):
            if not isinstance(row.get(field), str):
                raise ValueError(f"line {i + 1}: missing {field!r}")
        pairs.append(row)
    if not pairs:
        raise ValueError(f"no pairs in {path}")
    return pairs


def seed_audit_db(db: RunDB, pairs: List[dict]) -> None:
    """Dedup by source text (same contract as ingest); the dev's target
    rides in as the 'accepted' translation under audit."""
    by_text: Dict[str, dict] = {}
    for pair in pairs:
        entry = by_text.setdefault(
            pair["source_text"], {"keys": [], "target": pair["target_text"]})
        entry["keys"].append(pair["key"])
    uniques = [UniqueString(uid=f"u{i:04d}", text=text, keys=entry["keys"])
               for i, (text, entry) in enumerate(by_text.items())]
    db.seed(uniques)
    for unique in uniques:
        db.record(unique.uid, status="accepted",
                  target=by_text[unique.text]["target"],
                  resolution="external")


def classify_content(provider: Provider, db: RunDB) -> Dict[str, int]:
    """LLM content classification (skill: GUID keys ⇒ no prefix rules).
    Uncertainty prefers story — the smaller batch (fail expensive)."""
    rows = db.by_status("accepted")
    for start in range(0, len(rows), CLASSIFY_BATCH):
        batch = rows[start:start + CLASSIFY_BATCH]
        labels, _fp = agents.classify_domains(
            provider, [(r["uid"], r["text"]) for r in batch])
        for item in labels.items:
            db.label(item.key, item.domain, item.confidence)
    counts: Dict[str, int] = {}
    for row in db.by_status("accepted"):
        counts[row["domain"]] = counts.get(row["domain"], 0) + 1
    return counts


def split_files(db: RunDB, out_dir: Path, name: str, *, source_lang: str,
                target_lang: str) -> Tuple[Path, Path, Dict[str, int]]:
    """The two review artifacts (skill contract outputs 1+2)."""
    story_path = out_dir / f"split_story.{name}.jsonl"
    strings_path = out_dir / f"split_strings.{name}.jsonl"
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {"story": 0, "string": 0}
    with story_path.open("w", encoding="utf-8") as story_fh, \
            strings_path.open("w", encoding="utf-8") as strings_fh:
        for row in db.by_status("accepted"):
            is_story = row["domain"] in STORY_DOMAINS
            fh = story_fh if is_story else strings_fh
            counts["story" if is_story else "string"] += 1
            for key in row["keys"]:
                fh.write(json.dumps(
                    {"key": key, "source_language": source_lang,
                     "target_language": target_lang,
                     "source_text": row["text"],
                     "target_text": row["target"] or "",
                     "domain": row["domain"]}, ensure_ascii=False) + "\n")
    return story_path, strings_path, counts


def run_external_lqa(job, provider: Optional[Provider], pairs_path: Path, *,
                     name: str = "dev-audit", batch_story: int = 5,
                     batch_string: int = 20, t3_threshold: float = 0.75,
                     deterministic_only: bool = False) -> LQAReport:
    """Seed → classify → split → tier cascade → attempt-versioned report.
    `job` is a controller.Job; all artifact writes go through its store."""
    intake: IntakeBrief = job.store.read(0, "intake", IntakeBrief)
    locale = intake.target_locales[0]
    db = RunDB(job.store.run_db_path(f"lqa-{name}"))
    seed_audit_db(db, load_pairs(pairs_path))

    if provider is not None and not deterministic_only:
        classify_content(provider, db)

    attempt = job.store.new_attempt(5)
    out_dir = job.store.stage_dir(5, attempt)
    story_path, strings_path, split_counts = split_files(
        db, out_dir, name, source_lang=intake.source_lang,
        target_lang=locale)

    glossary = job._glossary(locale)
    cfg = LQAConfig(
        game=intake.game, source_lang=intake.source_lang, locale=locale,
        batch_size=batch_string, batch_size_story=batch_story,
        t3_confidence_threshold=t3_threshold,
        deterministic_only=deterministic_only or provider is None,
        client_lang=intake.client_lang,
        gate=GateConfig(source_lang=intake.source_lang, target_lang=locale,
                        locked_terms=(glossary.locked_map()
                                      if glossary else {})))
    ctx = LQAContext(provider=provider, cfg=cfg, run_db=db,
                     tm=TranslationMemory(job.store.tm_path()),
                     glossary=glossary, style_brief=job._style_or_none(),
                     tenant=job._tenant())
    report = run_lqa_stage(ctx, job.job_id)
    job.store.write(5, f"lqa_report.{name}", report,
                    produced_by="code:external-lqa@1", attempt=attempt)
    report_dict = {"story_file": str(story_path),
                   "strings_file": str(strings_path),
                   "split": split_counts}
    (out_dir / f"split_summary.{name}.json").write_text(
        json.dumps(report_dict, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return report
