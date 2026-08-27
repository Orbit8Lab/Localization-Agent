# Skill documents — the router

Playbooks for the chat orchestrator (`orbit8 chat`), loaded at runtime by
`src/orbit8/skill_docs.py` and injected into the agent's context based on
the stage the **Controller derives**, not on how the operator phrases a
request.

## The one rule these documents obey

From `localization-agent-langgraph-design.md` §7:

> a prompt instruction is a suggestion; a missing tool is a guarantee

A playbook may **select and sequence existing tools**. It cannot invent a
capability. Every doc declares its tools in frontmatter, the loader
validates each name against the orchestrator's live registry, and a doc
naming anything else **fails to load** — loudly, because a doc that
references a nonexistent tool teaches the agent to attempt the impossible
and then improvise.

## Routing

`job.derive()` returns the authoritative `(phase, gate)`; the loader looks
up the matching doc. Routing is therefore a lookup, and an agent cannot
talk its way into another stage's playbook.

| Derived phase | Gate | Playbook |
|---|---|---|
| INTAKE | G0 | [lifecycle/intake.md](lifecycle/intake.md) |
| INGEST | — | [lifecycle/ingest.md](lifecycle/ingest.md) |
| CONTEXT | — | [lifecycle/context.md](lifecycle/context.md) |
| ASSET | G1 | [lifecycle/asset.md](lifecycle/asset.md) |
| PILOT | G2 | [lifecycle/pilot.md](lifecycle/pilot.md) |
| PRODUCTION | — | [lifecycle/production.md](lifecycle/production.md) |
| LQA | — | [lifecycle/lqa.md](lifecycle/lqa.md) |
| FLAGGED | G3 | [lifecycle/flagged.md](lifecycle/flagged.md) |
| TESTING | G4 | [lifecycle/testing.md](lifecycle/testing.md) |
| RELEASE | G5 | [lifecycle/release.md](lifecycle/release.md) |
| INCREMENTAL | — | [operations/po-roundtrip.md](operations/po-roundtrip.md) |

Gates are folded into their phase's doc rather than split out: a gate is a
stop, and the useful content is *what to verify before requesting
approval* — which belongs next to the work that produced it.

## Operations — human-initiated flows

These are not lifecycle phases. A person starts them, usually on an
incremental drop that re-enters at S1.

- [operations/po-roundtrip.md](operations/po-roundtrip.md) — received
  `.po` → format audit → repair → delivery
- [operations/glossary-update.md](operations/glossary-update.md) —
  post-editing decisions → audited glossary change

## Policy specs (not playbooks)

[lqa-batch-split.md](lqa-batch-split.md) documents the Tier-3 batch policy
implemented in `LQAConfig` / `graphs/lqa.py`. It has no frontmatter and is
not loaded as a playbook — it is a spec the code implements, pinned against
drift by `tests/test_skill_docs.py`.

## Writing a new playbook

```markdown
---
name: asset
phase: ASSET
gate: G1
tools: [status, extract_glossary, add_glossary_terms, approve]
summary: build the T1 glossary and lock it at G1
---

## Sequence
1. `status` — confirm the derived phase
...

## Before requesting G1
- [ ] a condition that is checkable, not aspirational
```

`tools:` must list only real tool names. Run the suite after adding a
doc — a bad name fails the load test rather than surfacing mid-session.
