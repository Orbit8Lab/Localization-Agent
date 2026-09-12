"""The job assistant.

Two things matter more than answer quality. First, the assistant must be
grounded in the job's real artifacts rather than the model's impression
of localization. Second, and load-bearing: it must have **no** path to
acting. Its audience cannot judge whether locking a glossary is correct,
so "ok do it" must not be able to sign that decision.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from orbit8.server import chat, explain            # noqa: E402
from orbit8.server.app import create_app           # noqa: E402
from orbit8.server.runner import JobRunner         # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    return TestClient(create_app(jobs_root=tmp_path / "jobs", runner=runner))


def _job(client: TestClient) -> str:
    r = client.post("/jobs",
                    files={"file": ("s.json", io.BytesIO(b'{"K":"v"}'))},
                    data={"game": "SongOfKings", "source_lang": "zh",
                          "target_locales": "en", "autostart": "false"})
    return r.json()["job_id"]


# ------------------------------------------- one agent, not two

def test_the_web_chat_drives_the_cli_orchestrator():
    """A second agent for the browser would drift from the CLI one. The
    endpoint must construct `ChatOrchestrator`, not reimplement it."""
    source = Path("src/orbit8/server/chat.py").read_text(encoding="utf-8")
    assert "ChatOrchestrator" in source
    assert "agent.turn(" in source


def test_consequential_tools_are_gated_not_removed():
    """Full parity with the CLI: the tools exist, but signature and spend
    actions ask first."""
    from orbit8.server.consent import CONFIRMS
    from orbit8.orchestrator import ChatOrchestrator
    names = ChatOrchestrator.tool_names()
    assert "approve" in names and "approve" in CONFIRMS
    for gated in CONFIRMS:
        assert gated in names, f"{gated} is gated but is not a real tool"


# -------------------------------------------------------- grounding

def test_context_carries_the_real_stage_and_game(client: TestClient,
                                                 tmp_path: Path):
    from orbit8.controller import Job
    jid = _job(client)
    context = explain.collect(Job(tmp_path / "jobs", jid))
    assert context["game"] == "SongOfKings"
    assert context["source_lang"] == "zh"
    assert context["stage"]["phase"] == "INTAKE"
    assert any(a["name"] == "intake" for a in context["artifacts"])


def test_context_survives_a_missing_artifact(client: TestClient,
                                             tmp_path: Path):
    """A job that has not reached a stage is normal, not an error — the
    whole answer must not fail because one read missed."""
    from orbit8.controller import Job
    jid = _job(client)
    context = explain.collect(Job(tmp_path / "jobs", jid))
    assert "glossary_health" not in context      # stage 3 not reached
    assert context["stage"]["phase"] == "INTAKE"


# --------------------------------------------------- opening message

def test_opening_needs_no_model_call(client: TestClient):
    """The panel's first message is instant and free; a spinner before
    the first word would defeat the point for a nervous user."""
    jid = _job(client)
    r = client.get(f"/jobs/{jid}/chat/opening")
    assert r.status_code == 200
    assert "SongOfKings" in r.json()["message"]


def test_opening_explains_a_pending_gate_in_plain_words(monkeypatch):
    """The console already says 'asset lock'. That phrase is the problem,
    so the opener must say what it commits you to."""
    context = {"game": "SongOfKings",
               "stage": {"phase": "ASSET", "action": "lock", "gate": "G1"},
               "pending_gate": "G1",
               "gate_explainer": {**explain.GATE_MEANING["G1"],
                                  "id": "G1", "label": "asset lock"}}
    msg = explain.opening_message(context)
    assert "G1" in msg
    assert "glossary" in msg.lower()
    assert "dev team" in msg


def test_opening_surfaces_blockers_when_a_gate_is_stuck():
    context = {"game": "G",
               "stage": {"phase": "ASSET", "action": "fix", "gate": "G1"},
               "pending_gate": "G1",
               "gate_explainer": {**explain.GATE_MEANING["G1"],
                                  "id": "G1", "label": "asset lock"},
               "glossary_health": {"en": {"blockers": ["3 terms have no target"],
                                          "warnings": [], "stats": {}}}}
    assert "3 terms have no target" in explain.opening_message(context)


def test_opening_says_nothing_is_needed_when_no_gate_pends():
    context = {"game": "G", "stage": {"phase": "LQA", "action": "scanning",
                                      "gate": None}}
    assert "Nothing needs you" in explain.opening_message(context)


def test_suggestions_track_the_pending_gate():
    gated = chat._suggestions({"gate_explainer": {"id": "G1"}})
    assert any("G1" in s for s in gated)
    idle = chat._suggestions({})
    assert all("G1" not in s for s in idle)


# ------------------------------------------------------ chat itself

def test_chat_rejects_an_empty_message(client: TestClient):
    jid = _job(client)
    assert client.post(f"/jobs/{jid}/chat", json={"message": "  "}).status_code == 400


def test_chat_rejects_an_oversized_message(client: TestClient):
    jid = _job(client)
    r = client.post(f"/jobs/{jid}/chat", json={"message": "x" * 5000})
    assert r.status_code == 400


def test_chat_on_an_unknown_job_is_404(client: TestClient):
    assert client.post("/jobs/nope/chat", json={"message": "hi"}).status_code == 404


def test_missing_api_key_reports_a_configuration_problem(client: TestClient,
                                                         monkeypatch):
    """A deployment without a chat key should say so, not show a stack
    trace in the panel."""
    def no_provider(*a, **k):
        raise RuntimeError("no api key")
    monkeypatch.setattr("orbit8.server.chat.build_provider", no_provider)
    jid = _job(client)
    r = client.post(f"/jobs/{jid}/chat", json={"message": "what is this?"})
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]


def test_a_model_failure_is_reported_not_swallowed(client: TestClient,
                                                   monkeypatch):
    class Boom:
        name, model = "x", "y"
        def complete(self, *a, **k):
            raise RuntimeError("upstream 504")
    monkeypatch.setattr("orbit8.server.chat.build_provider",
                        lambda *a, **k: Boom())
    jid = _job(client)
    r = client.post(f"/jobs/{jid}/chat", json={"message": "hi"})
    assert r.status_code == 502


def test_the_session_is_reused_across_requests(client: TestClient,
                                              monkeypatch):
    """The orchestrator keeps conversation state in memory; HTTP does
    not. Without a session, "yes, do that" would arrive with no idea what
    "that" was."""
    import orbit8.server.chat as chat_mod
    built = []

    class Fake:
        def __init__(self, job, provider, **k):
            built.append(1)
            self.job, self.provider = job, provider
            self.operator = "console"
            self.on_action = lambda *a: None
        def _tools(self):
            return {}
        def turn(self, message):
            return "ok"

    monkeypatch.setattr(chat_mod, "build_provider",
                        lambda *a, **k: type("P", (), {
                            "name": "f", "model": "m", "tokens_spent": 0.0})())
    monkeypatch.setattr(chat_mod, "ChatOrchestrator", Fake)
    jid = _job(client)
    client.post(f"/jobs/{jid}/chat", json={"message": "one"})
    client.post(f"/jobs/{jid}/chat", json={"message": "two"})
    assert len(built) == 1, "a second turn must reuse the same orchestrator"


def test_each_job_gets_its_own_session(client: TestClient, monkeypatch):
    """Two jobs must not share a conversation, or the assistant answers
    about the wrong project."""
    import orbit8.server.chat as chat_mod
    jobs = []

    class Fake:
        def __init__(self, job, provider, **k):
            jobs.append(job.job_id)
            self.job, self.provider = job, provider
            self.operator = "console"
            self.on_action = lambda *a: None
        def _tools(self):
            return {}
        def turn(self, message):
            return "ok"

    monkeypatch.setattr(chat_mod, "build_provider",
                        lambda *a, **k: type("P", (), {
                            "name": "f", "model": "m", "tokens_spent": 0.0})())
    monkeypatch.setattr(chat_mod, "ChatOrchestrator", Fake)
    a, b = _job(client), _job(client)
    client.post(f"/jobs/{a}/chat", json={"message": "hi"})
    client.post(f"/jobs/{b}/chat", json={"message": "hi"})
    assert set(jobs) == {a, b}


def test_chat_requires_the_same_auth_as_every_other_route(tmp_path: Path,
                                                          monkeypatch):
    """The assistant must not be a way around authentication."""
    monkeypatch.setenv("ORBIT8_BASIC_AUTH", "alice:pw")
    c = TestClient(create_app(jobs_root=tmp_path / "jobs",
                              runner=JobRunner(tmp_path / "jobs", dry_run=True)))
    assert c.post("/jobs/any/chat", json={"message": "hi"}).status_code == 401
    assert c.get("/jobs/any/chat/opening").status_code == 401
