# Learning track

This document covers only Orbit8's future learning and self-improvement
capabilities. It is **not** the product development roadmap.

The authoritative implementation order is
[docs/FUTURE_DEVELOPMENT.md](docs/FUTURE_DEVELOPMENT.md): build the
domain-neutral harness foundation first, refit one real client workflow,
capture reviewed outcomes, define role contracts, and then add multi-agent
coordination and its harness.

The learning track may not change a live workflow before those prerequisites
exist. A prompt instruction is a suggestion; the Controller's permissions,
gates, and artifact contracts remain the guarantee.

## Current state

| Capability | State | What exists |
|---|---|---|
| Sandboxed tool building | Built, limited | The Adapter-Writer generates an ingest adapter; `sandbox.py` isolates it and only validated stdout crosses the boundary. |
| Runtime skill documents | Built | `skill_docs.py` loads playbooks by Controller-derived `(phase, gate)` and validates their declared tools against the live tool registry. |
| Observation and calibration | Built, write-only | `observation.py` records repair/ratchet outcomes and derived G3 verdicts. `skills.py` provides tenant-scoped tallies, candidate promotion requests, boundaries, decay checks, and offline calibration. |
| Runtime learning | Not built | Nothing retrieves a learned lesson into a prompt, applies a learned repair, files a promotion artifact, or activates a promoted skill. |
| Self-improvement | Not built | There is no experiment ledger, held-out harness evaluation, promotion workflow, monitoring loop, or automatic rollback. |

The existing observation and skill code is evidence infrastructure, not a
self-improving agent in operation.

## Preconditions

Before any learned behaviour can influence a live run, all of the following
must be true:

1. The harness foundation in `FUTURE_DEVELOPMENT.md` can run isolated,
   replayable cases and report artifact/route regressions.
2. The active client workflow records real, tenant-scoped human outcomes.
3. The quality signal is independent of the system being judged. A gate's
   own score may filter candidates, but cannot by itself promote them.
4. The Controller can enforce the learned behaviour's scope, approval and
   rollback conditions.

For future multi-agent workflows, role contracts and the multi-agent harness
are additional preconditions. Do not infer those contracts from today's
single-operator localization workflow.

## Remaining work

### 1. Complete the harness-first foundation

Follow Phase 1 of `docs/FUTURE_DEVELOPMENT.md`. In particular, capture a run
manifest, use an isolated root, support deterministic and recorded-provider
replay, and compare artifacts/routes rather than only aggregate counts.

This work is intentionally domain-neutral. The current localization stages
are the first protected cases; a later client workflow uses the same harness.

### 2. Make outcome evidence usable for the refitted workflow

Keep observations append-only and tenant-scoped. Preserve the distinction
between the ratchet's decision and a human decision:

- `first` / `accepted` / `rejected`: the deterministic ratchet;
- `pending` / `accepted` / `edited` / `rejected`: the human outcome.

For each new workflow, define equivalent human outcomes before attempting to
learn from it. A missing outcome is unknown, not approval.

### 3. Evaluate and calibrate offline

Use fixed observation data and harness cases to answer:

- Do defect/action signatures recur across distinct cases?
- Does the internal score agree with reviewed human outcomes?
- Which promotion threshold or boundary is binding?
- Does a proposed change improve held-out cases without increasing cost or
  unsafe escalation?

If signatures are mostly singletons, build a case-specific cache only if it
pays for itself; do not call it a reusable skill. If human outcomes disagree
with the internal scorer, fix the scorer before building a promoter.

### 4. Retrieve lessons only as non-binding hints

After the preceding evidence exists, introduce exact structured matching
first. A matched lesson may be supplied as a hint to the appropriate agent;
it cannot bypass the normal gate, route, or approval process.

Record each retrieval's signature, source, prompt/model fingerprint, outcome,
and whether it was an exact or fallback match. Add semantic retrieval only if
the harness shows the exact path has insufficient useful coverage.

### 5. Add audited promotion and bounded application

Implement a durable, Controller-owned promotion request artifact and a human
approval action. A promotion request must include:

- tenant, signature, proposed strategy and applicability boundary;
- distinct-case count, internal improvement and reviewed-human agreement;
- counter-examples, source attempts and store/prompt revision; and
- harness results on held-out cases.

An approved strategy may apply only inside its recorded boundary and must
pass through the ordinary deterministic gate. It must never write a glossary,
change a role permission, clear a gate, or alter Controller routing.

### 6. Monitor, demote and roll back

Keep an experiment ledger: what changed, why it was approved, its scope,
baseline, harness result and later human outcomes. Monitor recent—not
lifetime—utility. A deterioration files a demotion request; evidence of
active harm stops application immediately and requires review before resume.

## Non-negotiable boundaries

- Learned content is tenant-scoped by default. Sharing requires a separate,
  audited decision.
- Human-reviewed outcomes are the promotion signal; internal scores are
  supporting evidence only.
- Generated tools remain sandboxed and schema-validated forever.
- Agents do not directly write authoritative artifacts, advance stages,
  approve gates, or grant themselves tools.
- No learning system may rewrite its own Controller, permissions, policy
  checks, gates, or evaluation criteria.

## Definition of done: bounded self-improvement

Orbit8 is a safely self-improving system only when it can:

```text
observe -> evaluate on held-out human outcomes -> propose a bounded change
        -> human approval -> monitored application -> demote or roll back
```

Until then, it is a deterministic workflow with increasingly useful
observability—not a self-improving agent in operation.
