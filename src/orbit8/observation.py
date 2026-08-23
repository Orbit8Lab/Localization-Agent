"""The observation layer (PLAN §3) — Phase 1 of capability 3.

Append-only record of every repair attempt and what happened to it. This
module **only writes**. Nothing here retrieves, ranks, promotes, or changes
a prompt: those are Phase 4 (PLAN §6) and must not be reachable from here,
because a write-only module cannot regress the pipeline no matter how
wrong its contents are.

What it exists to answer, none of which is answerable today:

- Do defect signatures actually recur, or is every defect a singleton?
  (PLAN §4.2 — if they are singletons there is no skill to learn, and
  Phase 4 should not be built at all.)
- Does `_badness()` agree with a human reviewer at G3? (PLAN §5.6 — if it
  does not, promotion trained on badness would optimize against the
  reviewer.)
- What are the real values of the constants Phase 4 would need — the
  promotion threshold, the utility floor, the decay window? Today every
  one of them is a guess.

Two design commitments, both load-bearing later:

**Signatures are string-independent** (PLAN §4.2). A signature keyed by
segment uid can never accumulate, which makes it a cache rather than a
skill. `Finding.identity()` is `(key, bug_type, evidence)` and `key` IS
the uid, so it cannot be reused here — see `signature()`.

**The ratchet verdict is recorded for rejections too** (PLAN §4.3). A
rejected repair is not exhaust: it is the negative example that will
define where a skill stops applying (PLAN §6.3). Discarding it means a
skill generalizes exactly as far as its first few successes reached.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .schemas import Finding

SCHEMA_VERSION = 1

# Ratchet verdicts. ACCEPTED/REJECTED are the gate's strictly-better
# decision (graphs/translate.py::gate); FIRST is the opening candidate,
# which has no incumbent to beat and so is neither.
ACCEPTED = "accepted"
REJECTED = "rejected"
FIRST = "first"

# G3 verdicts (PLAN §5.6). PENDING until a human actually rules, which is
# the state every row starts in — an unreviewed row must never be
# mistaken for an approving one.
G3_PENDING = "pending"
G3_ACCEPTED = "accepted"
G3_EDITED = "edited"
G3_REJECTED = "rejected"


# ------------------------------------------------------- defect signature

# A style-rule finding carries its rule id in the message as "[RULE-ID] …"
# (gate_checks.py:410). That is the one place a real rule id survives into
# a Finding, so it is worth extracting rather than inventing a schema
# change: it is the most specific signature component available.
_RULE_ID_RE = re.compile(r"^\[([A-Z][A-Z0-9-]{1,31})\]")

# Evidence normalization. The goal is a signature that is stable across
# strings but still discriminating: the LOCKED TERM in a terminology
# finding is the thing that recurs, whereas a 40-character quotation of
# one segment's prose is unique to that segment and would make the
# signature a uid by another name.
_WS_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")

# Above this length, evidence is treated as a quotation of one specific
# string rather than a reusable token, and is dropped from the signature.
# Chosen to admit terms, placeholder lists and short markup while
# excluding prose; PLAN §4.2's granularity question is exactly where this
# constant should stop being a guess.
EVIDENCE_MAX = 40


def normalize_evidence(evidence: str) -> str:
    """Reduce evidence to its reusable part, or '' when it has none.

    Digits are masked because a placeholder index ({0} vs {1}) makes two
    instances of one defect class look like two classes.
    """
    text = _WS_RE.sub(" ", evidence).strip()
    if not text or len(text) > EVIDENCE_MAX:
        return ""
    return _DIGITS_RE.sub("#", text).casefold()


def signature(finding: Finding) -> str:
    """The string-independent defect signature (PLAN §4.2).

    Deliberately NOT `Finding.identity()`: that includes `key`, the
    segment uid, so it is unique per string by construction. The line
    this draws is the line between a cache (keyed by string) and a skill
    (keyed by defect class).

    Shape: ``bug_type/rule_id/evidence`` with empty components elided.
    Coarse by design — an over-coarse signature reveals itself in the log
    as a class with inconsistent outcomes, which is a measurement; an
    over-fine one just never accumulates, which looks like silence.
    """
    parts: List[str] = [finding.bug_type.value]
    match = _RULE_ID_RE.match(finding.message)
    if match:
        parts.append(match.group(1))
    evidence = normalize_evidence(finding.evidence)
    if evidence:
        parts.append(evidence)
    return "/".join(parts)


def signatures(findings: Sequence[Finding]) -> List[str]:
    """Distinct signatures over a finding set, order-stable."""
    seen: Dict[str, None] = {}
    for finding in findings:
        seen.setdefault(signature(finding), None)
    return list(seen)


# ----------------------------------------------------------- observations

@dataclass(frozen=True)
class Observation:
    """One candidate evaluation at the ratchet.

    `job_id`/`attempt` tie a row to the s4 attempt that produced it: an
    observation that cannot be traced back to its attempt is not auditable.

    PLAN §5.7 also asks for a STORE REVISION, and it is deliberately absent
    here. A revision is only meaningful once retrieved store content can
    influence a prompt (§5.3) — that is Phase 4. In Phase 1 nothing reads
    the store, so there is no revision to record, and a column that always
    held 0 would advertise an audit coordinate it did not have. Phase 4
    adds it together with the monotonic counter that gives it a value.
    """
    job_id: str
    locale: str
    attempt: int
    uid: str
    signatures: List[str]
    strategy: str                      # translate | repair | prefill | reuse
    iteration: int
    badness_before: Optional[int]      # None for the first candidate
    badness_after: int
    verdict: str                       # ACCEPTED | REJECTED | FIRST
    # The candidate text itself. Needed to tell an `accepted` G3 ruling from
    # an `edited` one: without it there is nothing to diff the human's
    # approved target against, and the accept/edit distinction — the whole
    # point of the G3 signal (PLAN §5.6) — is unrecoverable.
    target: Optional[str] = None
    tokens: float = 0.0
    fingerprint: Optional[str] = None
    batch_id: Optional[str] = None
    # Filled in later, when a human rules at G3 (PLAN §5.6). Kept on the
    # same row so a skill's utility is one query, not a join across a
    # table that may not exist yet.
    g3_verdict: str = G3_PENDING


class ObservationLog:
    """Append-only SQLite log. One file per job.

    Append-only is enforced by having no UPDATE path except the G3 verdict
    (which is a later fact about an existing row, not a revision of it)
    and no DELETE path at all.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        # Name-based access: this table has enough columns that positional
        # unpacking silently misaligns every field after an insertion, and
        # the failure looks like corrupt data rather than a schema change.
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL,
                job_id TEXT NOT NULL,
                locale TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                batch_id TEXT,
                uid TEXT NOT NULL,
                signatures_json TEXT NOT NULL,
                strategy TEXT NOT NULL,
                iteration INTEGER NOT NULL,
                badness_before INTEGER,
                badness_after INTEGER NOT NULL,
                verdict TEXT NOT NULL,
                target TEXT,
                tokens REAL NOT NULL DEFAULT 0,
                fingerprint TEXT,
                g3_verdict TEXT NOT NULL DEFAULT 'pending',
                g3_text TEXT
            )""")
        # One row per (uid, signature) pair, so a signature's history is a
        # range scan rather than a JSON scan over every row.
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS observation_signatures (
                observation_id INTEGER NOT NULL,
                signature TEXT NOT NULL,
                PRIMARY KEY (observation_id, signature)
            )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sig "
            "ON observation_signatures (signature)")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_obs_uid ON observations (uid)")
        self.conn.commit()

    # -- write ---------------------------------------------------------

    def record(self, obs: Observation) -> int:
        cur = self.conn.execute(
            "INSERT INTO observations (schema_version, job_id, locale, "
            "attempt, batch_id, uid, signatures_json, strategy, "
            "iteration, badness_before, badness_after, verdict, target, "
            "tokens, fingerprint, g3_verdict) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (SCHEMA_VERSION, obs.job_id, obs.locale, obs.attempt,
             obs.batch_id, obs.uid,
             json.dumps(obs.signatures, ensure_ascii=False), obs.strategy,
             obs.iteration, obs.badness_before, obs.badness_after,
             obs.verdict, obs.target, obs.tokens, obs.fingerprint,
             obs.g3_verdict))
        obs_id = int(cur.lastrowid)
        if obs.signatures:
            self.conn.executemany(
                "INSERT OR IGNORE INTO observation_signatures "
                "(observation_id, signature) VALUES (?, ?)",
                [(obs_id, sig) for sig in obs.signatures])
        self.conn.commit()
        return obs_id

    def record_g3(self, uid: str, locale: str, verdict: str,
                  text: Optional[str] = None) -> int:
        """Attach a human verdict to every observation of one string in one
        LOCALE.

        This is the only UPDATE the log permits, and it is not a revision:
        the G3 ruling is a NEW fact about an existing observation that did
        not exist when the row was written. Without it, every downstream
        utility estimate is our own scorer grading itself (PLAN §5.6).

        `locale` is mandatory and NOT defaulted. `uid` is the dedup hash of
        the SOURCE string, so it is identical across every target locale,
        while this log is job-scoped (one file, all locales). Keying on uid
        alone stamped a reviewer's ruling — and their translated text —
        onto every other locale's row: a reviewer editing the Japanese
        string marked the Korean one `edited` and overwrote its `g3_text`
        with Japanese. Since the G3 verdict is the only held-out human
        signal in the design, that fabricated agreement data in the one
        place the plan requires it be real.
        """
        if verdict not in (G3_PENDING, G3_ACCEPTED, G3_EDITED, G3_REJECTED):
            raise ValueError(f"unknown G3 verdict: {verdict!r}")
        cur = self.conn.execute(
            "UPDATE observations SET g3_verdict = ?, g3_text = ? "
            "WHERE uid = ? AND locale = ?", (verdict, text, uid, locale))
        self.conn.commit()
        return cur.rowcount

    # -- read (analysis only — NOT a retrieval path) -------------------

    def all_rows(self) -> List[dict]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM observations ORDER BY id").fetchall()]

    def for_uid(self, uid: str, locale: Optional[str] = None) -> List[dict]:
        """Observations of one string. Pass `locale` whenever the answer
        feeds a per-locale decision — a bare uid spans every locale in the
        job, because uid hashes the SOURCE text (see `record_g3`)."""
        if locale is None:
            return [self._row(r) for r in self.conn.execute(
                "SELECT * FROM observations WHERE uid = ? ORDER BY id",
                (uid,)).fetchall()]
        return [self._row(r) for r in self.conn.execute(
            "SELECT * FROM observations WHERE uid = ? AND locale = ? "
            "ORDER BY id", (uid, locale)).fetchall()]

    def for_signature(self, sig: str) -> List[dict]:
        return [self._row(r) for r in self.conn.execute(
            "SELECT o.* FROM observations o "
            "JOIN observation_signatures s ON s.observation_id = o.id "
            "WHERE s.signature = ? ORDER BY o.id", (sig,)).fetchall()]

    def signature_tally(self) -> List[dict]:
        """Per-signature counts — the table that answers "do signatures
        recur at all", i.e. PLAN §6.1's first stop condition.

        `distinct_strings` is the column that matters: a signature seen 20
        times on ONE string is a flapping repair loop, not a defect class,
        and the two warrant opposite conclusions (PLAN §4.1).
        """
        rows = self.conn.execute(
            "SELECT s.signature, COUNT(*) AS n, "
            "       COUNT(DISTINCT o.uid) AS distinct_strings, "
            "       SUM(o.verdict = ?) AS accepted, "
            "       SUM(o.verdict = ?) AS rejected, "
            "       SUM(o.g3_verdict = ?) AS g3_accepted, "
            "       SUM(o.g3_verdict IN (?, ?)) AS g3_overturned "
            "FROM observation_signatures s "
            "JOIN observations o ON o.id = s.observation_id "
            "GROUP BY s.signature ORDER BY n DESC, s.signature",
            (ACCEPTED, REJECTED, G3_ACCEPTED, G3_EDITED,
             G3_REJECTED)).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> Dict[str, int]:
        rows = self.conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM observations "
            "GROUP BY verdict").fetchall()
        return {r["verdict"]: r["n"] for r in rows}

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        out = dict(row)
        out["signatures"] = json.loads(out.pop("signatures_json"))
        return out
