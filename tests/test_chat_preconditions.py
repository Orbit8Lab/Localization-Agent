"""Two failures seen in a live session against a job that did not exist.

The session ran like this: `orbit8 chat` opened normally, printed a stage
playbook, and answered two questions describing the job's phase — all
before any tool revealed there was no job. The third turn called `status`
fourteen times, got byte-identical errors, exhausted the step budget, and
reported a generic "step limit" message that never showed the operator the
error explaining any of it.

Both halves are bugs, and they compound: the missing precondition creates
the impossible state, and the missing circuit breaker hides it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.cli import main
from orbit8.controller import Job
from orbit8.llm import EchoProvider
from orbit8.orchestrator import (MAX_STEPS_PER_TURN, REPEAT_FAILURE_LIMIT,
                                 ChatOrchestrator, ToolCall)
from orbit8.schemas import IntakeBrief


# ------------------------------------------------- the missing precondition

def test_chat_refuses_a_job_that_does_not_exist(tmp_path, capsys):
    """`Job()` constructs happily for a missing tree, so nothing stopped a
    session opening on one."""
    code = main(["chat", str(tmp_path / "jobs"), "demo-ko", "--by", "op"])
    assert code == 2
    assert "no job at" in capsys.readouterr().err


def test_the_refusal_says_how_to_fix_it(tmp_path, capsys):
    main(["chat", str(tmp_path / "jobs"), "demo-ko", "--by", "op"])
    assert "orbit8 job init" in capsys.readouterr().err


def test_the_refusal_lists_the_jobs_that_do_exist(tmp_path, capsys):
    """A mistyped id is at least as likely as a missing one."""
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    Job.init(tmp_path / "jobs", "real-ko",
             intake=IntakeBrief(game="G", source_lang="zh",
                                target_locales=["ko"]),
             source_files=[str(source)])

    main(["chat", str(tmp_path / "jobs"), "typo-ko", "--by", "op"])
    assert "real-ko" in capsys.readouterr().err


def test_the_refusal_does_not_need_an_api_key(tmp_path, monkeypatch, capsys):
    """The check must run BEFORE the provider is built, or a keyless box
    reports a missing key for what is really a missing job."""
    monkeypatch.delenv("DEEPSEEK_API", raising=False)
    monkeypatch.setenv("ORBIT8_ENV", str(tmp_path / "nonexistent"))
    assert main(["chat", str(tmp_path / "jobs"), "demo-ko",
                 "--by", "op"]) == 2
    assert "no job at" in capsys.readouterr().err


# ------------------------------------------------ attaching a source later

def test_a_job_can_be_created_without_a_source(tmp_path, capsys):
    """A job with no strings yet is a valid state — it waits at INTAKE, and
    only S1 needs the source. `--source` was required, which made the
    common case (project set up before the client's drop arrives)
    impossible from the CLI."""
    assert main(["job", "init", str(tmp_path / "jobs"), "later-ko",
                 "--game", "G", "--targets", "ko"]) == 0
    assert (tmp_path / "jobs" / "later-ko" / "job.json").exists()


def test_a_source_can_be_attached_afterwards(tmp_path):
    """The gap this closes: a csv/xlsx has to be standardized FIRST, and
    without this the only way to attach the result was to delete the job
    and redo the intake — throwing away gate approvals already earned."""
    source = tmp_path / "strings.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    main(["job", "init", str(tmp_path / "jobs"), "later-ko",
          "--game", "G", "--targets", "ko"])

    assert main(["job", "set-source", str(tmp_path / "jobs"), "later-ko",
                 str(source)]) == 0
    control = json.loads(
        (tmp_path / "jobs" / "later-ko" / "job.json").read_text())
    assert control["source_files"] == [str(source.resolve())]


def test_attaching_a_source_preserves_approvals(tmp_path):
    """Only source_files changes. Losing the intake artifact or a gate
    approval would make this a destructive operation dressed as a small
    one."""
    source = tmp_path / "strings.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    main(["job", "init", str(tmp_path / "jobs"), "later-ko",
          "--game", "G", "--targets", "ko"])
    job = Job(tmp_path / "jobs", "later-ko")
    job.next_step(dry_run=True)          # market analysis makes G0 pending
    job.approve("G0", by="tester")

    main(["job", "set-source", str(tmp_path / "jobs"), "later-ko",
          str(source)])
    assert Job(tmp_path / "jobs", "later-ko").approved("G0")


def test_an_existing_source_is_not_silently_replaced(tmp_path, capsys):
    """Discarding a configured source would make a re-run ingest something
    different from what the artifacts record."""
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    for path in (first, second):
        path.write_text('{"K":"开始"}', encoding="utf-8")
    main(["job", "init", str(tmp_path / "jobs"), "j", "--game", "G",
          "--targets", "ko", "--source", str(first)])

    assert main(["job", "set-source", str(tmp_path / "jobs"), "j",
                 str(second)]) == 2
    assert "--replace" in capsys.readouterr().err


def test_replace_overrides_the_guard(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    for path in (first, second):
        path.write_text('{"K":"开始"}', encoding="utf-8")
    main(["job", "init", str(tmp_path / "jobs"), "j", "--game", "G",
          "--targets", "ko", "--source", str(first)])

    assert main(["job", "set-source", str(tmp_path / "jobs"), "j",
                 str(second), "--replace"]) == 0
    control = json.loads((tmp_path / "jobs" / "j" / "job.json").read_text())
    assert control["source_files"] == [str(second.resolve())]


def test_a_missing_source_file_is_refused(tmp_path, capsys):
    main(["job", "init", str(tmp_path / "jobs"), "j", "--game", "G",
          "--targets", "ko"])
    assert main(["job", "set-source", str(tmp_path / "jobs"), "j",
                 "/nonexistent/strings.json"]) == 2
    assert "not found" in capsys.readouterr().err


def test_set_source_on_a_missing_job_is_refused(tmp_path, capsys):
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    assert main(["job", "set-source", str(tmp_path / "jobs"), "nope",
                 str(source)]) == 2
    assert "no job at" in capsys.readouterr().err


# --------------------------------------------------------- discovery

def test_job_list_answers_what_am_i_running(tmp_path, capsys):
    """Every other command requires knowing the job id already, so an
    operator returning to a machine had no way to find out what was on
    it."""
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    for job_id in ("alpha-ko", "beta-ja"):
        Job.init(tmp_path / "jobs", job_id,
                 intake=IntakeBrief(game="G", source_lang="zh",
                                    target_locales=["ko"]),
                 source_files=[str(source)])

    assert main(["job", "list", str(tmp_path / "jobs")]) == 0
    out = capsys.readouterr().out
    assert "alpha-ko" in out and "beta-ja" in out
    assert "INTAKE" in out


def test_job_list_on_an_empty_root_says_how_to_start(tmp_path, capsys):
    """'Nothing here' is a legitimate answer, and the useful half is the
    command that fixes it."""
    assert main(["job", "list", str(tmp_path / "jobs")]) == 0
    out = capsys.readouterr().out
    assert "no jobs under" in out
    assert "orbit8 job init" in out


def test_job_list_survives_a_damaged_job(tmp_path, capsys):
    """One unreadable tree must not hide every other job — that would make
    the discovery command useless exactly when it is most needed."""
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    Job.init(tmp_path / "jobs", "healthy",
             intake=IntakeBrief(game="G", source_lang="zh",
                                target_locales=["ko"]),
             source_files=[str(source)])
    broken = tmp_path / "jobs" / "broken"
    broken.mkdir(parents=True)
    (broken / "job.json").write_text("{not json", encoding="utf-8")

    assert main(["job", "list", str(tmp_path / "jobs")]) == 0
    out = capsys.readouterr().out
    assert "healthy" in out and "broken" in out


# ------------------------------------------------------ the repeat loop

@pytest.fixture
def chat(tmp_path) -> ChatOrchestrator:
    source = tmp_path / "s.json"
    source.write_text('{"K":"开始"}', encoding="utf-8")
    job = Job.init(tmp_path / "jobs", "demo",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["ko"]),
                   source_files=[str(source)])
    return ChatOrchestrator(job, EchoProvider("ko"), operator="op")


def _always_calls(monkeypatch, tool: str, args: dict):
    """Pin the model to one tool call forever — the observed behaviour."""
    monkeypatch.setattr(
        "orbit8.orchestrator.complete_json",
        lambda *a, **k: ToolCall(tool=tool, args=args))


def test_a_repeatedly_failing_call_stops_the_turn(chat, monkeypatch):
    """The core fix: identical inputs cannot produce a different outcome,
    so re-issuing the call only burns budget."""
    _always_calls(monkeypatch, "read_artifact",
                  {"stage": 0, "name": "does-not-exist"})
    reply = chat.turn("read that artifact")
    assert "failed the same way" in reply


def test_the_operator_sees_the_actual_error(chat, monkeypatch):
    """The old path ended in a generic step-limit message that hid the
    error — the operator was told 'I ran out of steps', not why."""
    _always_calls(monkeypatch, "read_artifact",
                  {"stage": 0, "name": "does-not-exist"})
    reply = chat.turn("read that artifact")
    assert "step limit" not in reply
    assert "does-not-exist" in reply or "error" in reply.lower()


def test_it_stops_well_before_the_step_budget(chat, monkeypatch):
    """14 identical calls took ~70 seconds of model time to reach a
    useless answer."""
    calls = []
    monkeypatch.setattr(
        "orbit8.orchestrator.complete_json",
        lambda *a, **k: (calls.append(1),
                         ToolCall(tool="read_artifact",
                                  args={"stage": 0, "name": "nope"}))[1])
    chat.turn("read it")
    assert len(calls) <= REPEAT_FAILURE_LIMIT + 1 < MAX_STEPS_PER_TURN


def test_an_unknown_tool_also_trips_the_breaker(chat, monkeypatch):
    """Hallucinating a nonexistent tool repeatedly is the same loop."""
    _always_calls(monkeypatch, "make_me_a_sandwich", {})
    assert "failed the same way" in chat.turn("do the thing")


def test_the_abort_is_traced(chat, monkeypatch):
    """A turn that ends early must say so in the trace, or the next
    session's episodic recall misreads it as a normal completion."""
    _always_calls(monkeypatch, "read_artifact", {"stage": 0, "name": "no"})
    chat.turn("read it")
    assert any(record.get("event") == "abort"
               and record.get("reason") == "repeated_failure"
               for record in chat.trace)


def test_a_different_error_does_not_trip_the_breaker(chat, monkeypatch):
    """Only IDENTICAL failures count. A tool failing differently each time
    is making progress — different inputs, different information."""
    counter = iter(range(MAX_STEPS_PER_TURN + 5))
    monkeypatch.setattr(
        "orbit8.orchestrator.complete_json",
        lambda *a, **k: ToolCall(tool="read_artifact",
                                 args={"stage": 0,
                                       "name": f"missing-{next(counter)}"}))
    reply = chat.turn("read them")
    assert "failed the same way" not in reply


def test_a_succeeding_call_never_trips_the_breaker(chat, monkeypatch):
    """Repeating a SUCCESSFUL call is not the failure mode being caught —
    it may be polling, and the step budget already bounds it."""
    _always_calls(monkeypatch, "status", {})
    reply = chat.turn("what is the status?")
    assert "failed the same way" not in reply


def test_the_turn_still_records_history_when_it_aborts(chat, monkeypatch):
    """An aborted turn is still a turn: the operator's message and the
    reply belong in history, or the next turn loses the thread."""
    _always_calls(monkeypatch, "read_artifact", {"stage": 0, "name": "no"})
    chat.turn("read it")
    assert ("operator", "read it") in chat.history
    assert any(role == "orbit8" for role, _ in chat.history)
