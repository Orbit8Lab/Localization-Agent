---
name: testing
phase: TESTING
gate: G4
tools: [status, next_step, read_artifact, approve]
summary: in-game test plan; the only stage that checks rendering rather than text
---

# TESTING — the only stage that sees the game

Every prior stage judged **text**. This one judges **rendering**, and they
fail differently: a string that is correct, terminologically perfect, and
passes every gate can still be wrong on screen — clipped by a widget,
wrapped mid-word, or breaking a line where the engine prints a literal
backslash.

The width budgets in the gate are anchored on shipped-game corpora, which
makes them a good prior and not ground truth. **This stage is where the
prior meets reality**, so its findings are unusually valuable: they are the
only in-game evidence the system ever receives.

## Sequence

1. `next_step` — generates the test plan for the locale.
2. `read_artifact test_plan.<locale>` — the prioritized surfaces and the
   strings each one exercises.
3. Testers execute the plan (outside this tool).
4. `approve G4` once results are in and understood.

## What the plan should prioritize

- Strings the width check flagged MEDIUM — the gate suspected overflow but
  could not prove it. These are exactly the cases where a human eye
  settles the question.
- The shortest UI labels. A one-glyph source expanding to a word is normal
  by ratio and still overflows a fixed button.
- Anything with a line-break separator. Per-entry conventions are not
  interchangeable, and a wrong one renders a visible backslash rather than
  a break.
- Strings whose targets were post-edited at G3, because they never went
  through the loop that validated the rest.

## Before requesting G4

- [ ] Testers actually ran the plan; an unexecuted plan approved is a
      rendering check that never happened.
- [ ] Overflow findings are recorded where they can inform the budgets —
      real in-game data is the only thing that turns the width priors into
      ground truth.
- [ ] Anything blocking is fixed and re-verified, not noted for later.

## What NOT to do here

- Do not approve G4 on the strength of the text-level gates. They already
  passed; that is why this stage exists.
