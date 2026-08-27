---
name: lqa
phase: LQA
tools: [status, next_step, read_artifact, list_artifacts, scan_po, flagged]
summary: tier cascade T1→T2→T3→verify; precision beats recall, because studios switch off tools that cry wolf
---

# LQA — the cost ladder

Tiers run in sequence, cheapest first, each on what survived the last:

| Tier | What | Cost |
|---|---|---|
| T1 | mechanical: placeholders, markup, locked terms, width | free |
| T2 | project-level consistency across the corpus | cheap |
| T3 | LLM semantic review of the remainder | expensive |
| verify | second-layer check over T3's findings | moderate |

The verifier exists because **precision beats recall by a wide margin
here**. A scan that reports 200 findings of which 60 are real gets switched
off by the studio, and then it catches nothing at all. A false positive
costs more than a miss.

## Sequence

1. `next_step` — runs the cascade for the locale.
2. `read_artifact lqa_report.<locale>` — counts by tier, severity, and
   bug type.
3. `scan_po` when auditing an external/developer translation rather than
   this pipeline's own output.

## Reading the report

- **By tier.** A finding count dominated by T1 is healthy — mechanical
  defects are the cheapest to find and fix. A count dominated by T3 means
  the mechanical layers are not catching what they should, and T3 is doing
  expensive work that a regex could have done.
- **By bug type.** One type dominating is a systemic signal: many
  `TERMINOLOGY` findings point at a glossary gap; many `LENGTH` at a width
  budget that does not match the widget class.
- **Verifier rejections.** How many T3 findings the verifier threw out. A
  high rejection rate means T3 is over-reporting, and the *tier* needs
  tuning rather than the translations.

## What NOT to do here

- Do not file every finding as a client-facing bug. That is the FLAGGED
  stage's job, and the queue is assembled deliberately.
- Do not lower a threshold to reduce the finding count. The number of
  findings is not the metric; the number of *real* findings is.

## No gate here

LQA flows into FLAGGED, where a human works the queue and G3 is pending.
