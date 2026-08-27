---
name: glossary-update
tools: [status, read_artifact, extract_glossary, add_glossary_terms, update_glossary, scan_po]
summary: turn post-editing decisions into locked terminology — the flywheel that compounds
---

# Operation — glossary update from post-editing decisions

Terminology is the one asset that compounds. A human decision enters the
ledger once, and from then on the **deterministic gate** enforces it on
every string — which is also how AI-introduced defects get *collected*
rather than argued about.

This flow runs after post-editing, converting what a reviewer decided into
something the gate can enforce.

## Sequence

1. `read_artifact` the PE decisions / bug report that carries the rulings.
2. `extract_glossary` — mine candidate terms from the corrected pairs.
3. Separate the two kinds of finding, because they take different fixes:
   - **wrong word** → a glossary entry fixes it
   - **wrong casing** → a `CAP-*` style rule fixes it
   Conflating them produces a glossary full of case variants that the gate
   then reports as violations of each other.
4. `add_glossary_terms` for rulings that are genuinely ratified.
5. `update_glossary` to apply the merged result.
6. `scan_po` to confirm the new terms actually fire on the corpus.

## Locking is the decision, not the default

- Only `locked: true` is law. A mined or draft entry is a best guess, and
  presenting it as mandatory makes the gate report "locked term violated"
  for a preference nobody ratified — which is how *correct* strings end up
  on a client bug report.
- A verb/noun term needs `forms`; without them the gate matches one surface
  form and flags every legitimate inflection.
- Casing follows the `case` policy, not the entry's own capitalization.

## After G1 the glossary is frozen

If the job has passed G1, this operation **cannot** write to the locked
glossary. Changes travel through an `AuditedFixRequest` artifact that
re-opens G1 — that is the only write path, and it is deliberate: a
terminology change silently applied mid-job would rewrite the standard that
earlier strings were judged against.

## Before adding a term

- [ ] The corpus actually contains it. An entry that never matches is dead
      weight the gate checks on every string forever.
- [ ] It is a term, not a phrasing preference. Preferences belong in the
      style brief, where they guide without failing the gate.
- [ ] Its `distinct_from` is set if a near-neighbor exists — polysemy
      collisions are the defect this field prevents.
