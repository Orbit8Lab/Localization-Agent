"""Regression tests for the four defects found in a live PE form
(lqa-scan-full-20260805-1053), where ~40% of suggestions were noise.

Each test names the real row that motivated it, so a future change that
reintroduces the defect fails with the evidence attached.
"""
from __future__ import annotations

from orbit8.gate_checks import GateConfig, locked_in_target, run_gate
from orbit8.schemas import BugType
from orbit8.style_defaults import ZH_EN


# --------------------------------------------------------------- verb/noun

def test_verb_form_satisfies_noun_glossary_entry():
    """PE row 73: 用于合成弓和背包 → 'Used to craft bows and backpacks'.

    合成 is stored as 'Crafting' (noun). The verb use is correct English
    and must NOT be rewritten to 'Used for Crafting bows'.
    """
    assert locked_in_target(
        "Crafting", "Used to craft bows and backpacks.", "en",
        morphology=ZH_EN.morphology,
        forms={"verb": "craft", "noun": "crafting"})


def test_noun_form_still_satisfies():
    """The noun use must keep passing — widening must not break it."""
    assert locked_in_target(
        "Crafting", "Open the Crafting menu", "en",
        morphology=ZH_EN.morphology,
        forms={"verb": "craft", "noun": "crafting"})


def test_declared_form_is_a_base_to_inflect_from():
    """PE row 71 of the bug report: 'Crafted from gathered materials'.
    A declared form is a lemma, not a fixed string — its inflections
    (crafted) must satisfy the term too."""
    assert locked_in_target(
        "Crafting", "Crafted from gathered materials", "en",
        morphology=ZH_EN.morphology,
        forms={"verb": "craft", "noun": "crafting"})


def test_lookalike_word_is_not_a_form():
    """Inflecting a declared form must not degrade into prefix matching."""
    assert not locked_in_target(
        "Crafting", "a crafty solution", "en",
        morphology=ZH_EN.morphology,
        forms={"verb": "craft", "noun": "crafting"})


def test_wrong_word_still_fails_with_forms():
    """Widening to declared forms must not accept an unrelated word."""
    assert not locked_in_target(
        "Crafting", "Used to build bows", "en",
        morphology=ZH_EN.morphology,
        forms={"verb": "craft", "noun": "crafting"})


def test_singular_satisfies_plural_entry():
    """PE row 71 / bug row 105: 道具 is stored as plural 'Props'; a
    singular occurrence ("the 'Skeleton' item") must not be forced to
    "the 'Skeleton' Props"."""
    assert locked_in_target("Props", "holding the Skeleton prop", "en",
                            morphology=ZH_EN.morphology)


# ------------------------------------------------------------------ casing

def test_case_only_difference_is_not_a_terminology_defect():
    """PE rows 103/104: 神庙 → 'Temple'. A lowercase 'temple' is a CASING
    question owned by CAP-08, not a terminology violation."""
    cfg = GateConfig(source_lang="zh", target_lang="en",
                     locked_terms={"神庙": "Temple"},
                     style_guide=ZH_EN)
    findings = run_gate("u0", "祭拜海滩神庙获得",
                        "Obtained by worshipping the beach temple", cfg)
    assert not [f for f in findings if f.bug_type == BugType.TERMINOLOGY]


def test_case_exact_entry_still_flags_casing():
    """Proper names opt back in: 落雷 = 'Thunder Strike' with case:exact
    must still reject 'thunder strike'."""
    cfg = GateConfig(source_lang="zh", target_lang="en",
                     locked_terms={"落雷": "Thunder Strike"},
                     term_case={"落雷": "exact"},
                     style_guide=ZH_EN)
    findings = run_gate("u1", "召唤落雷", "Summon a thunder strike", cfg)
    assert [f for f in findings if f.bug_type == BugType.TERMINOLOGY]


def test_wrong_word_flagged_regardless_of_case_policy():
    """Case-blindness must not hide a genuinely wrong word."""
    cfg = GateConfig(source_lang="zh", target_lang="en",
                     locked_terms={"落雷": "Thunder Strike"},
                     style_guide=ZH_EN)
    findings = run_gate("u2", "召唤落雷", "Summon a thunderbolt", cfg)
    assert [f for f in findings if f.bug_type == BugType.TERMINOLOGY]


# --------------------------------------------------- untranslated guard

def test_ascii_source_is_not_untranslated():
    """PE rows 126/136: source 'Error' and 'Text Block' contain no Han —
    there is nothing to translate, so target == source is correct."""
    cfg = GateConfig(source_lang="zh", target_lang="en")
    for src in ("Error", "Text Block"):
        findings = run_gate("u3", src, src, cfg)
        assert not [f for f in findings
                    if f.bug_type == BugType.UNTRANSLATED], src


def test_han_source_left_untranslated_still_flagged():
    """The real defect must survive the guard."""
    cfg = GateConfig(source_lang="zh", target_lang="en")
    findings = run_gate("u4", "距离天亮还有", "距离天亮还有", cfg)
    assert [f for f in findings if f.bug_type == BugType.UNTRANSLATED]


# ---------------------------------------------------- placeholder guard

def test_target_may_restore_placeholder_absent_from_source():
    """PE row 133: source '  距离天亮还有    s' lost its placeholder in
    the game data; target 'Dawn in: {0}s' correctly restores it."""
    cfg = GateConfig(source_lang="zh", target_lang="en")
    findings = run_gate("u5", "  距离天亮还有    s", "Dawn in: {0}s", cfg)
    assert not [f for f in findings if f.bug_type == BugType.PLACEHOLDER]


def test_dropped_placeholder_still_flagged():
    """Losing a placeholder the source HAS is still a high defect."""
    cfg = GateConfig(source_lang="zh", target_lang="en")
    findings = run_gate("u6", "距离天亮还有 {0}s", "Dawn soon", cfg)
    assert [f for f in findings if f.bug_type == BugType.PLACEHOLDER]


def test_changed_placeholder_still_flagged():
    """Substituting a different placeholder is still a defect."""
    cfg = GateConfig(source_lang="zh", target_lang="en")
    findings = run_gate("u7", "还有 {0}s", "Dawn in {1}s", cfg)
    assert [f for f in findings if f.bug_type == BugType.PLACEHOLDER]


# ------------------------------------------------- style guide resolution

def test_regional_source_tag_finds_project_style_guide(tmp_path):
    """A scan passing source_lang='zh' must still find a guide authored as
    'zh-CN-en.json'. Falling back to the built-in starter guide discards
    the project's own CAP-* rules with only a note to show for it — the
    a live run reported casing defects against rules it never loaded.
    """
    from orbit8.project_paths import REFERENCE_DIR, STYLE_SUBDIR
    from orbit8.project_paths import resolve_style_guide
    from orbit8.style_defaults import ZH_EN

    root = tmp_path / "proj"
    style_dir = root / REFERENCE_DIR / STYLE_SUBDIR
    style_dir.mkdir(parents=True)
    ZH_EN.save(style_dir / "zh-CN-en.json")
    (root / "PROJECT.md").write_text("marker", encoding="utf-8")

    guide, notes = resolve_style_guide("zh", "en", project_root=root)
    assert guide is not None
    assert any("zh-CN-en.json" in n for n in notes), notes
