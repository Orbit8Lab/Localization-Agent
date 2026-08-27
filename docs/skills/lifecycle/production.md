---
name: production
phase: PRODUCTION
tools: [status, next_step, read_artifact, flagged]
summary: the full translate loop; watch the ratchet and the budget, not individual strings
---

# PRODUCTION — the full translate loop

The only genuine control loop in the system:

    prefill → tm_reuse → translate → gate → critic → route → repair ─┐
                                                ↑                    │
                                                └────────────────────┘

Three hard stops are read **in code**, never by an agent: the ratchet
(strictly-better-or-rollback), the convergence rule (only NEW findings
justify another repair), and the per-batch token budget. The moment an
agent could decide "good enough," cost would have no ceiling.

There is no gate at the end of PRODUCTION — it flows into LQA. So the
review here is retrospective: read what the loop did.

## Sequence

1. `next_step` — runs the locale's production batch set.
2. `read_artifact run_summary.production.<locale>`.
3. `flagged` — the escalated population, if you need to see why.

## Reading the summary

The invariant worth checking first:

    accepted + escalated + mtpe_policy == segments_total

- **`prefilled` / `reused`** — resolved without a model call. High is good
  and free; a suspiciously high number can mean the TM is being reused
  across a boundary it should not cross.
- **`escalated`** (`resolution == failure`) — the loop could not clear the
  findings within `max_iterations`. A large number means the gate and the
  repair agent disagree systematically, which is a calibration problem
  rather than a model-quality one.
- **`mtpe_policy`** — routed to a human by domain policy or low confidence.
  Expected, and sized at CONTEXT.
- **`tokens_spent`** — against `token_budget_per_batch`. Batches that trip
  the budget finalize early, so a high spend can silently mean less
  repair, not more.

## What NOT to do here

- Do not re-run PRODUCTION hoping for a better result. Re-entry is
  attempt-versioned (`s4/attempt-02/…`) precisely so a client bug report
  keeps pointing at what it pointed at — but a re-run without a
  configuration change is spend without a hypothesis.
- Do not treat a high escalation count as a model failure before checking
  whether a gate check is firing wrongly. The ratchet rolls back anything
  that does not strictly improve, so a miscalibrated check can make every
  repair look like a failure.
