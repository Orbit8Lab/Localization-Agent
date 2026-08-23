"""The Job Controller — the thing that guarantees agents never control the
loop (design §1).

A boring, deterministic state machine: it scans the artifact tree, computes
the stage, checks gate status, and invokes exactly one stage executor per
`next_step` call. Artifacts are authoritative; LangGraph is a demoted stage
executor whose checkpoints never outlive a stage-run.

Gate holds live in controller state (job.json in v0 — the Postgres row of
the design doc), surfaced by `status`, resolved by a human `approve` action
that flips the record — never by resuming a thread.

Phase machine (LIFECYCLE):
    INTAKE ─G0─▶ INGEST ─▶ CONTEXT ─▶ ASSET ─G1─▶ PILOT ─G2─▶ PRODUCTION
       ─▶ LQA ─G3─▶ TESTING ─G4─▶ RELEASE ─G5─▶ INCREMENTAL
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .glossary import Glossary
from .graphs.asset import (AssetConfig, build_t1_from_delta, health_check,
                           run_asset_stage)
from .graphs.context import ContextConfig, run_context_stage
from .graphs.lqa import LQAConfig, LQAContext, run_lqa_stage
from .graphs.stages import (build_manifest, emit_translations, run_market_analysis,
                            run_marketing, run_testing_stage)
from .graphs.translate import (StageContext, TranslateConfig,
                               assemble_mtpe_queue, run_translate_stage)
from .gate_checks import GateConfig
from .ingest import run_ingest
from .llm import EchoProvider, Provider
from .memory import RunDB, TenantMemory, TranslationMemory
from .observation import (ACCEPTED, FIRST, G3_ACCEPTED, G3_EDITED,
                          ObservationLog)
from .schemas import (DomainLabels, GlossaryDelta, HealthReport, IngestReport,
                      IntakeBrief, LQAReport, MarketReport, MTPEQueue,
                      SourceBatch, StyleBrief, TestPlan, TranslateRunSummary,
                      UniqueString)
from .store import JobStore

GATES = [("G0", "scope sign-off"), ("G1", "asset lock"),
         ("G2", "pilot sign-off"), ("G3", "flagged-strings review"),
         ("G4", "test sign-off"), ("G5", "delivery sign-off")]
GATE_NAMES = dict(GATES)

ProviderFactory = Callable[[str], Provider]   # locale -> provider


@dataclass
class Stage:
    phase: str
    action: str
    gate: Optional[str] = None      # set ⇒ hard stop, waiting on a human
    target: Optional[str] = None    # locale, when the step is per-locale
    detail: Optional[str] = None


class Job:
    def __init__(self, root: Path, job_id: str):
        self.store = JobStore(Path(root), job_id)
        self.job_id = job_id

    # -------------------------------------------------------------- init

    @classmethod
    def init(cls, root: Path, job_id: str, *, intake: IntakeBrief,
             source_files: List[str], pilot_size: int = 30,
             tester_hours: float = 8.0) -> "Job":
        job = cls(root, job_id)
        if job.store.job_json.exists():
            raise RuntimeError(f"job {job_id} already exists")
        job.store.save_control({
            "job_id": job_id, "tenant_id": intake.tenant_id,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source_files": source_files, "pilot_size": pilot_size,
            "tester_hours": tester_hours, "approvals": {}})
        # The intake form is the job's constitution — the first artifact.
        job.store.write(0, "intake", intake, produced_by="human:intake-form")
        return job

    # ------------------------------------------------------------- state

    @property
    def control(self) -> dict:
        return self.store.load_control()

    def approved(self, gate: str) -> bool:
        return gate in self.control["approvals"]

    def approve(self, gate: str, *, by: str, note: Optional[str] = None) -> None:
        """A human action — the only thing that clears a gate (design §7)."""
        if gate not in GATE_NAMES:
            raise ValueError(f"unknown gate {gate!r}")
        pending = self.derive().gate
        if pending != gate:
            raise ValueError(
                f"{gate} is not the pending gate "
                f"(currently {'waiting on ' + pending if pending else 'no gate pending'})")
        control = self.control
        control["approvals"][gate] = {
            "by": by, "note": note,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.store.save_control(control)
        if gate == "G1":
            self._freeze_glossaries()
        if gate == "G3":
            self._absorb_flagged()

    # ------------------------------------------------------------ derive

    def derive(self) -> Stage:
        """Stage detection: derived from artifacts, then gate records —
        state can't lie because there is no free-floating status field."""
        store = self.store
        if not store.exists(0, "intake"):
            return Stage("INTAKE", "waiting on intake form")
        intake = store.read(0, "intake", IntakeBrief)
        locales = intake.target_locales

        if not store.exists(0, "market_report"):
            return Stage("INTAKE", "run market analysis (Market Analyst)")
        if not self.approved("G0"):
            return Stage("INTAKE", "review intake + market report", gate="G0")

        if not store.exists(1, "strings"):
            return Stage("INGEST", "ingest source files (deterministic)")
        if not store.exists(2, "style_brief"):
            return Stage("CONTEXT",
                         "style analysis + domain classification")
        if not store.exists(3, "glossary_delta"):
            return Stage("ASSET", "terminologist extraction")
        for locale in locales:
            if not store.exists(3, f"health.{locale}"):
                return Stage("ASSET", "glossary health check", target=locale)
        blockers = [locale for locale in locales
                    if store.read(3, f"health.{locale}", HealthReport).blockers]
        if blockers:
            return Stage("ASSET",
                         f"fix glossary blockers for {', '.join(blockers)}",
                         detail="G1 refuses to open while blockers exist")
        if not self.approved("G1"):
            return Stage("ASSET", "dev team reviews + locks the glossary",
                         gate="G1")

        for locale in locales:
            if not store.exists(4, f"run_summary.pilot.{locale}"):
                return Stage("PILOT", "pilot module, critic mode = all",
                             target=locale)
        if not self.approved("G2"):
            return Stage("PILOT", "client reviews the pilot", gate="G2")

        for locale in locales:
            if not store.exists(4, f"run_summary.production.{locale}"):
                return Stage("PRODUCTION", "full translate loop",
                             target=locale)
        for locale in locales:
            if not store.exists(5, f"lqa_report.{locale}"):
                return Stage("LQA", "tier cascade + verifier", target=locale)
        for locale in locales:
            if not store.exists(4, f"mtpe_queue.{locale}"):
                return Stage("FLAGGED", "assemble escalation package",
                             target=locale)
        if not self.approved("G3"):
            return Stage("FLAGGED", "human reviews flagged strings + MTPE queue",
                         gate="G3")

        for locale in locales:
            if not store.exists(6, f"test_plan.{locale}"):
                return Stage("TESTING", "generate in-game test plan",
                             target=locale)
        if not self.approved("G4"):
            return Stage("TESTING", "testers execute the plan", gate="G4")

        for locale in locales:
            if not store.exists(7, f"marketing_kit.{locale}"):
                return Stage("RELEASE", "marketing kit + store copy",
                             target=locale)
        if not store.exists(7, "manifest"):
            return Stage("RELEASE", "emit deliverables manifest")
        if not self.approved("G5"):
            return Stage("RELEASE", "client delivery sign-off", gate="G5")

        return Stage("INCREMENTAL", "watching for source deltas",
                     detail="delta ingestion not yet implemented")

    # -------------------------------------------------------------- next

    def next_step(self, provider_factory: Optional[ProviderFactory] = None,
                  *, dry_run: bool = False) -> Stage:
        """Run exactly one stage step, or report the pending gate. All
        artifact writes happen HERE — stage executors and agents return
        typed objects (design §7)."""
        stage = self.derive()
        if stage.gate:
            return stage
        handler = {
            "INTAKE": self._do_intake, "INGEST": self._do_ingest,
            "CONTEXT": self._do_context, "ASSET": self._do_asset,
            "PILOT": self._do_translate, "PRODUCTION": self._do_translate,
            "LQA": self._do_lqa, "FLAGGED": self._do_flagged,
            "TESTING": self._do_testing, "RELEASE": self._do_release,
        }.get(stage.phase)
        if handler is None:
            return stage                    # INCREMENTAL: nothing to run
        provider = self._provider(stage, provider_factory, dry_run)
        handler(stage, provider, dry_run)
        return stage

    def _provider(self, stage: Stage,
                  factory: Optional[ProviderFactory],
                  dry_run: bool) -> Provider:
        locale = stage.target or "xx"
        if dry_run or factory is None:
            return EchoProvider(locale)
        return factory(locale)

    # ---------------------------------------------------- stage handlers

    def _intake(self) -> IntakeBrief:
        return self.store.read(0, "intake", IntakeBrief)

    def _uniques(self) -> List[UniqueString]:
        batch = self.store.read(1, "uniques", SourceBatch)
        return [UniqueString(uid=r.key, text=r.text,
                             keys=(r.context or "").split("\x1f"))
                for r in batch.records]

    def _run_db(self, locale: str) -> RunDB:
        return RunDB(self.store.run_db_path(locale))

    def _tenant(self) -> TenantMemory:
        return TenantMemory(self.store.root, self.control["tenant_id"])

    def _observations(self) -> ObservationLog:
        """PLAN §3. Write-only: nothing in the pipeline reads this to make a
        decision, which is what makes Phase 1 unable to regress anything."""
        return ObservationLog(self.store.observations_path())

    def _style(self) -> StyleBrief:
        return self.store.read(2, "style_brief", StyleBrief)

    def _glossary(self, locale: str) -> Optional[Glossary]:
        """Post-G1 merged view: frozen T1 wins over tenant T2 genre layer."""
        import json
        intake = self._intake()
        version = 1
        t1 = None
        path = self.store.stage_dir(3) / f"glossary.v{version}.{locale}.json"
        staged = self.store.stage_dir(3) / f"t1.{locale}.staged.json"
        if path.exists():
            t1 = json.loads(path.read_text(encoding="utf-8"))["payload"]
        elif staged.exists():
            # Pre-G1 fallback: the staged (human-reviewed, not yet frozen)
            # T1 — better than no glossary for pre-lock audits; G1 approval
            # replaces this with the frozen artifact.
            t1 = json.loads(staged.read_text(encoding="utf-8"))
        t2 = {}
        for genre in intake.genre:
            t2.update(self._tenant().genre_glossary(genre, locale))
        if t1 is None and not t2:
            return None
        return Glossary.from_layers(intake.game, locale,
                                    t1={"terms": (t1 or {}).get("terms", {})},
                                    t2=t2, asset_version=version)

    def _do_intake(self, stage: Stage, provider: Provider,
                   dry_run: bool) -> None:
        report, fingerprint = run_market_analysis(provider, self._intake(),
                                                  dry_run=dry_run)
        self.store.write(0, "market_report", report,
                         produced_by="agent:market-analyst@1",
                         model_fingerprint=fingerprint)

    def _adapter_fallback(self, provider: Provider, dry_run: bool):
        """Unknown format ⇒ the Adapter-Writer generates a converter that
        runs ONLY inside the sandbox; only schema-validated stdout crosses
        back (codegen.py). The script is stored as an artifact and reused
        deterministically on later ingests of the same suffix."""
        from .codegen import AdapterRecord, generate_adapter, run_adapter

        def fallback(path: Path):
            name = f"adapter{path.suffix.replace('.', '_')}"
            if self.store.exists(1, name):
                stored = self.store.read(1, name, AdapterRecord)
                return run_adapter(stored.script, path)
            if dry_run:
                raise ValueError(
                    f"unsupported format {path.suffix!r} and dry-run cannot "
                    f"generate an adapter — run without --dry-run once, or "
                    f"convert the file to .json/.po")
            record, records, fingerprint = generate_adapter(provider, path)
            self.store.write(1, name, record,
                             produced_by="agent:adapter-writer@1",
                             model_fingerprint=fingerprint)
            return records
        return fallback

    def _do_ingest(self, stage: Stage, provider: Provider,
                   dry_run: bool) -> None:
        files = [Path(p) for p in self.control["source_files"]]
        records, uniques, report = run_ingest(
            files, fallback=self._adapter_fallback(provider, dry_run))
        self.store.write(1, "strings", SourceBatch(records=records),
                         produced_by="code:ingest@1")
        # uniques ride in a SourceBatch: uid->key, keys packed in context.
        from .schemas import SourceString
        packed = SourceBatch(records=[
            SourceString(key=u.uid, text=u.text,
                         context="\x1f".join(u.keys)) for u in uniques])
        self.store.write(1, "uniques", packed, produced_by="code:ingest@1")
        self.store.write(1, "ingest_report", report,
                         produced_by="code:ingest@1")

    def _do_context(self, stage: Stage, provider: Provider,
                    dry_run: bool) -> None:
        intake = self._intake()
        uniques = self._uniques()
        # Every locale's run DB is seeded here; domain labels change
        # behavior downstream (prompts, MTPE routing, test plans).
        run_dbs = [self._run_db(locale) for locale in intake.target_locales]
        for run_db in run_dbs:
            run_db.seed(uniques)
        cfg = ContextConfig(game=intake.game, source_lang=intake.source_lang,
                            target_locales=intake.target_locales,
                            dry_run=dry_run)
        brief, labels, fingerprints = run_context_stage(
            provider, cfg, uniques, run_dbs[0])
        for run_db in run_dbs[1:]:
            for item in labels.items:
                run_db.label(item.key, item.domain, item.confidence)
        self.store.write(2, "style_brief", brief,
                         produced_by="agent:context-analyst@1",
                         model_fingerprint=fingerprints["style"])
        self.store.write(2, "domain_labels", labels,
                         produced_by=("agent:domain-classifier@1"
                                      if fingerprints["classify"]
                                      else "code:classify-rules@0"),
                         model_fingerprint=fingerprints["classify"])

    def _do_asset(self, stage: Stage, provider: Provider,
                  dry_run: bool) -> None:
        intake = self._intake()
        uniques = self._uniques()
        if not self.store.exists(3, "glossary_delta"):
            cfg = AssetConfig(game=intake.game,
                              source_lang=intake.source_lang,
                              target_locales=intake.target_locales,
                              dry_run=dry_run)
            delta, fingerprint = run_asset_stage(provider, cfg, uniques)
            self.store.write(3, "glossary_delta", delta,
                             produced_by="agent:terminologist@1",
                             model_fingerprint=fingerprint)
            return
        delta = self.store.read(3, "glossary_delta", GlossaryDelta)
        locale = stage.target
        t1 = build_t1_from_delta(delta, locale)
        report = health_check(t1, locale, [u.text for u in uniques])
        import json
        staged = self.store.stage_dir(3) / f"t1.{locale}.staged.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(json.dumps({"terms": t1}, ensure_ascii=False,
                                     indent=2), encoding="utf-8")
        self.store.write(3, f"health.{locale}", report,
                         produced_by="code:health-check@1")

    def _freeze_glossaries(self) -> None:
        """G1 side effect: the staged T1 becomes the frozen, versioned
        asset. Nothing in this package writes to it afterwards — post-G1
        changes travel through AuditedFixRequest re-opening G1."""
        import json
        intake = self._intake()
        for locale in intake.target_locales:
            staged = self.store.stage_dir(3) / f"t1.{locale}.staged.json"
            frozen = self.store.stage_dir(3) / f"glossary.v1.{locale}.json"
            payload = (json.loads(staged.read_text(encoding="utf-8"))
                       if staged.exists() else {"terms": {}})
            frozen.write_text(json.dumps(
                {"schema": "GlossaryT1", "asset_version": 1,
                 "frozen_at_gate": "G1", "payload": payload},
                ensure_ascii=False, indent=2), encoding="utf-8")

    def _do_translate(self, stage: Stage, provider: Provider,
                      dry_run: bool) -> None:
        intake = self._intake()
        locale = stage.target
        kind = "pilot" if stage.phase == "PILOT" else "production"
        run_db = self._run_db(locale)
        glossary = self._glossary(locale)
        gate_cfg = GateConfig(source_lang=intake.source_lang,
                              target_lang=locale,
                              locked_terms=(glossary.locked_map()
                                            if glossary else {}))
        cfg = TranslateConfig(
            game=intake.game, source_lang=intake.source_lang, locale=locale,
            kind=kind, gate=gate_cfg, client_lang=intake.client_lang,
            dry_run=dry_run,
            # Pilot: critic on everything + best-of-2 sampling; production
            # ratchets with critic on flagged strings only.
            critic_mode="off" if dry_run else ("all" if kind == "pilot"
                                               else "flagged"),
            samples=2 if (kind == "pilot" and not dry_run) else 1)
        attempt = self.store.latest_attempt(4) or self.store.new_attempt(4)
        ctx = StageContext(provider=provider, cfg=cfg, run_db=run_db,
                           tm=TranslationMemory(self.store.tm_path()),
                           glossary=glossary, style_brief=self._style(),
                           observations=self._observations(),
                           attempt=attempt)
        limit = self.control["pilot_size"] if kind == "pilot" else None
        summary = run_translate_stage(ctx, self.job_id, limit=limit)
        self.store.write(4, f"run_summary.{kind}.{locale}", summary,
                         produced_by="code:translate-graph@1",
                         attempt=attempt)

    def _do_lqa(self, stage: Stage, provider: Provider,
                dry_run: bool) -> None:
        intake = self._intake()
        locale = stage.target
        glossary = self._glossary(locale)
        cfg = LQAConfig(game=intake.game, source_lang=intake.source_lang,
                        locale=locale, deterministic_only=dry_run,
                        client_lang=intake.client_lang,
                        gate=GateConfig(source_lang=intake.source_lang,
                                        target_lang=locale,
                                        locked_terms=(glossary.locked_map()
                                                      if glossary else {})))
        ctx = LQAContext(provider=None if dry_run else provider, cfg=cfg,
                         run_db=self._run_db(locale),
                         tm=TranslationMemory(self.store.tm_path()),
                         glossary=glossary, style_brief=self._style(),
                         tenant=self._tenant())
        report = run_lqa_stage(ctx, self.job_id)
        attempt = self.store.latest_attempt(5) or self.store.new_attempt(5)
        self.store.write(5, f"lqa_report.{locale}", report,
                         produced_by="code:lqa-cascade@1", attempt=attempt)

    def _do_flagged(self, stage: Stage, provider: Provider,
                    dry_run: bool) -> None:
        locale = stage.target
        items = assemble_mtpe_queue(self._run_db(locale), locale)
        self.store.write(4, f"mtpe_queue.{locale}",
                         MTPEQueue(locale=locale, items=items),
                         produced_by="code:flagged-assembler@1")

    def _absorb_flagged(self) -> None:
        """G3 side effect (v0 simplification): approving G3 asserts the
        human worked the queue — post-edited targets should be imported
        before approval. Remaining flagged/mtpe rows are accepted as-is,
        and human-confirmed pairs write back to the TM.

        This also records the per-string G3 verdict into the observation log
        (PLAN §3, §5.6). The verdict is DERIVED, not asked for: if the
        target at approval time differs from what S4 produced, a human
        changed it, and that is an overturn of our own gate's judgment. The
        derivation is deliberately conservative — see `_g3_verdict`.
        """
        intake = self._intake()
        tm = TranslationMemory(self.store.tm_path())
        observations = self._observations()
        for locale in intake.target_locales:
            run_db = self._run_db(locale)
            for row in run_db.by_status("flagged", "mtpe"):
                if row["target"]:
                    tm.store(row["text"], row["target"], locale,
                             origin="human")
                verdict, text = self._g3_verdict(observations, row, locale)
                if verdict is not None:
                    observations.record_g3(row["uid"], locale, verdict, text)
                run_db.record(row["uid"], status="accepted",
                              resolution=row["resolution"] or "post-edited")

    @staticmethod
    def _g3_verdict(observations: ObservationLog, row: dict,
                    locale: str) -> tuple[Optional[str], Optional[str]]:
        """What the human decided about one string in one locale, or
        (None, None) when the log cannot honestly tell.

        A row is `edited` when the approved target differs from the best
        candidate S4 recorded, `accepted` when it is unchanged. Silence is
        the third outcome and it is important: this v0 gate approves in
        bulk, so "the operator did not touch this string" is NOT evidence
        of agreement when there is no observation to compare against. A
        fabricated `accepted` would be worse than no data — it would look
        like human endorsement to every later utility estimate (PLAN §5.6).

        `locale` is required for the same reason `record_g3` requires it:
        `row["uid"]` hashes the source string, so it matches every locale.
        Without it, the diff below could compare a Korean approval against
        a Japanese candidate and call the result an edit.
        """
        prior = observations.for_uid(row["uid"], locale)
        if not prior:
            return None, None
        # The last candidate the RATCHET kept is what S4 handed to the
        # reviewer. Note these are ratchet verdicts (ACCEPTED/FIRST), not
        # G3 verdicts — the two vocabularies share the string "accepted"
        # and must not be confused: one is our gate's opinion, the other
        # is the human's, and telling them apart is the entire point.
        final = next((o for o in reversed(prior)
                      if o["verdict"] in (ACCEPTED, FIRST)), None)
        if final is None or not row["target"]:
            return None, None
        return ((G3_ACCEPTED, None) if row["target"] == final["target"]
                else (G3_EDITED, row["target"]))

    def _do_testing(self, stage: Stage, provider: Provider,
                    dry_run: bool) -> None:
        intake = self._intake()
        locale = stage.target
        plan, fingerprint = run_testing_stage(
            provider, game=intake.game, locale=locale,
            run_db=self._run_db(locale),
            tester_hours=self.control["tester_hours"],
            style_brief=self._style(), dry_run=dry_run)
        self.store.write(6, f"test_plan.{locale}", plan,
                         produced_by="agent:test-case-generator@1",
                         model_fingerprint=fingerprint)

    def _do_release(self, stage: Stage, provider: Provider,
                    dry_run: bool) -> None:
        intake = self._intake()
        if stage.target:
            market = self.store.read(0, "market_report", MarketReport)
            kit, fingerprint = run_marketing(
                provider, intake=intake, locale=stage.target,
                run_db=self._run_db(stage.target),
                style_brief=self._style(), market_summary=market.summary,
                dry_run=dry_run)
            self.store.write(7, f"marketing_kit.{stage.target}", kit,
                             produced_by="agent:marketing-writer@1",
                             model_fingerprint=fingerprint)
            return
        files: Dict[str, str] = {}
        for locale in intake.target_locales:
            out = self.store.stage_dir(7) / f"translated.{locale}.jsonl"
            emit_translations(self._run_db(locale), out,
                              source_lang=intake.source_lang, locale=locale)
            files[f"translated.{locale}"] = str(out)
        manifest = build_manifest(
            self.job_id, intake.target_locales, files,
            glossary_asset_version=1,
            changelog=[f"initial delivery {datetime.now(timezone.utc).date()}"])
        self.store.write(7, "manifest", manifest,
                         produced_by="code:deliverables@1")
