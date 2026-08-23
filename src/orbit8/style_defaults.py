"""Starter style guides for the language pairs Orbit8 runs today.

These are DEFAULTS, not doctrine: a project copies one into
``40-reference/style/<pair>.json`` and edits it. Rules here are the ones
that hold across game projects; project- or client-specific rules belong
in the project copy.

Every rule is tagged with its enforcement bin, so the split between
"checked in code", "judged by the reviewer" and "steering only" is
visible at a glance — and an advisory rule is an admission that nobody
verifies it.
"""
from __future__ import annotations

from .style_guide import Morphology, StyleGuide, StyleRule

# English targets: regular plurals plus the irregulars that show up in
# game terminology. Capitalization is NOT part of term identity (that is
# style rule ZH-EN-11), so matching stays case-insensitive.
EN_MORPHOLOGY = Morphology(
    strategy="suffix", suffixes=["s", "es"],
    irregular={"knife": ["knives"], "leaf": ["leaves"],
               "wolf": ["wolves"], "thief": ["thieves"],
               "staff": ["staves", "staffs"], "elf": ["elves"],
               "dwarf": ["dwarves", "dwarfs"], "life": ["lives"],
               "die": ["dice"], "man": ["men"], "woman": ["women"],
               "tooth": ["teeth"], "foot": ["feet"], "child": ["children"],
               "person": ["people"], "mouse": ["mice"], "goose": ["geese"]},
    verb_suffixes=["ing", "ed"],
    variants_note="Alternate WORDINGS (\"Source of Plague\" for "
                  "\"Plague Source\") are not morphology: record them as "
                  "`variants` on the glossary entry so the decision is "
                  "explicit and auditable.")

# Chinese targets do not inflect: a term's surface form never varies, so
# glossary compliance is exact-match.
ZH_MORPHOLOGY = Morphology(
    strategy="none",
    variants_note="Chinese has no inflection; any accepted alternate "
                  "rendering must be recorded as a glossary `variants` "
                  "entry.")

# Domain names are schemas.Domain values.
_UI = ["ui"]
_STORY = ["dialogue", "marketing"]
_SYSTEM = ["system"]

ZH_EN = StyleGuide(
    source_lang="zh-CN", target_lang="en", version="1",
    notes="Starter guide for Chinese→English game localization. "
          "Mechanical rules are enforced by the T1 gate; llm rules are "
          "cited by the T3 reviewer; advisory rules only steer the "
          "translator.",
    morphology=EN_MORPHOLOGY,
    rules=[
        StyleRule(
            id="ZH-EN-01", enforcement="mechanical", check="forbid_chars",
            value="，。！？；：、（）《》「」",
            text="No CJK full-width punctuation in English targets.",
            rationale="A Chinese comma in an English string is the most "
                      "common copy-paste artifact and looks broken in "
                      "game.",
            severity="high",
            examples={"good": "Restore Health, then attack.",
                      "bad": "Restore Health，then attack。"}),
        StyleRule(
            id="ZH-EN-02", enforcement="mechanical", check="forbid_pattern",
            value=r"[一-鿿]",
            text="No untranslated Han characters left in the target.",
            severity="high"),
        StyleRule(
            id="ZH-EN-03", enforcement="mechanical",
            check="require_source_parity", value=".!?。！？",
            text="Sentence-final punctuation must match the source: if "
                 "the source ends a sentence, so must the target.",
            severity="low",
            rationale="UI labels without a period read as buttons; "
                      "descriptions with one read as prose. Follow the "
                      "source's intent."),
        StyleRule(
            id="ZH-EN-04", enforcement="mechanical", check="forbid_pattern",
            value=r"\s{2,}",
            text="No double spaces.", severity="low"),
        StyleRule(
            id="ZH-EN-10", enforcement="llm", domains=_UI,
            text="UI strings are imperative and terse — verb-first, no "
                 "articles where they can be dropped.",
            severity="medium",
            examples={"good": "Equip Weapon", "bad": "You can equip the "
                                                     "weapon here"}),
        StyleRule(
            id="ZH-EN-11", enforcement="llm", domains=_UI,
            text="Title Case for buttons, item names and headings; "
                 "sentence case for descriptions and body text.",
            severity="medium"),
        StyleRule(
            id="ZH-EN-12", enforcement="llm", domains=_STORY,
            text="Dialogue keeps the speaker's register and personality; "
                 "do not flatten colloquial Chinese into neutral English.",
            severity="medium",
            examples={"bad": "The character says that he is angry.",
                      "good": "Tch — you'll pay for that."}),
        StyleRule(
            id="ZH-EN-13", enforcement="llm", domains=_SYSTEM,
            text="System and error messages are neutral and impersonal; "
                 "state what happened and what to do, never blame the "
                 "player.",
            severity="medium",
            examples={"good": "Connection lost. Retrying…",
                      "bad": "You broke the connection!"}),
        StyleRule(
            id="ZH-EN-14", enforcement="llm",
            text="Chinese four-character idioms (成语) are rendered by "
                 "MEANING, never literally.",
            severity="high",
            rationale="Literal renderings of 成语 are the most visible "
                      "machine-translation tell in EN builds."),
        StyleRule(
            id="ZH-EN-15", enforcement="llm",
            text="Prefer the noun-compound form over an of-phrase when "
                 "both are natural (Plague Source, not Source of "
                 "Plague), unless the glossary rules otherwise.",
            severity="low",
            rationale="Keeps compound game terms scannable and "
                      "consistent with sibling terms."),
        StyleRule(
            id="ZH-EN-20", enforcement="advisory",
            text="Chinese omits subjects freely; restore the implied "
                 "subject in English rather than writing fragments.",
            severity="low"),
        StyleRule(
            id="ZH-EN-21", enforcement="advisory", domains=_STORY,
            text="Vary sentence length in narration; Chinese source "
                 "often uses even, comma-linked clauses that read "
                 "monotonously if mirrored.",
            severity="low"),
    ])

EN_ZH = StyleGuide(
    source_lang="en", target_lang="zh-CN", version="1",
    notes="Starter guide for English→Simplified Chinese game "
          "localization.",
    morphology=ZH_MORPHOLOGY,
    rules=[
        StyleRule(
            id="EN-ZH-01", enforcement="mechanical", check="forbid_chars",
            value=",;?!",
            text="Use full-width punctuation (，；？！) in Chinese "
                 "targets, not half-width.",
            severity="high",
            rationale="Half-width punctuation in CJK text is the "
                      "standard machine-translation artifact.",
            examples={"good": "生命值不足，无法施放。",
                      "bad": "生命值不足, 无法施放."}),
        StyleRule(
            id="EN-ZH-02", enforcement="mechanical", check="forbid_pattern",
            value=r"[一-鿿]\s+[一-鿿]",
            text="No spaces between Han characters.",
            severity="medium"),
        StyleRule(
            id="EN-ZH-03", enforcement="mechanical", check="forbid_pattern",
            value=r"[一-鿿][A-Za-z0-9]|[A-Za-z0-9][一-鿿]",
            text="Insert a space between Han characters and Latin "
                 "letters or digits.",
            severity="low",
            examples={"good": "获得 100 金币", "bad": "获得100金币"}),
        StyleRule(
            id="EN-ZH-10", enforcement="llm", domains=_UI,
            text="UI labels are short — prefer 2–4 characters for "
                 "buttons; never translate an English label word for "
                 "word if a shorter idiomatic term exists.",
            severity="medium",
            examples={"good": "确认", "bad": "点击此处进行确认"}),
        StyleRule(
            id="EN-ZH-11", enforcement="llm", domains=_STORY,
            text="Dialogue uses natural spoken Chinese with sentence-"
                 "final particles (吧/啊/呢) where the tone calls for "
                 "them; avoid translationese word order.",
            severity="medium"),
        StyleRule(
            id="EN-ZH-12", enforcement="llm", domains=_SYSTEM,
            text="System messages use 请 for instructions and avoid 你 "
                 "when addressing the player directly.",
            severity="medium",
            examples={"good": "请稍后重试。", "bad": "你应该稍后再试。"}),
        StyleRule(
            id="EN-ZH-13", enforcement="llm",
            text="Do not translate English possessives literally with "
                 "的 chains; restructure (\"the guard's sword\" → 卫兵之剑 "
                 "or 卫兵佩剑, not 卫兵的的剑).",
            severity="medium"),
        StyleRule(
            id="EN-ZH-20", enforcement="advisory",
            text="Match the game's established register: wuxia/xianxia "
                 "settings take classical flavour (之/其), modern "
                 "settings take plain Mandarin.",
            severity="low"),
    ])

DEFAULT_GUIDES = {("zh-CN", "en"): ZH_EN, ("en", "zh-CN"): EN_ZH}


def default_guide(source_lang: str, target_lang: str):
    """Best-effort starter guide for a pair; base-language match is
    accepted (zh → zh-CN) so callers need not normalize locales."""
    key = (source_lang, target_lang)
    if key in DEFAULT_GUIDES:
        return DEFAULT_GUIDES[key]
    base = (source_lang.split("-")[0], target_lang.split("-")[0])
    for (src, tgt), guide in DEFAULT_GUIDES.items():
        if (src.split("-")[0], tgt.split("-")[0]) == base:
            return guide
    return None
