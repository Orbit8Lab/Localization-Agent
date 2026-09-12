"""Background execution of job steps.

Why there is no queue broker here: the artifact tree already *is* the
job state. A worker that dies mid-job leaves the artifacts it wrote, and
the next `next_step()` re-derives where to resume. That is the whole
point of deriving state instead of storing it (design §3.2), and it
means a thread plus a lock is sufficient for the beta. Redis earns its
place when work must span machines, not before.

What the runner adds on top of `Job.next_step()` is only this: a loop
that stops at gates, and an in-memory view of what a job is doing right
now, so the UI can distinguish "working" from "stuck on a gate" from
"crashed".
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

from ..controller import Job
from ..llm import build_provider

log = logging.getLogger(__name__)

# A job advances until it hits a gate; this only bounds a pathological
# loop where a stage reports progress without ever reaching one.
MAX_STEPS_PER_RUN = int(os.environ.get("ORBIT8_MAX_STEPS", "40"))


@dataclass
class RunState:
    """What a job is doing *now*. Deliberately not persisted: the durable
    truth is the artifact tree, and a stale 'running' row surviving a
    restart would be a lie the UI repeats."""
    status: str = "idle"          # idle | running | waiting_gate | error
    error: Optional[str] = None
    progress: dict = field(default_factory=dict)


class JobRunner:
    """Runs `next_step()` in a background thread, one run per job."""

    def __init__(self, jobs_root: Path,
                 provider_factory: Optional[Callable] = None,
                 dry_run: bool = False):
        self.root = Path(jobs_root)
        self.dry_run = dry_run
        self._factory = provider_factory
        self._states: Dict[str, RunState] = {}
        self._threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ public

    def state(self, job_id: str) -> RunState:
        with self._lock:
            return self._states.get(job_id, RunState())

    def start(self, job_id: str) -> RunState:
        """Begin advancing a job. Idempotent: a second call while the job
        is already running is a no-op, so a UI that polls and retries
        cannot start two workers on one artifact tree."""
        with self._lock:
            current = self._states.get(job_id)
            if current and current.status == "running":
                return current
            state = RunState(status="running")
            self._states[job_id] = state
            thread = threading.Thread(target=self._run, args=(job_id,),
                                      name=f"orbit8-job-{job_id}",
                                      daemon=True)
            self._threads[job_id] = thread
        thread.start()
        return state

    def join(self, job_id: str, timeout: Optional[float] = None) -> None:
        """Block until a job's run finishes. For tests and shutdown."""
        with self._lock:
            thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)

    # ----------------------------------------------------------- private

    def _provider_factory(self):
        if self._factory is not None:
            return self._factory
        if self.dry_run:
            return None
        provider = os.environ.get("ORBIT8_PROVIDER", "deepseek")
        model = os.environ.get("ORBIT8_MODEL") or None
        # Built per call rather than cached: a locale-specific factory is
        # what the controller expects, and a shared client across threads
        # is a harder thing to reason about than a cheap constructor.
        return lambda locale: build_provider(provider, model=model)

    def _run(self, job_id: str) -> None:
        """Advance the job until a gate, completion, or failure."""
        try:
            job = Job(self.root, job_id)
            factory = self._provider_factory()
            for _ in range(MAX_STEPS_PER_RUN):
                stage = job.derive()
                if stage.gate:
                    self._set(job_id, status="waiting_gate",
                              progress={"phase": stage.phase,
                                        "action": stage.action,
                                        "gate": stage.gate})
                    return
                self._set(job_id, status="running",
                          progress={"phase": stage.phase,
                                    "action": stage.action})
                before = (stage.phase, stage.action, stage.target)
                job.next_step(factory, dry_run=self.dry_run)
                after = job.derive()
                if (after.phase, after.action, after.target) == before:
                    # The step ran but the derivation did not move. Either
                    # the job is complete or a stage is not producing its
                    # declared artifact; either way, looping would spin.
                    self._set(job_id, status="idle",
                              progress={"phase": after.phase,
                                        "action": after.action})
                    return
            self._set(job_id, status="idle",
                      progress={"detail": f"stopped after {MAX_STEPS_PER_RUN} steps"})
        except Exception as err:                      # noqa: BLE001
            # A crashed job must be visible, not silent. The artifacts
            # written so far survive, so /jobs/{id}/run resumes.
            log.exception("job %s failed", job_id)
            self._set(job_id, status="error", error=f"{type(err).__name__}: {err}")

    def _set(self, job_id: str, *, status: str, error: Optional[str] = None,
             progress: Optional[dict] = None) -> None:
        with self._lock:
            self._states[job_id] = RunState(status=status, error=error,
                                            progress=progress or {})
