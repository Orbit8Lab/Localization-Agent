"""Deterministic Tier-1 gate — ported from localization-pipeline
`locpipe/gate.py`. Free, instant, runs on every string every iteration.
Never spend a model call on what a regex settles (design §4).

Emits the same ``Finding`` schema as the LLM Critic so downstream consumers
cannot tell (and must not care) which produced a finding.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from .schemas import BugType, Finding, Severity

# Placeholders that must survive translation verbatim.
PLACEHOLDER_PATTERNS = [
    r"\{[^{}\n]*\}",        # {0}, {name}, {level}
    r"%\d*\$?[sdif]",       # %s, %d, %1$s
    r"\$[A-Za-z_]+\$",      # $VAR$
    r"\[[^\[\]\n]+\]",      # [Attack], [Concept(...)]
]
MARKUP_PATTERN = r"</?[A-Za-z][^<>\n]*>|</>"   # <color=...>, <GeneralFont_12>, </>

SCRIPT_RANGES = {
    "han": r"一-鿿㐀-䶿",
    "kana": r"぀-ヿ",
    "hangul": r"가-힯ᄀ-ᇿ㄰-㆏",
    "cyrillic": r"Ѐ-ӿ",
}
# Scripts a target locale legitimately uses (latin/digits always allowed).
ALLOWED_SCRIPTS = {
    "zh": {"han"}, "zh-tw": {"han"}, "ja": {"han", "kana"},
    "ko": {"hangul", "han"},  # hanja is rare but legitimate in ko games
    "ru": {"cyrillic"}, "uk": {"cyrillic"},
}

# Targets whose locked terms legitimately inflect (case endings).
INFLECTED_TARGETS = {"ru", "uk", "pl", "cs"}


@dataclass
class GateConfig:
    source_lang: str = "zh"
    target_lang: str = "ko"
    locked_terms: Dict[str, str] = field(default_factory=dict)
    # source term -> operator-approved alternate renderings. A variant is
    # a DECISION recorded on the glossary entry, not a fuzzy match.
    term_variants: Dict[str, List[str]] = field(default_factory=dict)
    # source term -> {"verb": "craft", "noun": "crafting"}. A glossary
    # holds ONE string per term, but Chinese source terms are routinely
    # both verb and noun (合成 = craft / crafting). Declaring the forms is
    # what lets the gate accept "used to craft" without weakening into
    # fuzzy matching.
    term_forms: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # source term -> "exact" | "context" (default). Capitalization is a
    # STYLE question (rules CAP-*), not term identity: only a term
    # explicitly marked `exact` — a proper name — may raise a casing
    # finding here. Anything else is judged by the style rubric in T3.
    term_case: Dict[str, str] = field(default_factory=dict)
    dnt: List[str] = field(default_factory=list)
    length_ratio_bounds: tuple = (0.2, 5.0)
    min_len_for_ratio: int = 10
    # Display-width budget: string type -> max target/source width ratio.
    # Scoped by TYPE because overflow is a property of the widget, not the
    # language: a button cannot reflow, a wiki paragraph can. Types absent
    # from this map are exempt — silence beats crying wolf on text that has
    # room to wrap.
    #
    # Anchored on SHIPPED, professionally localized games — 9,597 en/zh
    # string pairs from Factorio and Endless Legend — at the p95 of each
    # string type: UI 2.38, Item 2.50, Skill 2.50, System 2.44. Those
    # strings passed a studio's QA in a real UI, so the ceiling is one
    # that demonstrably FITS rather than one describing how verbose a
    # given translator happens to be.
    #
    # Deliberately NOT fitted to the project being scanned: one live
    # project's own p95 is 2.80, and calibrating on it would inherit that
    # verbosity and define its own tail as normal. Two studios agreeing
    # (medians 1.60 and 1.67, different genres/engines/vendors) is the
    # evidence that makes an external anchor safer than a local one.
    #
    # Caveat carried deliberately: those corpora are en→zh while this
    # pipeline runs zh→en. Ratios are measured en_width/zh_width in both,
    # so they are comparable, but compression into Chinese is not the
    # exact mirror of expansion out of it. Re-fit when in-game overflow
    # data exists — that is ground truth, this is a well-founded proxy.
    width_budget: Dict[str, float] = field(default_factory=lambda: {
        "UI": 2.4, "Skill": 2.5, "Item": 2.5, "System": 2.45,
    })
    # Below this source width a ratio is statistical noise: a 1-glyph
    # source (2 columns) hits 3.5x on any ordinary English word, which is
    # a fact about short labels, not a defect. Such strings need a real
    # per-widget max_len, not a ratio.
    min_width_for_ratio: int = 6
    ko_hanja_max_run: int = 3
    ko_hanja_ratio: float = 0.25
    # Style rules whose enforcement bin is "mechanical" (style_guide.py).
    # Language-specific grammar lives in the guide DATA, never in this
    # engine code — adding a locale means authoring a guide, not editing
    # the gate.
    style_guide: object = None


def term_in_text(term: str, text: str) -> bool:
    """CJK-safe term matching: ``\\b`` never fires next to Han characters,
    so boundary anchors apply only on ASCII term edges."""
    term_l, text_l = term.lower(), text.lower()
    left = r"\b" if (term_l[:1].isascii() and term_l[:1].isalnum()) else ""
    right = r"\b" if (term_l[-1:].isascii() and term_l[-1:].isalnum()) else ""
    if not left and not right:
        return term_l in text_l
    return re.search(left + re.escape(term_l) + right, text_l) is not None


def term_spans(term: str, text: str) -> List[tuple]:
    """Every ``(start, end)`` where `term` matches, same rules as
    `term_in_text`. Positions are what makes longest-match possible."""
    term_l, text_l = term.lower(), text.lower()
    left = r"\b" if (term_l[:1].isascii() and term_l[:1].isalnum()) else ""
    right = r"\b" if (term_l[-1:].isascii() and term_l[-1:].isalnum()) else ""
    pattern = left + re.escape(term_l) + right
    return [(m.start(), m.end()) for m in re.finditer(pattern, text_l)]


_PLURAL = r"(?:s|es)?"


def _inflected_spans(term: str, text: str) -> List[tuple]:
    """Spans where `term` occurs with ANY of its words pluralized.

    An English multi-word term is written inflected in real scripts, and
    not always on the last word: the glossary holds "Firework Festival"
    while the script says "Fireworks Festival"; it holds "Spirit
    Guardian" while the script says "Spirit Guardians". Both broke the
    same way — the longer term matched nothing, so a nested short term
    ("Festival", "spirit") claimed the span and produced a terminology
    defect against a correct translation.

    Matching an optional plural suffix after EVERY word covers both, and
    the two earlier point-fixes (trailing "s", trailing "es") collapse
    into it. Only COVERAGE is decided here — which term owns the span —
    so a loose match costs nothing: the finding itself is still raised by
    the exact term, and a term absent from the source still matches
    nothing.
    """
    if not term or not term[-1:].isascii():
        return []
    words = [w for w in re.split(r"(\s+)", term.lower()) if w]
    pattern = "".join(re.escape(w) + ("" if w.isspace() else _PLURAL)
                      for w in words)
    left = r"\b" if term[:1].isalnum() else ""
    try:
        return [(m.start(), m.end())
                for m in re.finditer(left + pattern + r"\b", text.lower())]
    except re.error:
        return []


def applicable_terms(source: str,
                     locked_terms: Dict[str, str]) -> Dict[str, str]:
    """The locked terms a source string is actually subject to, resolving
    NESTED entries by longest match.

    A termbase legitimately contains a term inside a longer one —
    "Spirit" and "Spirit Guardian", "Season Seed" and "Autumn Season
    Seed", "Festival" and "Firework Festival". The check ran each entry
    independently, so "Find your Spirit Guardian" was held to BOTH
    "Spirit Guardian" -> 守护灵 AND "Spirit" -> 灵体, and a correct
    translation was reported as a terminology defect because 灵体 was
    absent. On the Nomori termbase 29 pairs nest this way, most of them
    the seasonal families, so the noise was systematic rather than rare.

    Longest match wins the text it covers: once "Autumn Spirit Guardian"
    claims a span, "Spirit Guardian" and "Spirit" cannot be required
    inside it. A shorter term still applies where it occurs on its own —
    "The spirit fled the Spirit Guardian" is subject to both.

    Ordering is by match length, then term length, so the decision never
    depends on dict insertion order (the glossary is merged from three
    tiers, and its order is not meaningful).
    """
    claimed: List[tuple] = []
    applicable: Dict[str, str] = {}
    candidates = []
    for term, rendering in locked_terms.items():
        spans = term_spans(term, source) or _inflected_spans(term, source)
        for start, end in spans:
            candidates.append((end - start, len(term), term, rendering,
                               start, end))
    for _len, _tlen, term, rendering, start, end in sorted(
            candidates, key=lambda c: (-c[0], -c[1], c[4])):
        if any(start < c_end and end > c_start for c_start, c_end in claimed):
            continue                    # covered by a longer term
        claimed.append((start, end))
        applicable[term] = rendering
    return applicable


def locked_in_target(locked: str, target: str, target_lang: str,
                     morphology=None, variants: Iterable[str] = (),
                     forms: Optional[Dict[str, str]] = None,
                     case: str = "context") -> bool:
    """Glossary-compliance matching.

    Four things can satisfy a locked term, in order of authority:

    1. the exact form;
    2. a declared part-of-speech FORM of it ("craft" for a term stored as
       "Crafting") — cross-POS identity the matcher cannot derive, so it
       is recorded on the entry as a decision;
    3. an operator-approved VARIANT recorded on the glossary entry
       ("Source of Plague" for "Plague Source") — a decision, never a
       guess;
    4. a legitimate inflection of it, per the target language's
       morphology profile (style_guide.Morphology). No language rules
       live here: this function executes a profile, it does not know
       English or Russian.

    ``case`` says whether capitalization is part of term IDENTITY. It is
    "context" by default — a term names a WORD, and whether that word is
    capitalized in a given sentence is a style rule (CAP-*), judged with
    the surrounding context in T3. Only a term declared "exact" (a proper
    name) is matched case-sensitively here.

    Without a profile the legacy behaviour applies (stem tolerance for
    the inflected-target list), so callers that predate style guides keep
    working.
    """
    if case == "exact" and locked not in target:
        # A proper name whose casing is wrong: the WORD is present but
        # not in its mandated form, so this is a real finding.
        if term_in_text(locked, target):
            return False
    if term_in_text(locked, target):
        return True
    # A declared form is a base to inflect from, not a fixed string: a
    # glossary that declares verb "craft" must also accept "crafted".
    for form in (forms or {}).values():
        if not form:
            continue
        if term_in_text(form, target):
            return True
        if morphology is not None and morphology.matches(form, target):
            return True
    for variant in variants:
        if variant and term_in_text(variant, target):
            return True
    if morphology is not None:
        return morphology.matches(locked, target)
    if target_lang.split("-")[0].lower() not in INFLECTED_TARGETS:
        return False
    words = locked.lower().split()
    if not words or len(words[-1]) < 5:
        return False
    for cut in (2, 3):
        stem = words[-1][:-cut]
        if len(stem) >= 4 and " ".join(words[:-1] + [stem]) in target.lower():
            return True
    return False


def _extract_placeholders(text: str) -> Counter:
    found: Counter = Counter()
    for pattern in PLACEHOLDER_PATTERNS + [MARKUP_PATTERN]:
        found.update(re.findall(pattern, text))
    return found


def _strip_untranslatables(text: str, cfg: GateConfig) -> str:
    for pattern in PLACEHOLDER_PATTERNS + [MARKUP_PATTERN]:
        text = re.sub(pattern, " ", text)
    for literal in cfg.dnt:
        text = text.replace(literal, " ")
    return text


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# --------------------------------------------------------- display width
#
# UI overflow is a GEOMETRY problem, so it has to be measured in the unit
# the widget cares about — rendered columns — not in characters and not in
# words. The two obvious metrics both mislead on zh→en:
#
#   弓 → "Longbow"    chars 7.0x   words 1.0x   width 3.5x
#   攻击力 → "Attack Power"  chars 4.0x   words 0.7x   width 2.0x
#
# Characters overstate expansion (they ignore that 攻 occupies two columns);
# WORDS erase it entirely — "Longbow" is one word however wide it renders,
# so a word-ratio rule reports no growth on exactly the short UI labels
# that overflow buttons. Word counting stays correct for QUOTING, which is
# what analysis.count_words does and why this is a separate function.
#
# Measured over a live zh→en corpus (1330 single-line entries), median
# target/source width is ~1.8x — the real expansion, well above the ~1.3x
# rule of thumb carried over from European pairs.

def display_width(text: str) -> int:
    """Rendered column count: East-Asian Wide/Fullwidth glyphs cost 2, all
    other characters 1.

    A proxy, deliberately: proportional fonts make "WWW" and "iii" both
    three columns but very different pixel widths, so this captures the
    systematic double-width effect and not the last stretch of it. Exact
    pixels need font metrics the .po does not carry.

    Combining marks (accents, Hangul jamo fillers) cost 0 — they render
    inside the preceding glyph rather than advancing the cursor.
    """
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def width_ratio(source: str, target: str) -> float:
    """Target display width as a multiple of the source's. 0.0 when the
    source has no measurable width (pure placeholder/markup)."""
    src = display_width(source)
    return (display_width(target) / src) if src else 0.0


_SPEAKER_SRC = re.compile(r"^\s*([A-Za-z][A-Za-z .\'-]{0,30}?)\s*:")
_SPEAKER_TGT = re.compile(r"^\s*([^\s:：]{1,20})\s*[:：]")


def speaker_mismatch(source: str, target: str,
                     locked_terms: Dict[str, str]) -> Optional[tuple]:
    """`(source speaker, target speaker, expected)` when a dialogue line's
    target names a DIFFERENT character, else None.

    Game scripts prefix dialogue with the speaker, and the speaker names
    are already locked glossary terms ("Yuki" -> "小雪"), so the mapping
    needed to check this exists without any new configuration.

    This is the signal for a MISALIGNED row — a target that is not a
    translation of its source at all. It fires only when BOTH sides carry
    a prefix AND the source speaker is a locked term AND the target's
    prefix is a known rendering of some OTHER locked term. Anything less
    certain — an unmapped name, a missing prefix, a target prefix that is
    not a known character — returns None rather than guessing.

    Why a prefix disagreement is sufficient evidence on its own: measured
    over the Nomori zh-CN corpus, 1302 speaker-tagged lines render the
    speaker correctly, 3 name a different character, and 57 are unmapped
    (skipped). Translators do not silently swap one character's name for
    another's — when the prefix says someone else, the line is someone
    else's.

    KNOWN LIMIT: a line whose ONLY defect is a misrendered speaker
    ("Kodama: Hello!" -> "小雪:你好！", a faithful translation with one
    wrong name) is reported as misalignment rather than terminology. That
    is a deliberate trade: the message names both speakers and asks the
    client to confirm the line mapping, so a reader can tell the two
    apart, and the corpus above contains no such case. Revisit if one
    appears.
    """
    src_match = _SPEAKER_SRC.match(source)
    tgt_match = _SPEAKER_TGT.match(target)
    if not src_match or not tgt_match:
        return None
    src_speaker = src_match.group(1).strip()
    tgt_speaker = tgt_match.group(1).strip()
    expected = None
    for term, rendering in locked_terms.items():
        if term.strip().lower() == src_speaker.lower():
            expected = rendering
            break
    if expected is None:
        return None                     # not a known character: stay quiet
    if _normalize(tgt_speaker) == _normalize(expected):
        return None                     # correct rendering
    # Only claim misalignment when the target names ANOTHER known
    # character. A merely misrendered speaker is a terminology defect and
    # belongs to that check, not this one.
    others = {r for t, r in locked_terms.items()
              if t.strip().lower() != src_speaker.lower()}
    if not any(_normalize(tgt_speaker) == _normalize(other)
               for other in others):
        return None
    return (src_speaker, tgt_speaker, expected)


def run_gate(key: str, source: str, target: str, cfg: GateConfig,
             term_decisions: Optional[Dict[str, str]] = None,
             max_len: Optional[int] = None,
             domain: Optional[str] = None,
             string_type: Optional[str] = None) -> List[Finding]:
    """All Tier-1 checks for one string. Returns [] when clean.
    ``domain`` selects the applicable style rules (UI rules must not fire
    on dialogue). ``string_type`` (standards.STRING_TYPES — UI/Skill/Item/
    …) selects the display-width budget; without it the width check stays
    silent rather than guessing a widget class."""
    findings: List[Finding] = []

    # 1. empty output
    if not target.strip():
        return [Finding(key=key, bug_type=BugType.OMISSION,
                        severity=Severity.HIGH,
                        message="Empty translation.", evidence="")]

    # 1b. MISALIGNMENT: the target is not a translation of this source.
    #     Runs before every component check because it SUBSUMES them. A
    #     misaligned dialogue row trips terminology (wrong speaker name)
    #     and placeholder (absent markup) by construction, and reporting
    #     those is actively harmful: told "render 'Yuki' as '小雪'", a
    #     client edits a translation that was never Yuki's line. The
    #     component failures are consequences; this is the cause.
    misaligned = speaker_mismatch(source, target, cfg.locked_terms)
    if misaligned:
        src_speaker, tgt_speaker, expected = misaligned
        return [Finding(
            key=key, bug_type=BugType.MISTRANSLATION, severity=Severity.HIGH,
            message=(f"Source/target misalignment: the source is spoken by "
                     f"{src_speaker!r} (expected {expected!r}) but the "
                     f"target is spoken by {tgt_speaker!r}. The target "
                     f"appears to be the translation of a DIFFERENT line, "
                     f"so this is not a terminology fix — confirm the line "
                     f"mapping in the export."),
            evidence=target[:80])]

    # 2. placeholder / markup integrity (multiset equality)
    src_ph, tgt_ph = _extract_placeholders(source), _extract_placeholders(target)
    if src_ph != tgt_ph:
        missing = list((src_ph - tgt_ph).elements())
        # A placeholder present ONLY in the target is not a defect: game
        # source strings routinely lose theirs ("距离天亮还有    s"), and a
        # target that restores it ("Dawn in: {0}s") is more correct than
        # the source. Report only what the translation DROPPED or CHANGED.
        extra = list((tgt_ph - src_ph).elements()) if missing else []
        if missing:
            findings.append(Finding(
                key=key, bug_type=BugType.PLACEHOLDER, severity=Severity.HIGH,
                message=f"Placeholder/markup mismatch: missing "
                        f"{missing or '[]'}, extra {extra or '[]'}.",
                evidence=" ".join(missing + extra)[:120] or target[:60]))

    # 3. glossary compliance: locked term in source => locked rendering in
    #    target; cross-check the Translator's claimed term_decisions.
    # Longest match wins: a nested entry ("Spirit" inside "Spirit
    # Guardian") must not be enforced inside the span the longer term
    # already claims. See `applicable_terms`.
    for term, locked in applicable_terms(source, cfg.locked_terms).items():
        morphology = getattr(cfg.style_guide, "morphology", None)
        if not locked_in_target(locked, target, cfg.target_lang,
                                morphology=morphology,
                                variants=cfg.term_variants.get(term, ()),
                                forms=cfg.term_forms.get(term),
                                case=cfg.term_case.get(term, "context")):
            claimed = (term_decisions or {}).get(term)
            claim_note = f" (model claimed {claimed!r})" if claimed else ""
            findings.append(Finding(
                key=key, bug_type=BugType.TERMINOLOGY, severity=Severity.HIGH,
                message=f"Locked term {term!r} must render as {locked!r}{claim_note}.",
                evidence=term))

    stripped_src = _strip_untranslatables(source, cfg)
    stripped_tgt = _strip_untranslatables(target, cfg)

    # 4. untranslated: target (normalized) identical to a non-trivial source.
    #    A source carrying none of its own script has nothing to translate
    #    — "Error", "Text Block" and other dev-English placeholders are
    #    CORRECT when echoed verbatim, so identity is only evidence of an
    #    untranslated string when the source is actually in source_lang.
    src_scripts_expected = ALLOWED_SCRIPTS.get(cfg.source_lang.lower(), set())
    source_has_own_script = not src_scripts_expected or any(
        re.search(f"[{SCRIPT_RANGES[s]}]", stripped_src)
        for s in src_scripts_expected)
    if (_normalize(stripped_src)
            and source_has_own_script
            and _normalize(stripped_src) == _normalize(stripped_tgt)
            and len(_normalize(stripped_src)) >= 4
            and cfg.source_lang != cfg.target_lang):
        findings.append(Finding(
            key=key, bug_type=BugType.UNTRANSLATED, severity=Severity.HIGH,
            message="Target is identical to source (untranslated).",
            evidence=target[:80]))
        # An untranslated string CANNOT satisfy the glossary: the locked
        # rendering is absent precisely because nothing was translated.
        # Reporting both produced two HIGH rows for one defect, and the
        # terminology one is unactionable on its own — you cannot fix the
        # term without translating the string. Keep the cause, drop the
        # consequence.
        findings = [f for f in findings
                    if f.bug_type != BugType.TERMINOLOGY]

    # 5. source-script leakage (e.g. Han characters in a ru target)
    src_scripts = ALLOWED_SCRIPTS.get(cfg.source_lang.lower(), set())
    tgt_scripts = ALLOWED_SCRIPTS.get(cfg.target_lang.lower(), set())
    for script in src_scripts - tgt_scripts:
        leaked = re.findall(f"[{SCRIPT_RANGES[script]}]+", stripped_tgt)
        leaked = [run for run in leaked
                  if not any(term_in_text(run, t)
                             for t in cfg.locked_terms.values())]
        if leaked:
            findings.append(Finding(
                key=key, bug_type=BugType.LEAKAGE, severity=Severity.HIGH,
                message=f"Source-script ({script}) characters leaked into "
                        f"{cfg.target_lang} target.",
                evidence=" ".join(leaked)[:80]))

    # 5b. hanja allowance for han-source → ko: single hanja words are
    #     legitimate; long runs / high ratios are leakage.
    if "han" in src_scripts and cfg.target_lang.lower().startswith("ko"):
        han_runs = re.findall(f"[{SCRIPT_RANGES['han']}]+", stripped_tgt)
        han_runs = [run for run in han_runs
                    if not any(term_in_text(run, t)
                               for t in cfg.locked_terms.values())]
        total_chars = len(re.sub(r"\s", "", stripped_tgt))
        han_chars = sum(len(run) for run in han_runs)
        if han_runs and (max(len(r) for r in han_runs) > cfg.ko_hanja_max_run
                         or (total_chars
                             and han_chars / total_chars > cfg.ko_hanja_ratio)):
            findings.append(Finding(
                key=key, bug_type=BugType.LEAKAGE, severity=Severity.HIGH,
                message=f"Han characters in ko target exceed the hanja "
                        f"allowance (runs {[len(r) for r in han_runs]}, "
                        f"ratio {han_chars / max(total_chars, 1):.0%}).",
                evidence=" ".join(han_runs)[:80]))

    # 6. hard UI length limit (from SourceString.max_len, when present).
    #    Measured in display columns: a budget authored against the source
    #    would be met by a target that renders far wider if counted naively.
    if max_len and display_width(target) > max_len:
        findings.append(Finding(
            key=key, bug_type=BugType.LENGTH, severity=Severity.HIGH,
            message=f"Target exceeds hard width limit "
                    f"({display_width(target)} > {max_len} columns).",
            evidence=target[:80]))

    # 6b. display-width expansion, scoped by string type. This is the check
    #     that catches UI overflow when no per-widget max_len exists — the
    #     normal case, since a .po carries no geometry. MEDIUM on purpose:
    #     it is a risk signal for a post-editor, not proof of a defect, and
    #     the only certain answer comes from seeing the string in-game.
    budget = cfg.width_budget.get(string_type or "") if string_type else None
    if budget and display_width(stripped_src) >= cfg.min_width_for_ratio:
        ratio = width_ratio(stripped_src, stripped_tgt)
        if ratio > budget:
            findings.append(Finding(
                key=key, bug_type=BugType.LENGTH, severity=Severity.MEDIUM,
                message=f"{string_type} target renders "
                        f"{display_width(stripped_tgt)} columns vs source "
                        f"{display_width(stripped_src)} ({ratio:.1f}x, "
                        f"budget {budget}x) — UI overflow risk.",
                evidence=target[:80]))

    # 7. length-ratio sanity (loose; expansion tuning comes from style
    #    packs). Only runs when a hard UI budget (max_len) exists: without
    #    one, ratio outliers are dominated by legitimate logographic
    #    compression ("REFLECTIONS" → "反射"), and a truncation warning
    #    with no limit to check against is unactionable noise.
    src_len = len(_normalize(stripped_src))
    if max_len is not None and src_len >= cfg.min_len_for_ratio:
        ratio = len(_normalize(stripped_tgt)) / max(src_len, 1)
        low, high = cfg.length_ratio_bounds
        if not (low <= ratio <= high):
            findings.append(Finding(
                key=key, bug_type=BugType.LENGTH, severity=Severity.MEDIUM,
                message=f"Suspicious length ratio {ratio:.2f} (bounds {low}-{high}).",
                evidence=target[:80]))

    # 8. style-guide rules tagged "mechanical" — punctuation width, CJK
    #    spacing, length caps and the like. The rules are DATA loaded from
    #    the project's style guide for this language pair; this gate only
    #    executes them, so it stays language-agnostic.
    if cfg.style_guide is not None:
        from .style_guide import check_mechanical
        severities = {"high": Severity.HIGH, "medium": Severity.MEDIUM,
                      "low": Severity.LOW}
        for violation in check_mechanical(cfg.style_guide, source, target,
                                          domain=domain):
            findings.append(Finding(
                key=key, bug_type=BugType.PUNCTUATION,
                severity=severities.get(violation.severity,
                                        Severity.MEDIUM),
                message=f"[{violation.rule_id}] {violation.message}",
                evidence=violation.evidence[:120] or target[:60]))

    return findings
