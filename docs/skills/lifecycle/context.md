---
name: context
phase: CONTEXT
tools: [status, next_step, read_artifact, analyze]
summary: style brief and domain classification — the classifier must fail expensive
---

# CONTEXT — style brief + domain classification

Two agents run map-parallel here: one derives the style brief (tone,
register, audience), the other labels each string's domain. Both feed
decisions that are hard to see later — the style brief shapes every prompt,
and the domain label decides which strings a human must post-edit.

## Sequence

1. `next_step` — runs S2.
2. `read_artifact style_brief` — check `confidence` and `sample_size`. A
   confident brief off a tiny sample is the failure mode worth catching.
3. `read_artifact domain_labels` — look at the **confidence
   distribution**, not just the labels.
4. `analyze` — corpus text stats (strings/words, story vs instruction) to
   sanity-check the brief against the actual corpus shape.

## The invariant that matters here

**The domain classifier fails expensive.** A low-confidence label routes
*to* the MTPE queue, never away from it, and such items are tagged
`low_confidence` distinctly from `domain_policy` and `failure`.

So a large low-confidence population is not a correctness problem — it is
a **cost** problem that will show up as a large human queue at G3. Report
it now, while the option to improve the classification still exists.

## What to check

- [ ] Style brief `confidence` is not "high" off a handful of strings.
- [ ] Register matches the genre. A werewolf social-deduction game and a
      cozy farming sim do not share a register, and the brief drives every
      later prompt.
- [ ] The low-confidence share is understood, because it is the size of
      the human bill.

## No gate here

CONTEXT flows into ASSET. The style brief is not frozen — but every
downstream prompt has already used it by the time anyone notices it was
wrong, so reading it now is cheaper than re-running production.
