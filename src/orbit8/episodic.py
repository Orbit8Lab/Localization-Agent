"""Episodic memory — reading back what this job's sessions already did.

`chat-traces/*.jsonl` have been written since the chat orchestrator
existed and never read. That is a real gap rather than a tidiness one: an
operator who says "compare it against the file I showed you earlier" is
referring to a fact the system recorded and then forgot, and the agent's
only recourse today is to re-run the tool or guess.

## Scope: this job only

Traces already live under `jobs/<job_id>/chat-traces/`, so job scoping is
the layout, not an added rule. Recall never crosses that boundary.

Cross-job recall ("how did we handle this last time") is more powerful and
is deliberately NOT built here: reading another job's trace is a
cross-tenant read, and PLAN §5.4 classifies namespace leakage between
tenants as a policy break. A recall feature is not worth reopening it. If
it is wanted later it needs the tenant check at the boundary, not a wider
glob.

## What is recalled

Tool calls and their arguments — the durable facts of a session (which
file was inspected, which locale was compared) — not the model's prose.
Prose is the least reliable and least dense part of a trace; arguments are
what a follow-up question actually refers to.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# Events worth recalling. "tool" carries the call AND its outcome; the
# others are either duplicates of it or model prose.
RECALLABLE = frozenset({"tool"})

# Argument keys that identify WHAT was acted on. A recall listing every
# argument is mostly noise; these are the ones a later question refers to.
IDENTIFYING_KEYS = ("path", "file", "files", "dir", "locale", "old", "new",
                    "name", "out_name", "stage", "gate", "assets", "pe_po")


@dataclass(frozen=True)
class Episode:
    """One recalled action from an earlier turn of this job."""
    session: str
    turn: int
    tool: str
    args: Dict[str, object]
    failed: Optional[str] = None

    def describe(self) -> str:
        detail = ", ".join(
            f"{key}={self._short(self.args[key])}"
            for key in IDENTIFYING_KEYS if key in self.args)
        line = f"turn {self.turn}: {self.tool}"
        if detail:
            line += f" ({detail})"
        if self.failed:
            # A failed call is worth recalling precisely BECAUSE it failed:
            # repeating it verbatim is the most common wasted turn.
            line += f" → failed: {self.failed}"
        return line

    @staticmethod
    def _short(value: object, limit: int = 80) -> str:
        text = (", ".join(map(str, value))
                if isinstance(value, (list, tuple)) else str(value))
        return text if len(text) <= limit else text[:limit] + "…"


class EpisodicMemory:
    """Read-only view over one job's chat traces."""

    def __init__(self, trace_dir: Path, *, job_id: Optional[str] = None):
        self.trace_dir = Path(trace_dir)
        self.job_id = job_id

    def _read(self, exclude: Optional[Path] = None) -> List[Episode]:
        """Every recallable action across this job's sessions, oldest first.

        Malformed lines are skipped rather than raised on: a trace is a
        debugging artifact written with best-effort appends, and a single
        torn line (a session killed mid-write) must not make the whole
        history unreadable.
        """
        if not self.trace_dir.exists():
            return []
        episodes: List[Episode] = []
        for path in sorted(self.trace_dir.glob("*.jsonl")):
            if exclude and path.resolve() == Path(exclude).resolve():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("event") not in RECALLABLE:
                    continue
                # The trace writes the failure reason as `error`
                # (orchestrator._trace). Reading only `failed` silently
                # dropped every one of them — and a failed call is the most
                # valuable thing here, because repeating it verbatim is the
                # most common wasted turn. `failed` stays accepted so
                # hand-written or older traces still read.
                reason = record.get("error") or record.get("failed")
                # Coerce defensively: a trace is a best-effort debugging
                # artifact, and one malformed field must not abort the read
                # of every other record in the file.
                try:
                    turn = int(record.get("turn", 0))
                except (TypeError, ValueError):
                    turn = 0
                args = record.get("args")
                episodes.append(Episode(
                    session=path.stem, turn=turn,
                    tool=str(record.get("tool", "?")),
                    args=args if isinstance(args, dict) else {},
                    failed=str(reason) if reason else None))
        return episodes

    def recall(self, query: str, *, limit: int = 6,
               exclude: Optional[Path] = None) -> List[Episode]:
        """Actions from earlier sessions relevant to `query`.

        Matching is literal — a token from the query appearing in the tool
        name or an identifying argument. Deliberately not semantic: the
        things worth recalling here are file paths, locales and tool names,
        which are exact strings. An embedding would add a dependency, a
        failure mode, and non-reproducibility to a substring search.

        Most recent first, because when two sessions touched the same file
        the later one is almost always the one being referred to.
        """
        # Two characters, not three: `ko`, `ja`, `zh`, `po` are exactly the
        # tokens that matter in this domain, and a 3-char floor silently
        # made every locale and format query unmatchable.
        tokens = [t for t in _tokenize(query) if len(t) >= 2]
        if not tokens:
            return []
        scored: List[tuple] = []
        for index, episode in enumerate(self._read(exclude=exclude)):
            haystack = _tokenize(
                episode.tool + " " + " ".join(
                    str(episode.args.get(key, ""))
                    for key in IDENTIFYING_KEYS))
            # Short tokens must match a WHOLE word: a bare "po" appearing
            # anywhere inside a path would otherwise match every entry and
            # turn recall into "return everything".
            hits = sum(1 for token in tokens
                       if any(token == word or
                              (len(token) > 3 and token in word)
                              for word in haystack))
            if hits:
                scored.append((hits, index, episode))
        scored.sort(key=lambda row: (-row[0], -row[1]))
        return [episode for _hits, _index, episode in scored[:limit]]

    def recent(self, limit: int = 6,
               exclude: Optional[Path] = None) -> List[Episode]:
        return list(reversed(self._read(exclude=exclude)))[:limit]

    def as_block_text(self, episodes: Sequence[Episode]) -> str:
        """Render for injection. Explicitly labelled as a RECORD of past
        actions, so the model does not read it as a result it already has —
        the recall says a file was inspected, not what the file contained.
        """
        if not episodes:
            return ""
        lines = ["[Earlier in this job — a record of actions taken, NOT "
                 "their results. Re-run a tool if you need the content.]"]
        for episode in episodes:
            lines.append(f"  {episode.describe()}")
        return "\n".join(lines)


def _tokenize(text: str) -> List[str]:
    out, current = [], []
    for char in str(text).lower():
        if char.isalnum() or char in "._-":
            current.append(char)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    # Path segments are searched whole and in parts: an operator says
    # "Game.po", the trace holds ".../10-received/20260810-PO/Game.po".
    for word in list(out):
        out.extend(part for part in word.replace("/", ".").split(".")
                   if part)
    return out
