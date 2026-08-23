"""Stage 1 — Ingest. 100% deterministic, therefore NOT a LangGraph graph
(design §2: resist the urge to wrap everything uniformly; a checkpointer
round-trip on a pure transform buys nothing).

All formats funnel into SourceString records; dedup assigns stable synthetic
uids (`u0000`, …). The wordcount in the report is load-bearing: it drives
quoting at G0 revisions and tester-hour allocation in Stage 6.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

from .schemas import IngestReport, SourceString, UniqueString


def ingest_json(path: Path) -> List[SourceString]:
    """Flat JSON object mapping key -> source text."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not all(
            isinstance(v, str) for v in data.values()):
        raise ValueError(f"{path}: expected a flat JSON object of strings")
    return [SourceString(key=k, text=v, file_ref=str(path))
            for k, v in data.items()]


def ingest_po(path: Path) -> List[SourceString]:
    """Minimal gettext .po reader (Unreal export style): msgctxt is the key,
    msgid the source text, and the ``#:`` reference comment (the UE
    widget/asset path) rides along as context — bug reports need it to
    point a dev at the actual widget, not an opaque GUID.
    Continuation-line strings are concatenated."""
    records: List[SourceString] = []
    key, msgid, mode = None, [], None
    location, next_location = None, None
    def flush():
        nonlocal key, msgid, mode, location
        text = "".join(msgid)
        if text:
            records.append(SourceString(
                key=key or f"po:{len(records)}", text=_po_unescape(text),
                context=location, file_ref=str(path)))
        key, msgid, mode, location = None, [], None, None

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#:"):
            next_location = line[2:].strip() or None
        elif line.startswith("msgctxt "):
            flush()
            key, mode = _po_unescape(line[8:].strip().strip('"')), "ctxt"
            location, next_location = next_location, None
        elif line.startswith("msgid "):
            msgid, mode = [line[6:].strip().strip('"')], "id"
            if key is None:            # entry without msgctxt
                location, next_location = next_location, None
        elif line.startswith("msgstr "):
            mode = "str"
        elif line.startswith('"') and mode == "id":
            msgid.append(line.strip('"'))
    flush()
    return records


def _po_unescape(text: str) -> str:
    return (text.replace("\\r", "\r").replace("\\n", "\n")
            .replace("\\t", "\t").replace('\\"', '"')
            .replace("\\\\", "\\"))


ADAPTERS = {".json": ingest_json, ".po": ingest_po}


def ingest_any(path: Path, fallback=None) -> List[SourceString]:
    """`fallback(path) -> List[SourceString]` handles suffixes outside
    ADAPTERS — the Controller passes the sandboxed adapter-writer here
    (codegen.py). Built-in formats never touch the LLM."""
    suffix = Path(path).suffix.lower()
    if suffix in ADAPTERS:
        return ADAPTERS[suffix](Path(path))
    if fallback is not None:
        return fallback(Path(path))
    raise ValueError(f"unsupported source format {suffix!r} "
                     f"(supported: {sorted(ADAPTERS)})")


def dedup(records: List[SourceString]) -> List[UniqueString]:
    """Stable synthetic uids; fan-out to game keys only at emission."""
    by_text: Dict[str, List[str]] = {}
    context: Dict[str, str] = {}
    for record in records:
        by_text.setdefault(record.text, []).append(record.key)
        if record.context and record.text not in context:
            context[record.text] = record.context
    return [UniqueString(uid=f"u{i:04d}", text=text, keys=keys,
                         context=context.get(text))
            for i, (text, keys) in enumerate(by_text.items())]


def run_ingest(source_files: List[Path], fallback=None
               ) -> Tuple[List[SourceString], List[UniqueString],
                          IngestReport]:
    records: List[SourceString] = []
    per_file: Dict[str, int] = {}
    for path in source_files:
        found = ingest_any(path, fallback=fallback)
        records.extend(found)
        per_file[str(path)] = len(found)
    if not records:
        raise ValueError("no records ingested")
    uniques = dedup(records)
    report = IngestReport(
        total_records=len(records),
        unique_strings=len(uniques),
        total_chars=sum(len(u.text) for u in uniques),
        dedup_ratio=round(1 - len(uniques) / len(records), 4),
        per_file=per_file)
    return records, uniques, report
