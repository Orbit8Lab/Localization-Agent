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

**This document is ordered by build sequence, not by capability number.**
The four capabilities are numbered as originally stated (§1); every
section after §2 is a phase, in the order it should be built. Where a
phase implements one of the stated capabilities, its heading says which.

---

## 1. Target capabilities (as stated)

1. **Tool-building tools** — handle undefined requests by generating
   scripts/tools at runtime.
2. **SKILL.md as runtime capability** — load skill docs to orchestrate
   multi-tool tasks.
3. **Memory of successes and failures** — persist what worked and what did
   not.
4. **Self-evolving agent** — improve through testing and experimentation.

### What already exists

- **Capability 1, in miniature.** `codegen.py` + `sandbox.py` is a real
  tool-builder: the Adapter-Writer sees a 4KB sample, writes a converter,
  and it runs in a separate `python -I -S` process with an empty env,
  POSIX rlimits, discarded side effects, and stdout validated against a
  schema — then stored as a fingerprinted s1 artifact and frozen in
  behavior. **The pattern to generalize is `generate once → validate →
  freeze → attribute`, not the generation itself.** The hard problem is
  that a generated tool stays untrusted forever.
- **Nothing of capability 3.** `RunDB` stores per-string outcomes and
  `chat-traces/*.jsonl` stores session events, but nothing records *"this
  approach was tried and did not work."*

---

## 2. Build order: 3 → 1 → 2 → 4

Capability 4 is unreachable without capability 3: self-evolution needs a
training signal, and today every session starts naive. The observation
layer is also the only piece that carries no risk to the pipeline, and it
converts guessed constants (promotion thresholds, match rules) into
measured ones.

| Phase | Section | Capability | Risk |
|---|---|---|---|
| Observation layer | §3 | 3 (part 1) | none — logging only |
| Fix the sketch's bugs | §4 | 3 | none — nothing built on it yet |
| Design corrections | §5 | 3 | these are what make it safe |
| Memory that acts — retrieval, dual-track promotion, decay | §6 | 3 (part 2) | first behavior change; entry condition in §6.1 |
| Generalized tool building | §7 | 1 | contained by `sandbox.py` |
| SKILL.md at runtime | §8 | 2 | selects tools, cannot invent them |
| Self-evolution | §9 | 4 | gated on all of the above |

Two ordering notes that matter more than the table:

- **Within capability 3, the first concrete task is capturing a
  per-string human verdict at G3** (§3, §5.6). Everything downstream
  either trains on that signal or trains on our own scorer, and there is
  no later point at which retrofitting it is cheaper.
- **§4 and §5 are not optional preliminaries.** They are the difference
  between a memory that compounds and one that silently degrades every
  future job. Build them before anything reads from the store.

---

## 3. PHASE 1 — the observation layer (capability 3, no risk)

> **STATUS: BUILT.** `src/orbit8/observation.py`, wired into
> `graphs/translate.py::gate` and `controller.py::_absorb_flagged`,
> readable via `orbit8 observations <root> <job>`. 343 tests pass.
> What implementing it taught is recorded in §3.1 — including one live
> ratchet bug it exposed.

Append-only logging of every repair attempt and its outcome. **No
retrieval, no promotion, no prompt changes.** Do this one alone.

Per repair attempt, record:

- the defect signature (see §4.2 on getting this right)
- the strategy applied
- badness before and after (`_badness()` in `graphs/translate.py`)
- **whether the ratchet accepted or rolled back the candidate**
- iteration number, token cost, model fingerprint
- job id, attempt number, and store revision (§5.7 — versioned like the
  artifact it describes)
- **the eventual G3 verdict for the string, once known** — accepted /
  edited / rejected, plus the post-edited text when it differs

Two of these are load-bearing later and cheap only now. The **ratchet
verdict** and the **G3 verdict** are the inputs to the utility score that
ranks retrieval (§6.2) and to the boundary that constrains a skill
(§6.3). A log without them supports neither, and backfilling a human
verdict after the fact is impossible — the reviewer has moved on.

The G3 line is the one to build deliberately, because it does not
exist yet: `controller.py:_absorb_flagged` bulk-marks flagged rows
`accepted` at G3 approval, so there is currently no per-string human
verdict to log (§5.6). Capturing one is pure observation — it changes no
control flow — and without it every downstream learning decision is made
against our own scorer instead of against a reviewer.

Why first:

- it cannot regress anything — pure observation, no control-flow change;
- it produces the dataset that says whether skill-matching and a
  promotion threshold would actually have worked. The threshold of 3 in
  the current sketch is a guess; a month of this log makes it measurable;
- it captures **successes as well as failures** (scoped per §5.2), which
  the reflection design otherwise misses;
- it is the only way to find out whether `_badness()` and a human
  reviewer agree — the question §5.6 turns on.

### 3.1 What building it settled (and one bug it found)

**A live ratchet bug, unrelated to learning.** `translate()` wrote each
candidate twice — once under the bare uid via `setdefault`, once under
`__sample__{n}__`. The gate therefore scored one model call twice and, on
the second copy, compared it against itself: `_badness() < _badness()` is
false, so it was recorded as a rejection. Nothing downstream noticed
because the ratchet keeps the right text either way. But it means the
measured accept rate on first translations would have read ~0%, and the
duplicate scoring was real wasted work in non-dry runs. Fixed in
`translate()`; the observation layer is what made it visible at all,
which is the argument for §1-before-everything in miniature.

**Candidate ordering was load-bearing and undefended.** `gate` must score
the incumbent before any repair candidate, or the repair is ratcheted
against nothing and a *worse* repair wins. This held only by dict
insertion order. Now explicit, with a test that feeds the hostile order.

**`Finding` has no `rule_id`.** §4.2 proposes
`(bug_type, locked_term, rule_id)`, but the schema has no such field — the
only real rule id survives inside the message text as `[RULE-ID] …`
(`gate_checks.py:410`). The signature therefore derives from
`(bug_type, extracted rule_id, normalized evidence)`. Evidence is
normalized (digits masked, case/space folded) and **dropped entirely above
40 chars**, because long evidence is a quotation of one string and would
recreate the §4.2 failure it is meant to avoid. That 40 is a guess, and it
is now a guess the log itself can correct.

**The G3 verdict can be derived, not asked for.** No new operator step:
`_absorb_flagged` already sees the approved target, so a target that
differs from the last candidate the ratchet kept *is* an edit. This
required storing the candidate text on the observation — without it
accepted-vs-edited is unrecoverable. The derivation stays **silent** when
there is no observation to compare against, because this v0 gate approves
in bulk and "the operator did not touch it" is not evidence of agreement.
A fabricated `accepted` would look like human endorsement forever.

**Two vocabularies, one word.** The ratchet says `accepted` and so does
G3, and they mean opposite-facing things: our gate's opinion versus the
human's. Telling them apart is the entire point of §5.6, so they are
separate constant sets and the comparison is explicit.

**`uid` is not a per-locale key** — found in review, after the first
implementation shipped it wrong. `uid` hashes the SOURCE string, so it is
identical across every target locale, while the observation log is
job-scoped (one file, all locales). Keying the G3 verdict on `uid` alone
stamped one reviewer's ruling — and their translated text — onto every
other locale: an editor fixing the Japanese string marked the Korean row
`edited` and overwrote its `g3_text` with Japanese. In a 5-locale job that
fabricates 80% of the agreement data, in the one place §5.6 requires it be
real. `record_g3` now takes a mandatory `locale`; `for_uid` takes an
optional one. Worth recording *why* it slipped: every test fixture used a
single target locale, so the bug was structurally invisible to the suite
that was meant to cover it.

**The store revision is deliberately absent.** §5.7 asks for it, and
Phase 1 does not record one: nothing here reads the store, so the column
could only ever hold `0` — advertising an audit coordinate it does not
have. Phase 4 adds it together with the monotonic counter that gives it a
value, since that is when retrieved store content starts influencing
prompts (§5.3). A test pins the absence so it returns on purpose.

---

## 4. PHASE 2 — known bugs in the current sketch

Fix these before anything is built on the registry. None has a caller
yet, so all three are free to fix now and expensive to fix later.

### 4.1 The promotion counter never reaches the threshold

`promote()` only writes when `seen_count >= 3`, but `seen_count` derives
from `match()`, which reads the key `promote()` is refusing to write:

```
occurrence 1: prior=None → seen_count=1 → 1>=3 false → nothing written
occurrence 2: prior=None → seen_count=1 → nothing written
occurrence 3: prior=None → seen_count=1 → nothing written   ← forever
```

Nothing is ever promoted. The tally must live in its own namespace and
increment unconditionally; promotion reads *that*.

Every write below also carries the attempt coordinates §5.7 requires and
the tenant namespace §5.4 requires — the fix for one bug must not
reintroduce a different one:

```python
def observe(self, sig, fix_fn_ref, *, attempt, job_id, revision):
    """attempt/job_id/revision: the store write is versioned the same way
    the s4/s5 artifact that produced it is (§5.7). Namespace is
    per-tenant (§5.4) — a tally is evidence about ONE client's asset."""
    ns = (self.tenant_id, "skills", "tally")
    tally = self.store.get(ns, key=sig)
    n = (tally.value["count"] + 1) if tally else 1
    self.store.put(ns, key=sig, value={
        "count": n,
        "last_fix": fix_fn_ref,
        "revision": revision,          # monotonic store revision
        "observed_at": [*(tally.value["observed_at"] if tally else []),
                        {"job": job_id, "attempt": attempt}],
    })
    if n >= PROMOTION_THRESHOLD:
        self.request_promotion(sig, fix_fn_ref, tally_count=n,
                              revision=revision)   # §5.8 — not a put
```

`observed_at` is the audit trail: it says *which attempts of which jobs*
voted for this skill. Without it a count of 5 is unfalsifiable — you
cannot tell 5 distinct defects from one flapping repair loop retried 5
times, and those two warrant opposite decisions.

Also `promoted_at_count` is *read* as a running counter but *written* as
the count at promotion time — two meanings on one field.

### 4.2 The defect signature can never match across strings

`Finding.identity()` is `(key, bug_type, evidence)` and `key` is the
segment uid. Any signature derived from it is unique to one string, so the
registry can never accumulate. A skill signature must be
**string-independent** — e.g. `(bug_type, locked_term, rule_id)`.

This is the line between a **cache** (keyed by string) and a **skill**
(keyed by defect class). Define it explicitly before building on it.

### 4.3 The tally has no rejection channel

`observe()` as sketched counts *occurrences of a defect signature*, not
*successes of a fix*. A repair the ratchet rolled back still increments
the counter. Record the ratchet verdict on the observation and count only
accepted applications toward promotion; keep the rejections, because a
signature with 9 rejections and 3 accepts is the most interesting row in
the table.

Keeping them is the schema half. §6.3 is the design half: rejections are
the **negative track** that defines where a skill stops applying, so they
are not exhaust to be retained for debugging — they are half of what a
skill is.

---

## 5. PHASE 3 — design corrections

### 5.1 A skill-applied fix MUST pass through the gate

The sketch routes a matched skill around the repair loop straight to
`apply_known_fix`. But the ratchet in `graphs/translate.py` (`gate` node)
is what makes quality monotonic: a candidate replaces the incumbent only
when **strictly better**, so a repair that fixes one thing and breaks
another is rolled back.

Wire `apply_known_fix → gate`, never `→ finalize`. A promoted-but-wrong
skill that bypasses the ratchet degrades output **silently, repeatedly,
across every job** — strictly worse than the one-off LLM error it
replaced.

### 5.2 Reflection must record successes, not only defects

`lqa_reflection_node` iterates `confirmed_defects` only. Without success
data there is no way to tell whether a promoted skill *helped*, which is
precisely the signal capability 4 needs.

But "log clean strings too" does not scale: at production volume the
passing population is ~95% of every job, it is the least informative data
per row, and it would dominate storage while answering nothing. Log the
**decision boundary**, not the population — three targeted classes:

1. **Every skill application, always** — with its ratchet verdict. This
   is the only row that directly answers *did the skill help*, and there
   are at most as many as there are matches.
2. **Near-miss passes** — a string that passed but sat within a margin of
   a threshold (e.g. width ratio ≥ 0.9 × budget, or a term match that
   needed a `forms` variant to pass). These are where a threshold change
   would flip an outcome, so they are the rows that calibrate §3's
   guessed constants.
3. **A small fixed-rate sample of the clean population** (~1%, sampled
   deterministically from the uid hash so it is reproducible). Without it
   the first two classes are a biased sample and the base rate is
   unknown — you cannot compute a skill's lift against nothing.

Class 3 exists purely to make classes 1 and 2 interpretable. Sampling
deterministically rather than randomly keeps the log a function of the
job, which the fingerprint story (§5.3) needs.

### 5.3 Retrieved lessons break model fingerprints

`translator_context_hints` injects store content into the Translator
prompt. Model fingerprints (`model id + prompt hash`) exist for
"six-weeks-later attribution and benchmark reproducibility" — if the
prompt varies with accumulated store state, the hash no longer identifies
the prompt, and two runs with identical fingerprints can diverge.

**The store revision must become part of the fingerprint.**

### 5.4 The skill namespace leaks across tenants

Lessons are namespaced `(game_id, language_pair, domain)` — correct. But
`SkillRegistry` uses `("skills", "repair")`, **global across all tenants
and games**. A fix mined from one client's asset would apply to another's.
Given the existing `TenantMemory` separation, that is a policy break.
Namespace skills per tenant, with promotion to a shared layer as an
explicit, audited decision.

### 5.5 Reflection runs before the human gate

Stage 5 writes lessons *before* G3 review, so the store learns from
findings a human may be about to overturn. Either run reflection post-G3,
or mark lessons provisional until confirmed. Training on unreviewed labels
is how a systematic bias gets locked in.

### 5.6 The promotion signal is a proxy metric, and it is the wrong one

§5.5 applies to *promotion*, not just to lesson-writing, and there it is
worse. As sketched, the entire training signal is `_badness()`
before/after plus the ratchet's accept/reject
(`graphs/translate.py:48`, `:204`) — **both computed by the same
deterministic scorer the repair is trying to satisfy.** Nothing in the
loop consults whether a human at G3 thought the output was better.

That is a closed loop, and closed loops optimize the metric rather than
the goal. `_badness()` is a severity-weighted count of findings from our
own gate. A skill that reliably lowers it is *reliably satisfying our
checks* — which is not the same claim as *improving the translation*, and
diverges exactly where the checks are weakest. The failure mode is
specific and plausible: a fix that suppresses a MEDIUM `LENGTH` finding
by truncating a phrase scores strictly better and reads worse. Promote
that three times and the pipeline has learned to truncate.

So:

- **Badness improvement is a necessary filter, not a sufficient one.** It
  gates what is *eligible* for promotion. It must not be what promotes.
- **Promotion additionally requires a minimum G3 agreement rate.** For a
  signature to promote, the strings it was applied to must have survived
  G3 without being overturned — a floor on both the rate and the sample
  size (e.g. ≥N reviewed applications, ≥X% not overturned). Below the
  sample floor the signature stays a candidate indefinitely; that is the
  correct outcome, not a stalemate to engineer around.
- **Disagreement is the most valuable row in the log.** A signature where
  badness improved and G3 overturned it is direct evidence that a gate
  check is miscalibrated. Those rows should surface for review rather than
  being silently discarded as promotion failures.

**Blocker: G3 does not currently emit a usable per-string verdict.**
`controller.py:_absorb_flagged` bulk-marks every flagged row `accepted`
on approval (the documented v0 simplification), so today's G3 signal
cannot distinguish "the reviewer agreed" from "the reviewer approved the
gate". Capturing a real per-string verdict — accepted / edited /
rejected, and the post-edited text when it differs — is therefore a
**prerequisite of promotion, not a refinement of it**, and it belongs in
§3's observation work where it carries no risk. This is also the cheapest
item on this page with the highest leverage: it is the difference between
learning from humans and learning from ourselves.

### 5.7 Store writes are not attempt-versioned

S4/S5 artifacts are attempt-versioned so a defect loop never overwrites
what a client bug report points at. Store writes are not, so the store
state that produced `attempt-01` is unrecoverable. Stamp the store
revision into the artifact envelope.

### 5.8 RESOLVED: promotion is audited, not automatic

This was an open question; it is now a decision. **A promoted skill is
filed as an `AuditedFixRequest`-style artifact for human approval. It does
not take effect on write.**

The reasoning is the architecture's own, applied consistently. A promoted
skill is new latent capability that will make silent decisions on every
future job, across every string matching its signature. That is precisely
the class of change the rest of this system already refuses to let happen
ungated: six human gates G0–G5, a glossary frozen after G1 whose only
write path is an `AuditedFixRequest` re-opening that gate
(`schemas.py:232`, `glossary.py:6`), and a design principle that a
capability which must not be exercised should be *absent*, not
discouraged. Automatic promotion would make the learning subsystem the
one component permitted to change pipeline behavior without a human — the
component with the least track record.

Concretely, `request_promotion()` in §4.1 writes a
`SkillPromotionRequest` artifact — signature, proposed fix reference,
tally with its `observed_at` trail, badness deltas, G3 agreement rate and
sample size, the proposed applicability boundary (§6.3), and the tenant
it was mined from. Approval is an explicit operator action, recorded with
the operator's name the way `approve` already records it.

The same channel handles **demotion**, which is not the afterthought the
word suggests — see §6.5. The asymmetry: promotion and demotion are both
filed automatically and both take effect on human approval, because
automatically removing a working skill is its own outage. The one
exception is a skill whose recent G3 agreement collapses, which is
evidence of active harm and should stop firing first and be reviewed
after.

**Revisit condition, stated in advance so this is not permanent by
default:** once §3 has enough data to show that approvals are
consistently rubber-stamping the same pattern — a signature class where
reviewers approve at a high rate with no overturns over a meaningful
sample — that class is a candidate for auto-promotion under a standing
policy. Let the observation layer earn that, rather than assuming it.

---

## 6. PHASE 4 — memory that acts (capability 3 complete)

The first phase that changes pipeline behavior. Everything it needs was
established above, which is the point of the ordering: the log exists
(§3), the signature is string-independent (§4.2), the tally counts
accepted applications (§4.3), applied fixes route through the ratchet
(§5.1), and promotion is an artifact a human approves (§5.8).

Ship it as two independently reversible steps:

1. **Retrieval only** — matched skills surface as *hints* to the repair
   agent, never as an applied fix. Wrong hints cost tokens; they cannot
   corrupt output. This also measures match precision against real
   traffic before anything depends on it.
2. **Application** — matched skills apply, through the gate (§5.1), under
   audited promotion (§5.8).

Do not merge these steps. Step 1 is where a bad signature granularity
(§4.2) reveals itself harmlessly.

### 6.1 Entry condition — Phase 4 is a decision, not a given

§3 satisfies capability 3 as literally stated ("persist what worked and
what did not"). It is write-only, so it cannot regress anything, and it
is already useful: it is how guessed constants become measured ones. This
phase is where the risk starts. Two findings in the §3 log should stop
it:

- **Signatures are nearly all singletons.** Then §4.2 resolved against
  us: there is no defect *class* to learn, only a per-string cache. Build
  the cache if it pays for itself, but do not call it a skill.
- **`_badness()` and G3 reviewers disagree often** (§5.6). Then the
  training signal is bad, and the correct next work is fixing the gate
  checks — not building a promoter on top of a signal we know is wrong.

Both are cheap to learn here and expensive to learn after Phase 4 ships.

### 6.2 Retrieval: exact structured match first, value-aware fallback

§4.2 makes the signature a **categorical key** — `(bug_type,
locked_term, rule_id)` — not free text. So the primary retrieval path is
an **exact structured lookup on that key**. No embeddings, no similarity
threshold, no relevance ambiguity, and it is a dict lookup rather than a
vector search. Anything an embedding would add here is a way to be wrong
about a question that has an exact answer.

Semantic retrieval earns its place only on the harder question: *does an
existing skill loosely apply to a defect that matches no known
signature?* For that fallback, plain nearest-neighbor similarity is the
wrong ranker — it answers "which skill is most similar", when the
question is "which skill is most likely to help". Rank instead on
**relevance × estimated utility**, where utility comes from the §3 log:

- mean badness delta when this skill was applied
- ratchet accept rate (§4.3)
- G3 agreement rate (§5.6)
- sample count, as a confidence weight on all three

This has a property worth stating plainly, because it is the reason to
prefer it over similarity: **cold start is handled by construction.** A
brand-new skill with two observations is down-weighted even when it is
the semantically nearest match, and it climbs only as it earns evidence.
No separate warm-up rule, no hand-set minimum age — the confidence weight
already says "we do not know yet."

Keep the two paths distinguishable in the log. An exact-match hit and a
semantic-fallback hit have different expected precision, and mixing them
in one metric hides which mechanism is actually working.

### 6.3 Promotion is dual-track: the skill AND its boundary

The `observe()` counter in §4.1 tracks positive occurrences only. §4.3
says keep the rejections; this says what to *do* with them, which is the
part that changes the design rather than the schema.

A skill has two learnable halves:

- **What to do** — learned from applications the ratchet accepted and G3
  did not overturn.
- **Where it stops applying** — learned from cases that matched the same
  signature and needed a *different* fix.

A rejected application is therefore not a failed attempt to discard. It
is a **negative example that narrows the skill's applicability
boundary**: "this fix works for `rule_id=X`, except when the string is
also width-constrained (§5.6's truncation case is exactly this shape)."

Without the second track a skill generalizes precisely as far as its
first few successes happened to reach, and then misfires silently outside
that range. That is the over-generalization failure mode already flagged
in the open questions — and note that the negative track is what makes
the granularity question (§4.2) *learnable* rather than a constant to
guess. A signature too coarse shows up as a boundary with many negative
examples, which is a signal to split it.

Concretely, a promoted skill carries both: the fix reference, and the
conditions under which it must not fire. The boundary is a gate condition
on the match, not advice in a prompt (design §7).

### 6.4 Verification against held-out cases, not the cases that trained it

Promotion must not rest on the ratchet's badness improvement alone —
§5.6 established why: it is our own scorer grading its own homework. The
check that means something is a verifier applied to cases the skill was
*not* derived from.

This system already has the verifier: **G3**. It is human, it is
already in the lifecycle, and it sees strings the skill did not train on.
That is why §5.6 makes a per-string G3 verdict a prerequisite rather than
a refinement — it is the only held-out signal available that is not a
reflection of our own gate.

So the promotion surface is the `AuditedFixRequest`-style artifact from
§5.8, **pre-populated from the §3 log with both tracks**:

- positive: badness deltas, ratchet accepts, G3 agreements, sample size
- negative: rejections and near-misses, i.e. the proposed boundary

The point of showing both is that a reviewer approving a skill should see
**its boundary, not just its win count.** A win count invites a
rubber-stamp; a boundary invites the question "is that the right
limit?" — which is the only question a human is better at than the log.

### 6.5 Decay and demotion — promotion is not permanent

Missing from the plan until now, and it is the failure mode that arrives
latest and quietest. Skill libraries accumulate stale entries as the
content drifts: a new engine version changes the markup, a new genre
changes register, a glossary update invalidates a term-level fix. The
skill keeps matching and keeps firing, and nothing says it stopped being
right.

The mechanism is already paid for. Artifacts are attempt-versioned and
store writes are revision-stamped (§5.7), so utility is computable over a
window rather than a lifetime:

- Score each promoted skill on its **last N applications**, not its
  cumulative count. A lifetime counter is dominated by history and cannot
  fall; that is precisely why it is the wrong statistic here.
- Below a utility floor, demote to **candidate** — it stops firing, keeps
  its history, and can re-promote if it recovers.
- Also expire on **staleness**: a skill unmatched for a long window is
  not validated, merely unexercised. Flag it for re-audit rather than
  trusting it indefinitely.

Demotion follows the §5.8 asymmetry: filed automatically, effective on
human approval, because automatically removing a working skill is its own
outage. The exception worth carving out is a skill whose recent G3
agreement collapses — that is evidence of active harm, and it should stop
firing immediately and file for review after the fact.

A library that can only grow is a library that quietly rots.

---

## 7. PHASE 5 — generalized tool building (capability 1)

Generalize the `codegen.py`/`sandbox.py` contract, do not loosen it:

- separate process, empty env, rlimits (CPU/mem/fsize/nproc), hard timeout
- side effects discarded; only schema-validated stdout crosses back
- stored as a fingerprinted artifact, frozen in behavior, re-run
  deterministically
- for adversarial-grade isolation, add a container
  (`docker run --network none --read-only`) around the same entry point

A generated tool is untrusted *permanently*, not just on first run.

This lands after capability 3 for a practical reason as well as a
sequencing one: the §3 log is what tells you which undefined requests
actually recur, and therefore which generated tools are worth freezing
into artifacts rather than regenerating.

---

## 8. PHASE 6 — SKILL.md at runtime (capability 2)

Today `docs/skills/*.md` are **specifications a human implemented**, not
files the code loads. Every "skill" match in `src/orbit8/` is a comment
citing provenance; nothing parses them. E.g. the batch policy in
`docs/skills/lqa-batch-split.md` (story n=5, strings n=20) is transcribed
into `LQAConfig` / `graphs/lqa.py`.

Consequence: **editing a skill doc changes nothing at runtime, and the
drift is silent.** No test asserts the doc and the constants agree.

Cheap first step, independent of every other phase and safe to do at any
time: a test pinning `LQAConfig` batch sizes to the numbers stated in the
doc. Catches drift now, and is a prerequisite for trusting a loader
later.

For an actual loader, the §7 constraint of the design doc says: a loaded
doc may select and sequence **existing** tools; it must not be able to
invent capability. That is also why this phase follows Phase 5 — a loader is
far more useful once the tool set it sequences can grow, and far more
dangerous if it can grow it itself. The tool set stays the guarantee.

---

## 9. PHASE 7 — self-evolution (capability 4)

Only reachable once §3 is producing data and §4/§5 are fixed. Phase 4
supplies two of the three prerequisites, which is the clearest argument
that it belongs before this one:

- a **held-out evaluation set** — otherwise "improvement" is measured on
  the data that produced the change. §6.4 gets the first real version of
  this by treating G3 as the verifier; note the trap it avoids, which is
  §5.6 from the other side: a held-out set scored by `_badness()` is
  still self-evaluation. The held-out signal has to be human;
- **rollback** — §6.5 provides it: windowed utility, demotion to
  candidate, and the immediate stop for a collapsing agreement rate. A
  system that can only promote cannot experiment, because every
  experiment is permanent;
- **an experiment ledger** — the one piece Phase 4 does *not* supply, and
  the only genuinely new work here. §3 records what happened to strings;
  a ledger records what *we changed and why*: which skill, which
  threshold, which prompt, and what moved afterward. Capability 4 is an
  experiment loop; without a ledger it is a random walk.

What "evolution" then means concretely is narrower than the word
suggests, and worth stating so it does not drift: proposing threshold
changes, signature splits (§4.2), and skill boundary revisions (§6.3),
each as an audited artifact measured against the held-out signal. It
does not mean the agent rewriting its own gate checks. The gate is what
the experiments are measured against — a system that can move its own
measuring stick has no signal at all.

---

## Open questions

- What is the right defect-signature granularity (§4.2)? Too coarse
  over-applies a fix; too fine never accumulates. Measurable from §3
  data — and §6.3 makes it partly self-correcting, since a too-coarse
  signature reveals itself as a boundary thick with negative examples.
  The open part is what to do then: split the signature automatically, or
  file the split for review like any other promotion.
- What are the actual promotion thresholds — tally count, G3 agreement
  rate, minimum reviewed sample (§5.6), the retrieval utility floor
  (§6.2), the decay floor and window N (§6.5)? Deliberately unset: these
  are the constants §3 exists to measure, and picking them now would
  repeat the guessed-threshold-of-3 mistake this plan opens by
  criticizing.
- Does the semantic fallback (§6.2) pay for itself at all? It is the only
  component here that adds an embedding dependency, and the exact-match
  path may well cover enough of the traffic to make it unnecessary. The
  §3 log answers this directly: count the defects that matched no known
  signature but where a human fix resembled an existing skill. Build the
  fallback only if that set is both large and coherent.
- Display-width budgets (`gate_checks.width_budget`) are currently
  anchored on shipped-game corpora. Real in-game overflow data would make
  them ground truth; that is the same "held-out evaluation" problem as §9.
