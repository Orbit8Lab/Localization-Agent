"""The LLM agents — stateless prompt functions, one call each.

Ported from localization-pipeline `locpipe/agents.py`; the skill documents in
localization-pipeline/docs/agents/ are the source of truth for each contract.
Orbit8 changes: every function returns ``(payload, model_fingerprint)`` so
the Controller can stamp attribution into the artifact envelope, and the
Translator takes a domain label for domain-aware prompt emphasis (S2 → S4).

Boundary rules (design §4, §7):
- The Critic reports findings; it NEVER decides whether to loop.
- Agents return typed objects; they never write artifacts or touch disk.
- The Critic never sees the Translator's reasoning (isolation rule).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .glossary import render_brief
from .llm import Provider, complete_json, model_fingerprint
from .schemas import (BatchTranslation, Domain, DomainLabels, Finding,
                      GlossaryBrief, GlossaryDelta, IntakeBrief, MarketingKit,
                      MarketReport, Review, StyleBrief, TestPlan, Verdict)

PRESERVE_RULES = (
    "**PRESERVE VERBATIM:** placeholders ({0}, {name}, %s, $VAR$, [Tag]) and "
    "markup (<color=...>...</color>, <GeneralFont_12>...</>) must appear in "
    "the translation unchanged, same count, untranslated."
)

# Domain-aware prompt emphasis (LIFECYCLE stage 2: labels change behavior).
DOMAIN_EMPHASIS = {
    Domain.UI: "UI strings: keep them SHORT (screen space is fixed), use the "
               "locale's imperative UI conventions, no trailing punctuation "
               "unless the source has it.",
    Domain.DIALOGUE: "Dialogue: preserve character voice and register; "
                     "natural spoken rhythm beats literal fidelity.",
    Domain.MAP: "Map/location names: evocative but concise; keep any "
                "established real-world or in-game naming conventions.",
    Domain.ITEM_DESC: "Item/skill descriptions: precision on stats, numbers "
                      "and placeholders; terminology consistency is critical.",
    Domain.SYSTEM: "System messages: clear, neutral, unambiguous; follow the "
                   "locale's standard system-message phrasing.",
    Domain.MARKETING: "Marketing copy: persuasive and idiomatic; adapt "
                      "rather than translate, but never invent claims.",
}

_ITEM_SEP = "### "


def _render_batch(items: List[Tuple[str, str]]) -> str:
    # "### END" terminates the block list so item boundaries are unambiguous
    # (both for models and for the dry-run EchoProvider's parser).
    return ("\n".join(f"{_ITEM_SEP}{key}\n{text}" for key, text in items)
            + f"\n{_ITEM_SEP}END")


def _json_shape(keys: List[str]) -> str:
    return (
        "Respond with ONLY this JSON object (no prose, no code fences):\n"
        '{"items": [{"key": "<key>", "target_text": "<translation>", '
        '"term_decisions": {"<source term>": "<rendering you used>"}, '
        '"notes": null}]}\n'
        f"items MUST contain exactly these keys, once each: {', '.join(keys)}")


def _style_section(brief: Optional[StyleBrief], locale: str) -> str:
    if brief is None:
        return ""
    lines = [f"**STYLE:** genre {'/'.join(brief.genre) or 'unknown'}; "
             f"tone {brief.tone}; register {brief.register}; "
             f"audience {brief.audience}."]
    for rule in brief.do:
        lines.append(f"- do: {rule}")
    for rule in brief.dont:
        lines.append(f"- don't: {rule}")
    note = brief.per_locale_notes.get(locale, "")
    if note:
        lines.append(f"- {locale}: {note}")
    return "\n".join(lines)


def _check_coverage(result: BatchTranslation, expected: List[str]) -> None:
    got = [item.key for item in result.items]
    if sorted(got) != sorted(expected):
        raise RuntimeError(
            f"batch coverage violation: expected keys {expected}, got {got}")


# -------------------------------------------------------------- translator

def translate_batch(provider: Provider, items: List[Tuple[str, str]], *,
                    source_lang: str, target_lang: str, game: str,
                    domain: Optional[Domain] = None,
                    glossary_brief: Optional[GlossaryBrief] = None,
                    style_brief: Optional[StyleBrief] = None,
                    tm_examples: Optional[List[Tuple[str, str]]] = None,
                    temperature: float = 0.3,
                    style_guide=None
                    ) -> Tuple[BatchTranslation, str]:
    """docs/agents/translator.md — domain-aware (design LIFECYCLE S2→S4).
    ``style_guide`` supplies the per-language-pair rules for this
    ``domain`` (STANDARDS §2.2)."""
    system = (
        f"You are a professional game-localization translator for {game} "
        f"({source_lang} → {target_lang}). Follow the glossary EXACTLY — it "
        "is locked by human review and overrides your judgment. Use its "
        "sense notes, English anchors, and distinct-from warnings to pick "
        "the correct rendering when a term is polysemous. Preserve all "
        "placeholders and markup verbatim. Output ONLY the JSON object "
        "requested, no prose, no code fences.")
    sections = [_style_section(style_brief, target_lang)]
    if style_guide is not None:
        sections.append(style_guide.render_prompt(
            domain.value if hasattr(domain, "value") else domain))
    if domain:
        sections.append(f"**DOMAIN:** {DOMAIN_EMPHASIS[domain]}")
    if glossary_brief:
        sections.append(render_brief(glossary_brief))
    if tm_examples:
        sections.append(
            "**EXISTING TRANSLATIONS (match their style/terms):**\n"
            + "\n".join(f'- "{s}" → "{t}"' for s, t in tm_examples[:3]))
    sections += [PRESERVE_RULES,
                 f"Translate each item into {target_lang}:",
                 _render_batch(items),
                 _json_shape([k for k, _ in items])]
    user = "\n\n".join(s for s in sections if s)
    result = complete_json(provider, system, user, BatchTranslation,
                           temperature=temperature,
                           max_tokens=200 * len(items) + 500)
    _check_coverage(result, [k for k, _ in items])
    return result, model_fingerprint(provider, system)


# ------------------------------------------------------------------ repair

def repair_batch(provider: Provider,
                 flagged: List[Tuple[str, str, str, List[Finding]]], *,
                 source_lang: str, target_lang: str, game: str,
                 glossary_brief: Optional[GlossaryBrief] = None,
                 style_brief: Optional[StyleBrief] = None,
                 style_guide=None,
                 domain: Optional[str] = None
                 ) -> Tuple[BatchTranslation, str]:
    """docs/agents/repair.md — flagged: [(key, source, previous, findings)].
    Consumes findings, produces a new candidate. The route node — never this
    agent — decides whether the loop continues.

    This agent also writes the SUGGESTED TRANSLATION shipped in client bug
    reports and PE forms, so its output must satisfy the same glossary and
    style standard as a delivery: a suggestion that violates the rules is
    a defect we authored ourselves.
    """
    system = (
        f"You are a professional game-localization translator for {game} "
        f"({source_lang} → {target_lang}) fixing specific reported defects. "
        "Change ONLY what the findings require; keep every other word of "
        "the previous translation. Your output is delivered to the client "
        "as the SUGGESTED TRANSLATION, so it must itself satisfy every "
        "glossary term and style rule below — fixing one defect while "
        "breaking another rule is a failed repair. Output ONLY "
        "the JSON object requested, no prose, no code fences.")
    blocks = []
    for key, source, previous, findings in flagged:
        lines = [f"{_ITEM_SEP}{key}", f"source: {source}",
                 f"previous: {previous}", "findings:"]
        for f in findings:
            lines.append(
                f"- [{f.bug_type.value}/{f.severity.value}] {f.message}"
                + (f" | evidence: {f.evidence}" if f.evidence else "")
                + (f" | suggested_fix: {f.suggested_fix}" if f.suggested_fix else ""))
        blocks.append("\n".join(lines))
    sections = []
    if glossary_brief:
        sections.append(render_brief(glossary_brief))
    sections.append(_style_section(style_brief, target_lang))
    if style_guide is not None:
        # the repair output IS a deliverable: it must obey the same
        # rules the reviewer judges by (STANDARDS §2.2)
        sections.append(style_guide.render_prompt(domain))
    sections += [PRESERVE_RULES,
                 "Fix each item:", "\n\n".join(blocks),
                 _json_shape([key for key, *_ in flagged])]
    user = "\n\n".join(s for s in sections if s)
    result = complete_json(provider, system, user, BatchTranslation,
                           temperature=0.1,
                           max_tokens=200 * len(flagged) + 500)
    _check_coverage(result, [key for key, *_ in flagged])
    return result, model_fingerprint(provider, system)


# ------------------------------------------------------------------ critic

CRITIC_TAXONOMY = (
    "placeholder: placeholder/markup broken · terminology: locked term "
    "violated (including right form but wrong SENSE per the glossary's "
    "sense notes) · untranslated: source left as-is · leakage: source-script "
    "characters remain · mistranslation: meaning wrong · omission: content "
    "dropped · grammar: target grammar broken · register: tone/formality "
    "wrong for context · length: UI overflow risk · punctuation: locale "
    "punctuation wrong · consistency: same source rendered differently "
    "across the asset")


def _client_lang_directive(client_lang: Optional[str]) -> str:
    if not client_lang:
        return ""
    return (f"**REPORT LANGUAGE:** write all free-text fields (message, "
            f"reasoning) in {client_lang} — the client team reads them "
            "directly. Structural fields (key, bug_type, severity, decision) "
            "keep their defined values.")


def review_batch(provider: Provider, items: List[Tuple[str, str, str]], *,
                 source_lang: str, target_lang: str, game: str,
                 known_findings: Optional[List[Finding]] = None,
                 glossary_brief: Optional[GlossaryBrief] = None,
                 style_brief: Optional[StyleBrief] = None,
                 client_lang: Optional[str] = None,
                 style_guide=None,
                 domain: Optional[str] = None) -> Tuple[Review, str]:
    """docs/agents/critic.md — items: [(key, source, target)]. Sees only
    source/target/constraints, never the Translator's reasoning. Produces
    findings and severities; does NOT decide whether to loop.
    ``style_guide`` supplies the rubric rules for this language pair and
    ``domain``; findings must cite the rule id."""
    system = (
        f"You are an independent game-localization QA reviewer "
        f"({source_lang} → {target_lang}). Report only defects you can "
        "quote evidence for. Judge terminology against the glossary brief "
        "INCLUDING its sense notes — the locked form used in the wrong "
        "sense is still a terminology defect. If a translation is correct, "
        "return an empty findings list — finding nothing wrong is a "
        "successful review. Do NOT invent problems. Output ONLY the JSON "
        "object requested.")
    sections = [f"**BUG TAXONOMY:** {CRITIC_TAXONOMY}",
                _client_lang_directive(client_lang)]
    if glossary_brief:
        sections.append(render_brief(glossary_brief))
    sections.append(_style_section(style_brief, target_lang))
    if style_guide is not None:
        # render_prompt owns the format + citation instruction
        # (STANDARDS §2.2); here we only say what BUG TYPE to file.
        rubric = style_guide.render_prompt(domain)
        if rubric:
            sections.append(
                rubric + "\nA style violation is a defect: file it with "
                "bug_type 'register' (or 'grammar' when the rule is "
                "grammatical).")
    if known_findings:
        sections.append(
            "**ALREADY KNOWN (do not re-report):**\n" + "\n".join(
                f"- {f.key}: [{f.bug_type.value}] {f.message}"
                for f in known_findings))
    sections.append("Review each item:\n" + "\n".join(
        f"{_ITEM_SEP}{key}\nsource: {src}\ntarget: {tgt}"
        for key, src, tgt in items))
    sections.append(
        "Respond with ONLY this JSON object:\n"
        '{"findings": [{"key": "...", "bug_type": "<taxonomy>", '
        '"severity": "high|medium|low", "message": "...", '
        '"evidence": "<exact offending span>", "suggested_fix": null}]}\n'
        'If everything is correct: {"findings": []}')
    user = "\n\n".join(s for s in sections if s)
    # Worst case every item is flagged with an evidence span and a fix, so
    # the budget must cover a finding per item, not an average — a
    # truncated completion is an unparseable one.
    result = complete_json(provider, system, user, Review,
                           temperature=0.0,
                           max_tokens=300 * len(items) + 800)
    return result, model_fingerprint(provider, system)


# ---------------------------------------------------------------- verifier

def verify_finding(provider: Provider, *, key: str, source: str, target: str,
                   finding: Finding, source_lang: str, target_lang: str,
                   game: str,
                   glossary_brief: Optional[GlossaryBrief] = None,
                   style_brief: Optional[StyleBrief] = None,
                   client_lang: Optional[str] = None) -> Tuple[Verdict, str]:
    """docs/agents/verifier.md — second-layer review of one flag. Returns
    confirm / overturn / uncertain; never introduces new bug categories."""
    system = (
        f"You are a senior game-localization QA reviewer running a "
        f"second-layer review ({source_lang} → {target_lang}). A first-pass "
        "detector flagged this translation. RE-EVALUATE that flag against "
        "the glossary and style constraints, then decide:\n"
        "- confirm: the flag is correct — the translation has this issue.\n"
        "- overturn: the flag is a false positive — the translation is "
        "acceptable.\n"
        "- uncertain: you cannot clearly decide — recommend human review.\n"
        "Be conservative with overturn: only overturn when it is CLEAR the "
        "first-pass flag was wrong. Do NOT introduce new bug categories. "
        "If you confirm and can produce a corrected translation, set "
        "suggested_target; otherwise null. Output ONLY the JSON object "
        "requested.")
    sections = [_client_lang_directive(client_lang)]
    if glossary_brief:
        sections.append(render_brief(glossary_brief))
    sections.append(_style_section(style_brief, target_lang))
    sections.append(
        "## First-pass flag\n"
        f"- bug_type: {finding.bug_type.value}\n"
        f"- severity: {finding.severity.value}\n"
        f"- message: {finding.message}\n"
        f"- evidence: {finding.evidence or '(none)'}")
    sections.append(
        "## Pair under review\n"
        f"- key: {key}\n"
        f"- source ({source_lang}): {source}\n"
        f"- target ({target_lang}): {target}")
    sections.append(
        "Respond with ONLY this JSON object:\n"
        '{"decision": "confirm|overturn|uncertain", "confidence": 0.0, '
        '"reasoning": "<= 3 short sentences", "suggested_target": null}')
    user = "\n\n".join(s for s in sections if s)
    result = complete_json(provider, system, user, Verdict,
                           temperature=0.0, max_tokens=600)
    return result, model_fingerprint(provider, system)


# --------------------------------------------------------- context analyst

def analyze_style(provider: Provider, samples: List[str], *, game: str,
                  source_lang: str, target_locales: List[str],
                  client_notes: Optional[str] = None) -> Tuple[StyleBrief, str]:
    """docs/agents/context-analyst.md — one call per job (Stage 2)."""
    system = (
        "You are a game-localization style analyst. From the sample "
        "strings, characterize the game's genre, tone, and register, and "
        "produce actionable translation guidance per target locale. Output "
        "ONLY the JSON object requested.")
    sections = [f"Game: {game} ({source_lang} source). "
                f"Target locales: {', '.join(target_locales)}."]
    if client_notes:
        sections.append(f"**CLIENT STYLE GUIDE:**\n{client_notes}")
    sections.append("**SAMPLE STRINGS (short → long):**\n"
                    + "\n".join(f"- {s}" for s in samples))
    sections.append(
        "Respond with ONLY this JSON object:\n"
        '{"genre": ["..."], "tone": "...", "register": "...", '
        '"audience": "...", "do": ["..."], "dont": ["..."], '
        '"per_locale_notes": {"<locale>": "..."}, '
        f'"confidence": "high|medium|low", "sample_size": {len(samples)}}}\n'
        f"per_locale_notes MUST contain a key for each of: "
        f"{', '.join(target_locales)}")
    brief = complete_json(provider, system, "\n\n".join(sections), StyleBrief,
                          temperature=0.2, max_tokens=1500)
    for locale in target_locales:            # contract: never missing keys
        brief.per_locale_notes.setdefault(locale, "")
    return brief, model_fingerprint(provider, system)


# ------------------------------------------------------- domain classifier

DOMAIN_GUIDE = (
    "dialogue: conversation/storyline lines · ui: buttons, menus, HUD "
    "labels · map: location/zone names · item_desc: item/skill/function "
    "descriptions · system: errors, mail, notifications · marketing: "
    "store/promo text")


def classify_domains(provider: Provider,
                     items: List[Tuple[str, str]]) -> Tuple[DomainLabels, str]:
    """docs/agents/domain-classifier.md — LLM upgrade over the rules-based
    v0 in context.py (design §8)."""
    system = (
        "You are a game-localization content classifier. Assign each "
        "string exactly one domain label. Output ONLY the JSON object "
        "requested.")
    user = "\n\n".join([
        f"**DOMAINS:** {DOMAIN_GUIDE}",
        "Classify each item:",
        _render_batch(items),
        "Respond with ONLY this JSON object:\n"
        '{"items": [{"key": "<key>", "domain": "<domain>", '
        '"confidence": 1.0}]}\n'
        f"items MUST contain exactly these keys, once each: "
        f"{', '.join(k for k, _ in items)}"])
    labels = complete_json(provider, system, user, DomainLabels,
                           temperature=0.0,
                           max_tokens=40 * len(items) + 200)
    got = sorted(i.key for i in labels.items)
    if got != sorted(k for k, _ in items):
        raise RuntimeError(f"domain coverage violation: expected "
                           f"{[k for k, _ in items]}, got {got}")
    return labels, model_fingerprint(provider, system)


# ------------------------------------------------------------ terminologist

def extract_terms(provider: Provider, texts: List[str], *, game: str,
                  source_lang: str, target_locales: List[str],
                  known_terms: Optional[List[str]] = None
                  ) -> Tuple[GlossaryDelta, str]:
    """docs/agents/terminologist.md — batch ~30 strings. Output is STAGED;
    nothing enters the locked glossary without human gate G1."""
    system = (
        f"You are a senior game-localization terminologist for {game}. "
        "Extract GAME-SPECIFIC terms only: roles, factions, mechanics, "
        "phases, states, props, locations, items, resources. No sentences, "
        "no generic words, no numbers, no markup. Proposed translations are "
        "SHORT TERMS. Also report variant clusters that need a dev "
        "decision. Output ONLY the JSON object requested.")
    sections = []
    if known_terms:
        sections.append("**ALREADY IN GLOSSARY (do not re-extract):** "
                        + ", ".join(known_terms[:200]))
    sections.append(f"**SOURCE STRINGS ({source_lang}):**\n"
                    + "\n".join(f"- {t}" for t in texts))
    sections.append(
        "Respond with ONLY this JSON object:\n"
        '{"new_terms": [{"term": "...", "type": "role|faction|mechanic|'
        'phase|state|prop|location|item|resource|other", "frequency": 0, '
        '"proposed": {' + ", ".join(f'"{loc}": "..."' for loc in target_locales)
        + '}, "context_sample": "..."}], '
        '"conflicts": [{"term": "...", "issue": "variant_cluster", '
        '"variants": ["..."], "recommendation": "..."}]}')
    delta = complete_json(provider, system, "\n\n".join(sections),
                          GlossaryDelta, temperature=0.0, max_tokens=4000)
    return delta, model_fingerprint(provider, system)


# ----------------------------------------------------------- market analyst

def analyze_market(provider: Provider,
                   intake: IntakeBrief) -> Tuple[MarketReport, str]:
    """docs/agents/market-analyst.md — advisory locale assessment at intake."""
    system = (
        "You are a game-market analyst for localization planning. For each "
        "candidate locale, assess demand for this genre, name comparable "
        "titles localized there, and recommend for/against localizing now. "
        "Be honest about weak markets — the client pays per locale. Output "
        "ONLY the JSON object requested.")
    user = "\n\n".join([
        f"Game: {intake.game} · genre: {', '.join(intake.genre) or 'unknown'} "
        f"· engine: {intake.engine} · platforms: "
        f"{', '.join(intake.platforms) or 'unknown'}",
        f"Reference titles: {', '.join(intake.reference_titles) or 'none given'}",
        f"Candidate locales: {', '.join(intake.target_locales)}",
        "Respond with ONLY this JSON object:\n"
        '{"assessments": [{"locale": "...", "demand": "strong|moderate|'
        'weak|unknown", "comparable_titles": ["..."], '
        '"recommendation": "...", "notes": null}], "summary": "..."}'])
    report = complete_json(provider, system, user, MarketReport,
                           temperature=0.2, max_tokens=2500)
    return report, model_fingerprint(provider, system)


# ----------------------------------------------------- test-case generator

def generate_test_cases(provider: Provider, *, game: str, target_lang: str,
                        domain_counts: Dict[str, int], tester_hours: float,
                        samples: List[Tuple[str, str, str]],
                        style_brief: Optional[StyleBrief] = None
                        ) -> Tuple[TestPlan, str]:
    """docs/agents/test-case-generator.md — samples: [(key, source, target)]."""
    system = (
        "You are a game-LQA test lead. Design an in-game test plan a human "
        "tester can execute: per-domain checks for UI overflow, "
        "placeholders rendered with live values, map labels, description "
        "accuracy in context, and system-message triggers. Size the plan "
        "to the tester hours given (~10 focused cases per hour). Steps "
        "must be concrete actions in the game, not file checks. Output "
        "ONLY the JSON object requested.")
    sections = [
        f"Game: {game}, target language: {target_lang}, "
        f"tester hours available: {tester_hours}.",
        "**STRING VOLUME PER DOMAIN:** " + ", ".join(
            f"{d}: {n}" for d, n in domain_counts.items()),
        _style_section(style_brief, target_lang),
        "**SAMPLE TRANSLATED STRINGS:**\n" + "\n".join(
            f"- [{k}] {s} → {t}" for k, s, t in samples[:40]),
        "Respond with ONLY this JSON object:\n"
        '{"game": "' + game + '", "target_lang": "' + target_lang + '", '
        f'"tester_hours": {tester_hours}, '
        '"cases": [{"id": "TC-001", "domain": "<domain>", "title": "...", '
        '"steps": ["..."], "expected": "...", "keys": ["..."]}], '
        '"coverage_note": "what this plan does NOT cover"}']
    plan = complete_json(provider, system,
                         "\n\n".join(s for s in sections if s), TestPlan,
                         temperature=0.2, max_tokens=6000)
    return plan, model_fingerprint(provider, system)


# --------------------------------------------------------- marketing writer

# (short, long) description character limits per store platform.
PLATFORM_LIMITS = {"steam": (300, 8000), "googleplay": (80, 4000),
                   "epic": (250, 4000)}
DEFAULT_LIMITS = (300, 4000)


def write_marketing(provider: Provider, *, intake: IntakeBrief,
                    target_locale: str,
                    sample_strings: Optional[List[str]] = None,
                    style_brief: Optional[StyleBrief] = None,
                    market_summary: Optional[str] = None
                    ) -> Tuple[MarketingKit, str]:
    """docs/agents/marketing-writer.md — studio-voice materials only. Never
    fabricated player reviews or comments presented as organic (platforms
    ban astroturfing)."""
    platforms = intake.platforms or ["steam"]
    limits = {p: PLATFORM_LIMITS.get(p.lower(), DEFAULT_LIMITS)
              for p in platforms}
    system = (
        f"You are a game-marketing copywriter localizing store presence "
        f"into {target_locale}. Write persuasive, market-appropriate copy "
        "grounded in the game's ACTUAL content (use the sample strings for "
        "flavor and terminology). Respect each platform's character limits "
        "STRICTLY. Never write fake player reviews or testimonials — "
        "studio-voice materials only. Output ONLY the JSON object "
        "requested.")
    sections = [
        f"Game: {intake.game} · genre: {', '.join(intake.genre) or 'unknown'} "
        f"· target market locale: {target_locale}",
        "Platforms and (short, long) description char limits: " + "; ".join(
            f"{p}: {s}/{l}" for p, (s, l) in limits.items()),
        _style_section(style_brief, target_locale)]
    if market_summary:
        sections.append(f"**MARKET ANALYSIS:** {market_summary}")
    if sample_strings:
        sections.append("**GAME CONTENT SAMPLE (localized):**\n"
                        + "\n".join(f"- {s}" for s in sample_strings[:30]))
    sections.append(
        "Respond with ONLY this JSON object:\n"
        '{"game": "' + intake.game + '", "target_locale": "' + target_locale
        + '", "key_messages": ["..."], '
        '"store_copy": [{"platform": "<platform>", '
        '"short_description": "...", "long_description": "...", '
        '"char_limit_short": 0, "char_limit_long": 0, '
        '"within_limits": true}], '
        '"social_posts": ["..."], "press_blurb": "..."}')
    kit = complete_json(provider, system,
                        "\n\n".join(s for s in sections if s),
                        MarketingKit, temperature=0.4, max_tokens=6000)
    for copy in kit.store_copy:      # limits verified in code, never trusted
        short, long_ = PLATFORM_LIMITS.get(copy.platform.lower(),
                                           DEFAULT_LIMITS)
        copy.char_limit_short, copy.char_limit_long = short, long_
        copy.within_limits = (len(copy.short_description) <= short
                              and len(copy.long_description) <= long_)
    return kit, model_fingerprint(provider, system)
