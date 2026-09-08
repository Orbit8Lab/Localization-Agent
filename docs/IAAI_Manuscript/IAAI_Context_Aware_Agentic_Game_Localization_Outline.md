# Next-Generation Multilingual Creative Translation: A Context-Aware Agentic Workflow

## Draft Abstract

Machine translation has traditionally treated the sentence or segment as the fundamental unit of translation. While this paradigm is effective for many forms of general translation, it creates important challenges for multilingual creative content such as video games, where translation decisions depend on narrative context, character identity, relationships, terminology, style, and events distributed across multiple lines and scenes. Large language models (LLMs) provide an opportunity to move beyond sentence-level translation through their ability to incorporate long-range context and richer instructions, but simply applying an LLM to individual segments does not fully exploit this capability. The challenge is to design a workflow that determines what content should be translated together, what contextual information should be provided, and how terminology and style constraints should be integrated into production-scale localization.

We present a context-aware agentic workflow for multilingual creative translation that dynamically organizes game content into translation batches and combines long-context information, narrative and character context, glossaries, and style-guide instructions during translation. The workflow integrates translation with downstream consistency validation, linguistic quality assurance, and post-editing, providing an end-to-end approach to creative localization. We evaluate the approach through controlled experiments and real-world game localization case studies, comparing conventional sentence-level machine translation, sentence-level LLM translation, and the proposed agentic workflow. We investigate the effects of batching strategy, contextual information, LLM selection, and localization constraints on translation quality, consistency, human post-editing effort, and operational cost. Our results aim to establish contextual translation as a practical next-generation machine translation paradigm for creative content, in which the translation unit is determined by narrative and semantic context rather than by isolated sentences. We further identify self-improving agent capabilities as a future direction for learning reusable workflow and skill decisions from human feedback while separating generalizable knowledge from customer-specific information.

---

## IAAI-27 Submission Requirements and Positioning

### Recommended Track

**Primary target: IAAI-27 — Emerging Applications of AI**

This paper is currently best aligned with the Emerging Applications track because the central contribution is an applied AI workflow for a real-world localization problem, demonstrated through early deployment/pilot-stage results and a clear path toward broader production deployment.

The official IAAI-27 CFP states that the Emerging Applications track is intended for real-world AI applications that are not yet fully deployed but have early deployment or pilot-stage results. Papers should address the practical problem, AI methodology, design rationale, technical quality, and a clear path toward full-scale deployment.

**Page limit: 6 pages**, using AAAI style and formatting. References and appendices are not included in the page limit. [Official IAAI-27 CFP](https://aaai.org/conference/aaai/aaai-27/iaai-27-call/)

### Alternative Track

**IAAI-27 — Deployed Applications**

Consider this track only if the system itself can be supported as being **in production and used by end users with meaningful performance/impact data**.

**Page limit: 8 pages**, using AAAI style and formatting. References and appendices are not included in the page limit.

The CFP emphasizes measurable benefits, deployment experience, design rationale, failures and redesign, and lessons learned from real-world use. Co-authors from the deploying organization are recommended.

### Deployment Insights

IAAI-27 also has a Deployment Insights track:

- **Full Paper:** 6 pages
- **Experience Report:** 2–4 pages

The Deployment Insights full-paper format is intended for deployed tools, practices, evaluations, and lessons learned that directly improve how applied AI systems are built and operated. Experience Reports are short practitioner accounts focused on deployment experience, failures/remediation, or operational practice.

For the current paper, these are secondary options because the main story is the **translation paradigm and agentic workflow**, rather than deployment practice alone.

### Current IAAI-27 Timeline

All deadlines are **11:59 PM Anywhere on Earth (UTC−12)**.

- **July 28, 2026:** IAAI-27 OpenReview submission site opens
- **September 8, 2026:** Paper submission deadline
- **October 30, 2026:** Notification of decisions
- **November 24, 2026:** Final decisions on conditionally accepted papers
- **December 14, 2026:** Camera-ready deadline
- **February 18–20, 2027:** IAAI-27, Montréal, Canada

The currently published official deadline is **September 8, 2026**, not September 5.

### What IAAI Looks For

The official CFP emphasizes applied AI and asks papers to clearly address:

1. **What real-world problem was explored, and why is it difficult or important?**
2. **What AI approach was used, and why were particular design decisions made?**
3. **What impact did the application have, and how was that impact measured?**
4. **What happened during development/deployment?**
5. **What went wrong, what was redesigned, and what was learned?**
6. **What lessons can other practitioners use?**

The paper should therefore prioritize:

**Real-world problem → design decision → deployed workflow → observed limitation → proposed solution → empirical evidence → practical lessons**

rather than presenting the work primarily as an abstract LLM/agent architecture.

### Positioning for This Paper

The central research claim should be:

> **Conventional machine translation treats the sentence as the fundamental translation unit. Creative translation requires a larger, contextual unit of meaning. A context-aware agentic LLM workflow can dynamically construct that unit and integrate narrative, character, terminology, and style context, providing a practical next-generation approach to multilingual creative translation.**

The paper should avoid framing the contribution as simply “LLMs translate better than NLP.” Instead, the comparison should be:

**Conventional sentence-level MT**

vs.

**LLM sentence-level translation**

vs.

**Context-aware agentic translation**

This isolates the contribution of the **workflow, context selection, and batching strategy**, rather than attributing all gains to using a stronger model.

### Self-Evolving Agent Position

Self-evolution should remain a **future direction**, not the main contribution of this paper.

The future direction is:

**Human feedback → evaluation → skill/workflow improvement → validation**

with particular attention to separating:

- Generalizable localization knowledge
- Customer/project-specific information

This preserves the original self-evolving research direction while keeping the present paper focused on contextual creative translation.

---

## IAAI Paper — Preliminary Outline

> **Current paper direction:** The primary focus is no longer the self-evolving agent. The main contribution is a context-aware, agentic LLM translation workflow for creative game localization. The self-evolving mechanism is positioned as a future extension for learning from human feedback and customer-specific workflow design.

## Core Story

### Old framing
**Existing game localization agent → human feedback → self-evolving agent**

### New framing
**Sentence-level MT has structural limitations for creative game localization → LLMs enable context-aware translation → agentic orchestration makes contextual translation operational at scale → batching + long context + style/story/glossary information improve creative localization → experiments demonstrate the advantages → self-improving agents are a future extension.**

### Central research question

> How can an agentic LLM translation workflow exploit context, batching, narrative structure, and localization-specific knowledge to produce more coherent creative translations than conventional sentence-level MT pipelines?

### One-sentence story

> We show that agentic LLM-based game localization can move beyond sentence-level translation by dynamically batching related content and integrating narrative, character, terminology, and style context, enabling the system to translate a game as a coherent creative work rather than as a collection of independent lines.

---

# 1. Introduction (~0.75–1 page)

## 1.1 The Problem: Creative Translation Is Not Sentence Translation

Game localization is a complex, iterative process that extends beyond machine translation. A production workflow involves translation, linguistic quality assurance (LQA), post-editing, validation, and interaction with game-specific localization tools and file formats.

Traditional machine translation pipelines are highly optimized around **sentence-level translation**:

**source sentence → translation → post-editing → glossary/style enforcement**

This works well when an individual sentence contains sufficient information to determine its appropriate translation.

However, creative game localization is fundamentally contextual. The appropriate translation of a line may depend on:

- Who is speaking
- Who is being addressed
- Character relationships
- Previous events
- Narrative arc
- Character personality
- Game terminology
- Register and tone
- Style guide requirements
- How related lines were translated elsewhere

Therefore, two linguistically similar sentences may require different translations, while two lines separated by hundreds of segments may need to be translated consistently.

## 1.2 From Sentence-Level MT to Context-Aware LLM Translation

LLMs introduce a fundamentally different opportunity.

Instead of treating each sentence as an independent translation unit, an LLM-based system can process larger contextual units and reason over relationships among multiple lines.

The important capability is not simply a larger context window. It is the ability to use context to make translation decisions:

**sentence → surrounding dialogue → scene → character → storyline → style guide → terminology**

This enables globally informed translation decisions while maintaining consistency across related content.

## 1.3 Why an Agent Is Needed

Simply putting an entire game script into an LLM prompt does not solve the problem.

A practical localization system needs to determine:

- What should be translated together?
- How large should a batch be?
- Which lines require shared context?
- Which terminology should be reinforced?
- Which style-guide information should be provided?
- How should previous translation decisions influence later decisions?
- How should long documents be processed within model/context constraints?
- How should outputs be validated?

Therefore, the key contribution is not simply an LLM. It is an **agentic translation workflow that orchestrates context, batching, terminology, style information, and iterative validation.**

## 1.4 Our Approach

We present an agentic game localization system designed specifically for **creative, context-dependent translation**.

The system organizes game content into appropriate translation units rather than translating every sentence independently. It combines:

- Context-aware batching
- Long-context processing
- Narrative and character information
- Glossaries and terminology
- Style-guide information
- Consistency checks
- LQA
- Post-editing

The workflow aims to preserve the storyline and creative intent of the original game while maintaining terminology and stylistic consistency across large volumes of localized content.

## 1.5 Contributions

1. **A context-aware agentic translation workflow for game localization** that moves beyond sentence-level translation by dynamically organizing related content into appropriate translation batches.

2. **A unified contextual translation framework** that integrates long-context information, terminology, character/story context, and style-guide constraints into creative translation and downstream LQA/post-editing.

3. **Empirical evaluation on real game localization workflows** demonstrating the effects of context length, batching strategy, LLM selection, and localization constraints on translation quality, consistency, and operational efficiency.

> Self-improving behavior can be introduced as a future extension in which human feedback and customer-specific localization decisions are used to refine the agent's skills and workflow configuration.

---

# 2. Background and Related Work (~0.75 page)

Keep this section compact. Avoid turning it into a long literature review.

## 2.1 Conventional Machine Translation for Localization

Discuss the traditional localization pipeline:

**MT → glossary enforcement → post-editing → LQA**

The key limitation is not that traditional MT is inherently poor. Rather, the fundamental translation unit is usually the **sentence/segment**.

This creates challenges for creative content where translation decisions depend on information distributed across multiple segments.

Relevant topics:

- Segment-level MT
- Translation memory
- Glossaries
- Terminology constraints
- Post-editing
- Context-aware MT

## 2.2 LLM-Based Creative Translation

Introduce LLMs and their ability to incorporate:

- Long context
- Narrative context
- Character information
- Style instructions
- Examples
- Terminology
- Semantic relationships

Core argument:

> LLMs allow localization to move from **segment translation** toward **contextual generation**.

Cite:

- Our previous two papers
- Relevant LLM localization literature
- Relevant recent AMTA publications
- Context-aware translation work
- Creative translation research

## 2.3 Agentic Translation Systems

Explain why agentic orchestration is necessary.

An LLM provides model capability; an **agentic system operationalizes that capability** through:

**planning → batching → context retrieval → translation → validation → LQA → post-editing**

The research question is therefore not:

> “Can an LLM translate better?”

but:

> **How should an agentic workflow organize and control an LLM to exploit contextual translation for real-world game localization?**

---

# 3. Agentic Architecture for Context-Aware Game Localization (~1.5 pages)

This should be the main methodology section.

## 3.1 Conventional Localization Workflow

Show the traditional pipeline:

**Game text → sentence-level MT → glossary check → post-editing → LQA**

Explain the limitations with examples where a line's appropriate translation depends on earlier/later context.

Example structure:

> Character A says something in Scene 1.  
> Hundreds of lines later, Character A uses a related phrase.  
> A sentence-level system may not know that these should share a translation strategy.

## 3.2 Context-Aware Translation Unit

Introduce the key concept:

### Translation Batch

Instead of:

**Line 1 → Translate**  
**Line 2 → Translate**  
**Line 3 → Translate**

the system performs:

**Scene / dialogue block / semantic group → contextual translation**

The agent determines an appropriate batch based on factors such as:

- Scene boundaries
- Character
- Dialogue relationship
- Semantic similarity
- Narrative context
- File structure
- Token/context constraints

### Figure 1

Compare:

**Traditional / sentence-level workflow**

versus

**Agentic context-aware workflow**

Visually emphasize how the translation unit changes.

## 3.3 Long-Context Integration

Organize context into layers:

### Local context
- Nearby dialogue

### Scene context
- Current scene
- Current story event

### Character context
- Character profile
- Relationship
- Speech pattern

### Global context
- Terminology
- Story background
- Previous translation decisions

### Style context
- Style guide
- Customer preferences
- Localization conventions

Important point:

> Not all context should be included every time. The agent should determine what context is relevant.

This makes the system genuinely agentic rather than simply “long-context prompting.”

## 3.4 Style Guide and Glossary Integration

Instead of treating glossary enforcement as only a post-processing step, incorporate localization knowledge before and during generation.

Potential conceptual flow:

**Terminology + character voice + style guide + narrative context → translation**

This distinguishes the approach from conventional MT pipelines that often enforce terminology after generation.

## 3.5 LQA and Post-Editing

Retain the existing LQA/post-editing components, but reposition them.

They are no longer the primary research contribution. Instead, they are downstream components that support production-level quality.

Full workflow:

**Context selection → Batching → Translation → Consistency validation → LQA → Post-editing**

---

# 4. Experimental Evaluation (~2 pages)

This should be the main empirical section.

## 4.1 Baseline Comparison

Compare:

### A. Conventional workflow
**Sentence-level MT → glossary → post-editing**

### B. LLM sentence-level translation
**LLM → sentence-by-sentence**

### C. Agentic contextual translation
**Agent → batching → long context → style/glossary → LLM**

Purpose:

> Determine whether improvements come from simply using a stronger LLM or from the proposed context-aware agentic workflow.

This three-way comparison is important.

## 4.2 Batching Strategy Experiment

Compare:

- Sentence-level batching
- Fixed-size batches
- Scene-based batches
- Agentically selected batches

Evaluate:

- Translation quality
- Consistency
- Character voice
- Narrative coherence
- Terminology consistency
- Human preference

Central question:

> **Which batching strategy best supports creative localization?**

## 4.3 Context Ablation

Compare full context against removal of individual components:

### Full context
Story + character + scene + glossary + style guide

### Ablations
- − Story context
- − Character context
- − Style guide
- − Glossary
- − Long-range context

Question:

> **Which contextual information actually contributes to creative localization quality?**

## 4.4 LLM Selection

Evaluate different LLMs on:

- Translation quality
- Context utilization
- Consistency
- Instruction following
- Token consumption
- Cost
- Latency

Question:

> Which model characteristics are most important for contextual creative translation?

## 4.5 Consistency Evaluation

Strongly consider adding this experiment.

Identify repeated or related content:

- Same character
- Same terminology
- Same phrase
- Same narrative concept

Compare whether systems translate related content consistently.

Potential metrics:

- Terminology consistency
- Repeated-phrase consistency
- Character voice consistency
- Human preference

This directly supports the central hypothesis.

---

# 5. Real-World Game Localization Case Studies (~1 page)

## 5.1 LQA Case

Demonstrate a real localization example where sentence-level translation creates a problem that contextual translation resolves.

Potential presentation:

**Original context → conventional MT output → agentic output → human assessment**

Focus on:

- Character voice
- Contextual meaning
- Terminology
- Narrative coherence

## 5.2 Creative Localization Case

Show a larger narrative segment where the system uses:

- Long context
- Multiple dialogue lines
- Character information
- Style guide
- Glossary
- Previous translation decisions

Then show the final localized output.

Key message:

> **The system translates the story rather than merely translating the lines.**

---

# 6. Operational Analysis

This section should support the IAAI applied-AI/deployment framing.

## 6.1 Token Consumption

Long-context translation has additional cost.

Question:

> When does additional context provide enough quality benefit to justify its token cost?

Report:

- Input tokens
- Output tokens
- Number of calls
- Total cost
- Quality improvement

## 6.2 Batching Efficiency

Compare:

- Number of model calls
- Total tokens
- Processing time
- Cost
- Translation quality

## 6.3 Human Intervention

Potential measures:

- Post-editing rate
- LQA correction rate
- Human acceptance rate
- Number of human interventions

This turns the contextual-translation argument into a real deployment story.

---

# 7. Discussion

## 7.1 From Translation Units to Contextual Units

Traditional MT:

> **sentence → translation**

Agentic LLM localization:

> **contextual unit → interpretation → translation**

Potential central conceptual contribution:

> The agent changes not only the translation model but also the **unit of translation**.

## 7.2 From Rules to Instructions

Traditional localization systems rely heavily on:

- Glossaries
- Rules
- Post-editing
- Translation memory

LLM-based systems can additionally encode:

- Character personality
- Narrative intent
- Tone
- Style
- Relationships
- Creative constraints

Therefore, localization knowledge can move from **hard rules and post-processing** toward **contextual instructions and generation-time guidance**.

## 7.3 Limitations

Discuss:

- Long-context cost
- Hallucination
- Context overload
- Inconsistent behavior
- Model dependence
- Need for human evaluation
- Difficulty defining optimal batch boundaries
- Potential degradation when too much irrelevant context is included

---

# 8. Future Work: Self-Improving Localization Agents

The self-evolving concept becomes a future direction rather than the main contribution.

Potential evolution targets:

- Optimal batching strategies
- Context selection
- Style-guide interpretation
- Skill selection
- Customer-specific workflow configuration
- LQA rules
- Tool selection

Future research question:

> If the agent repeatedly observes human decisions about good/bad translations, successful/unsuccessful batches, and accepted/rejected skills, can it learn how to improve its own localization workflow?

## 8.1 Customer-Specific Information

Separate:

### Generalizable Agent Knowledge
- Skills
- Workflows
- Validation logic
- Reusable Python functions
- General LQA rules

from:

### Ephemeral Customer Context
- Game-specific terminology
- Customer files
- Project-specific scripts
- Proprietary localization data
- Customer-specific instructions

The future self-evolving system should learn **generalizable localization skills** without retaining sensitive customer-specific information.

## 8.2 Transferability

Investigate whether skills learned from one game or localization environment can transfer to another.

Goal:

> **Learn from one environment → extract generalizable behavior → transfer to new localization environments.**

## 8.3 Stable Self-Evolution

Balance:

**Adaptability ↔ Stability**

The system should improve without:

- Accumulating bad skills
- Overfitting to one customer
- Introducing regressions
- Becoming unpredictable

Potential evolution loop:

**Evaluation → Selection → Integration → Validation → Stable Deployment**

---

# 9. Conclusion

Return to the fundamental idea:

> **Creative game localization is not fundamentally a sentence-by-sentence translation problem.**

The contribution of LLM-based agentic systems is therefore not merely better translation quality from a larger model.

It is the ability to:

- Redefine the unit of translation
- Dynamically assemble relevant context
- Incorporate narrative and stylistic information
- Maintain consistency across a creative work
- Operationalize contextual translation at production scale

The long-term direction is to make the agent capable of learning how to make these contextual and workflow decisions from human feedback.

---

# Recommended Figures and Tables

## Figure 1 — Conventional vs. Agentic Context-Aware Localization

**Left:** Sentence-level MT workflow  
**Right:** Context-aware agentic workflow

Emphasize the change in translation unit.

## Figure 2 — Context Assembly

Show:

**Game content → Agent → Relevant context selection → Translation batch → LLM → Validation**

with context sources:

- Scene
- Character
- Story
- Glossary
- Style guide
- Previous translation

## Figure 3 — Future Self-Improving Loop

**Human feedback → Evaluation → Skill modification → Skill selection → Agent → Execution → Feedback**

Keep this figure optional or place it in Future Work.

## Table 1 — System Comparison

| Capability | Sentence-level MT | LLM Sentence Translation | Context-Aware Agent |
|---|---|---|---|
| Sentence translation | ✓ | ✓ | ✓ |
| Long context | Limited | ✓ | ✓ |
| Dynamic batching | No | No/limited | ✓ |
| Narrative context | Limited | ✓ | ✓ |
| Character context | Limited | ✓ | ✓ |
| Style guide integration | Rule-based | Prompt-based | Agent-managed |
| Glossary | ✓ | ✓ | ✓ |
| Cross-line consistency | Limited | Moderate | Target capability |
| LQA integration | External | External | Integrated |
| Post-editing | External | External | Integrated |

## Table 2 — Experimental Results

Potential dimensions:

- Translation quality
- Human preference
- Narrative coherence
- Terminology consistency
- Character voice
- LQA error rate
- Token consumption
- Cost
- Latency
- Human post-editing effort

---

# Writing Strategy for IAAI

The paper should consistently emphasize:

**Real-world problem → design decision → deployed workflow → observed limitation → proposed solution → evidence → lessons learned**

Avoid framing it primarily as:

**New LLM architecture → benchmark → score improvement**

The strongest conceptual distinction is:

> **Conventional MT translates segments. Our agent translates contextual units.**

And the strongest practical distinction is:

> **The system does not simply give an LLM a larger context window; it actively determines what should be translated together and what information should be provided to the model.**

The self-evolving agent should be presented as the **next stage of this architecture**, particularly for learning customer-specific workflow decisions while preserving transferable, generalizable localization skills.
