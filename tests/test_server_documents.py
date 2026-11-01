"""Delete and replace-source endpoints (issue #19)."""
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
    runner = JobRunner(tmp_path / "jobs", dry_run=True)
    return TestClient(create_app(jobs_root=tmp_path / "jobs", runner=runner))


def _upload(client: TestClient) -> str:
    r = client.post(
        "/jobs",
        files={"file": ("strings.json",
                        io.BytesIO(json.dumps({"K1": "text"}).encode()),
                        "application/json")},
        data={"game": "G", "source_lang": "zh", "target_locales": "en",
              "autostart": "false"})
    assert r.status_code == 201
    return r.json()["job_id"]


def _put(client: TestClient, jid: str):
    return client.put(
        f"/jobs/{jid}/source",
        files={"file": ("new.json", io.BytesIO(b"{}"), "application/json")})


def test_delete_job_removes_it(client: TestClient):
    jid = _upload(client)
    assert client.delete(f"/jobs/{jid}").status_code == 204
    assert client.get(f"/jobs/{jid}").status_code == 404


def test_delete_unknown_job_is_404(client: TestClient):
    assert client.delete("/jobs/nope").status_code == 404


def test_replace_source_before_ingest(client: TestClient):
    jid = _upload(client)
    assert _put(client, jid).status_code == 200
    received = client.app.state.jobs_root / jid / "10-received"
    assert [p.name for p in received.iterdir()] == ["new.json"]


def test_replace_source_refused_after_ingest(client: TestClient):
    jid = _upload(client)
    s1 = client.app.state.jobs_root / jid / "s1"
    s1.mkdir(exist_ok=True)
    (s1 / "strings.json").write_text("{}")
    assert _put(client, jid).status_code == 409
