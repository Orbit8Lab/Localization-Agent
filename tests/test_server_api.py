"""The HTTP surface over the job controller.

These tests run the real `Job` against a tmp_path root with a dry-run
runner: no API key, no model call, no network. What is being checked is
that the API is a faithful projection of the controller — that status
comes from `derive()`, that a gate cannot be skipped, and that the
failure modes a browser can trigger (huge upload, bad job id, traversal)
are refused rather than served.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from orbit8.server.app import create_app           # noqa: E402
from orbit8.server.runner import JobRunner         # noqa: E402


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A dry-run runner: stages advance without spending a token."""
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    app = create_app(jobs_root=tmp_path / "jobs", runner=runner)
    return TestClient(app)


def _upload(client: TestClient, **over):
    body = {"game": "Test Game", "source_lang": "zh",
            "target_locales": "en,ja", "tenant_id": "acme",
            "autostart": "false"}
    body.update(over)
    return client.post(
        "/jobs",
        files={"file": ("strings.json",
                        io.BytesIO(json.dumps({"K1": "text"}).encode()),
                        "application/json")},
        data=body)


# ------------------------------------------------------------- the basics

def test_health_reports_the_jobs_root(client: TestClient):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_upload_creates_a_job_and_returns_immediately(client: TestClient):
    """The contract the UI depends on: POST returns a job id, not a
    finished job. A two-hour run must not hold the request open."""
    r = _upload(client)
    assert r.status_code == 201
    body = r.json()
    assert body["job_id"]
    assert body["game"] == "Test Game"
    assert body["target_locales"] == ["en", "ja"]


def test_status_comes_from_derive_not_a_stored_field(client: TestClient):
    """There is no job table. A fresh job must report the intake stage
    because that is what the artifact tree implies."""
    jid = _upload(client).json()["job_id"]
    stage = client.get(f"/jobs/{jid}").json()["stage"]
    assert stage["phase"] == "INTAKE"
    assert stage["action"]


def test_tenant_id_is_recorded_from_the_first_request(client: TestClient):
    """Threaded from day one so isolation is a filter later, not a
    migration."""
    jid = _upload(client).json()["job_id"]
    assert client.get(f"/jobs/{jid}").json()["tenant_id"] == "acme"


def test_jobs_can_be_listed_and_filtered_by_tenant(client: TestClient):
    _upload(client, tenant_id="acme")
    _upload(client, tenant_id="other")
    assert len(client.get("/jobs").json()) == 2
    only = client.get("/jobs", params={"tenant_id": "acme"}).json()
    assert len(only) == 1 and only[0]["tenant_id"] == "acme"


def test_the_uploaded_file_lands_in_the_job_directory(client: TestClient,
                                                      tmp_path: Path):
    jid = _upload(client).json()["job_id"]
    assert (tmp_path / "jobs" / jid / "10-received" / "strings.json").exists()


# ------------------------------------------------------------------ gates

def test_approving_the_wrong_gate_is_refused(client: TestClient):
    """The controller only clears the *pending* gate. Without this the
    API would be a way to skip G1 and translate against an unfrozen
    glossary."""
    jid = _upload(client).json()["job_id"]
    r = client.post(f"/jobs/{jid}/approve",
                    json={"gate": "G3", "by": "tester"})
    assert r.status_code == 409


def test_approving_an_unknown_gate_is_refused(client: TestClient):
    jid = _upload(client).json()["job_id"]
    r = client.post(f"/jobs/{jid}/approve",
                    json={"gate": "G9", "by": "tester"})
    assert r.status_code == 409


# ------------------------------------------------ what a browser can break

def test_unknown_job_is_404_not_500(client: TestClient):
    assert client.get("/jobs/nope").status_code == 404


@pytest.mark.parametrize("bad", ["../etc", "a/b", "a\\b"])
def test_job_id_path_traversal_is_refused(client: TestClient, bad: str):
    """`job_id` becomes a directory name; an unchecked value escapes the
    jobs root."""
    assert client.get(f"/jobs/{bad}").status_code in (400, 404)


def test_oversized_upload_is_refused_and_leaves_no_job(
        client: TestClient, tmp_path: Path, monkeypatch):
    """Enforced while streaming: reading the whole body first and
    checking after is how a big upload becomes an OOM."""
    monkeypatch.setattr("orbit8.server.app.MAX_UPLOAD_BYTES", 1024)
    r = client.post(
        "/jobs",
        files={"file": ("big.json", io.BytesIO(b"x" * 5000),
                        "application/json")},
        data={"game": "G", "source_lang": "zh", "target_locales": "en",
              "autostart": "false"})
    assert r.status_code == 413
    roots = [p for p in (tmp_path / "jobs").iterdir() if p.is_dir()]
    assert roots == [], "a refused upload must not leave a half-built job"


def test_empty_target_locales_is_refused(client: TestClient):
    r = _upload(client, target_locales="  ")
    assert r.status_code == 400


def test_duplicate_job_id_is_refused(client: TestClient):
    _upload(client, job_id="fixed-id")
    assert _upload(client, job_id="fixed-id").status_code == 409


# -------------------------------------------------------------- artifacts

def test_artifacts_are_listed_and_downloadable(client: TestClient):
    """Stage 0 holds the intake brief, so a fresh job already has one
    artifact to serve."""
    jid = _upload(client).json()["job_id"]
    refs = client.get(f"/jobs/{jid}/artifacts").json()
    assert any(r["name"] == "intake" and r["stage"] == 0 for r in refs)

    got = client.get(f"/jobs/{jid}/artifacts/0/intake")
    assert got.status_code == 200
    assert "Test Game" in got.text


def test_artifact_name_traversal_is_refused(client: TestClient):
    jid = _upload(client).json()["job_id"]
    r = client.get(f"/jobs/{jid}/artifacts/0/..%2F..%2Fjob")
    assert r.status_code in (400, 404)


def test_missing_artifact_is_404(client: TestClient):
    jid = _upload(client).json()["job_id"]
    assert client.get(f"/jobs/{jid}/artifacts/5/nothing").status_code == 404
