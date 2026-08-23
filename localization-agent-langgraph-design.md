# Localization agent — LangGraph design

Mapped onto Localization Pipeline V2 (`docs/LIFECYCLE.md`), 8 stages / 6 gates.

---

## 1. The core decision: who owns the state

The lifecycle doc already specifies a state model: *the Job Controller derives the
current stage from artifacts on disk*. LangGraph ships its own state model: a
checkpointer that serialises graph state to Postgres and resumes from it.

**These two must not both be authoritative.** If they are, you get jobs where the
artifact directory says Stage 5 and the checkpointer says Stage 4, and no rule for
which wins.

Recommendation: **artifacts stay authoritative, LangGraph is demoted to a stage
executor.**

| Layer | Owns | Lifetime |
|---|---|---|
| Job Controller (plain Python + Postgres) | current stage, gate status, attempt numbers | job lifetime (weeks–months) |
| LangGraph graph | one stage-run's internal work | minutes–hours |
| Checkpointer | crash recovery *within* a stage-run | discarded after artifact write |

The Controller is a boring deterministic state machine. It scans the artifact tree,
computes the stage, checks gate status, and invokes exactly one stage graph. It is
maybe 400 lines and it is the most important 400 lines in the system — it is the
thing that guarantees agents never control the loop.

### Why not one graph for the whole job

Four reasons, all load-bearing:

1. **Jobs outlive schemas.** A job spanning two months will cross several
   `JobState` schema revisions. A thread checkpointed under the old schema
   deserialises into a class that no longer exists. Stage-scoped graphs finish in
   hours, so no live thread ever spans a deploy.
2. **Gates are days long.** A thread parked in `interrupt()` for a client's
   two-week legal review is a liability, not a feature.
3. **Defect loops are backward edges.** S5 → S4 and S6 → S4 turn a whole-job graph
   into a cyclic mess with confusing checkpoint lineage. As stage-scoped graphs,
   re-entering S4 is simply a *new run* seeded by artifacts — clean, inspectable,
   and independently replayable.
4. **Incremental (∞) re-enters at S1.** Same argument. The delta path is a fresh
   sequence of stage-runs against a locked asset, not a resumed thread.

---

## 2. Graph decomposition

Ten graphs, not eight — Stage 4 splits into three because two gates sit inside it.

| Graph | Stage | Kind | Ends at |
|---|---|---|---|
| `g_intake` | 0 | mostly human + 1 agent | G0 |
| `g_ingest` | 1 | pure deterministic | — |
| `g_context` | 2 | 2 agents, map-parallel | — |
| `g_asset` | 3 | agent + deterministic gate | G1 |
| `g_pilot` | 4a | full agentic loop, critic mode all | G2 |
| `g_production` | 4b | full agentic loop, ratcheted | G3 (conditional) |
| `g_flagged` | 4c | assemble escalation package | G3 |
| `g_lqa` | 5 | tiered cascade | — |
| `g_testing` | 6 | agent + human execution | G4 |
| `g_release` | 7 | agent + deterministic manifest | G5 |

Every gate now falls *between* graphs. No LangGraph thread ever waits on a human
for more than the duration of a single stage-run. Gate holds live in Postgres as
Controller state, surfaced on the dashboard, resolved by a client or PM action that
flips a row — not by resuming a thread.

`g_ingest` deserves a note: it is 100% deterministic. It should not be a LangGraph
graph at all. Make it a plain function the Controller calls. Resist the urge to
wrap everything uniformly — uniformity here costs you clarity and adds a
checkpointer round-trip to a pure transform.

---

## 3. Artifact contract

Every artifact gets a Pydantic model and a version field. The Controller validates
on write and refuses to advance the stage on a validation failure.

```python
class Artifact(BaseModel):
    schema_version: int
    job_id: str
    stage: int
    attempt: int
    produced_at: datetime
    produced_by: str        # "code:ingest_adapter@1.4" | "agent:terminologist@2.1"
    model_fingerprint: str | None   # model id + prompt hash, agents only
```

Stage entry precondition = "these artifacts exist and validate". That single rule
gives you resumability, testability, and a replayable audit trail for free.

### Attempt versioning is not optional

Defects loop back to S4 repeatedly. Without attempt numbers, re-entry overwrites the
very artifact a client-facing `bug_report.xlsx` points at.

```
jobs/<job_id>/
  s3/glossary.v1.json          # frozen at G1
  s4/attempt-01/translated.jsonl
  s4/attempt-02/translated.jsonl
  s5/attempt-01/lqa_report.json
  s5/attempt-02/lqa_report.json
```

`model_fingerprint` matters more than it looks: when a studio disputes a
translation six weeks later, you need to know which model and prompt version
produced it. It is also what makes your AMTA benchmark numbers reproducible against
a frozen job.

---

## 4. Stage 4 — the actual agentic core

Everything else is a pipeline. This is the only place with a genuine control loop,
and it is where LangGraph earns its keep.

### State

```python
class TranslateState(TypedDict):
    job_id: str
    locale: str
    batch_id: str
    segments: list[SegmentRef]          # IDs, never full text
    iteration: int
    best_score: dict[str, float]        # seg_id -> best score so far (the ratchet)
    findings: Annotated[list[Finding], operator.add]
    converged: set[str]
    escalated: set[str]
    tokens_spent: float
```

Two things to get right or the loop misbehaves:

- `findings` needs the `operator.add` reducer. Parallel critic branches merge
  through it; without the reducer all but one branch is silently discarded.
- `segments` holds IDs. Text lives in the run DB. Checkpointing 40k strings per
  superstep will bury Postgres.

### Node flow

```
prefill ──> tm_reuse ──> translate ──> gate ──> critic ──> repair ──┐
                                        │                            │
                                        └──> converged ──> emit      │
                                                                     │
                            ┌────────────────────────────────────────┘
                            └──> route: iterate | mtpe_queue | escalate(G3)
```

- `prefill`, `tm_reuse`, `gate` — deterministic. The gate is mechanical only:
  placeholder integrity, markup wellformedness, length limits, T1 glossary
  compliance. Never spend a model call on what a regex settles.
- `translate` — domain-aware prompt selected by the S2 domain label.
- `critic` — produces findings and severities. **It does not decide whether to
  loop.**
- `repair` — consumes findings, produces a new candidate.
- `route` — deterministic conditional edge. It reads `iteration`, the ratchet, and
  the convergence rule, and picks the next edge.

### The ratchet and the routing rule

The lifecycle doc refers to "the ratchet and convergence rules" without defining
them in the excerpt I have. My assumption, which you should correct if wrong:

- **Ratchet** — a repair candidate is accepted only if it does not score worse than
  the incumbent on any dimension. Quality is monotonic across iterations; a repair
  that fixes terminology while breaking register is rejected, not accepted.
- **Convergence** — a segment exits the loop when the critic returns no findings
  above minor severity, or when score improvement between iterations falls below a
  threshold.

Both belong in `route`, in code, with the thresholds in job config. This is the
single most important boundary in the system: **the Critic decides findings, the
Controller decides iterations.** The moment an agent can decide "good enough, stop",
you have an agent controlling the loop and a system whose cost has no ceiling.

Hard caps regardless: `max_iterations` per segment, and a per-batch token budget in
state that trips `escalate` when exceeded.

### Routing to MTPE

Two paths into the human post-editing queue:

1. **Domain-based** — story and marketing route there unconditionally, per the doc.
2. **Failure-based** — segments that hit `max_iterations` without converging.

These should be tagged distinctly in the queue. A translator post-editing a
marketing string by policy needs different framing from one repairing a string the
system failed on four times.

---

## 5. Stage 5 — the tier cascade

Tiers 1/2/3 are a cost ladder and must run as one, not in parallel.

| Tier | Owner | Runs on |
|---|---|---|
| T1 mechanical | code | everything |
| T2 glossary & consistency | code + retrieval | everything that passes T1 |
| T3 semantic | LLM agent | only what survives T1 and T2 |

T2 has a structural catch: **consistency is not a per-segment property.** A term
rendered two ways across different menus is invisible to any segment-scoped check.
T2 needs a project-level pass over the full term→rendering map after fan-in, not a
map operation. Budget for it as a distinct node with the whole locale in scope.

T3 is your false-positive risk. Precision beats recall here by a wide margin —
studios abandon LQA tooling that cries wolf, and a noisy first pilot is very hard to
recover from commercially. Ship with an aggressive confidence threshold plus
per-issue-type suppression, tuned against the cross-lingual benchmark, and loosen it
as precedent data accumulates.

---

## 6. Memory: three lifetimes, three stores

This is the piece you flagged as the open difficulty. The trap is treating it as one
store. It is three, with different lifetimes and different write rules.

| Store | Scope | Contents | Write rule |
|---|---|---|---|
| Graph state | one stage-run | iteration counters, findings in flight | discarded on completion |
| Job asset | one job, permanent | TM, glossary T1, style brief, domain labels, run DB | append-only; T1 frozen at G1 |
| Tenant/genre memory | across jobs | T2 genre wording families, adjudicated precedents | curated, never auto-written |

The T2 tier is the interesting one commercially. Genre wording families accumulate
across every job you run in a genre, which means job number twelve in social
deduction starts materially stronger than job number one. That is a compounding
asset and it belongs in tenant-scoped storage with explicit curation — not
auto-appended from whatever the last Terminologist run produced.

Precedent memory (issues a reviewer accepted or rejected, with context) is the
feedback signal that makes the system adapt to a studio's process. Handle it
carefully: feeding back every rejection uncritically teaches the system to stop
flagging whatever a rushed reviewer dismissed in bulk. Require a minimum count and
consistency before a rejection pattern becomes a suppression rule.

Namespace everything by `(tenant_id, job_id)`. Multi-tenant leakage in a glossary
is a client-losing bug.

---

## 7. Permission boundaries

Enforce these in the tool layer, not the prompt. A prompt instruction is a
suggestion; a missing tool is a guarantee.

- **After G1 the glossary is immutable.** Repair must have no write path to it. It
  gets a `request_audited_fix()` tool that files a request re-opening G1 — and
  nothing else.
- **No agent writes an artifact directly.** Agents return typed objects; the
  Controller validates and writes. This is what makes `produced_by` trustworthy.
- **No agent advances a stage or clears a gate.** Not even by producing an artifact
  that implies it.
- **Critic cannot terminate its own loop** (see §4).

---

## 8. Sequencing note on the agent roster

The roster puts Domain Classifier at M6. But the S2 domain label is an input to
three separate things: S4 prompt selection, S4 MTPE routing, and S6 test-case
generation. Two of those (Translator, Critic) are already shipped at M1, and the
third is M7.

That means from M1 to M6 the shipped translation loop runs on either a single
generic prompt or a hand-maintained label. If it is the former, you are measuring
your shipped agents under conditions that will change materially at M6 — including
in any benchmark numbers you publish before then.

Worth considering pulling a rules-based v0 classifier forward. Domain labels for
game strings are substantially predictable from the source file path and key naming
convention (`UI_`, `ITEM_`, `DLG_`), which gets you most of the routing value for
very little work, with the LLM classifier at M6 upgrading precision rather than
introducing the capability.

One safety property for it whenever it lands: **the classifier should fail
expensive, not cheap.** A low-confidence label routes to MTPE, not away from it.
Misclassifying a marketing string as UI silently skips human post-editing on
exactly the content where errors are most visible.

---

## 9. Open questions

1. Exact definitions of the ratchet and convergence rules — §4 states my
   assumptions.
2. Is the pilot module (G2) locale-scoped or one pilot across all locales? Changes
   whether `g_pilot` is one run or N.
3. Does `mtpe_queue` re-enter the graph after post-editing, or is post-edited text
   final and passed straight to S5?
4. What resets on incremental delta — does the delta path re-run S2 context
   analysis, or inherit the style brief wholesale?
5. Tier 3 severity taxonomy — is it MQM-aligned? If the client-facing
   `bug_report.xlsx` uses MQM categories, studios with existing LQA vendors will
   read it without a legend.
