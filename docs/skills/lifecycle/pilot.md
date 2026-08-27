---
name: pilot
phase: PILOT
gate: G2
tools: [status, next_step, read_artifact, flagged, approve]
summary: a small run under maximum scrutiny — the cheapest place to find a systemic problem
---

# PILOT — critic on everything, before volume

The pilot exists to find *systemic* problems while they are still cheap.
It runs with `critic_mode="all"` and best-of-2 sampling, which is
deliberately more expensive per string than production and only affordable
because the sample is small.

Its purpose is not "check a few strings look fine." It is to answer: **is
anything wrong that will be wrong 40,000 times?**

## Sequence

1. `next_step` — runs the pilot for the locale.
2. `read_artifact run_summary.pilot.<locale>` — the shape of the run:
   accepted, escalated, mtpe_policy, tokens, iterations.
3. `flagged` — read the actual strings, not just the counts.
4. `read_artifact` the LQA report if one exists for the pilot.

## What to look for — systemic, not individual

| Signal | What it usually means |
|---|---|
| One term wrong the same way repeatedly | a glossary gap that G1 locked in |
| Consistent register mismatch | the style brief is wrong, not the model |
| Many LENGTH findings on one string type | the width budget for that widget class needs review |
| High `escalated` count | the repair loop cannot satisfy the gate — a check may be miscalibrated |
| Token spend near the budget on a small batch | production will trip the budget constantly |

A single awkward line is a translation opinion. The same defect four times
is a configuration problem, and this is the last stage where fixing it is
cheap.

## Before requesting G2

- [ ] The client has actually seen pilot output, in `client_lang`.
- [ ] No defect class recurs across strings unexplained. If one does, the
      fix belongs in the glossary or the style brief — which means
      re-opening G1, and that is the correct outcome, not a setback.
- [ ] The escalation rate is understood. It predicts the size of the human
      queue at G3.

## What NOT to do here

- Do not approve G2 because the individual strings read acceptably. The
  pilot's value is in the pattern, and the pattern is only visible if you
  look for repetition.
