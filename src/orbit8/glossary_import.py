"""Glossary import: turn human-locked review files (any format) into the
pipeline glossary artifacts.

Division of labor (the same two-wall pattern as ingest):

- **Agentic, sandboxed**: converting whatever the team sends (review .docx,
  locked .xlsx sheet, csv, …) into validated `ReviewRow`s — the
  Adapter-Writer generates the parser, the sandbox runs it, only
  schema-validated stdout crosses back.
- **Deterministic, in code**: the MERGE. Which suggestion wins, what gets
  flagged, what lands in the audit note — these are rules, not judgment
  calls (glossary is law; skill docs glossary-management steps 3/4/7):
    · base rows (have a final translation + keep flag): keep!=N enters
    · change rows (current→suggested): first alternative wins;
      "(verify)"-marked suggestions do NOT change the translation — they
      keep the current rendering and are flagged needs_review
    · every applied change appends a dated audit note with the rationale
    · change rows whose term is missing from the base are reported as
      conflicts, never invented into the glossary

Emitted artifacts:
    t1.<locale>.staged.json        # the Orbit8 job's staged T1 (G1 locks it)
    glossary_<locale>_terms.json   # locpipe RAG format, for LQA / MT
    glossary_<locale>_terms.csv    # reviewable flat sheet
"""
from __future__ import annotations

import csv as csv_mod
import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import Field

from .codegen import AdapterRecord, generate_converter, run_converter
from .llm import Provider
from .schemas import Strict

REVIEW_SYSTEM = (
    "You write small Python converters that extract GLOSSARY REVIEW ROWS "
    "from localization review files. Rules:\n"
    "- Python 3 STDLIB ONLY (no pip; docx/xlsx are zip archives — use "
    "zipfile + xml.etree.ElementTree; xlsx cell values of type s index "
    "into xl/sharedStrings.xml).\n"
    "- The script receives the input file path as sys.argv[1].\n"
    "- Print to STDOUT one JSON array of row objects with these fields "
    "(null when absent):\n"
    '  {"term": "<source-language term>", "final": "<definitive '
    'translation, from columns like translation/translation_zh>", '
    '"current": "<current translation in a change-request table>", '
    '"suggested": "<suggested replacement, verbatim incl. any '
    'alternatives and (verify) markers>", "type": "<term type>", '
    '"count": <number>, "keep": "<Y/N flag>", "note": "<rationale/'
    'comment>"}\n'
    "- Review TABLES map by column MEANING (header names vary). A locked "
    "term sheet fills term/final/type/count/keep; a change-request table "
    "fills term/current/suggested/note.\n"
    "- Skip header rows and empty rows. Preserve cell text verbatim — "
    "never resolve alternatives or strip markers like (verify).\n"
    "- Read with encoding='utf-8', errors='replace'.\n"
    "Output ONLY the Python code. No prose, no markdown fences."
)

_HEADER_TERMS = {"term", "term (en)", "term_en", "术语", "词条"}


class ReviewRow(Strict):
    term: str
    final: Optional[str] = None
    current: Optional[str] = None
    suggested: Optional[str] = None
    type: Optional[str] = None
    count: Optional[float] = None
    keep: Optional[str] = None
    note: Optional[str] = None


class GlossaryImportReport(Strict):
    locale: str
    base_terms: int
    changes_applied: int
    needs_review: int
    dropped_keep_n: int
    conflicts: List[str] = Field(default_factory=list)
    outputs: Dict[str, str] = Field(default_factory=dict)


def validate_review_stdout(stdout: str, file_ref: str) -> List[ReviewRow]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as err:
        raise ValueError(f"stdout is not valid JSON: {err}") from err
    if not isinstance(data, list) or not data:
        raise ValueError("expected a non-empty JSON array of review rows")
    rows: List[ReviewRow] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("term"), str):
            raise ValueError(f"item {i} has no string 'term': "
                             f"{str(item)[:120]}")
        if item["term"].strip().lower() in _HEADER_TERMS:
            continue                       # tolerated: repeated header rows
        try:
            rows.append(ReviewRow.model_validate(item))
        except Exception as err:
            raise ValueError(f"item {i} invalid: {err}") from err
    if not rows:
        raise ValueError("no data rows after dropping headers")
    if not any(r.final or r.suggested for r in rows):
        raise ValueError("rows carry neither final translations nor "
                         "suggestions — column mapping is likely wrong")
    return rows


# ------------------------------------------------------------------ merge

def _first_alternative(suggested: str) -> str:
    head = suggested.split("/")[0]
    return re.sub(r"[（(][^()（）]*[)）]", "", head).strip()


def merge_review(rows: List[ReviewRow], *, today: Optional[str] = None
                 ) -> Tuple[Dict[str, dict], GlossaryImportReport]:
    """Deterministic merge — the rules ARE the review policy."""
    stamp = today or date.today().isoformat()
    base: Dict[str, dict] = {}
    dropped = 0
    # Locked-sheet guard: when ANY row carries a keep decision, rows
    # WITHOUT one come from undecided sheets (raw extraction, candidate
    # lists) and must not enter the locked glossary (skill step 2: only
    # reviewed rows survive; "prefer a _lock sheet").
    has_keep = any(r.keep is not None for r in rows)
    for row in rows:
        if row.final is None:
            continue
        if has_keep and row.keep is None:
            continue
        if row.keep and not row.keep.strip().upper().startswith("Y"):
            dropped += 1
            continue
        base[row.term.strip()] = {
            "translation": row.final.strip(),
            "type": (row.type or "other").strip(),
            "count": int(row.count) if row.count else 0,
            "confidence": "reviewed_locked",
            "comment": (row.note or "").strip() or None,
        }

    index = {term.lower(): term for term in base}
    applied = flagged = 0
    conflicts: List[str] = []
    for row in rows:
        if row.suggested is None:
            continue
        term = row.term.strip()
        # exact match first; then with the qualifier parenthetical dropped
        # ("Note (mechanic)" → "Note")
        key = index.get(term.lower()) or index.get(
            re.sub(r"\s*[（(][^()（）]*[)）]\s*$", "", term).strip().lower())
        if key is None:
            conflicts.append(
                f"{row.term}: change request has no base entry "
                f"(suggested {row.suggested!r})")
            continue
        entry = base[key]
        rationale = (row.note or "").strip()
        if "verify" in row.suggested.lower():
            entry["confidence"] = "needs_review"
            entry["comment"] = (f"{stamp} review: VERIFY — suggested "
                                f"{row.suggested!r}. {rationale}").strip()
            flagged += 1
            continue
        new = _first_alternative(row.suggested)
        if not new:
            conflicts.append(f"{row.term}: unparsable suggestion "
                             f"{row.suggested!r}")
            continue
        old = entry["translation"]
        entry["translation"] = new
        alternatives = [a.strip() for a in row.suggested.split("/")[1:]
                        if a.strip()]
        if alternatives:
            entry["alternatives"] = alternatives
        entry["comment"] = (f"{stamp} review: {old} → {new}. "
                            f"{rationale}").strip()
        applied += 1

    report = GlossaryImportReport(
        locale="", base_terms=len(base), changes_applied=applied,
        needs_review=flagged, dropped_keep_n=dropped, conflicts=conflicts)
    return base, report


# ------------------------------------------------------------------- emit

def emit_rag_json(terms: Dict[str, dict], path: Path, *, game: str,
                  locale: str, source_lang: str,
                  language_name: str = "Chinese") -> None:
    """locpipe RAG format (skill glossary-management step 3) — what LQA/MT
    reads with enable_rag."""
    payload = {
        "metadata": {"language": language_name, "locale": locale,
                     "game": game, "source_lang": source_lang,
                     "extraction_method": "reviewed_localization_asset",
                     "locked_date": date.today().isoformat(),
                     "total_terms": len(terms), "auto_generated": False},
        "terms": {term: {k: v for k, v in entry.items() if v is not None}
                  for term, entry in sorted(terms.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def emit_csv(terms: Dict[str, dict], path: Path, locale: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv_mod.writer(fh)
        writer.writerow(["term_en", "type", "count",
                         f"translation_{locale}", "confidence",
                         "alternatives", "comment"])
        for term, entry in sorted(terms.items()):
            writer.writerow([
                term, entry.get("type", "other"), entry.get("count", 0),
                entry["translation"], entry.get("confidence", ""),
                " / ".join(entry.get("alternatives", [])),
                entry.get("comment") or ""])


def emit_staged_t1(terms: Dict[str, dict], path: Path) -> None:
    """The Orbit8 job's staged T1 (glossary.py Glossary.from_layers shape);
    needs_review entries carry their flag into the sense_note so G1
    reviewers see it."""
    t1 = {}
    for term, entry in terms.items():
        t1[term] = {"translation": entry["translation"],
                    "type": entry.get("type", "other")}
        if entry.get("confidence") == "needs_review":
            t1[term]["sense_note"] = entry.get("comment", "needs review")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"terms": t1}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


# ------------------------------------------------------------ orchestrate

def import_review_files(provider: Provider, files: List[Path], *,
                        adapter_cache=None) -> Tuple[List[ReviewRow],
                                                     List[AdapterRecord]]:
    """Convert each review file through a (cached or generated) sandboxed
    converter; concatenate validated rows. `adapter_cache` maps a cache key
    to a stored script: (get(key) -> script|None, put(key, record, fp))."""
    rows: List[ReviewRow] = []
    records: List[AdapterRecord] = []
    for path in files:
        key = f"review{Path(path).suffix.replace('.', '_')}"
        script = adapter_cache.get(key) if adapter_cache else None
        if script:
            raw_rows = run_converter(script, path, validate_review_stdout)
        else:
            record, raw_rows, fingerprint = generate_converter(
                provider, path, system_prompt=REVIEW_SYSTEM,
                validate=validate_review_stdout)
            records.append(record)
            if adapter_cache:
                adapter_cache.put(key, record, fingerprint)
        rows.extend(raw_rows)
    return rows, records
