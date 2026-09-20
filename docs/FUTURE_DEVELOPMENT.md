# Future development — harness first

## Decision

Orbit8 will build a reusable **harness foundation before it refits the
workflow, introduces multiple agents, or lets learned behaviour influence a
live run**.

The current product is a localization workflow with one effective operator.
It already has the right safety primitives — Controller-owned transitions,
artifact-authoritative state, bounded stage graphs, gates, and model
fingerprints.  The harness makes those primitives observable, replayable and
testable without assuming a future client workflow, role model, or agent
roster.

The harness is not another agent and it cannot advance a live job, approve a
gate, alter a glossary, or change a policy.  It runs a copy of inputs in an
isolated workspace and reports what happened.

```
current workflow
      |
      v
harness foundation ---------> protects every later change
      |
      v
incremental workflow refit
      |
      v
feedback + role contracts
      |
      v
multi-agent coordination
      |
      v
multi-agent evaluation + bounded learning
```

## The development sequence

| Phase | Objective | Do not do yet | Exit condition |
|---|---|---|---|
| 1. Harness foundation | Make one existing stage run isolated, attributable, replayable and comparable. | No client-domain rewrite; no new agents; no automatic learning. | A deterministic run can be replayed and a recorded agent run can be compared to its baseline. |
| 2. Incremental workflow refit | Replace one localization workflow slice with the client's actual case/work-item flow. | Do not infer people, teams, or authority that the client has not defined. | One low-risk client workflow completes with Controller-owned artifacts and gates. |
| 3. Outcome capture | Record the human decisions and real outcomes that define quality for the new workflow. | Do not promote or apply a learned strategy. | Reviewed cases carry trustworthy outcome labels. |
| 4. Role contracts | Define responsibilities, permissions, handoff artifacts and approval ownership. | No autonomous delegation or multi-agent evaluation. | Every meaningful handoff has a named role, required inputs and a completion rule. |
| 5. Multi-agent coordination | Add bounded specialist agents that operate through the role contracts. | Agents may not self-assign authority, self-approve, or bypass a handoff. | The Controller can enforce all handoffs and role-scoped permissions. |
| 6. Multi-agent harness | Evaluate the real coordination flow with the same permissions used in production. | Do not evaluate an imagined role workflow. | Synthetic/de-identified cases detect bad handoffs, policy breaches and regressions before release. |
| 7. Bounded self-improvement | Use held-out human outcomes to propose reversible, tenant-scoped improvements. | Never let learning rewrite the Controller, permissions, gates or policy checks. | Every promoted change has evidence, a boundary, monitoring and rollback. |

## Phase 1 — harness foundation

This is the next implementation target.  It is deliberately domain-neutral:
the first cases may be today’s localization jobs, while future client cases
use the same interfaces.

### Scope

1. **Isolated execution.** Clone or seed a job/case into a temporary harness
   root.  The runner must never write to a live artifact tree or live RunDB.
2. **Run manifest.** Capture the code revision, resolved stage configuration,
   input artifact hashes, skill-document hashes, provider/model fingerprint,
   locale/tenant coordinates, timestamps, token use and exit status.
3. **Event capture.** Record Controller-derived stage transitions, tool calls,
   graph route decisions, gate holds, artifact writes and emitted output
   fingerprints.
4. **Replay providers.** Support deterministic replay first, then a recorded
   provider that returns captured model responses.  A recorded replay tests
   graph wiring and safety boundaries without asking a changing model to
   reproduce exact prose.
5. **Assertions and comparison.** Check artifact schemas, required outputs,
   forbidden state transitions, attempt boundaries, token/step budgets, and
   stable output fingerprints.  Produce a readable diff, not only a pass/fail
   count.
6. **CLI and CI.** Provide commands to record a baseline, run a case and
   compare a candidate change.  Deterministic drift may fail CI; model-quality
   differences should be reported for review until human-labelled evaluation
   exists.

### Existing components to reuse

- `controller.Job` remains the only authority that derives and advances a
  lifecycle stage.
- `store.ArtifactStore` remains the durable input/output boundary.
- `replay.py` supplies the initial deterministic LQA fingerprint comparison.
- `observation.py` supplies append-only outcome evidence; it must remain
  write-only until a later, explicitly approved learning phase.
- Provider abstractions in `llm.py` provide the seam for recorded responses.

### Initial non-goals

- No customer-support schema, ticket taxonomy, CSAT metric, or escalation
  department is assumed.
- No PM, post-editor, LQA tester or customer agent is introduced.
- No live case is mutated by a harness run.
- No exact-match assertion is made about free-form live-model output.

### Acceptance criteria

- A case declares every input and expected invariant in a versioned manifest.
- Re-running deterministic work from the same manifest produces the same
  fingerprint.
- Removing or changing a required input makes the run explicitly
  unreplayable; it must never silently substitute a different input.
- A recorded-agent replay cannot skip a gate or write an artifact outside the
  isolated run root.
- A comparison identifies changed artifacts/routes/findings, not merely a
  changed aggregate count.

## Phase 2 — refit one client workflow at a time

Do not rename the whole localization pipeline into a hypothetical support
system.  Select one low-risk client workflow, define its durable artifacts
and use the Controller/harness to protect it.  A likely generic shape is:

```
intake -> classify -> retrieve approved context -> draft -> check
                                                   |            |
                                                   v            v
                                             request input   send/escalate
                                                   \            /
                                                    -> outcome
```

Each new artifact must state its schema, producer, inputs, and the Controller
rule that permits the next transition.  Add a harness case alongside the new
workflow before extending it.

## Phase 3 — capture outcomes before learning

The new workflow, not the current localization vocabulary, determines what
counts as improvement.  Capture human approval/edit/rejection, escalation
correction, resolution/reopen status, and any client-approved quality metric.
Keep those facts append-only and tenant-scoped.  They are evidence for later
evaluation; they must not yet alter prompts, routing or permissions.

## Phases 4–6 — roles, coordination and its harness

Only after a client workflow exists should the system model roles such as
customer, project manager, post-editor and LQA tester.  Every role contract
needs:

- permitted tools/actions;
- owned artifacts and required inputs;
- handoff destination, reason and completion condition; and
- a human or Controller authority for approval/escalation.

Specialist agents then operate as bounded workers over these contracts.  The
multi-agent harness follows afterwards and runs the same role permissions in
an isolated environment.  It measures handoff correctness, unauthorized
action attempts, escalation precision, work completion, cost and latency.

## Phase 7 — bounded self-improvement

The current observation and skill work is a useful prerequisite, but it is
not a live self-improving system.  Activate it only after the harness has
baselines and the refitted workflow has reviewed outcomes.

The allowed loop is:

```
observe -> evaluate on held-out human outcomes -> propose a bounded change
        -> human approval -> monitored application -> demote or roll back
```

Initially, learned material may be supplied only as non-binding hints.  Any
later automatic application must remain tenant-scoped, pass the existing
checks, include an applicability boundary, and stop immediately when monitored
human outcomes deteriorate.

## Change-control rule

Every new workflow, tool, agent, role rule, prompt policy or learned strategy
must add or update a harness case before it is deployed.  The harness is the
stable engineering platform; the workflow and agent roster may evolve on top
of it.
