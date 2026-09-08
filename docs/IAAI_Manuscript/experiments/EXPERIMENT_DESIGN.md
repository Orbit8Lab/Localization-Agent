# Experimental design as submitted (§4)

This maps the outline's §4 onto what was actually run, and — as
importantly for IAAI — states what was NOT run and why. A 6-page
Emerging Applications paper cannot carry five experiments credibly; it
can carry two well-grounded ones plus an honest scope statement.

## Run

### §4.1/§4.2 merged — the ablation ladder (Experiment 1)

The outline proposed a three-way system comparison (§4.1) and a separate
batching study (§4.2). These are merged into one ladder because the
conditions overlap almost entirely, and a merged ladder is *stronger*:
each rung adds exactly one mechanism, so an improvement is attributable
to that mechanism rather than to a bundle.

| | translation unit | grouping | glossary | style guide | maps to outline |
|---|---|---|---|---|---|
| B1 | sentence (n=1) | — | — | — | §4.1 B "LLM sentence-level" |
| B2 | batch n=20 | blind slice | — | — | §4.2 "fixed-size batches" |
| B3 | batch n=20 | blind slice | ✓ | — | — |
| B4 | batch n=20 | template / conversation | ✓ | ✓ | §4.1 C "agentic" |

**Conventional sentence-level MT (§4.1 A) is represented by the shipped
MT already in the corpus**, not by a fresh NMT run. This is a deliberate
substitution and the paper must say so: the `Target_MT` column of the
MTPE form is the output the studio actually reviewed and paid to fix, so
it is a true production baseline rather than a reconstruction. It is also
the only baseline whose defects a professional has already adjudicated.

### §4.5 — consistency (Experiment 1, `inconsistent_terms`)

Measured corpus-wide as one source term rendered two ways. This is the
metric that most directly tests the central hypothesis, because it is
*structurally* invisible to a per-sentence system: nothing in a
single-sentence call can know how the same term was rendered 300 lines
earlier.

### LQA accuracy (Experiment 2)

Not a numbered section in the outline, but it is the other half of the
user's question and the corpus supports it directly.

| | tiers | glossary | style guide |
|---|---|---|---|
| L1 | T1+T2 deterministic | — | — |
| L2 | T1+T2 | ✓ | — |
| L3 | T1+T2+T3 (n=20 semantic batches) | ✓ | — |
| L4 | T1+T2+T3 (n=20) | ✓ | ✓ |

## Not run, and why

**§4.3 full context ablation.** The outline asks to ablate story,
character, and scene context separately. This corpus cannot support it:
the 174 reviewed rows are UI/System/Skill/Item strings (73/65/27/10) with
essentially no long-form dialogue, and character profiles do not exist as
a project asset for project002. Running a "− character context" arm here
would produce a null result that says nothing about character context and
everything about the sample. Reported as a limitation (§7.3) with the
case study (§5) carrying the narrative argument qualitatively.

**§4.4 LLM selection.** Partially available from earlier measurement
(`deepseek-v4-pro` 18 findings/231s vs `deepseek-v4-flash` 11
findings/69.5s on 60 pairs; HuggingFace `Qwen3.8-27B` 504s at n=20 —
a gateway request-duration cap, not a model limitation). Enough for a
paragraph in §6, not enough for a section. Presented as an operational
note rather than a model comparison, because it is two models on one
corpus at one batch size.

**Human preference / post-editing effort.** The outline lists these under
§4.2 and §6.3. They require a fresh human study; the existing MTPE pass
is a single post-editor's verdicts, with no second rater, so
inter-annotator agreement is unmeasurable. The paper should claim
"agreement with one professional post-editor's shipped decisions" and
never "human preference," which implies a designed study.

## Threats to validity to state explicitly

1. **Single corpus, single language pair, single post-editor.** n=174
   with 57 positives. One string ≈ 1.8 points of recall. All tables carry
   Wilson intervals.
2. **Not a random sample.** These are the strings a human chose to review
   in one pass, skewed toward UI/System.
3. **The MT baseline is a fixed artifact.** It cannot be re-run under
   other settings, so B1–B4 compare against it rather than against a
   controlled NMT arm.
4. **Ground truth is one professional's judgment,** which is the right
   standard commercially (they decide what ships) but is not a consensus
   gold standard.
5. **CONFIRMED CIRCULARITY between the style guide and the ground
   truth — this must be stated in the paper, not buried.**

   The style rules table says so itself: its stated purpose is
   「记录 PE 过程中发现的『场景 → 格式规则』类规律」 — *to record the
   scene→format regularities discovered during post-editing*. Individual
   rules cite PE evidence ids (CAP-04 → `J-01`). The rules table was last
   written 2026-08-05, the MTPE form 2026-08-22.

   So condition B4/L4 supplies the model with rules distilled from the
   same human pass that produced the labels. A measured gain from the
   style guide therefore partly reflects **teaching to the test**, and
   the honest reading of B3→B4 and L3→L4 is:

   > *Given a project's conventions written down, can the workflow apply
   > them consistently at scale?*

   which is a real and useful engineering claim, and NOT:

   > *Does adding a style guide improve translation quality in general?*

   which this design cannot support. Two mitigations are available and
   both should be reported:

   - **Rule-level split.** 21 of 27 rules are marked `Confirmed` and
     6 `Proposed`. All were nonetheless authored in the same pass, so
     this does not fully break the loop.
   - **Cross-project transfer (the strong test) — SEARCHED FOR, NOT
     AVAILABLE.** The archive was checked for a corpus with independent
     human verdicts that the project002 guide never saw:

     | candidate | why it does not work |
     |---|---|
     | project003 SongOfKings | LQA recorded as in-game screenshots; the one machine-readable bug report is **en→zh**, the opposite direction |
     | project004 `双语对照表.xlsx` | 1,349 rows, but all 174 project002 ground-truth keys appear in it — it **is** the project002 corpus, misfiled; its `AI_ExpectedBug` labels are model-generated and the human `TEST_*` columns are 0/1349 filled |

     So no held-out human-labelled corpus exists today. The paper must
     therefore scope the style-guide claim to consistency-of-application
     (above) and name cross-project transfer as the specific next
     experiment — which is exactly the §8.2 transferability question,
     now with a concrete blocker: **there is no second labelled corpus,
     and producing one is a human-annotation task, not a compute task.**
