# IAAI-27 experiments — batching and style-guide effects

Two experiments over one corpus with real human ground truth.

## Why this corpus

`project002` (绯月杀, zh→en) is the only corpus in the archive where a
professional post-editor left a machine-readable verdict on every string:
`10-received/20260803-MTPE/mtpe_form.xlsx` has 174 reviewed rows, each
marked `Accept Translation` or `Reject&Modification`, and every rejection
carries the editor's corrected target.

That single artifact grounds both experiments:

- **translation** — did a condition produce the text the editor signed
  off on, or would they still have had to edit it?
- **LQA** — did the scanner flag the 57 rows the editor rejected, and
  stay quiet on the 117 they accepted?

The alternative corpus, `project003`, records its LQA as in-game
screenshots — not machine-comparable — so it is used for the qualitative
case study in §5, not for these numbers.

The style guide is also real: `40-reference/style/绯月杀_风格规则表_StyleRules.xlsx`
holds 27 rules the post-editor wrote **during** this project, one per
regularity they found themselves fixing repeatedly. This matters for the
paper's argument — the rules are not authored for the experiment, they
are the by-product of the manual pass the workflow is trying to avoid.

## Ground truth caveat

174 rows is a real but modest sample: at 57 positives, one string is
~1.8 points of recall. Every table reports raw counts and a Wilson
interval so effect sizes are not over-read. The rows are also not a
random sample of the game — they are the strings a human chose to review
in the 2026-08-03 pass, which skews toward UI and System text (73 UI,
65 System, 36 Skill/Item) and away from long-form dialogue. Conclusions
about narrative coherence therefore rest on the §5 case study, not on
these counts.

## Running

```bash
.venv/bin/python docs/IAAI_Manuscript/experiments/scripts/build_dataset.py
.venv/bin/python docs/IAAI_Manuscript/experiments/scripts/build_style_guide.py
.venv/bin/python docs/IAAI_Manuscript/experiments/scripts/run_translation.py --tag main
.venv/bin/python docs/IAAI_Manuscript/experiments/scripts/run_lqa.py --tag main
.venv/bin/python docs/IAAI_Manuscript/experiments/scripts/analyze.py
```

Both runners write results after every condition, so a run that dies at
condition 4 keeps the three that already cost money.

## Conditions

Translation (each step adds exactly ONE mechanism, so a gain is attributable):

| | batch | grouping | glossary | style guide |
|---|---|---|---|---|
| B1 sentence | 1 | — | — | — |
| B2 batch | 20 | blind slice | — | — |
| B3 batch+gloss | 20 | blind slice | ✓ | — |
| B4 agentic | 20 | template / conversation | ✓ | ✓ |

LQA:

| | tiers | glossary | style guide |
|---|---|---|---|
| L1 deterministic | T1+T2 | — | — |
| L2 +glossary | T1+T2 | ✓ | — |
| L3 +LLM batched | T1+T2+T3 (n=20) | ✓ | — |
| L4 +style guide | T1+T2+T3 (n=20) | ✓ | ✓ |

## Metrics

Translation is scored as exact match against the editor's final target
after whitespace normalisation — **case is deliberately not folded**,
because casing IS the defect in 13 of the 27 client rules, so folding it
would score the very errors under study as successes.

`regressions` counts strings the editor had ACCEPTED that a condition
rewrote anyway. A condition can lift exact-match on rejected rows while
quietly churning correct ones, which costs review time rather than
saving it; without this column that trade is invisible.

LQA is scored as a detector (precision/recall/F1) with FP rate reported
alongside, since a scanner that flags everything has perfect recall and
no value.
