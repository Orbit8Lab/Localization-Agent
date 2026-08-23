"""Stage 4 — the actual agentic core (design §4). Everything else is a
pipeline; this is the only place with a genuine control loop, and it is
where LangGraph earns its keep.

    prefill ──> tm_reuse ──> translate ──> gate ──> critic ──> route ──┐
                                                                       │
                    ┌── repair <── iterate ────────────────────────────┤
                    └──> gate (re-check) …          finalize <─────────┘
                                                    (accept | mtpe | escalate)

Boundary rules, enforced here in code:
- The gate is mechanical only — never spend a model call on what a regex
  settles.
- The Critic produces findings; it does NOT decide whether to loop.
- `route` is a deterministic conditional edge: it reads the iteration
  counter, the ratchet, and the convergence rule. The moment an agent can
  decide "good enough, stop", cost has no ceiling.
- Hard caps regardless: max_iterations per batch and a per-batch token
  budget that trips escalation when exceeded.

State holds segment IDs and batch-sized candidates only — full-corpus text
lives in the run DB (checkpointing 40k strings per superstep would bury the
checkpointer). One graph invocation processes ONE batch; the stage driver
(`run_translate_stage`) iterates batches and the Controller writes the
artifacts. The checkpointer is crash recovery within a stage-run, discarded
after the artifact write (design §1).
"""
from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Dict, List, Optional, Tuple, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .. import agents
from ..gate_checks import GateConfig, run_gate
from ..glossary import Glossary
from ..llm import Provider
from ..memory import RunDB, TranslationMemory
from ..observation import (ACCEPTED, FIRST, REJECTED, Observation,
                           ObservationLog, signatures)
from ..schemas import (Domain, Finding, MTPE_DOMAINS, MTPEItem, MTPEReason,
                       Severity, StyleBrief, TranslateRunSummary)

SEVERITY_WEIGHT = {Severity.HIGH: 100, Severity.MEDIUM: 10, Severity.LOW: 1}


def _badness(findings: List[Finding]) -> int:
    return sum(SEVERITY_WEIGHT[f.severity] for f in findings)


@dataclass
class TranslateConfig:
    game: str
    source_lang: str
    locale: str
    kind: str = "production"            # pilot | production
    batch_size: int = 15
    max_iterations: int = 2
    critic_mode: str = "flagged"        # off | flagged | all (pilot: all)
    samples: int = 1                    # best-of-N (pilot/high-stakes only)
    temperature: float = 0.3
    token_budget_per_batch: float = 60_000
    mtpe_confidence_threshold: float = 0.6   # classifier fails expensive (§8)
    gate: GateConfig = field(default_factory=GateConfig)
    client_lang: Optional[str] = None
    # Wire test: echo stubs are accepted as-is (the gate would rightly flag
    # them as leakage/untranslated and loop). The TM stub guard keeps them
    # out of reuse; critic_mode must be "off" in dry runs.
    dry_run: bool = False


class Candidate(TypedDict):
    target: str
    findings: List[Finding]
    term_decisions: Dict[str, str]


class TranslateState(TypedDict, total=False):
    job_id: str
    locale: str
    batch_id: str
    segments: List[dict]                # SegmentRef dumps: {uid, domain}
    iteration: int
    pending: List[str]                  # uids still needing an LLM candidate
    best: Dict[str, Candidate]          # uid -> best-so-far (the ratchet)
    seen: Dict[str, List[str]]          # uid -> finding identities seen
    critic_flagged: List[str]           # uids that ever drew a critic finding
    findings: Annotated[List[Finding], operator.add]   # audit log (reducer:
    # parallel critic branches merge through it; without it all but one
    # branch would be silently discarded)
    needs_repair: List[str]
    tokens_start: float
    decision: str                       # iterate | finalize


@dataclass
class StageContext:
    """Everything the nodes close over. Agents receive data through calls;
    they never read files (docs/agents rule 5)."""
    provider: Provider
    cfg: TranslateConfig
    run_db: RunDB
    tm: Optional[TranslationMemory] = None
    glossary: Optional[Glossary] = None
    style_brief: Optional[StyleBrief] = None
    # PLAN §3: write-only observation of the ratchet. Optional so every
    # existing caller keeps working and a run without it behaves
    # identically — this layer must be impossible to depend on.
    observations: Optional[ObservationLog] = None
    attempt: int = 1                   # the s4 attempt these rows belong to
    store_revision: int = 0


def build_translate_graph(ctx: StageContext):
    cfg = ctx.cfg

    def _text(uid: str) -> str:
        return ctx.run_db.get(uid)["text"]

    def _brief_for(uids: List[str]):
        if not ctx.glossary:
            return None
        return ctx.glossary.brief_for([_text(u) for u in uids])

    # ------------------------------------------------------------ prefill

    def prefill(state: TranslateState) -> dict:
        pending, resolved = [], 0
        for seg in state["segments"]:
            uid = seg["uid"]
            hit = ctx.glossary.prefill(_text(uid)) if ctx.glossary else None
            if hit is not None:
                ctx.run_db.record(uid, status="accepted", target=hit,
                                  resolution="prefill")
                resolved += 1
            else:
                pending.append(uid)
        return {"pending": pending, "iteration": 0, "best": {}, "seen": {},
                "critic_flagged": [],
                "tokens_start": ctx.provider.tokens_spent}

    # ----------------------------------------------------------- tm_reuse

    def tm_reuse(state: TranslateState) -> dict:
        pending = []
        for uid in state["pending"]:
            hit = (ctx.tm.lookup(_text(uid), cfg.locale)
                   if ctx.tm else None)
            if hit is not None:
                ctx.run_db.record(uid, status="accepted", target=hit,
                                  resolution="reuse")
            else:
                pending.append(uid)
        return {"pending": pending}

    # ---------------------------------------------------------- translate

    def translate(state: TranslateState) -> dict:
        uids = state["pending"]
        if not uids:
            return {"best": {}}
        items = [(uid, _text(uid)) for uid in uids]
        domain = Domain(state["segments"][0]["domain"])
        brief = _brief_for(uids)
        tm_examples = ctx.tm.examples(cfg.locale) if ctx.tm else None
        best: Dict[str, Candidate] = dict(state.get("best", {}))
        # Best-of-N (pilot): extra independent samples at rising temperature;
        # the gate's ratchet keeps whichever candidate scores best.
        for sample in range(max(1, cfg.samples)):
            translation, _fp = agents.translate_batch(
                ctx.provider, items, source_lang=cfg.source_lang,
                target_lang=cfg.locale, game=cfg.game, domain=domain,
                glossary_brief=brief, style_brief=ctx.style_brief,
                tm_examples=tm_examples,
                temperature=min(1.0, cfg.temperature + 0.2 * sample))
            for item in translation.items:
                candidate: Candidate = {"target": item.target_text,
                                        "findings": [],
                                        "term_decisions": item.term_decisions}
                if item.key not in best:
                    # First sample becomes the incumbent directly. Writing it
                    # under a staging key TOO would make the gate score one
                    # model call twice and log the second copy as a rejection
                    # of the first, understating the accept rate on first
                    # translations (PLAN §3 — the log's whole purpose).
                    best[item.key] = candidate
                    continue
                # later samples land via the gate ratchet below; store the
                # raw alternative under a staging key
                best[f"__sample__{sample}__{item.key}"] = candidate
        return {"best": best}

    # --------------------------------------------------------------- gate

    def gate(state: TranslateState) -> dict:
        """Mechanical checks + the ratchet. A candidate replaces the
        incumbent only when STRICTLY better — a repair that fixed one thing
        and broke another is rolled back, keeping quality monotonic."""
        best: Dict[str, Candidate] = {}
        # (candidate, strategy) pairs kept together: the staging key carries
        # provenance — repair candidates are written as
        # __sample__r{iteration}__{uid} (see repair()), extra samples as
        # __sample__{n}__{uid}, and the incumbent under the bare uid. A
        # log that cannot tell a repair's outcome from a first
        # translation's cannot measure whether repair works at all.
        staged: Dict[str, List[Tuple[Candidate, str]]] = {}
        for key, cand in state.get("best", {}).items():
            if key.startswith("__sample__r"):
                uid, strategy = key.split("__")[-1], "repair"
            elif key.startswith("__sample__"):
                uid, strategy = key.split("__")[-1], "translate"
            else:
                # The bare-uid incumbent, re-scored on every pass. It is
                # not a new attempt at anything, so it is not observed.
                uid, strategy = key, "incumbent"
            staged.setdefault(uid, []).append((cand, strategy))
        # An incumbent already scored on a previous pass is not a new
        # attempt; one appearing on the FIRST pass is the opening
        # translation and must be counted. `seen` (populated by route) is
        # empty until the first repair, which distinguishes the two without
        # threading extra state through the graph.
        first_pass = state.get("iteration", 0) == 0
        new_findings: List[Finding] = []
        for uid, candidates in staged.items():
            seg = ctx.run_db.get(uid)
            # Score the incumbent FIRST so a repair candidate is always
            # ratcheted against a real incumbent. Relying on dict order
            # here would work today and break silently the moment a node
            # writes its keys differently — and it would corrupt the
            # ratchet itself, not just the log.
            candidates.sort(key=lambda pair: pair[1] != "incumbent")
            for cand, strategy in candidates:
                findings = [] if cfg.dry_run else run_gate(
                    uid, seg["text"], cand["target"], cfg.gate,
                    term_decisions=cand["term_decisions"])
                scored: Candidate = {"target": cand["target"],
                                     "findings": findings,
                                     "term_decisions": cand["term_decisions"]}
                incumbent = best.get(uid)
                accepted = (incumbent is None
                            or _badness(findings)
                            < _badness(incumbent["findings"]))
                if strategy != "incumbent" or first_pass:
                    _observe(state, uid, scored, incumbent,
                             "translate" if strategy == "incumbent"
                             else strategy, accepted)
                if accepted:
                    best[uid] = scored
            new_findings.extend(best[uid]["findings"])
        return {"best": best, "findings": new_findings}

    # -------------------------------------------------------- observation

    def _observe(state: TranslateState, uid: str, scored: Candidate,
                 incumbent: Optional[Candidate], strategy: str,
                 accepted: bool) -> None:
        """Write one ratchet observation (PLAN §3). Write-only.

        Records REJECTIONS as well as accepts, deliberately: a rolled-back
        repair is the negative example that will later bound a skill's
        applicability (PLAN §4.3, §6.3). Logging only the winners is how a
        skill silently generalizes past where it works.

        Never raises. This is a logging concern hanging off the pipeline's
        single most important decision, and no observation is worth
        failing a translate batch for.
        """
        if ctx.observations is None:
            return
        try:
            ctx.observations.record(Observation(
                job_id=state.get("job_id", ""),
                locale=cfg.locale,
                attempt=ctx.attempt,
                revision=ctx.store_revision,
                batch_id=state.get("batch_id"),
                uid=uid,
                # Signatures of what is STILL WRONG with this candidate —
                # the defect classes the next repair would target.
                signatures=signatures(scored["findings"]),
                strategy=strategy,
                target=scored["target"],
                iteration=state.get("iteration", 0),
                badness_before=(None if incumbent is None
                                else _badness(incumbent["findings"])),
                badness_after=_badness(scored["findings"]),
                verdict=(FIRST if incumbent is None
                         else ACCEPTED if accepted else REJECTED),
                tokens=(ctx.provider.tokens_spent
                        - state.get("tokens_start", 0.0)),
            ))
        except Exception:                      # pragma: no cover - guard
            pass

    # ------------------------------------------------------------- critic

    def critic(state: TranslateState) -> dict:
        """Produces findings and severities. It does not decide whether to
        loop — that is route's job, in code."""
        best = dict(state.get("best", {}))
        if cfg.critic_mode == "off" or not best:
            return {}
        flagged_before = set(state.get("critic_flagged", []))
        if cfg.critic_mode == "flagged":
            # Strings that ever drew a critic finding stay under review even
            # after a repair clears their gate findings — otherwise a repair
            # that fixes the mechanical defect silently sheds the critic's
            # judgment finding.
            targets = [uid for uid, c in best.items()
                       if c["findings"] or uid in flagged_before]
        else:
            targets = list(best)
        if not targets:
            return {}
        items = [(uid, _text(uid), best[uid]["target"]) for uid in targets]
        known = [f for uid in targets for f in best[uid]["findings"]]
        review, _fp = agents.review_batch(
            ctx.provider, items, source_lang=cfg.source_lang,
            target_lang=cfg.locale, game=cfg.game, known_findings=known,
            glossary_brief=_brief_for(targets),
            style_brief=ctx.style_brief, client_lang=cfg.client_lang)
        now_flagged = set(flagged_before)
        accepted: List[Finding] = []
        for finding in review.findings:
            if finding.key in best and finding.evidence:
                best[finding.key]["findings"].append(finding)
                now_flagged.add(finding.key)
                accepted.append(finding)
        return {"best": best, "critic_flagged": sorted(now_flagged),
                "findings": accepted}

    # -------------------------------------------------------------- route

    def route(state: TranslateState) -> dict:
        """THE deterministic control decision (design §4). Reads iteration,
        ratchet, convergence, and the token budget — nothing else."""
        best = state.get("best", {})
        seen = {uid: list(ids) for uid, ids in state.get("seen", {}).items()}
        iteration = state.get("iteration", 0)
        spent = ctx.provider.tokens_spent - state.get("tokens_start", 0.0)

        needs_repair: List[str] = []
        for uid, cand in best.items():
            above_low = [f for f in cand["findings"]
                         if f.severity != Severity.LOW]
            if not above_low:
                continue            # converged: all-LOW is accepted anyway
            # Convergence: repair only when findings contain something NEW.
            identities = [str(f.identity()) for f in above_low]
            if iteration > 0 and all(i in seen.get(uid, []) for i in identities):
                continue            # repeat findings — repairing again is spend
            seen.setdefault(uid, []).extend(
                i for i in identities if i not in seen.get(uid, []))
            needs_repair.append(uid)

        budget_tripped = spent > cfg.token_budget_per_batch
        if (not needs_repair or iteration >= cfg.max_iterations
                or budget_tripped):
            return {"decision": "finalize", "needs_repair": [],
                    "seen": seen}
        return {"decision": "iterate", "needs_repair": needs_repair,
                "seen": seen, "iteration": iteration + 1}

    # ------------------------------------------------------------- repair

    def repair(state: TranslateState) -> dict:
        best = dict(state.get("best", {}))
        flagged = [(uid, _text(uid), best[uid]["target"],
                    best[uid]["findings"])
                   for uid in state["needs_repair"]]
        repaired, _fp = agents.repair_batch(
            ctx.provider, flagged, source_lang=cfg.source_lang,
            target_lang=cfg.locale, game=cfg.game,
            glossary_brief=_brief_for([uid for uid, *_ in flagged]),
            style_brief=ctx.style_brief)
        # Stage the repair candidates; the gate node ratchets them against
        # the incumbents (equal-or-worse candidates are rolled back there).
        for item in repaired.items:
            best[f"__sample__r{state['iteration']}__{item.key}"] = {
                "target": item.target_text, "findings": [],
                "term_decisions": item.term_decisions}
        return {"best": best}

    # ----------------------------------------------------------- finalize

    def finalize(state: TranslateState) -> dict:
        """Accept, or route to MTPE — tagged distinctly (design §4): a
        translator post-editing by policy needs different framing from one
        repairing a string the system failed on four times."""
        for seg in state["segments"]:
            uid = seg["uid"]
            row = ctx.run_db.get(uid)
            if row["status"] == "accepted" and row["resolution"] in (
                    "prefill", "reuse"):
                continue
            cand = state.get("best", {}).get(uid)
            if cand is None:
                continue
            domain = Domain(row["domain"])
            findings = cand["findings"]
            unresolved = any(f.severity != Severity.LOW for f in findings)
            if unresolved:
                ctx.run_db.record(uid, status="mtpe", target=cand["target"],
                                  resolution=MTPEReason.FAILURE.value,
                                  findings=findings)
            elif domain in MTPE_DOMAINS:
                ctx.run_db.record(uid, status="mtpe", target=cand["target"],
                                  resolution=MTPEReason.DOMAIN_POLICY.value,
                                  findings=findings)
            elif row["confidence"] < cfg.mtpe_confidence_threshold:
                # Classifier fails expensive, not cheap: a low-confidence
                # label routes TO MTPE, never away from it (design §8).
                ctx.run_db.record(uid, status="mtpe", target=cand["target"],
                                  resolution=MTPEReason.LOW_CONFIDENCE.value,
                                  findings=findings)
            else:
                ctx.run_db.record(uid, status="accepted",
                                  target=cand["target"],
                                  resolution="translated", findings=findings)
                if ctx.tm:
                    ctx.tm.store(row["text"], cand["target"], cfg.locale)
        return {"decision": "done"}

    graph = StateGraph(TranslateState)
    graph.add_node("prefill", prefill)
    graph.add_node("tm_reuse", tm_reuse)
    graph.add_node("translate", translate)
    graph.add_node("gate", gate)
    graph.add_node("critic", critic)
    graph.add_node("route", route)
    graph.add_node("repair", repair)
    graph.add_node("finalize", finalize)

    graph.add_edge(START, "prefill")
    graph.add_edge("prefill", "tm_reuse")
    graph.add_edge("tm_reuse", "translate")
    graph.add_edge("translate", "gate")
    graph.add_edge("gate", "critic")
    graph.add_edge("critic", "route")
    graph.add_conditional_edges(
        "route", lambda s: s["decision"],
        {"iterate": "repair", "finalize": "finalize"})
    graph.add_edge("repair", "gate")
    graph.add_edge("finalize", END)
    return graph


def run_translate_stage(ctx: StageContext, job_id: str,
                        limit: Optional[int] = None) -> TranslateRunSummary:
    """Stage driver: one graph invocation per (domain-grouped) batch. The
    Controller — not this function, not any agent — writes the artifacts."""
    cfg = ctx.cfg
    refs = ctx.run_db.refs("pending")
    if limit:
        refs = refs[:limit]

    # Batch by domain so the domain-aware prompt applies batch-wide.
    by_domain: Dict[Domain, List[dict]] = {}
    for ref in refs:
        by_domain.setdefault(ref.domain, []).append(ref.model_dump(mode="json"))

    compiled = build_translate_graph(ctx).compile(checkpointer=InMemorySaver())
    batch_no = 0
    for domain, segments in sorted(by_domain.items(), key=lambda kv: kv[0].value):
        for start in range(0, len(segments), cfg.batch_size):
            batch = segments[start:start + cfg.batch_size]
            batch_no += 1
            compiled.invoke(
                {"job_id": job_id, "locale": cfg.locale,
                 "batch_id": f"b{batch_no:03d}", "segments": batch,
                 "iteration": 0, "findings": []},
                config={"configurable":
                        {"thread_id": f"{job_id}-{cfg.locale}-b{batch_no:03d}"},
                        "recursion_limit": 12 + 4 * cfg.max_iterations})

    counts = ctx.run_db.counts()
    rows = ctx.run_db.all_segments()
    return TranslateRunSummary(
        job_id=job_id, locale=cfg.locale, kind=cfg.kind,
        segments_total=len(rows),
        accepted=counts.get("accepted", 0),
        prefilled=sum(1 for r in rows if r["resolution"] == "prefill"),
        reused=sum(1 for r in rows if r["resolution"] == "reuse"),
        escalated=sum(1 for r in rows
                      if r["resolution"] == MTPEReason.FAILURE.value),
        mtpe_policy=sum(1 for r in rows if r["resolution"] in
                        (MTPEReason.DOMAIN_POLICY.value,
                         MTPEReason.LOW_CONFIDENCE.value)),
        tokens_spent=ctx.provider.tokens_spent,
        iterations_max=cfg.max_iterations)


def assemble_mtpe_queue(run_db: RunDB, locale: str) -> List[MTPEItem]:
    """g_flagged (4c): the escalation package for gate G3 / the MTPE queue."""
    items = []
    for row in run_db.by_status("mtpe"):
        items.append(MTPEItem(
            uid=row["uid"], source=row["text"], target=row["target"] or "",
            domain=Domain(row["domain"]),
            reason=MTPEReason(row["resolution"]),
            findings=row["findings"]))
    return items
