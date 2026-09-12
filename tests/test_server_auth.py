"""HTTP Basic auth on the deployed API.

The beta runs on a public URL and the API spends model tokens, so the
interesting cases are the ways it could be open without anyone noticing:
a missing env var, an unknown user that answers differently from a wrong
password, or a route someone forgot to protect.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from orbit8.server.app import create_app           # noqa: E402
from orbit8.server.auth import parse_users         # noqa: E402
from orbit8.server.runner import JobRunner         # noqa: E402

CREDS = ("alice", "s3cret")


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ORBIT8_BASIC_AUTH", "alice:s3cret,bob:hunter2")
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    return TestClient(create_app(jobs_root=tmp_path / "jobs", runner=runner))


# ------------------------------------------------------------ parsing

def test_parse_users_handles_a_colon_in_the_password():
    assert parse_users("a:p:ss") == {"a": "p:ss"}


@pytest.mark.parametrize("raw", ["nocolon", "user:", ":password"])
def test_malformed_credentials_are_rejected_loudly(raw: str):
    """A silently-ignored malformed entry is a user who cannot log in, or
    worse, an auth table that is quietly empty."""
    with pytest.raises(ValueError):
        parse_users(raw)


# ----------------------------------------------------- enforcement

ROUTES = [
    ("get", "/jobs"),
    ("get", "/jobs/anything"),
    ("get", "/jobs/anything/artifacts"),
    ("get", "/jobs/anything/artifacts/0/intake"),
    ("post", "/jobs/anything/run"),
    ("post", "/jobs/anything/approve"),
]


@pytest.mark.parametrize("verb,path", ROUTES)
def test_every_job_route_requires_credentials(client: TestClient,
                                              verb: str, path: str):
    """Parameterized so a route added later without auth fails here."""
    assert getattr(client, verb)(path).status_code == 401


def test_upload_requires_credentials(client: TestClient):
    r = client.post("/jobs",
                    files={"file": ("s.json", io.BytesIO(b"{}"))},
                    data={"game": "G", "source_lang": "zh",
                          "target_locales": "en"})
    assert r.status_code == 401


def test_health_stays_open_for_the_platform_probe(client: TestClient):
    """Fly's health check runs without secrets and this leaks nothing."""
    assert client.get("/health").status_code == 200


def test_correct_credentials_are_accepted(client: TestClient):
    assert client.get("/jobs", auth=CREDS).status_code == 200


def test_a_second_user_also_works(client: TestClient):
    assert client.get("/jobs", auth=("bob", "hunter2")).status_code == 200


def test_wrong_password_is_refused(client: TestClient):
    assert client.get("/jobs", auth=("alice", "wrong")).status_code == 401


def test_unknown_user_is_refused(client: TestClient):
    assert client.get("/jobs", auth=("mallory", "s3cret")).status_code == 401


def test_a_users_password_does_not_work_for_another_user(client: TestClient):
    assert client.get("/jobs", auth=("alice", "hunter2")).status_code == 401


def test_challenge_header_is_sent(client: TestClient):
    """Without WWW-Authenticate a browser will not prompt."""
    r = client.get("/jobs")
    assert r.headers.get("www-authenticate", "").startswith("Basic")


# ------------------------------------------------------- fail closed

def test_production_without_credentials_refuses_to_boot(tmp_path: Path,
                                                        monkeypatch):
    """The likeliest way to publish an open API is forgetting the secret,
    so it must stop the boot rather than serve anonymously."""
    monkeypatch.delenv("ORBIT8_BASIC_AUTH", raising=False)
    monkeypatch.setenv("ORBIT8_ENV", "production")
    with pytest.raises(RuntimeError, match="ORBIT8_BASIC_AUTH"):
        create_app(jobs_root=tmp_path / "jobs",
                   runner=JobRunner(tmp_path / "jobs", dry_run=True))


def test_local_development_needs_no_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ORBIT8_BASIC_AUTH", raising=False)
    monkeypatch.delenv("ORBIT8_ENV", raising=False)
    app = create_app(jobs_root=tmp_path / "jobs",
                     runner=JobRunner(tmp_path / "jobs", dry_run=True))
    assert TestClient(app).get("/jobs").status_code == 200


def test_docs_are_off_in_production(tmp_path: Path, monkeypatch):
    """The schema names every field of every artifact; that is a detail
    customers do not need."""
    monkeypatch.setenv("ORBIT8_BASIC_AUTH", "a:b")
    monkeypatch.setenv("ORBIT8_ENV", "production")
    c = TestClient(create_app(jobs_root=tmp_path / "jobs",
                              runner=JobRunner(tmp_path / "jobs", dry_run=True)))
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404


def test_docs_can_be_re_enabled_explicitly(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORBIT8_BASIC_AUTH", "a:b")
    monkeypatch.setenv("ORBIT8_ENV", "production")
    monkeypatch.setenv("ORBIT8_ENABLE_DOCS", "1")
    c = TestClient(create_app(jobs_root=tmp_path / "jobs",
                              runner=JobRunner(tmp_path / "jobs", dry_run=True)))
    assert c.get("/docs").status_code == 200
