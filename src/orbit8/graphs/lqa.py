"""Stage 5 — LQA tier cascade (design §5). Tiers are a cost ladder and run
as one sequence, never in parallel:

    T1 mechanical (code, everything)
      → T2 glossary & consistency (code, everything that passes T1;
          consistency is NOT a per-segment property — T2 is a project-level
          pass over the full rendering map after fan-in)
        → T3 semantic (LLM Critic, only what survives T1 and T2)
          → Verifier second layer on T3 findings (confirm/overturn/uncertain)

T3 is the false-positive risk: precision beats recall by a wide margin —
studios abandon LQA tooling that cries wolf. Confirmed findings are filtered
by an aggressive confidence threshold plus per-issue-type suppression rules
from curated tenant memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import agents
from ..gate_checks import (GateConfig, locked_in_target, run_gate,
                           term_in_text)
from ..glossary import Glossary
from ..llm import Provider
from ..memory import RunDB, TenantMemory, TranslationMemory
from ..schemas import (BugType, Finding, LQAItem, LQAReport, Severity,
                       StyleBrief, Verdict, VerdictDecision, VerifiedFinding)


def _string_type_for(row: dict) -> Optional[str]:
    """The row's UI class, derived from its SourceLocation path.

    ``context`` is preferred over ``keys``: a .po's msgctxt is an opaque
    GUID, so only the ``#:`` path carries the widget class. Falling back
    to a GUID would classify everything as "Others" and silently disable
    the width budget.

    Reuses po_translate's classifier rather than restating the hint table:
    the width budget and the PE form must agree on what counts as a UI
    string, and two copies of that mapping would drift.
    """
    from ..po_translate import _string_type
    location = row.get("context") or ""
    if not location:
        keys = row.get("keys") or []
        location = keys[0] if keys else ""
    return _string_type(location) if location else None


@dataclass
class LQAConfig:
    game: str
    source_lang: str
    locale: str
    batch_size: int = 10
    # docs/skills/lqa-batch-split.md: story text (dialogue/marketing) gets
    # small Tier-3 batches — voice/continuity review needs a small window.
    batch_size_story: int = 5
    deterministic_only: bool = False      # T1+T2 only, zero LLM calls
    second_layer: bool = True
    requeue: bool = True                  # flagged strings back to G3 review
    t3_confidence_threshold: float = 0.75
    gate: GateConfig = field(default_factory=GateConfig)
    client_lang: Optional[str] = None


class LQAState(TypedDict, total=False):
    findings_t1: Dict[str, List[Finding]]   # uid -> mechanical findings
    findings_t2: Dict[str, List[Finding]]
    findings_t3: Dict[str, List[Finding]]
    verified: Dict[str, List[dict]]         # uid -> VerifiedFinding dumps
    # Every T3 finding's fate — kept or dropped, and why. A filter that
    # cannot show what it filtered is indistinguishable from a detector
    # that found nothing.
    t3_audit: List[dict]
    # Batches whose LLM call failed — a COVERAGE gap, kept separate from
    # the finding audit so an unreviewed string is never read as clean.
    t3_errors: List[dict]
    # Per-tier in/out counts, written by each node as it runs. The report
    # validator (verify_cascade) checks the numbers telescope — cascade
    # compliance becomes a property of the artifact, not a claim.
    ledger: Dict[str, int]


@dataclass
class LQAContext:
    provider: Optional[Provider]
    cfg: LQAConfig
    run_db: RunDB
    tm: Optional[TranslationMemory] = None
    glossary: Optional[Glossary] = None
    style_brief: Optional[StyleBrief] = None
    tenant: Optional[TenantMemory] = None
    # per-language-pair style rules (style_guide.StyleGuide): mechanical
    # rules run in the T1 gate, llm rules become the T3 rubric
    style_guide: object = None
    # optional (event, detail) callback — long T3 runs are otherwise
    # indistinguishable from a hang
    on_progress: Optional[Callable[[str, dict], None]] = None


def build_lqa_graph(ctx: LQAContext):
    cfg = ctx.cfg

    def _accepted() -> List[dict]:
        return ctx.run_db.by_status("accepted")

    # ------------------------------------------------------ T1 mechanical

    def tier1(_: LQAState) -> dict:
        rows = _accepted()
        found: Dict[str, List[Finding]] = {}
        for row in rows:
            # The string's UI class comes from its own PO location (keys),
            # which is what selects the display-width budget: overflow is a
            # property of the widget, so a button and a wiki paragraph must
            # not be judged against the same number.
            findings = run_gate(row["uid"], row["text"], row["target"] or "",
                                cfg.gate, domain=row.get("domain"),
                                string_type=_string_type_for(row))
            if findings:
                for finding in findings:
                    finding.tier = 1
                found[row["uid"]] = findings
        return {"findings_t1": found,
                "ledger": {"accepted": len(rows), "t1_flagged": len(found)}}

    # ------------------------------------- T2 project-level consistency

    def tier2(state: LQAState) -> dict:
        """Runs on everything that passed T1. A term rendered two ways
        across different menus is invisible to any segment-scoped check —
        this pass holds the whole locale in scope."""
        t1 = state.get("findings_t1", {})
        rows = [r for r in _accepted() if r["uid"] not in t1]
        found: Dict[str, List[Finding]] = {}
        ledger = dict(state.get("ledger", {}))
        ledger["t2_input"] = len(rows)

        # (a) near-identical sources (case/space-normalized) rendered
        #     differently across the asset
        by_norm: Dict[str, List[dict]] = {}
        for row in rows:
            norm = " ".join((row["text"] or "").lower().split())
            by_norm.setdefault(norm, []).append(row)
        for norm, group in by_norm.items():
            targets = {(r["target"] or "").strip() for r in group}
            if len(group) > 1 and len(targets) > 1:
                for row in group:
                    found.setdefault(row["uid"], []).append(Finding(
                        key=row["uid"], bug_type=BugType.CONSISTENCY,
                        severity=Severity.MEDIUM, tier=2,
                        message="Same source rendered differently across "
                                f"the asset ({len(targets)} variants).",
                        evidence=(row["target"] or "")[:80]))

        # (b) human-confirmed TM pair contradicted by the shipped target
        if ctx.tm:
            tm_map = ctx.tm.rendering_map(cfg.locale)
            for row in rows:
                confirmed = tm_map.get(row["text"])
                if confirmed and confirmed.strip() != (row["target"] or "").strip():
                    found.setdefault(row["uid"], []).append(Finding(
                        key=row["uid"], bug_type=BugType.CONSISTENCY,
                        severity=Severity.MEDIUM, tier=2,
                        message="Target diverges from the TM-confirmed "
                                "rendering for this exact source.",
                        evidence=confirmed[:80]))

        # (c) glossary-term rendering variance across the full corpus.
        #     Compliance uses the SAME matcher as the T1 gate: a declared
        #     form ("craft" for "Crafting") and a legitimate inflection
        #     are renderings of the term, not divergences from it.
        if ctx.glossary:
            morphology = getattr(cfg.gate.style_guide, "morphology", None)
            entries = {t.term: t for t in ctx.glossary.terms.values()}
            for term, locked in ctx.glossary.locked_map(
                    locked_only=True).items():
                entry = entries.get(term)
                using = [r for r in rows if term_in_text(term, r["text"])]
                misses = [r for r in using
                          if not locked_in_target(
                              locked, r["target"] or "", cfg.locale,
                              morphology=morphology,
                              forms=(entry.forms if entry else None),
                              case=(entry.case if entry else "context"))]
                if using and misses and len(misses) < len(using):
                    for row in misses:
                        found.setdefault(row["uid"], []).append(Finding(
                            key=row["uid"], bug_type=BugType.CONSISTENCY,
                            severity=Severity.MEDIUM, tier=2,
                            message=f"Term {term!r} rendered as {locked!r} "
                                    "elsewhere in the asset but not here.",
                            evidence=term))
        ledger["t2_flagged"] = len(found)
        return {"findings_t2": found, "ledger": ledger}

    # -------------------------------------------------- T3 semantic (LLM)

    STORY_DOMAINS = {"dialogue", "marketing"}

    def tier3(state: LQAState) -> dict:
        flagged = set(state.get("findings_t1", {})) | set(
            state.get("findings_t2", {}))
        survivors = [r for r in _accepted() if r["uid"] not in flagged]
        ledger = dict(state.get("ledger", {}))
        ledger["t3_input"] = len(survivors)
        if cfg.deterministic_only or ctx.provider is None:
            ledger["t3_ran"] = 0
            return {"findings_t3": {}, "ledger": ledger}
        ledger["t3_ran"] = 1
        # Batch policy (docs/skills/lqa-batch-split.md): story n=5,
        # pure strings n=20 — one batch size fits neither.
        story = [r for r in survivors if r["domain"] in STORY_DOMAINS]
        strings = [r for r in survivors if r["domain"] not in STORY_DOMAINS]
        batches: List[List[dict]] = []
        for rows, size in ((story, cfg.batch_size_story),
                           (strings, cfg.batch_size)):
            batches += [rows[i:i + size] for i in range(0, len(rows), size)]
        found: Dict[str, List[Finding]] = {}
        audit: List[dict] = []
        errors: List[dict] = []
        failed_batches = 0
        failed_uids: List[str] = []
        for batch in batches:
            items = [(r["uid"], r["text"], r["target"] or "") for r in batch]
            brief = (ctx.glossary.brief_for([r["text"] for r in batch])
                     if ctx.glossary else None)
            # batches are homogeneous by construction (story vs strings),
            # so one domain selects the rubric for the whole batch
            domains = {r.get("domain") for r in batch}
            try:
                review, _fp = agents.review_batch(
                    ctx.provider, items, source_lang=cfg.source_lang,
                    target_lang=cfg.locale, game=cfg.game,
                    glossary_brief=brief, style_brief=ctx.style_brief,
                    client_lang=cfg.client_lang,
                    style_guide=ctx.style_guide,
                    domain=(domains.pop() if len(domains) == 1 else None))
            except Exception as err:
                # One malformed completion must not discard the work of
                # every other batch. The batch is recorded as UNREVIEWED
                # (a coverage gap the report must show), never as clean.
                failed_batches += 1
                failed_uids.extend(uid for uid, _, _ in items)
                # NOT a t3_audit entry: the audit trail accounts for
                # FINDINGS, and a failed batch produced none. Coverage
                # gaps live in the ledger (t3_unreviewed) instead.
                errors.append({"uids": [uid for uid, _, _ in items],
                               "error": f"{type(err).__name__}: "
                                        f"{str(err)[:200]}"})
                if ctx.on_progress:
                    ctx.on_progress("t3_batch_failed",
                                    {"size": len(items),
                                     "error": str(err)[:200]})
                continue
            if ctx.on_progress:
                ctx.on_progress("t3_batch", {"size": len(items),
                                             "findings": len(
                                                 review.findings)})
            for finding in review.findings:
                finding.tier = 3            # provenance is stamped in code
                if finding.evidence:
                    found.setdefault(finding.key, []).append(finding)
                else:                           # dropped, but never silently
                    audit.append({"uid": finding.key,
                                  "finding": finding.model_dump(mode="json"),
                                  "kept": False, "reason": "no_evidence"})
        ledger["t3_batches"] = len(batches)
        ledger["t3_batches_failed"] = failed_batches
        ledger["t3_unreviewed"] = len(failed_uids)
        return {"findings_t3": found, "t3_audit": audit,
                "t3_errors": errors, "ledger": ledger}

    # ------------------------------------------------ verifier + filters

    def verify(state: LQAState) -> dict:
        """Second-layer review of T3 findings only — mechanical T1/T2
        findings need no LLM confirmation. Suppression rules and the
        confidence threshold applied here, in code."""
        suppressed_types = {
            rule["bug_type"] for rule in (ctx.tenant.suppressions()
                                          if ctx.tenant else [])
            if rule.get("action") == "suppress"}
        verified: Dict[str, List[dict]] = {}
        ledger = dict(state.get("ledger", {}))
        second_layer = (cfg.second_layer and not cfg.deterministic_only
                        and ctx.provider is not None)
        ledger["second_layer"] = int(second_layer)

        for uid, findings in {**state.get("findings_t1", {}),
                              **state.get("findings_t2", {})}.items():
            verified.setdefault(uid, []).extend(
                VerifiedFinding(finding=f).model_dump(mode="json")
                for f in findings)

        audit = list(state.get("t3_audit", []))

        def record(uid: str, finding: Finding, verdict: Optional[Verdict],
                   kept: bool, reason: str) -> None:
            audit.append({
                "uid": uid, "kept": kept, "reason": reason,
                "finding": finding.model_dump(mode="json"),
                "verdict": verdict.model_dump(mode="json") if verdict else None})

        for uid, findings in state.get("findings_t3", {}).items():
            row = ctx.run_db.get(uid)
            for finding in findings:
                if finding.bug_type.value in suppressed_types:
                    record(uid, finding, None, False, "suppressed")
                    continue
                verdict: Optional[Verdict] = None
                if second_layer:
                    verdict, _fp = agents.verify_finding(
                        ctx.provider, key=uid, source=row["text"],
                        target=row["target"] or "", finding=finding,
                        source_lang=cfg.source_lang, target_lang=cfg.locale,
                        game=cfg.game,
                        glossary_brief=(ctx.glossary.brief_for([row["text"]])
                                        if ctx.glossary else None),
                        style_brief=ctx.style_brief,
                        client_lang=cfg.client_lang)
                    if verdict.decision == VerdictDecision.OVERTURN:
                        record(uid, finding, verdict, False, "overturned")
                        continue
                    if (verdict.decision == VerdictDecision.CONFIRM
                            and verdict.confidence < cfg.t3_confidence_threshold):
                        record(uid, finding, verdict, False,
                               f"below_threshold({verdict.confidence:.2f}"
                               f"<{cfg.t3_confidence_threshold})")
                        continue        # precision beats recall (design §5)
                record(uid, finding, verdict, True,
                       verdict.decision.value if verdict else "unverified")
                verified.setdefault(uid, []).append(
                    VerifiedFinding(finding=finding,
                                    verdict=verdict).model_dump(mode="json"))
        ledger["t3_raw"] = len(audit)
        ledger["t3_kept"] = sum(1 for entry in audit if entry["kept"])
        return {"verified": verified, "t3_audit": audit, "ledger": ledger}

    graph = StateGraph(LQAState)
    graph.add_node("tier1", tier1)
    graph.add_node("tier2", tier2)
    graph.add_node("tier3", tier3)
    graph.add_node("verify", verify)
    graph.add_edge(START, "tier1")
    graph.add_edge("tier1", "tier2")     # strict cascade — a cost ladder,
    graph.add_edge("tier2", "tier3")     # never parallel
    graph.add_edge("tier3", "verify")
    graph.add_edge("verify", END)
    return graph


def run_lqa_stage(ctx: LQAContext, job_id: str) -> LQAReport:
    """Runs the cascade and refuses to return a report that cannot prove
    the cascade ran (verify_cascade) — a non-compliant report must never
    become an artifact."""
    cfg = ctx.cfg
    state = build_lqa_graph(ctx).compile().invoke({})
    verified: Dict[str, List[dict]] = state.get("verified", {})
    t3_audit: List[dict] = state.get("t3_audit", [])
    ledger: Dict[str, int] = state.get("ledger", {})
    t3_stats: Dict[str, int] = {"raw": len(t3_audit),
                                "kept": sum(1 for a in t3_audit if a["kept"])}
    for entry in t3_audit:
        if not entry["kept"]:
            reason = entry["reason"].split("(")[0]
            t3_stats[reason] = t3_stats.get(reason, 0) + 1

    items: List[LQAItem] = []
    by_severity: Dict[str, int] = {}
    by_bug_type: Dict[str, int] = {}
    confirmed = overturned = uncertain = total = 0
    block_ship = False

    for uid, dumped in sorted(verified.items()):
        if not dumped:
            continue
        row = ctx.run_db.get(uid)
        parsed = [VerifiedFinding.model_validate(v) for v in dumped]
        for vf in parsed:
            total += 1
            severity = vf.finding.severity
            by_severity[severity.value] = by_severity.get(severity.value, 0) + 1
            by_bug_type[vf.finding.bug_type.value] = (
                by_bug_type.get(vf.finding.bug_type.value, 0) + 1)
            if vf.verdict is None or vf.verdict.decision == VerdictDecision.CONFIRM:
                confirmed += 1
                if severity == Severity.HIGH:
                    block_ship = True
            elif vf.verdict.decision == VerdictDecision.UNCERTAIN:
                uncertain += 1
        items.append(LQAItem(uid=uid, game_keys=row["keys"],
                             source=row["text"], target=row["target"] or "",
                             findings=parsed))
        if cfg.requeue and any(
                vf.finding.severity != Severity.LOW for vf in parsed):
            # Flagged strings go back to human review at G3 — surfaced,
            # never silently dropped.
            ctx.run_db.record(uid, status="flagged",
                              findings=[vf.finding for vf in parsed])

    checked = len(ctx.run_db.by_status("accepted", "flagged"))
    report = LQAReport(
        job_id=job_id, locale=cfg.locale, checked=checked,
        flagged_strings=len(items), findings_total=total,
        confirmed=confirmed,
        overturned=t3_stats.get("overturned", 0),
        uncertain=uncertain,
        block_ship=block_ship, by_severity=by_severity,
        by_bug_type=by_bug_type, items=items,
        t3_stats=t3_stats, t3_audit=t3_audit, cascade_ledger=ledger,
        t3_errors=state.get("t3_errors", []))
    violations = verify_cascade(report)
    if violations:
        raise CascadeViolation(
            "LQA cascade audit failed — refusing to write the report:\n  - "
            + "\n  - ".join(violations))
    return report


class CascadeViolation(RuntimeError):
    """The LQA report does not prove the T1→T2→T3→verify cascade ran."""


def verify_cascade(report: LQAReport) -> List[str]:
    """Structural audit of an LQAReport: did the four-step cascade actually
    run, in order, over every string? Returns violations ([] = compliant).

    Enforced in code, not prompts (design §7): tier counts must telescope
    (accepted → T1 → T2 → T3), every finding must carry a tier stamp, T3
    accounting must reconcile with the audit trail, and the report's own
    totals must agree with its items. An agent cannot fake compliance —
    the ledger is written by the graph nodes as they execute."""
    violations: List[str] = []

    def check(ok: bool, message: str) -> None:
        if not ok:
            violations.append(message)

    led = report.cascade_ledger
    required = ("accepted", "t1_flagged", "t2_input", "t2_flagged",
                "t3_input", "t3_ran", "second_layer", "t3_raw", "t3_kept")
    missing = [k for k in required if k not in led]
    if missing:
        return [f"cascade_ledger missing {missing} — "
                f"one or more tiers never ran"]

    # -- the telescope: each tier saw exactly what the previous one passed
    check(led["t2_input"] == led["accepted"] - led["t1_flagged"],
          f"T2 input {led['t2_input']} != accepted {led['accepted']} - "
          f"T1 flagged {led['t1_flagged']}")
    check(led["t3_input"] == led["t2_input"] - led["t2_flagged"],
          f"T3 input {led['t3_input']} != T2 input {led['t2_input']} - "
          f"T2 flagged {led['t2_flagged']}")
    check(report.checked == led["accepted"],
          f"report.checked {report.checked} != ledger accepted "
          f"{led['accepted']}")

    # -- coverage gaps must be declared, never inferred from silence
    unreviewed = led.get("t3_unreviewed", 0)
    check(unreviewed == sum(len(e.get("uids", []))
                            for e in report.t3_errors),
          f"ledger says {unreviewed} strings went unreviewed but "
          f"t3_errors accounts for "
          f"{sum(len(e.get('uids', [])) for e in report.t3_errors)}")

    # -- T3 accounting reconciles with the audit trail
    check(report.t3_stats.get("raw", 0) == led["t3_raw"],
          f"t3_stats.raw {report.t3_stats.get('raw')} != ledger t3_raw "
          f"{led['t3_raw']}")
    check(report.t3_stats.get("kept", 0) == led["t3_kept"],
          f"t3_stats.kept {report.t3_stats.get('kept')} != ledger t3_kept "
          f"{led['t3_kept']}")
    check(len(report.t3_audit) == led["t3_raw"],
          f"t3_audit has {len(report.t3_audit)} entries, ledger says "
          f"{led['t3_raw']} raw T3 findings")
    dropped = sum(count for reason, count in report.t3_stats.items()
                  if reason not in ("raw", "kept"))
    check(led["t3_raw"] == led["t3_kept"] + dropped,
          f"T3 raw {led['t3_raw']} != kept {led['t3_kept']} + dropped "
          f"{dropped} — findings vanished without an audit reason")
    if not led["t3_ran"]:
        check(led["t3_raw"] == 0,
              "T3 findings recorded although tier 3 never ran")

    # -- tier provenance on every surviving finding
    tier3_kept = 0
    for item in report.items:
        for vf in item.findings:
            tier = vf.finding.tier
            if tier not in (1, 2, 3):
                violations.append(
                    f"{item.uid}: finding has no tier stamp "
                    f"({vf.finding.bug_type.value})")
                continue
            if tier == 3:
                tier3_kept += 1
                if led["second_layer"] and vf.verdict is None:
                    violations.append(
                        f"{item.uid}: tier-3 finding kept without a "
                        f"second-layer verdict")
            elif vf.verdict is not None:
                violations.append(
                    f"{item.uid}: tier-{tier} finding carries an LLM "
                    f"verdict — T1/T2 are code, not judgment")
    check(tier3_kept == led["t3_kept"],
          f"items contain {tier3_kept} tier-3 findings, ledger kept "
          f"{led['t3_kept']}")

    # -- report-internal totals agree with the items
    total = sum(len(item.findings) for item in report.items)
    check(report.findings_total == total,
          f"findings_total {report.findings_total} != {total} findings "
          f"in items")
    check(report.flagged_strings == len(report.items),
          f"flagged_strings {report.flagged_strings} != {len(report.items)} "
          f"items")
    check(sum(report.by_severity.values()) == total,
          f"by_severity sums to {sum(report.by_severity.values())}, "
          f"items hold {total}")
    check(sum(report.by_bug_type.values()) == total,
          f"by_bug_type sums to {sum(report.by_bug_type.values())}, "
          f"items hold {total}")
    return violations
