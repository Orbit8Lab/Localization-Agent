"""FastAPI application: jobs in, artifacts out.

Design notes that are load-bearing, not decoration:

* **No job table.** Status comes from `Job.derive()`, which reads the
  artifact tree. A database row would be a second source of truth that
  can drift from the artifacts; the whole controller design exists to
  avoid exactly that (design §3.2).
* **Uploads land here, not on the web front end.** Vercel caps request
  bodies at 4.5MB and real game exports exceed it, so the browser must
  POST the file to this service directly.
* **`tenant_id` is threaded from job creation.** It defaults to
  `"default"` for the single-tenant beta, but every job carries one from
  day one so isolation is a filter later rather than a migration.
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import (Depends, FastAPI, File, Form, HTTPException, UploadFile,
                     status)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..controller import Job
from ..schemas import IntakeBrief
from ..tenancy import mixed_tenant_warning
from .auth import build_auth_dependency, users_from_env
from .runner import JobRunner

# Uploads are capped here rather than at the proxy: the limit is a
# product decision (how big a job may be), not a deployment detail.
MAX_UPLOAD_BYTES = int(os.environ.get("ORBIT8_MAX_UPLOAD_BYTES",
                                      100 * 1024 * 1024))


# ------------------------------------------------------------ wire types

class StageOut(BaseModel):
    """`Stage` as JSON. `gate` set means a human must act before the job
    can advance — the UI renders that differently from work in progress."""
    phase: str
    action: str
    gate: Optional[str] = None
    target: Optional[str] = None
    detail: Optional[str] = None


class JobSummary(BaseModel):
    job_id: str
    tenant_id: str
    created_at: Optional[str] = None
    game: Optional[str] = None
    stage: StageOut
    run_state: str = "idle"
    run_error: Optional[str] = None


class JobDetail(JobSummary):
    source_lang: Optional[str] = None
    target_locales: List[str] = Field(default_factory=list)
    approvals: dict = Field(default_factory=dict)
    progress: Optional[dict] = None


class ApproveIn(BaseModel):
    gate: str
    by: str
    note: Optional[str] = None


class ArtifactRef(BaseModel):
    stage: int
    name: str
    attempts: List[int]


# ------------------------------------------------------------------ app

def create_app(jobs_root: Optional[Path] = None,
               runner: Optional[JobRunner] = None) -> FastAPI:
    """Build the app. Both dependencies are injectable so tests can run
    against a tmp_path root and a synchronous runner."""
    root = Path(jobs_root or os.environ.get("ORBIT8_JOBS_ROOT", "./jobs"))
    root.mkdir(parents=True, exist_ok=True)
    run = runner or JobRunner(root)

    # Fail closed: a deployment without credentials would be a public API
    # that creates jobs and spends model tokens. Forgetting to set a
    # secret is the likeliest way to publish one by accident, so it stops
    # the boot instead of passing quietly.
    users = users_from_env()
    if not users and os.environ.get("ORBIT8_ENV") == "production":
        raise RuntimeError(
            "ORBIT8_BASIC_AUTH is required when ORBIT8_ENV=production; "
            "set it with: fly secrets set ORBIT8_BASIC_AUTH='user:password'")
    require_user = build_auth_dependency(users)

    # Interactive docs are handy in beta and are an information leak once
    # there are real customers; off by default in production.
    docs_on = os.environ.get("ORBIT8_ENV") != "production" or (
        os.environ.get("ORBIT8_ENABLE_DOCS") == "1")
    app = FastAPI(title="Orbit8 Agent", version="0.1.0",
                  docs_url="/docs" if docs_on else None,
                  redoc_url="/redoc" if docs_on else None,
                  openapi_url="/openapi.json" if docs_on else None)

    # Vercel and this service are different origins, so the browser
    # needs CORS from the start. Origins come from the environment
    # because the preview URL differs per deployment.
    origins = [o for o in os.environ.get("ORBIT8_CORS_ORIGINS", "").split(",")
               if o]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"])

    app.state.jobs_root = root
    app.state.runner = run

    def get_job(job_id: str) -> Job:
        """Load a job or 404. Rejects path separators: `job_id` becomes a
        directory name, so an unchecked value is a traversal."""
        if not job_id or "/" in job_id or "\\" in job_id or job_id == "..":
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "invalid job id")
        job = Job(root, job_id)
        if not job.store.job_json.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"no such job: {job_id}")
        return job

    # ------------------------------------------------------------ health

    @app.get("/health")
    def health() -> dict:
        """Unauthenticated on purpose: the platform health check runs
        before any secret is available, and this leaks nothing."""
        return {"ok": True, "jobs_root": str(root)}

    # -------------------------------------------------------------- jobs

    @app.post("/jobs", response_model=JobDetail,
              status_code=status.HTTP_201_CREATED,
              dependencies=[Depends(require_user)])
    async def create_job(
            file: UploadFile = File(...),
            game: str = Form(...),
            source_lang: str = Form(...),
            target_locales: str = Form(...),
            tenant_id: str = Form("default"),
            engine: str = Form("unknown"),
            genre: str = Form(""),
            job_id: Optional[str] = Form(None),
            autostart: bool = Form(True)) -> JobDetail:
        """Accept a source file and open a job.

        Returns as soon as the intake artifact is written. The agent runs
        in the background, so a two-hour job does not hold the request
        open.
        """
        jid = job_id or f"job-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8]}"
        if (root / jid / "job.json").exists():
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"job {jid} already exists")

        locales = [x.strip() for x in target_locales.split(",") if x.strip()]
        if not locales:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "target_locales must name at least one locale")

        # Stream to disk under the job's own directory, and enforce the
        # size cap while writing: reading first and checking after is how
        # a large upload becomes an OOM.
        received = root / jid / "10-received"
        received.mkdir(parents=True, exist_ok=True)
        dest = received / Path(file.filename or "source.bin").name
        written = 0
        try:
            with dest.open("wb") as out:
                while chunk := await file.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status.HTTP_413_CONTENT_TOO_LARGE,
                            f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
                    out.write(chunk)
        except HTTPException:
            shutil.rmtree(root / jid, ignore_errors=True)
            raise

        intake = IntakeBrief(
            game=game, source_lang=source_lang, target_locales=locales,
            engine=engine, tenant_id=tenant_id,
            genre=[g.strip() for g in genre.split(",") if g.strip()])

        warning = mixed_tenant_warning(root, tenant_id)
        job = Job.init(root, jid, intake=intake, source_files=[str(dest)])
        if warning:
            # Surfaced, not swallowed: co-locating two organizations
            # under one root defeats the file boundary.
            job.store.save_control({**job.control,
                                    "tenant_warning": warning})
        if autostart:
            run.start(jid)
        return _detail(job, run)

    @app.get("/jobs", response_model=List[JobSummary],
             dependencies=[Depends(require_user)])
    def list_jobs(tenant_id: Optional[str] = None) -> List[JobSummary]:
        out: List[JobSummary] = []
        for child in sorted(root.iterdir()):
            if not (child / "job.json").exists():
                continue
            job = Job(root, child.name)
            control = job.control
            if tenant_id and control.get("tenant_id") != tenant_id:
                continue
            out.append(_summary(job, run))
        return out

    @app.get("/jobs/{job_id}", response_model=JobDetail,
             dependencies=[Depends(require_user)])
    def job_detail(job: Job = Depends(get_job)) -> JobDetail:
        return _detail(job, run)

    @app.post("/jobs/{job_id}/approve", response_model=JobDetail,
              dependencies=[Depends(require_user)])
    def approve(body: ApproveIn, job: Job = Depends(get_job)) -> JobDetail:
        """Clear a gate. The controller refuses any gate that is not the
        pending one, so this cannot skip ahead."""
        try:
            job.approve(body.gate, by=body.by, note=body.note)
        except ValueError as err:
            raise HTTPException(status.HTTP_409_CONFLICT, str(err))
        run.start(job.job_id)          # a cleared gate may unblock work
        return _detail(job, run)

    @app.post("/jobs/{job_id}/run", response_model=JobDetail,
              dependencies=[Depends(require_user)])
    def run_job(job: Job = Depends(get_job)) -> JobDetail:
        """Resume a job that is idle — after a crash, or a gate opened
        out of band."""
        run.start(job.job_id)
        return _detail(job, run)

    # --------------------------------------------------------- artifacts

    @app.get("/jobs/{job_id}/artifacts", response_model=List[ArtifactRef],
             dependencies=[Depends(require_user)])
    def list_artifacts(job: Job = Depends(get_job)) -> List[ArtifactRef]:
        """Only stages 4 and 5 are attempt-versioned; the rest write flat
        into `sN/`. `artifact_names` scans attempt directories, so it
        sees nothing for a flat stage — list both shapes."""
        refs: List[ArtifactRef] = []
        for stage in range(8):
            versioned = job.store.artifact_names(stage)
            for name, attempts in sorted(versioned.items()):
                refs.append(ArtifactRef(stage=stage, name=name,
                                        attempts=attempts))
            flat = job.store.job_dir / f"s{stage}"
            if flat.is_dir():
                for artifact in sorted(flat.glob("*.json")):
                    if artifact.stem not in versioned:
                        refs.append(ArtifactRef(stage=stage,
                                                name=artifact.stem,
                                                attempts=[]))
        return refs

    @app.get("/jobs/{job_id}/artifacts/{stage}/{name}",
             dependencies=[Depends(require_user)])
    def get_artifact(stage: int, name: str,
                     attempt: Optional[int] = None,
                     job: Job = Depends(get_job)) -> FileResponse:
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "invalid artifact name")
        # `find_attempt` returns None for an unversioned stage even when
        # the artifact exists, so fall through to the flat path instead
        # of treating None as "missing".
        att = attempt if attempt is not None else job.store.find_attempt(
            stage, name)
        path = job.store.stage_dir(stage, att) / f"{name}.json"
        if not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such artifact")
        return FileResponse(path, media_type="application/json",
                            filename=f"{job.job_id}-{stage}-{name}.json")

    return app


# ------------------------------------------------------------- rendering

def _stage_out(job: Job) -> StageOut:
    stage = job.derive()
    return StageOut(phase=stage.phase, action=stage.action, gate=stage.gate,
                    target=stage.target, detail=stage.detail)


def _summary(job: Job, run: JobRunner) -> JobSummary:
    control = job.control
    state = run.state(job.job_id)
    return JobSummary(
        job_id=job.job_id,
        tenant_id=control.get("tenant_id", "default"),
        created_at=control.get("created_at"),
        game=_intake_field(job, "game"),
        stage=_stage_out(job),
        run_state=state.status,
        run_error=state.error)


def _detail(job: Job, run: JobRunner) -> JobDetail:
    control = job.control
    state = run.state(job.job_id)
    return JobDetail(
        **_summary(job, run).model_dump(),
        source_lang=_intake_field(job, "source_lang"),
        target_locales=_intake_field(job, "target_locales") or [],
        approvals=control.get("approvals", {}),
        progress=state.progress)


def _intake_field(job: Job, field: str):
    """Read one field off the intake artifact, tolerating its absence —
    a job whose intake failed to write should still list, not 500."""
    try:
        brief = job.store.read(0, "intake", IntakeBrief)
    except Exception:
        return None
    return getattr(brief, field, None)
