"""Episodic memory — reading back this job's own chat traces.

Traces have been written since the orchestrator existed and never read.
The gap is real: an operator saying "compare it to the file I showed you
earlier" refers to a fact the system recorded and then forgot.

Two properties are load-bearing:

- **Job scoping.** Recall never crosses into another job's traces, because
  a cross-job read is a cross-tenant read and PLAN §5.4 calls namespace
  leakage between tenants a policy break.
- **Actions, not results.** A recall says a file was inspected, never what
  it contained. Blurring that would let the agent "remember" file content
  it no longer has.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.episodic import Episode, EpisodicMemory


def _trace(path: Path, records: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False)
                  for record in records) + "\n", encoding="utf-8")
    return path


def _tool(turn: int, tool: str, **args) -> dict:
    return {"event": "tool", "turn": turn, "tool": tool, "args": args}


@pytest.fixture
def memory(tmp_path) -> EpisodicMemory:
    _trace(tmp_path / "traces" / "s1.jsonl", [
        {"event": "operator", "turn": 1, "message": "look at the drop"},
        _tool(1, "inspect_file", path="10-received/20260810-PO/Game.po"),
        _tool(2, "compare_po", old="old/Game.po", new="new/Game.po"),
        {"event": "respond", "turn": 2, "message": "12 entries changed"},
        _tool(3, "flagged", locale="ko"),
    ])
    return EpisodicMemory(tmp_path / "traces", job_id="demo")


# ------------------------------------------------------------- recall

def test_a_past_action_is_recalled_by_filename(memory):
    episodes = memory.recall("what did we see in Game.po")
    assert episodes
    assert any(e.tool == "inspect_file" for e in episodes)


def test_a_path_segment_matches_the_full_path(memory):
    """The operator says "Game.po"; the trace holds the whole path. A
    recall that only matched whole strings would never fire."""
    assert memory.recall("Game.po")


def test_recall_matches_a_tool_name(memory):
    assert any(e.tool == "compare_po" for e in memory.recall("compare_po"))


def test_an_unrelated_query_recalls_nothing(memory):
    """Recall must not pad the context with whatever it has. An irrelevant
    memory costs budget and invites the model to use it."""
    assert memory.recall("quarterly revenue projections") == []


def test_a_query_of_only_short_words_recalls_nothing(memory):
    """Two-letter tokens match everything; that is noise, not recall."""
    assert memory.recall("is it ok") == []


def test_recall_is_capped(memory):
    assert len(memory.recall("po", limit=2)) <= 2


def test_recent_returns_newest_first(memory):
    assert memory.recent(limit=1)[0].tool == "flagged"


# ------------------------------------------- actions, never results

def test_only_tool_events_are_recalled(memory):
    """Model prose is the least reliable and least dense part of a trace."""
    assert all(e.tool in {"inspect_file", "compare_po", "flagged"}
               for e in memory.recent(limit=10))


def test_the_rendered_block_says_it_holds_actions_not_content(memory):
    """The line that stops the agent 'remembering' file contents it does
    not have."""
    text = memory.as_block_text(memory.recent(limit=3))
    assert "NOT" in text and "Re-run a tool" in text


def test_an_empty_recall_renders_nothing(memory):
    """No memories must produce no block at all — not an empty header that
    spends budget saying nothing."""
    assert memory.as_block_text([]) == ""


def test_a_failed_call_is_recalled_with_its_failure(tmp_path):
    """Worth recalling precisely BECAUSE it failed: repeating a failing
    call verbatim is the most common wasted turn."""
    _trace(tmp_path / "t" / "s.jsonl", [
        {"event": "tool", "turn": 1, "tool": "inspect_file",
         "args": {"path": "missing.po"}, "failed": "no such file"}])
    episode, = EpisodicMemory(tmp_path / "t").recent()
    assert episode.failed == "no such file"
    assert "failed" in episode.describe()


# ------------------------------------------------------- job scoping

def test_recall_never_crosses_into_another_job(tmp_path):
    """PLAN §5.4: another job's trace is another tenant's data. The
    boundary is the directory, and it is not negotiable."""
    _trace(tmp_path / "job-a" / "s.jsonl",
           [_tool(1, "inspect_file", path="secret-client-file.po")])
    _trace(tmp_path / "job-b" / "s.jsonl",
           [_tool(1, "inspect_file", path="our-own-file.po")])

    episodes = EpisodicMemory(tmp_path / "job-b").recall("po")
    paths = [e.args.get("path") for e in episodes]
    assert "our-own-file.po" in paths
    assert "secret-client-file.po" not in paths


def test_the_current_session_can_be_excluded(tmp_path):
    """The live session's own turns are already in `history`; recalling
    them would duplicate them at a lower tier."""
    current = _trace(tmp_path / "t" / "now.jsonl",
                     [_tool(1, "inspect_file", path="current.po")])
    _trace(tmp_path / "t" / "before.jsonl",
           [_tool(1, "inspect_file", path="earlier.po")])
    memory = EpisodicMemory(tmp_path / "t")
    paths = [e.args.get("path") for e in memory.recall("po",
                                                       exclude=current)]
    assert paths == ["earlier.po"]


def test_multiple_sessions_of_one_job_are_all_visible(tmp_path):
    _trace(tmp_path / "t" / "s1.jsonl", [_tool(1, "flagged", locale="ko")])
    _trace(tmp_path / "t" / "s2.jsonl", [_tool(1, "flagged", locale="ja")])
    locales = {e.args.get("locale")
               for e in EpisodicMemory(tmp_path / "t").recall("flagged")}
    assert locales == {"ko", "ja"}


# ------------------------------------------------------- robustness

def test_a_torn_line_does_not_break_the_history(tmp_path):
    """Traces are best-effort appends; a session killed mid-write leaves a
    partial line. One torn line must not make the rest unreadable."""
    path = tmp_path / "t" / "s.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(_tool(1, "flagged", locale="ko")) + "\n"
        + '{"event": "tool", "turn": 2, "to\n'          # torn
        + json.dumps(_tool(3, "compare_po", old="a.po", new="b.po")) + "\n",
        encoding="utf-8")
    assert len(EpisodicMemory(tmp_path / "t").recent(limit=10)) == 2


def test_a_missing_trace_directory_is_empty_not_an_error(tmp_path):
    assert EpisodicMemory(tmp_path / "nope").recall("anything") == []


def test_a_trace_with_no_tool_calls_recalls_nothing(tmp_path):
    _trace(tmp_path / "t" / "s.jsonl",
           [{"event": "operator", "turn": 1, "message": "hello"}])
    assert EpisodicMemory(tmp_path / "t").recent() == []


def test_long_arguments_are_shortened_in_the_description():
    episode = Episode(session="s", turn=1, tool="standardize",
                      args={"files": ["x" * 300]})
    assert len(episode.describe()) < 200
