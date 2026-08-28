"""The Context Manager — deciding what the LLM sees (context.py).

The invariant everything else here serves: **nothing is dropped silently.**
This system has already shipped that bug once — `OBSERVATION_LIMIT`
re-truncated `inspect_file` output one layer up, and the agent then
described a file region it had never been shown. A model that cannot tell
"absent" from "nonexistent" answers confidently from an absence.

So the assembler is deterministic (same inputs → same context, so a
session replays) and every cut is announced in the text the model reads,
not only in a return value the model never sees.
"""
from __future__ import annotations

import pytest

from orbit8.context import (Block, ContextAssembler, Elision, MIN_TRIM_TOKENS,
                            PROTECTED, REQUEST_LABEL, TIER_EPISODIC,
                            TIER_EVIDENCE, TIER_HISTORY, TIER_PLAYBOOK,
                            TIER_SYSTEM, TIER_TASK, estimate_tokens,
                            history_blocks)


def _block(tier, tokens=100, label="", trimmable=False, order=0) -> Block:
    """A block of approximately `tokens` estimated tokens.

    Sized against the real estimator rather than a character count: "x " *
    100 is 200 ASCII chars, which is ~50 tokens, not 100. Guessing here
    makes budget tests pass because everything fit, not because the tier
    logic worked.
    """
    text = "x " * (tokens * 2)
    assert estimate_tokens(text) >= tokens
    return Block(tier=tier, text=text, label=label,
                 trimmable=trimmable, order=order)


# ------------------------------------------------------- token estimation

def test_cjk_is_charged_more_than_latin_per_character():
    """A budget calibrated on English blows up on this pipeline's actual
    corpus: Chinese runs ~1-1.5 chars/token where ASCII runs ~4."""
    assert estimate_tokens("开始游戏") > estimate_tokens("abcd")


def test_the_estimate_grows_with_length():
    assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_a_custom_estimator_is_honoured():
    """A real tokenizer must be swappable — the heuristic is a default,
    never an assumption baked into the assembler."""
    assembler = ContextAssembler(budget_tokens=1000,
                                 estimator=lambda text: len(text))
    result = assembler.assemble([Block(tier=TIER_TASK, text="abc")])
    assert result.tokens == len(result.text)


# ---------------------------------------------------------- the budget

def test_everything_fits_when_the_budget_is_generous():
    assembler = ContextAssembler(budget_tokens=100_000, reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_TASK, 10, "task"), _block(TIER_HISTORY, 10, "h1")])
    assert result.complete and not result.elisions


def test_history_is_dropped_before_evidence():
    """Tier order under pressure: what a tool returned THIS turn is why the
    model was called; a turn from ten minutes ago is not."""
    # 150 fits ONE 100-token block, so the tiers must actually choose.
    assembler = ContextAssembler(budget_tokens=150, reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_EVIDENCE, 100, "evidence"),
        _block(TIER_HISTORY, 100, "old-turn")])
    assert "evidence" in result.included
    assert "old-turn" not in result.included


def test_the_oldest_history_goes_first():
    """The previous `history[-30:]` dropped by COUNT regardless of size and
    never said it had. Order is by recency, and the loss is reported."""
    # Room for exactly one of the two, so recency has to decide.
    assembler = ContextAssembler(
        budget_tokens=90 + ContextAssembler.NOTICE_RESERVE_TOKENS,
        reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_HISTORY, 60, "oldest", order=0),
        _block(TIER_HISTORY, 60, "newest", order=9)])
    assert "newest" in result.included
    assert "oldest" not in result.included


def test_the_playbook_yields_before_the_conversation_is_gutted():
    """A stage playbook is guidance that can be re-derived; the operator's
    own words cannot."""
    # EQUAL-sized blocks, so only the TIER can decide which survives — a
    # size difference here would let the test pass without the tier logic
    # working at all.
    assembler = ContextAssembler(
        budget_tokens=250 + ContextAssembler.NOTICE_RESERVE_TOKENS,
        reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_EVIDENCE, 100, "evidence"),
        _block(TIER_PLAYBOOK, 100, "playbook"),
        _block(TIER_HISTORY, 100, "turn")])
    assert result.included == ["evidence", "playbook"]
    assert "turn" not in result.included


def test_a_zero_or_negative_budget_is_refused():
    with pytest.raises(ValueError):
        ContextAssembler(budget_tokens=0)


def test_the_reply_reserve_is_held_back():
    """A budget spent entirely on input leaves nothing to answer with."""
    assembler = ContextAssembler(budget_tokens=1000, reserve_tokens=900)
    assert assembler.usable == 100 - ContextAssembler.NOTICE_RESERVE_TOKENS


def test_a_starved_budget_is_detectable():
    """When reservations eat the whole budget, `usable` clamps to 1 and
    assembly degenerates silently: nothing clears MIN_TRIM_TOKENS so the
    trim path never runs, and protected blocks pass through whole. That
    looks identical to broken trimming, so it needs a name."""
    starved = ContextAssembler(budget_tokens=1000, reserve_tokens=995)
    assert starved.starved
    assert not ContextAssembler(budget_tokens=10_000,
                                reserve_tokens=100).starved


def test_the_context_notice_has_reserved_room():
    """The notice is appended AFTER selection, so it is outside every
    block's allowance. Unreserved, it spent budget nobody accounted for —
    the exact class of bug this module exists to remove."""
    assembler = ContextAssembler(budget_tokens=1000, reserve_tokens=0)
    assert assembler.usable == 1000 - ContextAssembler.NOTICE_RESERVE_TOKENS


# ------------------------------------- the invariant: nothing vanishes

def test_every_drop_is_announced_in_the_text():
    """THE invariant. The elision list is not enough — the model reads the
    TEXT, and a notice it never sees cannot stop it from answering from an
    absence."""
    assembler = ContextAssembler(budget_tokens=200, reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_EVIDENCE, 80, "evidence"),
        _block(TIER_HISTORY, 200, "dropped-turn")])
    assert "CONTEXT NOTICE" in result.text
    assert "your view is partial" in result.text


def test_a_complete_context_carries_no_notice():
    """The notice must mean something: it cannot appear when nothing was
    cut, or the model learns to ignore it."""
    assembler = ContextAssembler(budget_tokens=100_000, reserve_tokens=0)
    result = assembler.assemble([_block(TIER_TASK, 10, "task")])
    assert "CONTEXT NOTICE" not in result.text
    assert result.complete


def test_the_trim_marker_is_paid_for_out_of_the_allowance():
    """The marker is part of what gets sent, so it comes OUT of the
    allowance. Sizing the head to the full allowance and then appending
    the marker pushed the block back over budget — worst near
    MIN_TRIM_TOKENS, where the marker is a large share of the whole."""
    assembler = ContextAssembler(budget_tokens=MIN_TRIM_TOKENS + 100,
                                 reserve_tokens=0)
    block = Block(tier=TIER_EVIDENCE, text="data " * 5000, label="big",
                  trimmable=True)
    trimmed, _dropped = assembler._trim(block, assembler.usable)
    assert trimmed.tokens(estimate_tokens) <= assembler.usable


def test_the_request_is_rendered_last_whatever_its_tier():
    """Tier order decides what SURVIVES; it should not decide what the
    model reads last. The request is pinned to the end so the notice can
    sit beside it."""
    assembler = ContextAssembler(budget_tokens=100_000, reserve_tokens=0)
    result = assembler.assemble([
        Block(tier=TIER_EVIDENCE, order=10_000, label=REQUEST_LABEL,
              text="THE-REQUEST"),
        Block(tier=TIER_EVIDENCE, order=0, label="e0", text="EVIDENCE"),
        Block(tier=TIER_TASK, text="TASK")])
    assert result.text.index("EVIDENCE") < result.text.index("THE-REQUEST")
    assert result.text.rstrip().endswith("THE-REQUEST")


def test_the_notice_sits_immediately_after_the_request():
    """Recency is the point: a notice buried mid-context is one the model
    is least likely to honour."""
    assembler = ContextAssembler(budget_tokens=200, reserve_tokens=0)
    result = assembler.assemble([
        Block(tier=TIER_EVIDENCE, order=10_000, label=REQUEST_LABEL,
              text="THE-REQUEST"),
        _block(TIER_HISTORY, 200, "dropped")])
    assert result.text.index("THE-REQUEST") < result.text.index(
        "CONTEXT NOTICE")


def test_a_trimmed_block_says_so_at_the_cut():
    """Truncation is marked INLINE, where the content stops — the model
    must not read a severed tail as the end of the content."""
    assembler = ContextAssembler(budget_tokens=400, reserve_tokens=0)
    result = assembler.assemble([
        Block(tier=TIER_EVIDENCE, text="data " * 2000, label="big",
              trimmable=True)])
    assert "TRUNCATED" in result.text
    assert "Do NOT describe content beyond this point" in result.text


def test_the_notice_tells_the_model_what_to_do_about_it():
    """Naming the gap is half the job; the other half is the recovery —
    re-read it or say so, rather than infer."""
    assembler = ContextAssembler(budget_tokens=200, reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_EVIDENCE, 80, "evidence"),
        _block(TIER_HISTORY, 200, "gone")])
    assert "re-read it with a tool rather than inferring" in result.text


def test_elisions_are_itemised_for_the_caller():
    assembler = ContextAssembler(budget_tokens=200, reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_EVIDENCE, 80, "keep"),
        _block(TIER_HISTORY, 200, "lost")])
    assert [e.label for e in result.elisions] == ["lost"]
    assert result.elisions[0].kind == "dropped"
    assert result.elisions[0].dropped_tokens > 0


# --------------------------------------- protected tiers cannot be evicted

def test_a_huge_tool_result_cannot_evict_the_job_state():
    """The failure this ordering prevents: an agent that loses track of the
    stage calls the wrong stage's actions, and does it confidently."""
    assembler = ContextAssembler(budget_tokens=150, reserve_tokens=0)
    result = assembler.assemble([
        Block(tier=TIER_TASK, text="phase=ASSET gate=G1", label="job-state"),
        Block(tier=TIER_EVIDENCE, text="x " * 50_000, label="huge",
              trimmable=True)])
    assert "job-state" in result.included
    assert "phase=ASSET" in result.text


def test_going_over_budget_is_reported_not_hidden():
    """If the protected tiers alone exceed the budget, the budget is wrong.
    Saying so beats shipping a context that cannot work."""
    assembler = ContextAssembler(budget_tokens=50, reserve_tokens=0)
    result = assembler.assemble([
        Block(tier=TIER_TASK, text="t " * 500, label="job-state")])
    assert result.over_budget
    assert "job-state" in result.included


def test_the_protected_set_is_the_three_that_matter():
    assert PROTECTED == {TIER_SYSTEM, TIER_TASK, TIER_EVIDENCE}


def test_a_block_too_small_to_trim_usefully_is_dropped_whole():
    """A 20-token fragment of a tool result informs nothing and still costs
    budget; dropping it and saying so is more honest."""
    assembler = ContextAssembler(budget_tokens=MIN_TRIM_TOKENS - 20,
                                 reserve_tokens=0)
    result = assembler.assemble([
        _block(TIER_TASK, 5, "task"),
        Block(tier=TIER_HISTORY, text="y " * 5000, label="big-history",
              trimmable=True)])
    assert "big-history" not in result.included


# ------------------------------------------------------- determinism

def test_the_same_inputs_produce_the_same_context():
    """Reproducibility is what relevance scoring would have cost, and it is
    what lets a session be replayed from its trace."""
    blocks = [_block(TIER_EVIDENCE, 100, "e"), _block(TIER_HISTORY, 100, "h"),
              _block(TIER_PLAYBOOK, 100, "p")]
    a = ContextAssembler(budget_tokens=300, reserve_tokens=0)
    b = ContextAssembler(budget_tokens=300, reserve_tokens=0)
    assert a.assemble(blocks).text == b.assemble(blocks).text


def test_input_order_does_not_change_the_result():
    """Selection is by tier and recency, never by the order blocks happened
    to be appended."""
    blocks = [_block(TIER_HISTORY, 50, "h", order=1),
              _block(TIER_EVIDENCE, 50, "e"),
              _block(TIER_TASK, 50, "t")]
    assembler = ContextAssembler(budget_tokens=400, reserve_tokens=0)
    assert (assembler.assemble(blocks).included
            == assembler.assemble(list(reversed(blocks))).included)


def test_tiers_are_rendered_highest_priority_first():
    assembler = ContextAssembler(budget_tokens=100_000, reserve_tokens=0)
    result = assembler.assemble([
        Block(tier=TIER_HISTORY, text="HISTORY-MARK"),
        Block(tier=TIER_TASK, text="TASK-MARK"),
        Block(tier=TIER_EVIDENCE, text="EVIDENCE-MARK")])
    assert (result.text.index("TASK-MARK")
            < result.text.index("EVIDENCE-MARK")
            < result.text.index("HISTORY-MARK"))


# ------------------------------------------------------------ helpers

def test_history_blocks_number_turns_in_order():
    blocks = history_blocks([("operator", "a"), ("orbit8", "b")])
    assert [b.order for b in blocks] == [0, 1]
    assert blocks[0].text.startswith("[operator]")


def test_history_blocks_are_not_trimmable():
    """Half an operator instruction is worse than a stated absence."""
    assert all(not b.trimmable
               for b in history_blocks([("operator", "x" * 9000)]))


def test_an_empty_assembly_is_valid():
    result = ContextAssembler(budget_tokens=100).assemble([])
    assert result.text == "" and result.complete


def test_the_summary_reports_completeness():
    assembler = ContextAssembler(budget_tokens=100_000, reserve_tokens=0)
    assert "complete" in assembler.assemble(
        [_block(TIER_TASK, 5)]).summary()


# ------------------------------------------------------- the budget knob

def test_the_budget_defaults_to_the_conservative_value(monkeypatch):
    """100k is chosen to be servable by every current model, so the budget
    cannot promise room the API refuses."""
    from orbit8.orchestrator import CONTEXT_BUDGET_TOKENS, context_budget
    monkeypatch.delenv("ORBIT8_CONTEXT_BUDGET", raising=False)
    assert context_budget() == CONTEXT_BUDGET_TOKENS == 100_000


def test_the_budget_can_be_raised_without_editing_source(monkeypatch):
    """The value is provisional — it should track the model's real window,
    and that must not require a code change."""
    from orbit8.orchestrator import context_budget
    monkeypatch.setenv("ORBIT8_CONTEXT_BUDGET", "500000")
    assert context_budget() == 500_000


@pytest.mark.parametrize("bad", ["nonsense", "", "-5", "0", "12.5"])
def test_a_bad_budget_override_falls_back_rather_than_raising(monkeypatch,
                                                              bad):
    """A malformed env var must not make the chat interface unusable."""
    from orbit8.orchestrator import CONTEXT_BUDGET_TOKENS, context_budget
    monkeypatch.setenv("ORBIT8_CONTEXT_BUDGET", bad)
    assert context_budget() == CONTEXT_BUDGET_TOKENS
