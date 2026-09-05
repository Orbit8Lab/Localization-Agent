"""Chat orchestrator: the JSON tool loop drives real controller tools; the
model layer is scripted so tests are deterministic and offline."""
import json
from pathlib import Path

import pytest

from orbit8.controller import Job
from orbit8.orchestrator import ChatOrchestrator
from orbit8.schemas import IntakeBrief


class ScriptedProvider:
    name = "scripted"
    model = "test"
    tokens_spent = 0.0

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.prompts.append(user)
        return self.outputs.pop(0)


@pytest.fixture()
def job(tmp_path: Path) -> Job:
    source = tmp_path / "s.json"
    source.write_text(json.dumps({"UI_A": "你好"}), encoding="utf-8")
    intake = IntakeBrief(game="G", source_lang="zh", target_locales=["ko"])
    return Job.init(tmp_path / "jobs", "j1", intake=intake,
                    source_files=[str(source)])


def _call(tool, args=None, message=None):
    return json.dumps({"tool": tool, "args": args or {}, "message": message})


def test_status_then_respond(job: Job):
    provider = ScriptedProvider([
        _call("status"),
        _call("respond", message="Job is at INTAKE, no gates approved."),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    reply = chat.turn("现在到哪一步了？")
    assert "INTAKE" in reply
    # the status observation reached the model on the second step
    assert '"phase": "INTAKE"' in provider.prompts[1]


def test_next_step_and_gate_stop(job: Job):
    provider = ScriptedProvider([
        _call("next_step"),
        _call("respond", message="Ran intake analysis; now waiting on G0."),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    chat.turn("推进一步")
    assert job.store.exists(0, "market_report")     # the tool really ran
    assert '"pending_gate": "G0"' in provider.prompts[1]


def test_approve_records_operator_not_model(job: Job):
    provider = ScriptedProvider([
        _call("next_step"),
        _call("approve", {"gate": "G0"}),
        _call("respond", message="G0 approved."),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    chat.turn("跑市场分析，然后我确认通过 G0")
    record = job.control["approvals"]["G0"]
    assert record["by"] == "tian"                   # the human, never the LLM
    assert "chat" in record["note"]


def test_out_of_order_approve_is_an_error_observation(job: Job):
    provider = ScriptedProvider([
        _call("approve", {"gate": "G3"}),           # G3 is not pending
        _call("respond", message="Cannot approve G3 yet."),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    chat.turn("approve G3")
    assert "G3" not in job.control["approvals"]     # controller refused
    assert "error" in provider.prompts[1].lower()   # error surfaced as data


def test_unknown_tool_is_reported_to_the_model(job: Job):
    provider = ScriptedProvider(
        [_call("delete_everything")] +              # not in the tool set
        [_call("respond", message="I cannot do that")])
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    chat.turn("do something weird")
    assert "unknown tool" in provider.prompts[1]


def test_the_step_limit_reply_says_what_was_done(job: Job):
    """The old message named neither what the turn learned nor what to
    type next, so the operator restarted work already done. An unfinished
    turn should be resumable."""
    reply = ChatOrchestrator._step_limit_reply([
        '[tool] list_files({"dir": "10-received"}) -> ["a.xlsx", "b.xlsx"]',
        '[tool] standardize({"files": ["a.xlsx"]}) -> error: bad format',
    ])
    assert "list_files" in reply and "standardize" in reply
    assert "error: bad format" in reply          # failures stay visible
    assert "continue" in reply                   # and how to resume


def test_a_long_result_is_summarised_not_dumped(job: Job):
    """The summary must stay readable — replaying 12KB of tool output
    would bury the one line that matters."""
    reply = ChatOrchestrator._step_limit_reply(
        ['[tool] inspect_file({"path": "big.po"}) -> ' + "x" * 5000])
    assert "5000 chars" in reply
    assert "xxxxxxxxxx" not in reply


def test_the_step_cap_is_env_overridable(monkeypatch):
    """Tunable per box for the same reason as the context budget: what
    counts as a long turn depends on the project."""
    from orbit8.orchestrator import MAX_STEPS_PER_TURN, max_steps_per_turn
    monkeypatch.delenv("ORBIT8_MAX_STEPS", raising=False)
    assert max_steps_per_turn() == MAX_STEPS_PER_TURN
    monkeypatch.setenv("ORBIT8_MAX_STEPS", "30")
    assert max_steps_per_turn() == 30
    monkeypatch.setenv("ORBIT8_MAX_STEPS", "nonsense")
    assert max_steps_per_turn() == MAX_STEPS_PER_TURN


def test_repeating_one_call_stops_before_the_step_cap(job: Job):
    """This used to run to the 14-step limit and report "step limit",
    which hid WHY. A tool called repeatedly with identical args returns
    identical results — no new information — so the turn now stops and
    says so, whether or not the call "succeeded"."""
    provider = ScriptedProvider([_call("status")] * 14)
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    reply = chat.turn("check the status forever")
    assert "returned the same result" in reply
    assert len(provider.prompts) < 14                # stopped early
