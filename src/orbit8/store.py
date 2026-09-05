"""Artifact store — the authoritative state of a job (design §1, §3).

The artifact tree IS the job state. The Controller derives the current stage
by scanning this tree; LangGraph checkpoints are demoted to crash recovery
within a single stage-run and discarded after the artifact write.

Layout (attempt versioning per design §3 — defect loops re-enter S4/S5
repeatedly, and re-entry must never overwrite the artifact a client-facing
bug report points at):

    jobs/<job_id>/
      job.json                       # gate approvals + config (v0 stand-in
                                     # for the Postgres controller store)
      s0/intake.json …
      s3/glossary.v1.json            # frozen at G1
      s4/attempt-01/…  s4/attempt-02/…
      s5/attempt-01/…
      runs/<locale>.db               # segment text + per-string state
      assets/tm.db                   # job-scoped translation memory
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel

from .schemas import SCHEMA_VERSION, Envelope

M = TypeVar("M", bound=BaseModel)

# Stages whose artifacts are attempt-versioned (backward defect edges land here).
VERSIONED_STAGES = {4, 5}


class ArtifactError(RuntimeError):
    pass


class JobStore:
    def __init__(self, root: Path, job_id: str):
        self.root = Path(root)
        self.job_id = job_id
        self.job_dir = self.root / job_id

    # ------------------------------------------------------------ layout

    def stage_dir(self, stage: int, attempt: Optional[int] = None) -> Path:
        base = self.job_dir / f"s{stage}"
        if stage in VERSIONED_STAGES:
            n = attempt or self.latest_attempt(stage) or 1
            return base / f"attempt-{n:02d}"
        return base

    def latest_attempt(self, stage: int) -> Optional[int]:
        base = self.job_dir / f"s{stage}"
        if not base.exists():
            return None
        attempts = [int(m.group(1)) for p in base.iterdir()
                    if (m := re.fullmatch(r"attempt-(\d+)", p.name))]
        return max(attempts) if attempts else None

    def find_attempt(self, stage: int, name: str) -> Optional[int]:
        """The newest attempt actually holding `name`, or None.

        `latest_attempt` answers a different question — "what ran most
        recently" — and the two diverge as soon as a stage is run more
        than once with different artifact names. Stage 5 does exactly
        that: every `lqa run` opens its own attempt, so auditing four
        locales leaves four attempts each holding ONE locale's report.
        Reading the ja report out of the latest attempt then fails with a
        missing-artifact path, even though the file is right there in an
        earlier attempt.
        """
        base = self.job_dir / f"s{stage}"
        if not base.exists():
            return None
        found = [int(m.group(1)) for p in base.iterdir()
                 if (m := re.fullmatch(r"attempt-(\d+)", p.name))
                 and (p / f"{name}.json").exists()]
        return max(found) if found else None

    def artifact_names(self, stage: int) -> Dict[str, List[int]]:
        """Every artifact name in a stage → the attempts holding it.

        Exists to make a miss self-explanatory: "missing artifact
        <path>" tells the operator nothing about which names ARE
        available, which is the only thing they need to recover.
        """
        base = self.job_dir / f"s{stage}"
        names: Dict[str, List[int]] = {}
        if not base.exists():
            return names
        for attempt_dir in sorted(base.iterdir()):
            m = re.fullmatch(r"attempt-(\d+)", attempt_dir.name)
            if not m:
                continue
            for artifact in sorted(attempt_dir.glob("*.json")):
                names.setdefault(artifact.stem, []).append(int(m.group(1)))
        return names

    def new_attempt(self, stage: int) -> int:
        """Claim the next attempt number for a stage.

        `exist_ok=True` made this read-then-create, so two processes
        could compute the same `n` and both "succeed" — the second then
        wrote its report into the first's directory. Auditing four
        locales in parallel is the obvious way to use this tool, and it
        would have silently lost a locale's findings.

        Creating the directory EXCLUSIVELY makes the claim atomic: the
        loser of a race gets FileExistsError and retries with the next
        number, so concurrent callers get distinct attempts.
        """
        if stage not in VERSIONED_STAGES:
            raise ArtifactError(f"stage {stage} is not attempt-versioned")
        base = self.job_dir / f"s{stage}"
        base.mkdir(parents=True, exist_ok=True)
        n = (self.latest_attempt(stage) or 0) + 1
        while True:
            try:
                (base / f"attempt-{n:02d}").mkdir()
                return n
            except FileExistsError:
                n += 1

    # ------------------------------------------------------------- write

    def write(self, stage: int, name: str, payload: BaseModel, *,
              produced_by: str, model_fingerprint: Optional[str] = None,
              attempt: Optional[int] = None) -> Path:
        """Validate-on-write: the payload is already a validated model, and
        the envelope is validated too. Only the Controller calls this —
        agents return typed objects and never touch the filesystem
        (design §7: that is what makes `produced_by` trustworthy)."""
        envelope = Envelope(
            schema_name=type(payload).__name__,
            job_id=self.job_id, stage=stage,
            attempt=attempt or (self.latest_attempt(stage) or 1
                                if stage in VERSIONED_STAGES else 1),
            produced_at=datetime.now(timezone.utc),
            produced_by=produced_by,
            model_fingerprint=model_fingerprint,
            payload=payload.model_dump(mode="json"),
        )
        path = self.stage_dir(stage, attempt) / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
        return path

    # -------------------------------------------------------------- read

    def read(self, stage: int, name: str, model_cls: Type[M], *,
             attempt: Optional[int] = None) -> M:
        path = self.stage_dir(stage, attempt) / f"{name}.json"
        if not path.exists():
            raise ArtifactError(f"missing artifact {path}")
        envelope = Envelope.model_validate_json(path.read_text(encoding="utf-8"))
        if envelope.schema_name != model_cls.__name__:
            raise ArtifactError(
                f"{path}: schema is {envelope.schema_name!r}, "
                f"expected {model_cls.__name__!r}")
        if envelope.schema_version != SCHEMA_VERSION:
            raise ArtifactError(
                f"{path}: schema_version {envelope.schema_version} "
                f"!= current {SCHEMA_VERSION}")
        return model_cls.model_validate(envelope.payload)

    def exists(self, stage: int, name: str, *,
               attempt: Optional[int] = None) -> bool:
        try:
            return (self.stage_dir(stage, attempt) / f"{name}.json").exists()
        except Exception:
            return False

    # ------------------------------------------------- controller state

    @property
    def job_json(self) -> Path:
        return self.job_dir / "job.json"

    def load_control(self) -> dict:
        if not self.job_json.exists():
            raise ArtifactError(f"no job at {self.job_dir} (run `orbit8 job init`)")
        return json.loads(self.job_json.read_text(encoding="utf-8"))

    def save_control(self, data: dict) -> None:
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self.job_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # --------------------------------------------------------- databases

    def run_db_path(self, locale: str) -> Path:
        path = self.job_dir / "runs" / f"{locale}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def smoke_db_path(self, locale: str) -> Path:
        """Scratch DB for a pre-flight smoke run.

        Kept OUT of `runs/` on purpose: everything in `runs/` is the real
        translation state, and a smoke run marking segments accepted there
        would silently shrink the batch it was meant to de-risk. Under
        `smoke/` it is obviously disposable, and `derive()` never looks at
        it, so a smoke run can never advance the job.
        """
        path = self.job_dir / "smoke" / f"{locale}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def tm_path(self) -> Path:
        path = self.job_dir / "assets" / "tm.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def observations_path(self) -> Path:
        """The PLAN §3 observation log. Job-scoped and NOT attempt-scoped:
        its whole purpose is comparing outcomes ACROSS attempts, so each row
        carries its own attempt number instead."""
        path = self.job_dir / "assets" / "observations.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
