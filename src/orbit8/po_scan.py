"""Standalone LQA scan of a bilingual .po (docs/STANDARDS.md §4.2).

The file-in / file-out path: a translated .po plus the project's active
glossary go in; a client bug report and an LQA PE form come out. No job
pipeline required — but the SAME tier cascade runs (T1 mechanical → T2
project consistency → T3 LLM semantic → second-layer verify), with the
same tier stamps, cascade ledger and ``verify_cascade`` guard, so a
standalone report is exactly as auditable as a stage-5 one.

Beyond the per-string cascade this adds the check only a whole-file view
can make: the same source shipped with DIFFERENT renderings (the defect
class the PE round-trip creates when reviewers decide duplicate
locations independently).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .classify import classify_deterministic
from .exports import read_po_entries
from .gate_checks import GateConfig
from .glossary import Glossary
from .graphs.lqa import LQAConfig, LQAContext, run_lqa_stage
from .memory import RunDB
from .pe_form import emit_pe_form
from .po_translate import _string_type
from .schemas import LQAReport, Severity, UniqueString


@dataclass
class ScanResult:
    report: LQAReport
    inconsistent: List[dict] = field(default_factory=list)
    suggestions: Dict[str, str] = field(default_factory=dict)
    # proposed fixes that still broke a rule after one retry — dropped
    # rather than shipped, but reported so the gap is visible
    rejected_suggestions: List[dict] = field(default_factory=list)
    bug_rows: int = 0
    pe_rows: int = 0
    outputs: Dict[str, str] = field(default_factory=dict)


def _seed(db: RunDB, entries: List[Tuple[str, str, str, str]]
          ) -> Tuple[Dict[str, dict], List[dict]]:
    """Dedup by source text (ingest's contract). Returns the uid map and
    the inconsistency records: one source, several renderings."""
    by_text: Dict[str, dict] = {}
    renderings: Dict[str, Dict[str, List[str]]] = defaultdict(
        lambda: defaultdict(list))
    for key, source, target, location in entries:
        if not source.strip() or not target.strip():
            continue
        entry = by_text.setdefault(source, {"keys": [], "target": target,
                                            "locations": []})
        entry["keys"].append(key)
        if location:
            entry["locations"].append(location)
        renderings[source][target].append(key)

    # ``context`` carries the SourceLocation (the ``#:`` path), not the
    # msgctxt GUID that ``keys`` holds: the widget class — and so the
    # display-width budget — is derivable only from the path.
    uniques = [UniqueString(uid=f"u{i:04d}", text=text, keys=e["keys"],
                            context=(e["locations"][0]
                                     if e["locations"] else None))
               for i, (text, e) in enumerate(by_text.items())]
    db.seed(uniques)
    uid_map: Dict[str, dict] = {}
    for unique in uniques:
        entry = by_text[unique.text]
        # classification decides which style rules apply and how the
        # string is batched — deterministic signals only here (the #:
        # path); an LLM pass can refine it via classify.classify_batch.
        label = classify_deterministic(
            entry["keys"][0] if entry["keys"] else "",
            entry["locations"][0] if entry["locations"] else "")
        db.record(unique.uid, status="accepted", target=entry["target"],
                  resolution="external")
        db.label(unique.uid, label.domain, label.confidence)
        uid_map[unique.uid] = {**entry, "source": unique.text,
                               "domain": label.domain.value,
                               "domain_source": label.source}

    inconsistent = [
        {"source": source,
         "renderings": [{"target": target, "keys": keys}
                        for target, keys in sorted(variants.items())]}
        for source, variants in renderings.items() if len(variants) > 1]
    return uid_map, inconsistent


def scan_po(po_path: Path, glossary_path: Optional[Path], out_dir: Path, *,
            provider=None, game: str, locale: str = "en",
            source_lang: str = "zh-CN", job_id: str = "standalone-scan",
            deterministic_only: bool = False, batch_story: int = 5,
            batch_string: int = 20, suggestions: bool = True,
            style_brief=None, style_guide=None,
            on_progress=None) -> ScanResult:
    """Run the full cascade over a bilingual .po and write the review
    package. ``glossary_path`` is a T1 file (locked terms + family rules
    become hard gate constraints)."""
    from .bug_report import (build_suggestions, write_bug_report_xlsx,
                             write_tech_summary)

    po_path, out_dir = Path(po_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = [(k, zh, en, loc)
               for k, zh, en, loc in read_po_entries(po_path) if k]

    glossary = None
    locked: Dict[str, str] = {}
    variants: Dict[str, List[str]] = {}
    forms: Dict[str, Dict[str, str]] = {}
    term_case: Dict[str, str] = {}
    if glossary_path:
        t1 = Glossary.load_t1_file(Path(glossary_path))
        glossary = Glossary.from_layers(
            t1.get("metadata", {}).get("game") or game, locale, t1=t1)
        locked = {zh: entry["translation"]
                  for zh, entry in t1.get("terms", {}).items()
                  if entry.get("locked")}
        # operator-approved alternate renderings, per term
        variants = {zh: list(entry["variants"])
                    for zh, entry in t1.get("terms", {}).items()
                    if entry.get("variants")}
        # declared part-of-speech forms (合成 = craft / crafting) and the
        # per-term casing policy — both are glossary DECISIONS
        forms = {zh: dict(entry["forms"])
                 for zh, entry in t1.get("terms", {}).items()
                 if entry.get("forms")}
        term_case = {zh: entry["case"]
                     for zh, entry in t1.get("terms", {}).items()
                     if entry.get("case")}
        for zh, rule in (t1.get("metadata", {}).get("family_rules")
                         or {}).items():
            locked.setdefault(zh, rule["translation"])

    db = RunDB(out_dir / "scan.db")
    uid_map, inconsistent = _seed(db, entries)

    if style_guide is None:
        from .project_paths import resolve_style_guide
        style_guide, style_notes = resolve_style_guide(
            source_lang, locale, start=po_path)
        for note in style_notes:
            if on_progress:
                on_progress("style_guide", {"note": note})

    cfg = LQAConfig(
        game=game, source_lang=source_lang, locale=locale,
        batch_size=batch_string, batch_size_story=batch_story,
        deterministic_only=deterministic_only or provider is None,
        requeue=False,
        gate=GateConfig(source_lang=source_lang, target_lang=locale,
                        locked_terms=locked, term_variants=variants,
                        term_forms=forms, term_case=term_case,
                        style_guide=style_guide))
    ctx = LQAContext(provider=provider, cfg=cfg, run_db=db,
                     glossary=glossary, style_brief=style_brief,
                     style_guide=style_guide, on_progress=on_progress)
    report = run_lqa_stage(ctx, job_id)      # raises on cascade violation

    fixes: Dict[str, str] = {}
    rejected_fixes: List[dict] = []
    if suggestions and provider is not None:
        # suggestions are held to the delivery standard: same glossary +
        # style rules, verified by the same gate that judged the original
        fixes = build_suggestions(
            provider, report.items, game=game, source_lang=source_lang,
            locale=locale, glossary=glossary, style_brief=style_brief,
            style_guide=style_guide, gate_cfg=cfg.gate,
            domains={uid: info.get("domain")
                     for uid, info in uid_map.items()},
            rejected=rejected_fixes)
        if rejected_fixes and on_progress:
            on_progress("suggestions_rejected",
                        {"count": len(rejected_fixes)})

    locations = {uid: (info["locations"][0] if info["locations"]
                       else (info["keys"][0] if info["keys"] else ""))
                 for uid, info in uid_map.items()}
    bug_path = out_dir / f"bug_report_{locale}.xlsx"
    bug_rows = write_bug_report_xlsx(report, bug_path, suggestions=fixes,
                                     game=game, locations=locations)
    write_tech_summary(report, out_dir / f"tech_summary_{locale}.md",
                       game=game, suggestions_count=len(fixes))

    # LQA PE form: one row per FLAGGED string (§4.2), worst severity first
    order = {Severity.HIGH.value: 0, Severity.MEDIUM.value: 1,
             Severity.LOW.value: 2}
    rows: List[Dict[str, str]] = []
    for item in sorted(
            report.items,
            key=lambda i: min((order.get(vf.finding.severity.value, 3)
                               for vf in i.findings), default=3)):
        info = uid_map.get(item.uid, {})
        notes = "; ".join(
            f"{vf.finding.bug_type.value}/{vf.finding.severity.value}"
            f" T{vf.finding.tier or '?'}: {vf.finding.message}"
            for vf in item.findings)
        rows.append({
            "StringID": (info.get("keys") or [item.uid])[0],
            "StringType": _string_type(
                (info.get("locations") or [""])[0]),
            "Source": item.source,
            "Target_Original": item.target,
            "Target_Suggested": fixes.get(item.uid, ""),
            # agent-authored context column: WHY this row is here. PE_Note
            # stays empty — that column belongs to the post-editor.
            "Findings": notes[:500]})
    pe_path = out_dir / f"lqa_pe_form_{locale}.xlsx"
    emit_pe_form(pe_path, "lqa", rows, extra_columns=("Findings",))

    if inconsistent:
        incons_path = out_dir / "inconsistent_renderings.json"
        incons_path.write_text(
            json.dumps(inconsistent, ensure_ascii=False, indent=1),
            encoding="utf-8")

    if rejected_fixes:
        (out_dir / "rejected_suggestions.json").write_text(
            json.dumps(rejected_fixes, ensure_ascii=False, indent=1),
            encoding="utf-8")
    result = ScanResult(
        report=report, inconsistent=inconsistent, suggestions=fixes,
        rejected_suggestions=rejected_fixes,
        bug_rows=bug_rows, pe_rows=len(rows),
        outputs={"bug_report": str(bug_path), "pe_form": str(pe_path),
                 "tech_summary": str(
                     out_dir / f"tech_summary_{locale}.md")})
    (out_dir / "scan_report.json").write_text(json.dumps(
        {"po": str(po_path), "glossary": str(glossary_path or ""),
         "entries": len(entries), "checked": report.checked,
         "flagged_strings": report.flagged_strings,
         "findings": report.findings_total,
         "by_severity": report.by_severity,
         "cascade_ledger": report.cascade_ledger,
         "inconsistent_sources": len(inconsistent),
         "suggestions": len(fixes),
         "suggestions_rejected": len(rejected_fixes),
         "bug_rows": bug_rows, "pe_rows": len(rows)},
        ensure_ascii=False, indent=1), encoding="utf-8")
    return result
