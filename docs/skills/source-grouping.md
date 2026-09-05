# Skill — Source string grouping and difference detection

Status: v1 · 2026-09-05 · Implemented by `orbit8/templates.py` +
`orbit8/grouping.py` (this document is the source of truth; keep code in
sync).

## Why

Game source tables contain large numbers of near-identical entries. Some
are true duplicates that should collapse. Others look near-identical but
are meaningfully distinct, and collapsing them is an error.

A similarity score says two strings are 94% alike. It does not say whether
they differ by a number, a capital letter, a plural suffix or a glossary
term — and those four call for four different actions. **The
classification is the product.**

Source-side, deliberately: an inconsistency caught here is fixed once,
before it propagates into every target language. Tier 2 (`graphs/lqa.py`)
does the target-side counterpart after translation.

## D1 — Cluster on templates, not raw strings

    "+10 HP"  →  template "<NUM> HP"   slots ["+10"]
    "+20 HP"  →  template "<NUM> HP"   slots ["+20"]

One family (so they batch and translate consistently), two instances (so
neither is silently collapsed). This resolves numbers structurally instead
of by tuning a threshold — a threshold can only trade "group them" against
"keep them apart".

Slot types: `<NUM>`, `<VAR>`, `<TAG>`, `<URL>`, `<PATH>`. `VAR` and `TAG`
reuse the T1 gate's `PLACEHOLDER_PATTERNS` / `MARKUP_PATTERN` rather than
declaring their own — two inventories that drift would mean the templater
and the gate disagree about what a placeholder is, and the gate is the one
that blocks a release.

## D3 — Character n-grams as the only similarity measure

Language-agnostic: `攻撃力が上がった` and `Attack power increased` run
through identical code. No word segmentation, no morphological analyzer,
no per-language tokenizer — each of which is a dependency and a failure
mode per language, for worse near-duplicate detection.

n-grams are padded, because a 2-character CJK label has one unpadded
trigram and one gram cannot overlap with anything.

## D4 — No neural embeddings in the core path

Every distinction this tool must PRESERVE — numeric value, case, plural
suffix, single-token lexical swap — is a surface distinction, and sentence
encoders are trained to erase exactly those.

Measured on this repo's own cases with
`paraphrase-multilingual-MiniLM-L12-v2`: `恢复5点生命` / `恢复10点生命`
score **0.775** cosine against **1.000** character-bigram. The encoder is
not malfunctioning; it is doing its job, and its job is the wrong one
here. Encoders are also weakest on short strings, which is most game UI.

Removes torch, ONNX Runtime, tokenizer libraries and model weights from
the dependency graph entirely. Reconsider only if paraphrase grouping is
ever required (§Open questions), and then as a third pass over family
representatives — never as the primary path.

## Pipeline

| Stage | Operation | Where |
|---|---|---|
| 1 | Normalize — NFKC, whitespace collapse; case NOT folded | `normalize` |
| 2 | Bucket — Unicode script ranges | `script_of` |
| 3 | Templatize — typed slots + retained values | `templatize` |
| 4 | Family formation — folded-template hash, then n-gram near-merge | `build_families` |
| 5 | Intra-family diff — align, classify | `diff_family` |
| — | Evaluation — profile, stratified sample, per-class precision | `profile_corpus`, `sample_for_review`, `precision_by_class` |

**NFKC first.** Full-width and half-width forms are otherwise distinct
characters, silently fragmenting Japanese families — `ＨＰ` would never
meet `HP`.

**Normalization is recorded, never destructive.** Casing and spacing
differences are themselves findings; a pipeline that folded them away
could not report them. `Normalized.raw` recovers the original.

**Family keys fold case and punctuation.** Members must MEET to be
compared. Relying on near-merge for this does not work: `Start Game` /
`Start game` score 0.54 trigram Jaccard, and any threshold loose enough to
merge them also merges genuinely unrelated strings. Folding is safe
because the real text survives on the member, so the CASE difference is
still reported.

**Script is part of the family key.** An identical template across scripts
is a coincidence, not a family; merging would put zh and ja strings in one
batch.

## Difference taxonomy

| Class | Test | Example | Action |
|---|---|---|---|
| `EXACT_DUP` | identical post-normalization | — | collapse |
| `CASE` | equal under casefold | `Start Game` / `Start game` | standardize to majority |
| `FORMAT` | equal after stripping punctuation | `Are you sure?` / `Are you sure ?` | standardize |
| `VAR` | differing tokens are placeholders | `{0} gold` / `{1} gold` | verify index intent |
| `NUMERIC` | differing tokens are numeric | `+10 HP` / `+20 HP` | keep distinct, same family |
| `INFLECTION` | differing tokens share a long stem | `Card` / `Cards` | review |
| `LEXICAL` | differing tokens unrelated | `Sword` / `Blade` | glossary candidate |
| `AGREEMENT` | closed-class / agreement suffixes | `der` / `die` | review, source-language only |

Every diverging token pair must agree for a specific class to win; a mixed
divergence falls through to `LEXICAL`, the class that gets human eyes.
Failing toward MORE review is the rule the domain classifier follows
(design §8).

Tokens hold placeholders together before splitting on punctuation —
otherwise `{0}` tears into `0` and `{0} gold` / `{1} gold` reports as
NUMERIC, a different finding with a different action.

Alignment uses `difflib`, not a naive zip: an inserted word would
otherwise shift every later token and report the whole tail as different.

Inflection is a **common-prefix ratio** (> 0.7, divergence at the tail),
covering en/de/fr/es/it with zero language-specific code. Chinese and
Japanese do not inflect for number, so their exclusion is correct rather
than a gap.

## Scope decisions for this pipeline

- **AGREEMENT is out of scope by default.** The source here is zh or ja
  (`style_defaults.py` ships only zh-CN↔en; `intake_wizard.py` expects a
  Chinese or Japanese studio), and neither inflects for gender. The
  closed-class inventory is a caller-supplied parameter, never inferred —
  it only applies if the source is ever German or French.
- **Statistical LID is not used.** Unicode script ranges fully resolve the
  CJK/Latin split, which is the only split needed. LID would only separate
  languages *within* Latin script, and is unreliable below ~20 characters
  — most game UI text.
- **MinHash/LSH is not implemented.** Below ~50k strings brute force is
  exact and fast enough that LSH is complexity without benefit. Above
  `NEAR_MERGE_LIMIT`, near-merge is skipped loudly rather than silently
  taking minutes.
- **Glossary detection uses the existing termbase.** `Glossary` already
  has T1/T2/T3 layers, `locked_map()`, `forms` and provenance. The
  bootstrap path in the design doc overlaps `term_extract.py` and should
  be reconciled there rather than built twice.

## Evaluation — ship nothing on intuition

`profile_corpus` reports the length distribution FIRST. Several heuristics
degrade on very short input (the prefix ratio is meaningless on two
characters), so `under_5_chars` decides whether the defaults apply at all.

`sample_for_review` is stratified **by class**, because precision differs
per class: a tool 95% precise on CASE and 30% on LEXICAL needs different
thresholds per class, not one global cutoff. Rows come back with
`is_real_inconsistency: None` — nothing is trusted until labelled.
`precision_by_class` scores the labelled sample.

Sampling is seeded and deterministic, so a labelled sample stays
comparable across runs.

## Determinism

Batch layout rides on every `model_fingerprint`, so two runs of one job
must lay out identically. Family formation sorts by size then template,
merges against a SEED (not running membership, which would let "a≈b, b≈c,
a≉c" chain unrelated families), and picks canonical forms by majority with
a lexicographic tiebreak. No LLM decides grouping — that would put a spend
decision inside a prompt (design §7).

## Consumers

- `grouping.plan_similarity_batches` — LQA Tier 3 string batching, so
  members of a template family reach one reviewer call.
- Conversation batching (`grouping.plan_story_batches`) is orthogonal and
  unchanged: story text groups by scene, not by template.

## Not yet built

Stages 6–7 of the design doc — minority-deviation ranking, standardization
proposals, and the emitted review queue. `CLASS_WEIGHT` is in place for
the ranking score. These are deliberately deferred until per-class
precision is measured, because §12 says thresholds must not be set on
intuition and the weights are exactly such thresholds.
