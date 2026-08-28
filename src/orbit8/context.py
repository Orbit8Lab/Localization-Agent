"""Context Manager — deciding what the LLM sees on any one call.

The model does not manage its own context. This module does, and it is the
component that was missing: before it, three independent constants decided
the matter by accident —

    SYSTEM              ~2.7k tokens, always sent
    OBSERVATION_LIMIT   12000 chars PER tool result
    history[-30:]       a blind sliding window, entries unbounded

— none aware of the others. Fourteen tool calls in one turn could reach
~50k tokens before history was added, and nothing in the code knew that.
Each limit was reasonable alone; nobody owned their sum.

## Why tiers, not relevance scoring

The obvious design ranks candidate blocks by relevance and keeps the
top-scoring set. This does not, because relevance scoring means a block can
disappear for reasons no one can reconstruct, and this system has already
been bitten by exactly that: `OBSERVATION_LIMIT` silently re-truncated
`inspect_file` output one layer up, and the agent confidently described a
file region it had never been shown.

So selection here is **deterministic and announced**:

- fixed priority tiers, recency within a tier;
- identical inputs produce byte-identical context, so a session replays;
- **every drop is stated in the context itself.** A model working from a
  partial view must know the view is partial — otherwise it answers
  confidently from an absence, which is the failure mode that matters.

The tiers, highest priority first:

    0 SYSTEM     the tool contract          never dropped
    1 TASK       derived phase/gate         never dropped
    2 EVIDENCE   this turn's tool results   never dropped, may be trimmed
    3 PLAYBOOK   the stage's SKILL.md       dropped whole if tight
    4 EPISODIC   recalled prior turns       dropped whole if tight
    5 HISTORY    the conversation           oldest dropped first

TASK outranks EVIDENCE because a huge tool result must never be able to
evict the agent's knowledge of which stage it is in — that is how an agent
starts calling the wrong stage's actions. EVIDENCE outranks HISTORY
because what a tool returned THIS turn is why the model was called at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence

# Tier constants — lower number wins when the budget is tight.
TIER_SYSTEM = 0
TIER_TASK = 1
TIER_EVIDENCE = 2
TIER_PLAYBOOK = 3
TIER_EPISODIC = 4
TIER_HISTORY = 5

TIER_NAMES = {
    TIER_SYSTEM: "system", TIER_TASK: "task", TIER_EVIDENCE: "evidence",
    TIER_PLAYBOOK: "playbook", TIER_EPISODIC: "episodic",
    TIER_HISTORY: "history",
}

# Tiers that must survive at any budget. If these alone exceed it, the
# budget is wrong and `assemble` says so rather than quietly shipping a
# context that cannot work.
PROTECTED = frozenset({TIER_SYSTEM, TIER_TASK, TIER_EVIDENCE})

# Characters per token. A deliberate approximation, and deliberately
# pessimistic for this corpus: CJK runs ~1-1.5 chars/token where ASCII runs
# ~4. Estimating low means the budget is under-spent rather than blown, and
# a blown context window fails the whole call. Swap in a real tokenizer by
# passing `estimator=` — the assembler never assumes this heuristic.
CHARS_PER_TOKEN_ASCII = 4.0
CHARS_PER_TOKEN_CJK = 1.5

_CJK_RE = re.compile(r"[　-鿿＀-￯가-힯]")


def estimate_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate that respects CJK density.

    Treating a Chinese string as 4 chars/token underestimates it by ~3x,
    which is how a budget that looks fine on English blows up on the corpus
    this pipeline actually processes.
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk / CHARS_PER_TOKEN_CJK + other / CHARS_PER_TOKEN_ASCII) + 1


@dataclass
class Block:
    """One candidate piece of context.

    `trimmable` marks content that can be cut down rather than dropped
    whole — a long tool result keeps its head and says how much was cut.
    Prose blocks (the system prompt, the playbook) are not trimmable,
    because half a contract is worse than none.
    """
    tier: int
    text: str
    label: str = ""
    trimmable: bool = False
    # Higher = kept longer within the same tier. History uses turn order,
    # so the newest turn survives the longest.
    order: int = 0

    def tokens(self, estimator: Callable[[str], int] = estimate_tokens) -> int:
        return estimator(self.text)


@dataclass
class Elision:
    """A record of something the assembler removed.

    Every one of these is surfaced to the model. An elision the model
    cannot see is indistinguishable, from inside the model, from content
    that never existed.
    """
    tier: int
    label: str
    dropped_tokens: int
    kind: str                  # "dropped" | "trimmed"

    def describe(self) -> str:
        what = "elided" if self.kind == "dropped" else "truncated"
        name = self.label or TIER_NAMES.get(self.tier, "block")
        return f"{name} {what} (~{self.dropped_tokens} tokens)"


@dataclass
class AssembledContext:
    """The result: the text to send, plus a full account of what was cut."""
    text: str
    tokens: int
    elisions: List[Elision] = field(default_factory=list)
    included: List[str] = field(default_factory=list)
    over_budget: bool = False

    @property
    def complete(self) -> bool:
        """True when the model is seeing everything that was offered."""
        return not self.elisions

    def summary(self) -> str:
        if self.complete:
            return f"{self.tokens} tokens, complete"
        return (f"{self.tokens} tokens, {len(self.elisions)} elision(s): "
                + "; ".join(e.describe() for e in self.elisions))


# The block holding the operator's current message. Named here rather than
# spelled as a literal in two modules, so the renderer's "pin this last"
# rule cannot drift from the label the orchestrator actually assigns.
REQUEST_LABEL = "request"

# Minimum tokens a trimmable block keeps before it is dropped outright.
# Below this a "trimmed" result is a fragment that cannot inform anything,
# and pretending otherwise wastes budget on noise.
MIN_TRIM_TOKENS = 120


class ContextAssembler:
    """Fits blocks into a token budget by tier, and reports every cut.

    Stateless and pure: the same blocks and budget always produce the same
    context. That is what makes a session replayable from its trace, and it
    is the property relevance scoring would cost.
    """

    def __init__(self, budget_tokens: int = 60_000, *,
                 estimator: Callable[[str], int] = estimate_tokens,
                 reserve_tokens: int = 2_000):
        if budget_tokens <= 0:
            raise ValueError("budget_tokens must be positive")
        self.budget_tokens = budget_tokens
        self.estimator = estimator
        # Headroom for the model's own reply. A budget spent entirely on
        # input leaves nothing to answer with.
        self.reserve_tokens = reserve_tokens

    # The CONTEXT NOTICE is appended after selection, so it is not part of
    # any block's allowance. Small and bounded, but "spends budget nobody
    # accounted for" is precisely the class of bug this module exists to
    # remove, so it is reserved up front rather than discovered later.
    NOTICE_RESERVE_TOKENS = 60

    @property
    def usable(self) -> int:
        """Budget left for content after the reply and notice reservations.

        Clamped to >= 1 so assembly never divides by zero, but a value that
        small means the reservations have eaten the entire budget — every
        block then looks unfittable, the trim path is skipped (nothing
        clears MIN_TRIM_TOKENS), and protected blocks pass through whole.
        `starved` names that state so it can be asserted on rather than
        discovered as mysterious over-budget output.
        """
        return max(1, self.budget_tokens - self.reserve_tokens
                   - self.NOTICE_RESERVE_TOKENS)

    @property
    def starved(self) -> bool:
        """True when reservations leave no room to assemble anything."""
        return self.usable < MIN_TRIM_TOKENS

    def assemble(self, blocks: Sequence[Block]) -> AssembledContext:
        ordered = sorted(blocks, key=lambda b: (b.tier, -b.order))
        kept: List[Block] = []
        elisions: List[Elision] = []
        spent = 0
        budget = self.usable

        for block in ordered:
            cost = block.tokens(self.estimator)
            if spent + cost <= budget:
                kept.append(block)
                spent += cost
                continue

            remaining = budget - spent
            if block.trimmable and remaining >= MIN_TRIM_TOKENS:
                trimmed, dropped = self._trim(block, remaining)
                kept.append(trimmed)
                spent += trimmed.tokens(self.estimator)
                elisions.append(Elision(block.tier, block.label, dropped,
                                        "trimmed"))
                continue

            if block.tier in PROTECTED:
                # A protected block does not fit. Keeping it whole and
                # going over budget is the lesser evil: dropping the task
                # state or this turn's evidence produces a confident answer
                # about the wrong thing, which is worse than a call that
                # fails loudly at the API boundary.
                kept.append(block)
                spent += cost
                continue

            elisions.append(Elision(block.tier, block.label, cost, "dropped"))

        text = self._render(kept, elisions)
        return AssembledContext(
            text=text, tokens=self.estimator(text), elisions=elisions,
            included=[b.label or TIER_NAMES.get(b.tier, "?") for b in kept],
            over_budget=spent > budget)

    def _trim(self, block: Block, allowance: int) -> tuple:
        """Cut a block to `allowance`, keeping the head and stating the cut.

        The marker is inside the text, not only in the elision list,
        because the model reads the text — a note it never sees cannot stop
        it from describing content that was removed.
        """
        full = block.tokens(self.estimator)
        # Convert the token allowance back to characters conservatively.
        ratio = len(block.text) / max(1, full)
        # The marker is part of what gets sent, so it has to come OUT of
        # the allowance. Sizing the head to the full allowance and then
        # appending the marker pushed the block back over budget — worst
        # near MIN_TRIM_TOKENS, where the marker is a large fraction of the
        # whole allowance and the overshoot distorts every later selection.
        marker_cost = self.estimator(
            "\n…[TRUNCATED: ~000000 tokens not shown. Do NOT describe "
            "content beyond this point — request it explicitly.]")
        head_allowance = max(1, allowance - marker_cost)
        keep_chars = max(1, int(head_allowance * ratio * 0.9))
        head = block.text[:keep_chars]
        dropped = full - self.estimator(head)
        marker = (f"\n…[TRUNCATED: ~{max(0, dropped)} tokens not shown. "
                  f"Do NOT describe content beyond this point — "
                  f"request it explicitly.]")
        return (Block(tier=block.tier, text=head + marker, label=block.label,
                      trimmable=False, order=block.order),
                max(0, dropped))

    @staticmethod
    def _render(kept: Sequence[Block], elisions: Sequence[Elision]) -> str:
        """Render kept blocks, with the request and the notice last.

        Selection order is by TIER, which is right for deciding what
        survives and wrong for deciding what the model reads last. The
        current request is pinned to the end regardless of its tier, and
        the notice sits immediately after it — the claim "adjacent to the
        request" was previously just a comment, and tier ordering could put
        the request in the middle with the notice paragraphs away.
        """
        body = [b.text for b in kept
                if b.label != REQUEST_LABEL and b.text.strip()]
        request = [b.text for b in kept
                   if b.label == REQUEST_LABEL and b.text.strip()]
        parts = body + request
        if elisions:
            notice = "; ".join(e.describe() for e in elisions)
            parts.append(
                f"[CONTEXT NOTICE — your view is partial: {notice}. "
                f"If an answer depends on elided material, say so or "
                f"re-read it with a tool rather than inferring it.]")
        return "\n\n".join(parts)


# ------------------------------------------------------------ convenience

def history_blocks(history: Iterable[tuple], *,
                   start_order: int = 0) -> List[Block]:
    """Turn (role, text) pairs into history blocks, newest highest.

    Order is by position, so when the budget bites the OLDEST turns go
    first — the opposite of the previous `history[-30:]`, which dropped by
    count regardless of size and never said it had.
    """
    items = list(history)
    return [Block(tier=TIER_HISTORY, text=f"[{role}] {text}",
                  label=f"turn-{index}", trimmable=False,
                  order=start_order + index)
            for index, (role, text) in enumerate(items)]
