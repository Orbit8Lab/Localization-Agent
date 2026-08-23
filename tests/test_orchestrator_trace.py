"""The session trace — the agent's debugging surface."""
import json
from pathlib import Path

from orbit8.controller import Job
from orbit8.orchestrator import ChatOrchestrator
from orbit8.schemas import IntakeBrief


class ScriptedProvider:
    """Replays a fixed list of tool-call JSON blobs."""
    name, model = "scripted", "test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.tokens_spent = 0.0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        return json.dumps(self.replies.pop(0))


def _job(tmp_path: Path) -> Job:
    source = tmp_path / "strings.json"
    source.write_text(json.dumps([{"key": "K1", "text": "你好"}]),
                      encoding="utf-8")
    intake = IntakeBrief(game="G", source_lang="zh",
                         target_locales=["en"], genre=[],
                         engine="unreal")
    return Job.init(tmp_path / "jobs", "j1", intake=intake,
                    source_files=[str(source)])


def test_trace_records_args_results_and_errors(tmp_path: Path):
    job = _job(tmp_path)
    trace_path = tmp_path / "trace.jsonl"
    chat = ChatOrchestrator(
        job,
        ScriptedProvider([
            {"tool": "list_files", "args": {"dir": "/nope-does-not-exist"}},
            {"tool": "no_such_tool", "args": {}},
            {"tool": "respond", "args": {}, "message": "done"},
        ]),
        operator="tester", trace_path=trace_path)
    assert chat.turn("look around") == "done"

    calls = [r for r in chat.trace if r["event"] == "tool"]
    assert [c["tool"] for c in calls] == ["list_files", "no_such_tool"]
    # arguments are captured verbatim — the thing the console line hides
    assert calls[0]["args"] == {"dir": "/nope-does-not-exist"}
    assert "seconds" in calls[0]
    # an unknown tool is recorded as a failure, not silently swallowed
    assert calls[1]["error"] == "unknown tool"
    # operator message and final reply bracket the turn
    events = [r["event"] for r in chat.trace]
    assert events[0] == "operator" and events[-1] == "respond"
    assert all(r["turn"] == 1 for r in chat.trace)

    # the on-disk JSONL mirrors the in-memory trace, one record per line
    lines = trace_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == len(chat.trace)
    assert json.loads(lines[1])["tool"] == "list_files"


def test_trace_survives_tool_exception(tmp_path: Path):
    job = _job(tmp_path)
    chat = ChatOrchestrator(
        job,
        ScriptedProvider([
            # missing required arg → handler raises → recorded, not fatal
            {"tool": "read_artifact", "args": {}},
            {"tool": "respond", "args": {}, "message": "ok"},
        ]),
        operator="tester")
    assert chat.turn("read something") == "ok"
    call = [r for r in chat.trace if r["event"] == "tool"][0]
    assert call["error"] or call["result"].startswith("error:")


def test_trace_is_optional(tmp_path: Path):
    job = _job(tmp_path)
    chat = ChatOrchestrator(
        job, ScriptedProvider([{"tool": "respond", "args": {},
                                "message": "hi"}]),
        operator="tester")                     # no trace_path
    assert chat.turn("hello") == "hi"
    assert chat.trace and chat.trace_path is None
