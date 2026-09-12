"""The web assistant acting on a job.

Full tool parity with the CLI, including `approve`. What the browser adds
is confirmation: a consequential tool describes itself and stops, and
runs only when the human sends the token back. These tests pin that the
gate is real — that a model deciding to approve does not, by itself,
approve.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from orbit8.server.app import create_app           # noqa: E402
from orbit8.server.consent import (CONFIRMS, ConsentLedger,  # noqa: E402
                                   describe, wrap_tools)
from orbit8.server.runner import JobRunner         # noqa: E402


# ------------------------------------------------------ the ledger

def test_a_consequential_tool_does_not_run_on_first_call():
    ran = []
    tools = {"approve": lambda a: ran.append(a) or "approved"}
    wrapped = wrap_tools(tools, job_id="j1", ledger=ConsentLedger())
    out = wrapped["approve"]({"gate": "G1"})
    assert "CONFIRMATION_REQUIRED" in out
    assert ran == [], "the action must not have happened"


def test_it_runs_once_the_token_comes_back():
    ran = []
    tools = {"approve": lambda a: ran.append(a) or "approved"}
    ledger = ConsentLedger()
    out = wrap_tools(tools, job_id="j1", ledger=ledger)["approve"]({"gate": "G1"})
    token = out.split("[token=")[-1].rstrip("]")
    result = wrap_tools(tools, job_id="j1", ledger=ledger,
                        approved_token=token)["approve"]({"gate": "G1"})
    assert result == "approved"
    assert ran == [{"gate": "G1"}]


def test_a_token_is_single_use():
    """Otherwise a retry, or a refresh, double-approves."""
    tools = {"approve": lambda a: "approved"}
    ledger = ConsentLedger()
    out = wrap_tools(tools, job_id="j1", ledger=ledger)["approve"]({"gate": "G1"})
    token = out.split("[token=")[-1].rstrip("]")
    wrap_tools(tools, job_id="j1", ledger=ledger,
               approved_token=token)["approve"]({"gate": "G1"})
    again = wrap_tools(tools, job_id="j1", ledger=ledger,
                       approved_token=token)["approve"]({"gate": "G1"})
    assert "CONFIRMATION_REQUIRED" in again


def test_a_token_cannot_cross_jobs():
    tools = {"approve": lambda a: "approved"}
    ledger = ConsentLedger()
    out = wrap_tools(tools, job_id="j1", ledger=ledger)["approve"]({"gate": "G1"})
    token = out.split("[token=")[-1].rstrip("]")
    other = wrap_tools(tools, job_id="j2", ledger=ledger,
                       approved_token=token)["approve"]({"gate": "G1"})
    assert "CONFIRMATION_REQUIRED" in other


def test_the_confirmed_action_is_the_one_the_human_saw():
    """The token carries the original args, so a model cannot describe
    approving G1 and then execute G3."""
    ran = []
    tools = {"approve": lambda a: ran.append(a) or "ok"}
    ledger = ConsentLedger()
    out = wrap_tools(tools, job_id="j1", ledger=ledger)["approve"]({"gate": "G1"})
    token = out.split("[token=")[-1].rstrip("]")
    wrap_tools(tools, job_id="j1", ledger=ledger,
               approved_token=token)["approve"]({"gate": "G3"})
    assert ran == [{"gate": "G1"}], "must run the args that were confirmed"


def test_a_token_expires(monkeypatch):
    import orbit8.server.consent as consent
    tools = {"approve": lambda a: "approved"}
    ledger = ConsentLedger()
    out = wrap_tools(tools, job_id="j1", ledger=ledger)["approve"]({"gate": "G1"})
    token = out.split("[token=")[-1].rstrip("]")
    monkeypatch.setattr(consent, "TOKEN_TTL_SECONDS", -1)
    late = wrap_tools(tools, job_id="j1", ledger=ledger,
                      approved_token=token)["approve"]({"gate": "G1"})
    assert "CONFIRMATION_REQUIRED" in late


def test_cheap_and_read_only_tools_are_not_gated():
    """Gating everything would make the assistant useless."""
    tools = {"status": lambda a: "ok", "read_artifact": lambda a: "{}",
             "list_artifacts": lambda a: "[]", "next_step": lambda a: "ran"}
    wrapped = wrap_tools(tools, job_id="j1", ledger=ConsentLedger())
    for name in tools:
        assert "CONFIRMATION_REQUIRED" not in wrapped[name]({})


def test_the_gated_set_covers_signature_and_spend():
    assert "approve" in CONFIRMS                     # a human's mark
    for expensive in ("translate_po", "lqa_run"):    # real money
        assert expensive in CONFIRMS


def test_the_description_names_the_gate():
    assert "G1" in describe("approve", {"gate": "G1"})


def test_a_missing_template_argument_does_not_crash_the_turn():
    assert describe("approve", {}) != ""


# ------------------------------------------------- through the API

class _Fake:
    """Stands in for the orchestrator: runs one scripted tool call."""
    def __init__(self, job, tool, args):
        self.job, self.tool, self.args = job, tool, args
        self.provider = type("P", (), {"name": "fake", "model": "m",
                                       "tokens_spent": 0.0})()
        self.operator = "console"
        self.on_action = lambda *a: None
        self.history = []

    def _tools(self):
        return {"approve": lambda a: self.job.approve(
            a["gate"], by=self.operator) or "approved"}

    def turn(self, message):
        observation = self._tools()[self.tool](self.args)
        self.on_action(self.tool, observation)
        return f"did {self.tool}"


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    app = create_app(jobs_root=tmp_path / "jobs",
                     runner=JobRunner(tmp_path / "jobs", dry_run=True))
    return TestClient(app)


def _job(client: TestClient, auth=None) -> str:
    kwargs = {"auth": auth} if auth else {}
    r = client.post("/jobs",
                    files={"file": ("s.json", io.BytesIO(b'{"K":"v"}'))},
                    data={"game": "G", "source_lang": "zh",
                          "target_locales": "en", "autostart": "false"},
                    **kwargs)
    return r.json()["job_id"]


def _job_at_g0(client: TestClient, jobs_root: Path, auth=None) -> str:
    """A job whose pending gate is actually G0.

    `approve` refuses any gate that is not pending, so a test that
    approves straight after create would exercise that refusal rather
    than the consent flow. Run stage steps until the gate appears.
    """
    from orbit8.controller import Job
    jid = _job(client, auth=auth)
    job = Job(jobs_root, jid)
    for _ in range(6):
        if job.derive().gate == "G0":
            return jid
        job.next_step(None, dry_run=True)
    raise AssertionError(f"job never reached G0: {job.derive()}")


def test_asking_to_approve_returns_a_pending_confirmation(client, monkeypatch,
                                                          tmp_path):
    """The end-to-end property: 'approve G0' does not approve G0."""
    from orbit8.controller import Job
    import orbit8.server.chat as chat_mod
    jid = _job_at_g0(client, tmp_path / "jobs")
    monkeypatch.setattr(
        chat_mod, "build_provider",
        lambda *a, **k: type("P", (), {"name": "f", "model": "m",
                                       "tokens_spent": 0.0})())
    monkeypatch.setattr(
        chat_mod, "ChatOrchestrator",
        lambda job, provider, **k: _Fake(job, "approve", {"gate": "G0"}))

    r = client.post(f"/jobs/{jid}/chat", json={"message": "approve G0"})
    assert r.status_code == 200
    body = r.json()
    assert body["pending"] is not None
    assert body["pending"]["tool"] == "approve"
    assert "G0" in body["pending"]["description"]
    # The job must be untouched.
    assert Job(tmp_path / "jobs", jid).control["approvals"] == {}


def test_confirming_performs_the_action(client, monkeypatch, tmp_path):
    from orbit8.controller import Job
    import orbit8.server.chat as chat_mod
    jid = _job_at_g0(client, tmp_path / "jobs")
    monkeypatch.setattr(
        chat_mod, "build_provider",
        lambda *a, **k: type("P", (), {"name": "f", "model": "m",
                                       "tokens_spent": 0.0})())
    monkeypatch.setattr(
        chat_mod, "ChatOrchestrator",
        lambda job, provider, **k: _Fake(job, "approve", {"gate": "G0"}))

    first = client.post(f"/jobs/{jid}/chat", json={"message": "approve G0"})
    token = first.json()["pending"]["token"]
    second = client.post(f"/jobs/{jid}/chat",
                         json={"message": "yes", "confirm_token": token})
    assert second.status_code == 200
    approvals = Job(tmp_path / "jobs", jid).control["approvals"]
    assert "G0" in approvals


def test_the_approval_is_attributed_to_the_authenticated_user(
        tmp_path, monkeypatch):
    """A gate signature must carry a name, not 'console'."""
    import orbit8.server.chat as chat_mod
    monkeypatch.setenv("ORBIT8_BASIC_AUTH", "alice:pw")
    app = create_app(jobs_root=tmp_path / "jobs",
                     runner=JobRunner(tmp_path / "jobs", dry_run=True))
    c = TestClient(app)
    jid = _job_at_g0(c, tmp_path / "jobs", auth=("alice", "pw"))
    monkeypatch.setattr(
        chat_mod, "build_provider",
        lambda *a, **k: type("P", (), {"name": "f", "model": "m",
                                       "tokens_spent": 0.0})())
    monkeypatch.setattr(
        chat_mod, "ChatOrchestrator",
        lambda job, provider, **k: _Fake(job, "approve", {"gate": "G0"}))

    first = c.post(f"/jobs/{jid}/chat", json={"message": "approve G0"},
                   auth=("alice", "pw"))
    token = first.json()["pending"]["token"]
    c.post(f"/jobs/{jid}/chat",
           json={"message": "yes", "confirm_token": token},
           auth=("alice", "pw"))

    from orbit8.controller import Job
    assert Job(tmp_path / "jobs", jid).control["approvals"]["G0"]["by"] == "alice"


def test_job_changed_tells_the_ui_to_refetch(client, monkeypatch, tmp_path):
    import orbit8.server.chat as chat_mod
    jid = _job_at_g0(client, tmp_path / "jobs")
    monkeypatch.setattr(
        chat_mod, "build_provider",
        lambda *a, **k: type("P", (), {"name": "f", "model": "m",
                                       "tokens_spent": 0.0})())
    monkeypatch.setattr(
        chat_mod, "ChatOrchestrator",
        lambda job, provider, **k: _Fake(job, "approve", {"gate": "G0"}))
    first = client.post(f"/jobs/{jid}/chat", json={"message": "approve G0"})
    assert first.json()["job_changed"] is False      # nothing happened yet
    token = first.json()["pending"]["token"]
    second = client.post(f"/jobs/{jid}/chat",
                         json={"message": "yes", "confirm_token": token})
    assert second.json()["job_changed"] is True
