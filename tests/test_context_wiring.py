"""Tool results must reach the EVIDENCE tier (context.py + orchestrator).

The tier design is only worth having if the thing it was built for
actually flows through it. It did not: `turn()` called
`_transcript(pending_user)` with no evidence and appended every tool
result straight to `self.history`, so the EVIDENCE tier held nothing but
the request block, and tool output — the whole reason the model was
called — competed in HISTORY: lowest priority, and not trimmable, so
under pressure it was dropped WHOLE rather than cut down.

A tier system nothing routes through is decoration. These tests pin the
wiring, not just the sorting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbit8.context import (TIER_EVIDENCE, TIER_HISTORY, Block,
                            ContextAssembler)
from orbit8.controller import Job
from orbit8.llm import EchoProvider
from orbit8.orchestrator import ChatOrchestrator
from orbit8.schemas import IntakeBrief


def _set_budget(chat: ChatOrchestrator, content_tokens: int) -> None:
    """Give the assembler `content_tokens` of USABLE room.

    Setting `budget_tokens` alone is a trap: the live assembler reserves
    the reply headroom plus the whole SYSTEM prompt (~4.4k), so a naive
    `budget_tokens = 1500` leaves `usable == 1`. Every block then looks
    unfittable, nothing clears MIN_TRIM_TOKENS so the trim path is skipped,
    and protected blocks pass through whole — which looks exactly like the
    trimming being broken.
    """
    chat.assembler.budget_tokens = (
        content_tokens + chat.assembler.reserve_tokens
        + ContextAssembler.NOTICE_RESERVE_TOKENS)
    assert not chat.assembler.starved


@pytest.fixture
def chat(tmp_path: Path) -> ChatOrchestrator:
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始游戏"}', encoding="utf-8")
    job = Job.init(tmp_path / "jobs", "demo",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["ko"]),
                   source_files=[str(source)])
    return ChatOrchestrator(job, EchoProvider("ko"), operator="tester")


def test_evidence_passed_in_reaches_the_evidence_tier(chat):
    result = chat.build_context("what changed?",
                                evidence=["[tool] status(...) -> {...}"])
    assert "evidence-0" in result.included


def test_a_tool_result_outranks_the_conversation(chat):
    """The ordering that was inert before: this turn's tool output must
    survive when an old turn does not."""
    chat.history = [("operator", "x " * 4000), ("orbit8", "y " * 4000)]
    chat.assembler.budget_tokens = 900
    result = chat.build_context("summarize", evidence=["FRESH-TOOL-RESULT"])
    assert "FRESH-TOOL-RESULT" in result.text


def test_a_huge_tool_result_is_trimmed_rather_than_dropped(chat):
    """In HISTORY it was non-trimmable, so an oversized result vanished
    entirely. As evidence it keeps its head and says what was cut."""
    _set_budget(chat, 1500)
    result = chat.build_context("read it", evidence=["DATA " * 200_000])
    assert "DATA" in result.text
    assert "TRUNCATED" in result.text


def test_the_job_state_still_outranks_a_huge_tool_result(chat):
    """Promoting evidence must not let it evict the task state — an agent
    that loses the stage calls the wrong stage's actions."""
    chat.assembler.budget_tokens = 1200
    result = chat.build_context("read it", evidence=["DATA " * 200_000])
    assert "job-state" in result.included
    assert "[job state]" in result.text


def test_evidence_retires_into_history_after_the_turn(chat):
    """Evidence means 'what happened THIS turn'. Without demotion, every
    past turn's output would keep outranking the current one forever."""
    evidence = ["[tool] status(...) -> ok"]
    chat._retire_evidence(evidence)
    assert evidence == []
    assert ("tool", "[tool] status(...) -> ok") in chat.history


def test_retired_evidence_is_no_longer_high_tier(chat):
    """After retirement the same text competes as history — which is
    correct, and is what makes room for the NEXT turn's evidence."""
    chat._retire_evidence(["[tool] old-result"])
    result = chat.build_context("next question", evidence=["new-result"])
    assert "evidence-0" in result.included          # the new one
    assert any(label.startswith("turn-") for label in result.included)


def test_the_turn_loop_passes_evidence_to_the_assembler(chat, monkeypatch):
    """The wiring itself: `turn()` must hand its accumulated tool results
    to `_transcript`. Passing none is what made the tier unreachable."""
    seen = {}

    def spy(user_msg, evidence=None):
        seen["evidence"] = list(evidence or [])
        return "transcript"

    monkeypatch.setattr(chat, "_transcript", spy)

    calls = iter([
        '{"tool": "status", "args": {}}',
        '{"tool": "respond", "message": "done"}',
    ])
    monkeypatch.setattr("orbit8.orchestrator.complete_json",
                        lambda *a, **k: __import__("orbit8.orchestrator",
                                                   fromlist=["ToolCall"]
                                                   ).ToolCall
                        .model_validate_json(next(calls)))
    chat.turn("what is the status?")
    # by the second model call the status result must be in evidence
    assert seen["evidence"], "tool results never reached the assembler"
    assert "status" in seen["evidence"][0]
