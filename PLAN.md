# PLAN.md — next version: toward a self-evolving agent

Direction for the version *after* the current freeze. The current release
is a deterministic pipeline: the Job Controller owns the loop, agents are
stateless prompt functions, artifacts are authoritative. Everything below
adds *learning* without surrendering any of that.

The governing constraint, from `localization-agent-langgraph-design.md` §7:

> a prompt instruction is a suggestion; a missing tool is a guarantee

Every item here adds interpretive capability, which is exactly what that
principle excludes. So none of it may be *trusted* — it must be
**contained**, the way `sandbox.py` contains generated adapters today.

---

## 0. Target capabilities (as stated)

1. **Tool-building tools** — handle undefined requests by generating
   scripts/tools at runtime.
2. **SKILL.md as runtime capability** — load skill docs to orchestrate
   multi-tool tasks.
3. **Memory of successes and failures** — persist what worked and what did
   not.
4. **Self-evolving agent** — improve through testing and experimentation.

### What already exists

- **Item 1, in miniature.** `codegen.py` + `sandbox.py` is a real
  tool-builder: the Adapter-Writer sees a 4KB sample, writes a converter,
  and it runs in a separate `python -I -S` process with an empty env,
  POSIX rlimits, discarded side effects, and stdout validated against a
  schema — then stored as a fingerprinted s1 artifact and frozen in
  behavior. **The pattern to generalize is `generate once → validate →
  freeze → attribute`, not the generation itself.** The hard problem is
  that a generated tool stays untrusted forever.
- **Nothing of item 3.** `RunDB` stores per-string outcomes and
  `chat-traces/*.jsonl` stores session events, but nothing records *"this
  approach was tried and did not work."*

### Recommended order

**3 → 1 → 2 → 4.** Item 4 is unreachable without item 3: self-evolution
needs a training signal, and today every session starts naive. Build the
observation layer first (§1 below) — it is the only piece that carries no
risk to the pipeline, and it converts guessed constants (promotion
thresholds, match rules) into measured ones.

---

## 1. FIRST: the observation layer (no risk, do this alone)

Append-only logging of every repair attempt and its outcome. **No
retrieval, no promotion, no prompt changes.**

Per repair attempt, record:

- the defect signature (see §2 on getting this right)
- the strategy applied
- badness before and after (`_badness()` in `graphs/translate.py`)
- **whether the ratchet accepted or rolled back the candidate**
- iteration number, token cost, model fingerprint

Why first:

- it cannot regress anything — pure observation, no control-flow change;
- it produces the dataset that says whether skill-matching and a
  promotion threshold would actually have worked. The threshold of 3 in
  the current sketch is a guess; a month of this log makes it measurable;
- it captures **successes as well as failures**, which the reflection
  design below otherwise misses.

---

## 2. Known bugs in the current sketch — fix before building

### 2.1 The promotion counter never reaches the threshold

`promote()` only writes when `seen_count >= 3`, but `seen_count` derives
from `match()`, which reads the key `promote()` is refusing to write:

```
occurrence 1: prior=None → seen_count=1 → 1>=3 false → nothing written
occurrence 2: prior=None → seen_count=1 → nothing written
occurrence 3: prior=None → seen_count=1 → nothing written   ← forever
```

Nothing is ever promoted. The tally must live in its own namespace and
increment unconditionally; promotion reads *that*:

```python
def observe(self, sig, fix_fn_ref):
    tally = self.store.get(("skills", "tally"), key=sig)
    n = (tally.value["count"] + 1) if tally else 1
    self.store.put(("skills", "tally"), key=sig,
                   value={"count": n, "last_fix": fix_fn_ref})
    if n >= PROMOTION_THRESHOLD:
        self.store.put(self.namespace, key=sig,
                       value={"fix_fn_ref": fix_fn_ref, "promoted_at": n})
```

Also `promoted_at_count` is *read* as a running counter but *written* as
the count at promotion time — two meanings on one field.

### 2.2 The defect signature can never match across strings

`Finding.identity()` is `(key, bug_type, evidence)` and `key` is the
segment uid. Any signature derived from it is unique to one string, so the
registry can never accumulate. A skill signature must be
**string-independent** — e.g. `(bug_type, locked_term, rule_id)`.

This is the line between a **cache** (keyed by string) and a **skill**
(keyed by defect class). Define it explicitly before building on it.

---

## 3. Design corrections

### 3.1 A skill-applied fix MUST pass through the gate

The sketch routes a matched skill around the repair loop straight to
`apply_known_fix`. But the ratchet in `graphs/translate.py` (`gate` node)
is what makes quality monotonic: a candidate replaces the incumbent only
when **strictly better**, so a repair that fixes one thing and breaks
another is rolled back.

Wire `apply_known_fix → gate`, never `→ finalize`. A promoted-but-wrong
skill that bypasses the ratchet degrades output **silently, repeatedly,
across every job** — strictly worse than the one-off LLM error it
replaced.

### 3.2 Reflection must record successes, not only defects

`lqa_reflection_node` iterates `confirmed_defects` only. Without success
data there is no way to tell whether a promoted skill *helped*, which is
precisely the signal item 4 needs. Log clean strings too, or at minimum
log every skill application with its outcome.

### 3.3 Retrieved lessons break model fingerprints

`translator_context_hints` injects store content into the Translator
prompt. Model fingerprints (`model id + prompt hash`) exist for
"six-weeks-later attribution and benchmark reproducibility" — if the
prompt varies with accumulated store state, the hash no longer identifies
the prompt, and two runs with identical fingerprints can diverge.

**The store revision must become part of the fingerprint.**

### 3.4 The skill namespace leaks across tenants

Lessons are namespaced `(game_id, language_pair, domain)` — correct. But
`SkillRegistry` uses `("skills", "repair")`, **global across all tenants
and games**. A fix mined from one client's asset would apply to another's.
Given the existing `TenantMemory` separation, that is a policy break.
Namespace skills per tenant, with promotion to a shared layer as an
explicit, audited decision.

### 3.5 Reflection runs before the human gate

Stage 5 writes lessons *before* G3 review, so the store learns from
findings a human may be about to overturn. Either run reflection post-G3,
or mark lessons provisional until confirmed. Training on unreviewed labels
is how a systematic bias gets locked in.

### 3.6 Store writes are not attempt-versioned

S4/S5 artifacts are attempt-versioned so a defect loop never overwrites
what a client bug report points at. Store writes are not, so the store
state that produced `attempt-01` is unrecoverable. Stamp the store
revision into the artifact envelope.

---

## 4. Item 2 — SKILL.md at runtime

Today `docs/skills/*.md` are **specifications a human implemented**, not
files the code loads. Every "skill" match in `src/orbit8/` is a comment
citing provenance; nothing parses them. E.g. the batch policy in
`docs/skills/lqa-batch-split.md` (story n=5, strings n=20) is transcribed
into `LQAConfig` / `graphs/lqa.py`.

Consequence: **editing a skill doc changes nothing at runtime, and the
drift is silent.** No test asserts the doc and the constants agree.

Cheap first step, independent of everything else: a test pinning
`LQAConfig` batch sizes to the numbers stated in the doc. Catches drift
now, and is a prerequisite for trusting a loader later.

For an actual loader, the §7 constraint says: a loaded doc may select and
sequence **existing** tools; it must not be able to invent capability. The
tool set stays the guarantee.

---

## 5. Item 1 — generalized tool building

Generalize the `codegen.py`/`sandbox.py` contract, do not loosen it:

- separate process, empty env, rlimits (CPU/mem/fsize/nproc), hard timeout
- side effects discarded; only schema-validated stdout crosses back
- stored as a fingerprinted artifact, frozen in behavior, re-run
  deterministically
- for adversarial-grade isolation, add a container
  (`docker run --network none --read-only`) around the same entry point

A generated tool is untrusted *permanently*, not just on first run.

---

## 6. Item 4 — self-evolution, and what would make it real

Only reachable once §1 is producing data and §2/§3 are fixed. The
prerequisites:

- a **held-out evaluation set** — otherwise "improvement" is measured on
  the data that produced the change;
- **rollback** — a promoted skill that degrades the ratchet must be
  demotable, which needs the success/failure record from §1;
- **an experiment ledger** — what was tried, what changed, what happened.
  Item 4 is an experiment loop; without a ledger it is a random walk.

---

## Open questions

- What is the right defect-signature granularity (§2.2)? Too coarse
  over-applies a fix; too fine never accumulates. Measurable from §1 data.
- Should promotion be automatic at all, or should it file an
  `AuditedFixRequest`-style artifact for human approval — matching how
  post-G1 glossary changes already work?
- Display-width budgets (`gate_checks.width_budget`) are currently
  anchored on shipped-game corpora. Real in-game overflow data would make
  them ground truth; that is the same "held-out evaluation" problem as §6.
