---
name: flagged
phase: FLAGGED
gate: G3
tools: [status, next_step, flagged, read_artifact, list_artifacts, approve]
summary: work the MTPE queue, then approve G3 — which absorbs the queue irreversibly
---

# FLAGGED — the human review gate

The stage where a human decides what the machine got wrong. It is also the
only place the system collects a **held-out human signal**: approving G3
records, per string, whether the reviewer agreed with the pipeline's own
judgment. Every later learning decision is measured against that signal, so
a careless G3 does not just ship a bad string — it teaches the wrong lesson.

## What the queue distinguishes

Reasons are tagged distinctly on purpose (design §4). A translator
post-editing *by policy* needs different framing from one repairing a
string the system failed on four times:

| Reason | Means |
|---|---|
| `domain_policy` | routed to a human because its domain always is |
| `failure` | the repair loop could not clear the findings |
| `low_confidence` | the classifier was unsure, so it failed expensive |

## Sequence

1. `status` — confirm phase FLAGGED and that G3 is pending.
2. `next_step` — assembles the MTPE queue artifact if not yet built.
3. `flagged` — read the queue. Group by reason: `failure` items carry the
   findings that defeated the loop and deserve attention first.
4. `read_artifact` on the LQA report for the same locale when a finding
   needs context.
5. Post-edit the targets **before** approving. Import them so the run DB
   holds the corrected text.
6. `approve G3` — only once the queue has actually been worked.

## Before requesting G3

- [ ] Every `failure` item has been looked at. These are the strings the
      pipeline knows it could not fix.
- [ ] Post-edited text is imported, not pending in a spreadsheet.
      Approving first and importing later loses the accept-vs-edit
      distinction permanently.
- [ ] The reviewer is named in `approve --by`. The verdict is attributed.

## What approving G3 does — irreversibly

`approve G3` triggers `_absorb_flagged`, which:

1. writes every human-confirmed pair back to the TM as `origin=human`
   (these win all future lookups over machine pairs);
2. marks remaining flagged/mtpe rows `accepted`;
3. records the per-string G3 verdict into the observation log — `accepted`
   when the target is unchanged, `edited` when the human rewrote it.

Point 3 is why the ordering in the sequence matters. **A string approved
before its post-edit is imported is recorded as human-endorsed.** That is
a fabricated agreement, and it is indistinguishable downstream from a real
one.

## Signals worth reporting to the operator

- Queue size by reason, with `failure` called out separately.
- Any string whose findings were HIGH severity and remain unresolved.
- After approval: how many rows were recorded `edited` vs `accepted` — a
  high edit rate is evidence a gate check is miscalibrated, not just that
  the model had a bad day.
