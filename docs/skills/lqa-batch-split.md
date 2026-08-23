# Skill — LQA batch split: story vs pure strings

Status: v1 · 2026-07-30 · Implemented by `orbit8/external_lqa.py` +
`graphs/lqa.py` (this document is the source of truth; keep code in sync).

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

| Class | Tier-3 batch size | Rationale |
|---|---|---|
| string | **n = 20** | short, independent items; large batches safe |
| story  | **n = 5**  | voice/continuity review needs a small window |

Tier 1/2 are deterministic and batch-independent; the policy applies to
Tier 3 (Critic) only. Verifier remains per-finding.

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
# defaults: --batch-string 20 --batch-story 5
```
