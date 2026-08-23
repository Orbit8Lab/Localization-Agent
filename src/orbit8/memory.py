"""Memory: three lifetimes, three stores (design §6).

| Store        | Scope            | Write rule                              |
|--------------|------------------|-----------------------------------------|
| Graph state  | one stage-run    | discarded on completion (langgraph)     |
| Job asset    | one job          | append-only; T1 glossary frozen at G1   |
| Tenant/genre | across jobs      | curated, NEVER auto-written             |

Everything is namespaced by (tenant_id, job_id): multi-tenant leakage in a
glossary is a client-losing bug.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .schemas import Domain, Finding, SegmentRef, UniqueString

# EchoProvider stubs must never poison the TM (dry-run guard, ported from
# locpipe/providers.py::is_dry_run_stub).
_STUB_RE = re.compile(r"^\[[a-z]{2,3}(-[A-Za-z]{2,4})?\]")


def is_dry_run_stub(text: str) -> bool:
    return bool(_STUB_RE.match(text))


# ------------------------------------------------------------------ run DB

class RunDB:
    """Per-(job, locale) segment store. Graph state holds SegmentRef IDs
    only — the text and per-string status live here (design §4)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS segments (
                uid TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                keys_json TEXT NOT NULL,
                context TEXT,
                domain TEXT NOT NULL DEFAULT 'ui',
                confidence REAL NOT NULL DEFAULT 1.0,
                status TEXT NOT NULL DEFAULT 'pending',
                target TEXT,
                resolution TEXT,
                findings_json TEXT NOT NULL DEFAULT '[]'
            )""")
        self.conn.commit()

    def seed(self, uniques: List[UniqueString]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO segments (uid, text, keys_json, context) "
            "VALUES (?, ?, ?, ?)",
            [(u.uid, u.text, json.dumps(u.keys, ensure_ascii=False), u.context)
             for u in uniques])
        self.conn.commit()

    def label(self, uid: str, domain: Domain, confidence: float = 1.0) -> None:
        self.conn.execute(
            "UPDATE segments SET domain = ?, confidence = ? WHERE uid = ?",
            (domain.value, confidence, uid))
        self.conn.commit()

    def record(self, uid: str, *, status: str, target: Optional[str] = None,
               resolution: Optional[str] = None,
               findings: Optional[List[Finding]] = None) -> None:
        self.conn.execute(
            "UPDATE segments SET status = ?, target = COALESCE(?, target), "
            "resolution = COALESCE(?, resolution), findings_json = ? WHERE uid = ?",
            (status, target, resolution,
             json.dumps([f.model_dump(mode="json") for f in (findings or [])],
                        ensure_ascii=False), uid))
        self.conn.commit()

    def get(self, uid: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT uid, text, keys_json, context, domain, confidence, status, "
            "target, resolution, findings_json FROM segments WHERE uid = ?",
            (uid,)).fetchone()
        return self._row(row) if row else None

    def by_status(self, *statuses: str) -> List[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT uid, text, keys_json, context, domain, confidence, status, "
            f"target, resolution, findings_json FROM segments "
            f"WHERE status IN ({marks}) ORDER BY uid", statuses).fetchall()
        return [self._row(r) for r in rows]

    def all_segments(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT uid, text, keys_json, context, domain, confidence, status, "
            "target, resolution, findings_json FROM segments ORDER BY uid").fetchall()
        return [self._row(r) for r in rows]

    def refs(self, *statuses: str) -> List[SegmentRef]:
        return [SegmentRef(uid=s["uid"], domain=Domain(s["domain"]))
                for s in (self.by_status(*statuses) if statuses
                          else self.all_segments())]

    def counts(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM segments GROUP BY status").fetchall()
        return {status: n for status, n in rows}

    @staticmethod
    def _row(row) -> dict:
        return {
            "uid": row[0], "text": row[1], "keys": json.loads(row[2]),
            "context": row[3], "domain": row[4], "confidence": row[5],
            "status": row[6], "target": row[7], "resolution": row[8],
            "findings": [Finding.model_validate(f) for f in json.loads(row[9])],
        }


# ---------------------------------------------------------------------- TM

class TranslationMemory:
    """Job-scoped, append-only. Human-confirmed MTPE edits write back with
    origin='human' and win lookups over machine pairs."""

    _RANK = "CASE origin WHEN 'human' THEN 0 ELSE 1 END"

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(Path(path))
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tm (
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                locale TEXT NOT NULL,
                origin TEXT NOT NULL DEFAULT 'machine',
                UNIQUE(source, locale, origin)
            )""")
        self.conn.commit()

    def store(self, source: str, target: str, locale: str,
              origin: str = "machine") -> None:
        if is_dry_run_stub(target):
            return                               # poisoned-reuse guard
        self.conn.execute(
            "INSERT OR REPLACE INTO tm (source, target, locale, origin) "
            "VALUES (?, ?, ?, ?)", (source, target, locale, origin))
        self.conn.commit()

    def lookup(self, source: str, locale: str) -> Optional[str]:
        row = self.conn.execute(
            f"SELECT target FROM tm WHERE source = ? AND locale = ? "
            f"ORDER BY {self._RANK} LIMIT 1", (source, locale)).fetchone()
        return row[0] if row else None

    def examples(self, locale: str, limit: int = 3) -> List[Tuple[str, str]]:
        rows = self.conn.execute(
            f"SELECT source, target FROM tm WHERE locale = ? "
            f"ORDER BY {self._RANK} LIMIT ?", (locale, limit)).fetchall()
        return [(s, t) for s, t in rows]

    def rendering_map(self, locale: str) -> Dict[str, str]:
        rows = self.conn.execute(
            f"SELECT source, target FROM tm WHERE locale = ? "
            f"ORDER BY {self._RANK} DESC", (locale,)).fetchall()
        return dict(rows)          # human pairs overwrite machine pairs


# ------------------------------------------------------------ tenant memory

class TenantMemory:
    """Cross-job store: T2 genre wording families and adjudicated precedents.

    Curated, never auto-written (design §6): agents and stage graphs may only
    `propose_*` — proposals land in an inbox for explicit human curation.
    Feeding back every reviewer rejection uncritically teaches the system to
    stop flagging whatever a rushed reviewer dismissed in bulk, so precedent
    promotion requires MIN_PRECEDENT_COUNT consistent observations.
    """

    MIN_PRECEDENT_COUNT = 3

    def __init__(self, root: Path, tenant_id: str):
        self.dir = Path(root) / "tenants" / tenant_id
        self.inbox = self.dir / "inbox"

    # -- reads (curated data only) -------------------------------------

    def genre_glossary(self, genre: str, locale: str) -> Dict[str, str]:
        """T2 layer for the merged GlossaryBrief (T1 wins conflicts)."""
        path = self.dir / "genre" / genre / f"{locale}.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def suppressions(self) -> List[dict]:
        """Curated false-positive suppression rules for LQA T3."""
        path = self.dir / "suppressions.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # -- proposals (the only write path for automation) ----------------

    def propose(self, kind: str, payload: dict) -> Path:
        """Append a curation proposal; a human promotes it (or not)."""
        self.inbox.mkdir(parents=True, exist_ok=True)
        path = self.inbox / f"{kind}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return path
