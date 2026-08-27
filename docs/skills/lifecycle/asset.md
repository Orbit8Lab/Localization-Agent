---
name: asset
phase: ASSET
gate: G1
tools: [status, next_step, read_artifact, list_artifacts, extract_glossary, add_glossary_terms, approve]
summary: build the T1 glossary, resolve every conflict, and lock it at G1
---

# ASSET — build and lock the glossary

The highest-leverage stage in the job. Terminology is the one asset that
compounds: a decision made here is enforced mechanically on every string
in every later stage, and a decision *missed* here becomes a defect class
that recurs for the life of the project.

**After G1 the glossary is frozen.** This package has no write path to a
locked glossary — changes travel through an `AuditedFixRequest` that
re-opens G1. So the cost of locking too early is a re-opened gate, and the
cost of locking a wrong term is every string that uses it.

## Sequence

1. `status` — confirm the derived phase is ASSET and see which locales
   still need work.
2. `next_step` — runs the terminologist extraction (S3), producing
   `glossary_delta`.
3. `read_artifact glossary_delta` — read the proposals and, more
   importantly, the **conflicts**: variant clusters, polysemy, collisions.
4. For each ruling the dev team makes: `add_glossary_terms`.
   - A term is only law when `locked: true`. A mined or draft entry is the
     termbase's current best guess, and presenting it as mandatory makes
     the gate report "locked term violated" for a preference nobody
     ratified.
   - Verb/noun terms need `forms`; casing follows the `case` policy rather
     than the glossary entry's own capitalization.
5. `next_step` — glossary health check per locale.
6. `status` — health must report **zero blockers**.

## Before requesting G1

- [ ] Every conflict in `glossary_delta` has an explicit ruling. An
      unresolved variant cluster does not fail loudly later; it produces
      inconsistent output that reads like a translation-quality problem.
- [ ] Health blockers == 0. **The Controller refuses to open G1 while
      blockers exist** — if `status` still shows ASSET with a "fix glossary
      blockers" action, G1 is not available and `approve` will raise.
- [ ] Every `locked: true` term is one the dev team actually ratified.
- [ ] Terms needing morphology carry `forms`.

## What NOT to do here

- Do not lock a term to silence a health warning. The warning is cheaper
  than the frozen mistake.
- Do not add terms the source corpus does not contain. A glossary entry
  that never matches is dead weight the gate still checks on every string.
- Do not approve G1 to "unblock" the pipeline. G1 is the freeze; there is
  no cheap undo.

## Signals worth reporting to the operator

- Conflicts left unruled, by count and kind.
- Any term locked in this session, with who ruled it.
- Locales whose health check still shows blockers, and which check.
