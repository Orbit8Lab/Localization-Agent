# Skill — LQA batch split: story vs pure strings

Status: v2 · 2026-09-05 · Implemented by `orbit8/grouping.py` +
`graphs/lqa.py` + `graphs/translate.py` (this document is the source of
truth; keep code in sync).

## Why

Semantic LQA (Tier 3) quality depends on how much attention each string
gets. UI/UX strings are short, pattern-like, and near-independent — a
reviewer holds 20 of them without degradation. Story text needs the
opposite: register, character voice, and continuity judgments demand a
small window. One batch size fits neither.

## Classification: story vs pure string

Every bilingual pair is classified by CONTENT (keys are often opaque GUIDs
— UE exports — so key-prefix rules from `graphs/context.py` do not apply;
use the LLM Domain Classifier, `docs/agents` contract `classify`).

| Class | Domains | Criteria | Examples |
|---|---|---|---|
| **story** | `dialogue`, `marketing` | narrative or persuasive prose: character speech, journal/lore entries, tutorial narration with voice, store copy | "Guided by the soul of his grandfather…", journal entries |
| **string** | `ui`, `system`, `map`, `item_desc` | labels, buttons, settings, system messages, names, stat lines; typically ≤ 1 sentence, imperative or nominal | "APPLY SETTINGS", "Press any key", "Anti-Aliasing" |

Edge rules:
- Multi-sentence hint text with narrative voice → **story**; terse
  mechanical hints → **string**.
- When the classifier is uncertain, prefer **story** (the smaller batch —
  fail expensive, consistent with design §8).

## Batch policy

Size is only half the policy. WHICH strings share a call decides what the
reviewer can see, and the two classes fail in opposite ways — so they are
grouped on different axes.

| Class | Tier-3 batch size | Grouping axis | Rationale |
|---|---|---|---|
| string | **n = 20** | **similarity** | short, independent items; the failure mode is INCONSISTENCY between near-identical sources, which is invisible unless both are in the same call |
| story  | **n = 5**  | **conversation** | voice/continuity judgments need a small window AND a whole exchange; five lines from three scenes cannot be judged for continuity |

Tier 1/2 are deterministic and batch-independent; the policy applies to
Tier 3 (Critic) only. Verifier remains per-finding.

### Axis 1 — conversation (story)

`grouping.plan_story_batches`. `group_id`/`seq` are derived from the game
key's own structure (`dlg.ch01.scene03.007` → group `dlg.ch01.scene03`,
seq 7), across `.`/`/`/`:`/`-`/`_` separators. A conversation is emitted
whole and in sequence order; short scenes pack together; a scene longer
than the window splits into CONTIGUOUS stretches rather than an arbitrary
sample. Opaque keys (GUID-keyed UE exports — see above) derive nothing and
fall back to the previous contiguous slice.

The same planner runs in Stage 4: story batches translate by conversation
too, with the prompt stating that the items are consecutive lines of one
exchange (`agents.translate_batch(conversation=True)`) — but only when the
batch really is one group and more than one line survived TM reuse.

### Axis 2 — template family (pure strings)

`grouping.plan_similarity_batches`, over
[`source-grouping.md`](source-grouping.md). Sources are grouped by
TEMPLATE — `恢复5点生命` and `恢复10点生命` share `恢复<NUM>点生命`, so they
reach one call with no threshold involved, and stay distinct instances
rather than collapsing. Families are then packed up to `n`: most strings
belong to a family of one, and a call per singleton would cost more than
the blind slice it replaced.

This replaced a raw-text similarity threshold. A threshold could only
trade "group them" against "keep them apart"; a template gives both. The
remaining threshold applies only to the second pass that merges
near-identical TEMPLATES.

Deterministic on purpose: batch layout rides on every model fingerprint,
so two runs of one job must lay out identically. An LLM deciding grouping
would also put a spend decision inside a prompt (design §7).

## Contract

Input: bilingual JSONL (`key, source_language, target_language,
source_text, target_text`).

Outputs (attempt-versioned under `s5/attempt-NN/`):
1. `split_story.<name>.jsonl` — story pairs (n=5 batches)
2. `split_strings.<name>.jsonl` — pure-string pairs (n=20 batches)
3. domain labels persisted in the audit run DB (`runs/lqa-<name>.db`)
4. `lqa_report.<name>.json` — the tier-cascade report over BOTH files

The two split files are the review artifacts a human can re-run or
spot-check independently; the report unifies findings.

## Invocation

```bash
orbit8 lqa run <root> <job> --pairs <bilingual.jsonl> --name dev-audit
# defaults: --batch-string 20 --batch-story 5, similarity threshold 0.6
```
