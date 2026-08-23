"""Single-agent stage executors: S0 intake (Market Analyst), S6 testing
(Test-case Generator), S7 release (Marketing Writer + deliverables).

These are linear one-or-two-call pipelines with no control loop, so they are
plain stage functions — the same §2 reasoning that keeps ingest out of
LangGraph. If any of them grows a loop or fan-out, promote it to a graph.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import agents
from ..llm import Provider
from ..memory import RunDB
from ..schemas import (DeliverablesManifest, IntakeBrief, MarketAssessment,
                       MarketingKit, MarketReport, StyleBrief, TestPlan)


# ------------------------------------------------------------- S0 intake

def run_market_analysis(provider: Provider, intake: IntakeBrief,
                        dry_run: bool = False
                        ) -> Tuple[MarketReport, Optional[str]]:
    if dry_run:
        return MarketReport(
            assessments=[MarketAssessment(locale=loc,
                                          recommendation="dry-run stub")
                         for loc in intake.target_locales],
            summary="dry-run stub"), None
    return agents.analyze_market(provider, intake)


# ------------------------------------------------------------ S6 testing

def run_testing_stage(provider: Provider, *, game: str, locale: str,
                      run_db: RunDB, tester_hours: float,
                      style_brief: Optional[StyleBrief] = None,
                      dry_run: bool = False
                      ) -> Tuple[TestPlan, Optional[str]]:
    rows = run_db.by_status("accepted", "flagged", "mtpe")
    domain_counts: Dict[str, int] = {}
    for row in rows:
        domain_counts[row["domain"]] = domain_counts.get(row["domain"], 0) + 1
    if dry_run:
        return TestPlan(game=game, target_lang=locale,
                        tester_hours=tester_hours, cases=[],
                        coverage_note="dry-run stub"), None
    samples = [(r["uid"], r["text"], r["target"] or "") for r in rows[:40]]
    return agents.generate_test_cases(
        provider, game=game, target_lang=locale,
        domain_counts=domain_counts, tester_hours=tester_hours,
        samples=samples, style_brief=style_brief)


# ------------------------------------------------------------ S7 release

def run_marketing(provider: Provider, *, intake: IntakeBrief, locale: str,
                  run_db: RunDB, style_brief: Optional[StyleBrief] = None,
                  market_summary: Optional[str] = None,
                  dry_run: bool = False
                  ) -> Tuple[MarketingKit, Optional[str]]:
    if dry_run:
        return MarketingKit(game=intake.game, target_locale=locale,
                            key_messages=["dry-run stub"], store_copy=[]), None
    samples = [r["target"] for r in run_db.by_status("accepted")[:30]
               if r["target"]]
    return agents.write_marketing(
        provider, intake=intake, target_locale=locale,
        sample_strings=samples, style_brief=style_brief,
        market_summary=market_summary)


def emit_translations(run_db: RunDB, out_path: Path, *, source_lang: str,
                      locale: str) -> int:
    """Fan uids back out to game keys and emit LQA-compatible JSONL."""
    count = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in run_db.by_status("accepted", "flagged"):
            for game_key in row["keys"]:
                fh.write(json.dumps({
                    "key": game_key, "source_language": source_lang,
                    "target_language": locale, "source_text": row["text"],
                    "target_text": row["target"] or ""},
                    ensure_ascii=False) + "\n")
                count += 1
    return count


def build_manifest(job_id: str, locales: List[str],
                   files: Dict[str, str],
                   glossary_asset_version: int,
                   changelog: List[str]) -> DeliverablesManifest:
    return DeliverablesManifest(
        job_id=job_id, locales=locales, files=files,
        glossary_asset_version=glossary_asset_version, changelog=changelog)
