"""Two-phase confirmation for consequential tools.

The web assistant has the CLI's full tool set, including `approve`. That
parity is the point — but a browser is not a terminal. On the CLI the
operator is the author, watching a trace. In the console the reader may
be a producer who does not know what a glossary lock is, and "looks fine,
go ahead" must not silently become a signature on G1.

So the consequential tools are wrapped, not removed: the first call
returns a description of what would happen and a token; the action runs
only when a second call carries that token back. The model cannot mint
one — the token is generated here, handed to the UI, and the UI shows a
button. That turns "the model decided" into "the human confirmed", while
leaving every read-only and cheap tool free to run.

Two classes of wrapped tool:

* **Signature** (`approve`) — a gate is a human's mark (design §7).
* **Spend** (`translate_po`, `lqa_run`, …) — these bill real money. The
  per-finding verifier is ~80% of LLM calls and uncapped, so an
  unconfirmed `lqa_run` on a large file is a cost incident.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

# Tools that change a job in a way a human should assent to, with the
# sentence shown to that human. Kept explicit rather than derived from a
# naming convention: a new expensive tool must be added deliberately.
CONFIRMS: Dict[str, str] = {
    "approve": "Sign gate {gate} for this job. This is recorded under your "
               "name and is how the project moves forward.",
    "translate_po": "Translate strings with a language model. This spends "
                    "tokens and can take a long time on a large file.",
    "lqa_run": "Run the full quality-assurance cascade. This is the most "
               "expensive operation in the system.",
    "deliver_po": "Write deliverable files for this job.",
    "standardize": "Rewrite strings in place to match the style guide.",
    "update_glossary": "Change the project glossary. Downstream "
                       "translation follows whatever this sets.",
    "add_glossary_terms": "Add terms to the project glossary.",
}

TOKEN_TTL_SECONDS = 600


@dataclass
class Pending:
    """One awaiting confirmation. Bound to a job and a tool call so a
    token cannot be replayed against different arguments."""
    token: str
    job_id: str
    tool: str
    args: dict
    description: str
    created_at: float = field(default_factory=time.time)

    def expired(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) - self.created_at > TOKEN_TTL_SECONDS


class ConsentLedger:
    """Issues and redeems confirmation tokens. In-memory: a token that
    does not survive a restart is correct, because the human's intent
    does not survive it either."""

    def __init__(self) -> None:
        self._pending: Dict[str, Pending] = {}

    def issue(self, job_id: str, tool: str, args: dict,
              description: str) -> Pending:
        self._prune()
        record = Pending(token=secrets.token_urlsafe(16), job_id=job_id,
                         tool=tool, args=args, description=description)
        self._pending[record.token] = record
        return record

    def redeem(self, token: str, job_id: str) -> Pending:
        """Consume a token. Raises `KeyError` with a reason the UI can
        show; a token is single-use so a retry cannot double-approve."""
        record = self._pending.pop(token, None)
        if record is None:
            raise KeyError("that confirmation is no longer valid; ask again")
        if record.expired():
            raise KeyError("that confirmation expired; ask again")
        if record.job_id != job_id:
            # Defensive: a token minted for one job must never act on
            # another, even if the UI is confused.
            raise KeyError("that confirmation belongs to a different job")
        return record

    def peek(self, token: str) -> Optional[Pending]:
        return self._pending.get(token)

    def _prune(self) -> None:
        now = time.time()
        for tok in [t for t, r in self._pending.items() if r.expired(now)]:
            self._pending.pop(tok, None)


def describe(tool: str, args: dict) -> str:
    template = CONFIRMS.get(tool, "Run {tool}.")
    try:
        return template.format(tool=tool, **args)
    except (KeyError, IndexError):
        # A template referencing an argument the model did not supply must
        # not blow up the turn; fall back to naming the call.
        return f"{template.split('{')[0].strip()} ({tool} {json.dumps(args, default=str)[:120]})"


def wrap_tools(tools: Dict[str, Callable[[dict], str]], *, job_id: str,
               ledger: ConsentLedger,
               approved_token: Optional[str] = None
               ) -> Dict[str, Callable[[dict], str]]:
    """Return the registry with consequential tools gated.

    `approved_token` is the one token redeemed for THIS turn: the tool it
    was issued for runs directly, everything else still asks. That is what
    makes "yes, do it" execute exactly the action the human saw.
    """
    redeemed: Optional[Pending] = None
    if approved_token:
        try:
            redeemed = ledger.redeem(approved_token, job_id)
        except KeyError:
            redeemed = None

    wrapped: Dict[str, Callable[[dict], str]] = {}
    for name, handler in tools.items():
        if name not in CONFIRMS:
            wrapped[name] = handler
            continue

        def gated(args: dict, _name: str = name,
                  _handler: Callable[[dict], str] = handler) -> str:
            if (redeemed is not None and redeemed.tool == _name):
                return _handler(redeemed.args or args)
            record = ledger.issue(job_id, _name, args, describe(_name, args))
            # Returned as a tool observation, so the model reports it as
            # "this needs your confirmation" rather than claiming it acted.
            return ("CONFIRMATION_REQUIRED: this action was NOT performed. "
                    f"Tell the user: {record.description} "
                    f"They must confirm it in the console. "
                    f"[token={record.token}]")

        wrapped[name] = gated
    return wrapped
