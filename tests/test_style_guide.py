"""Style guides as data: scoping, mechanical enforcement, rendering."""
import json
from pathlib import Path

from orbit8.style_defaults import EN_ZH, ZH_EN, default_guide
from orbit8.style_guide import (StyleGuide, StyleRule, check_mechanical,
                                render_markdown)


def test_rules_are_scoped_by_string_type():
    ui_rules = {r.id for r in ZH_EN.for_domain("ui")}
    dialogue_rules = {r.id for r in ZH_EN.for_domain("dialogue")}
    # a UI-only rule must not apply to dialogue, and vice versa
    assert "ZH-EN-10" in ui_rules and "ZH-EN-10" not in dialogue_rules
    assert "ZH-EN-12" in dialogue_rules and "ZH-EN-12" not in ui_rules
    # global rules apply everywhere
    assert "ZH-EN-01" in ui_rules and "ZH-EN-01" in dialogue_rules


def test_mechanical_checks_catch_real_artifacts():
    # CJK punctuation and leftover Han in an English target
    hits = check_mechanical(ZH_EN, "生命值不足", "Health too low，还有中文")
    ids = {v.rule_id for v in hits}
    assert "ZH-EN-01" in ids and "ZH-EN-02" in ids
    assert all(v.evidence for v in hits)          # evidence always cited
    # a clean target trips nothing
    assert check_mechanical(ZH_EN, "生命值不足", "Health too low") == []


def test_mechanical_checks_for_the_reverse_pair():
    # half-width punctuation + missing space around Latin in zh target
    hits = check_mechanical(EN_ZH, "Gain 100 gold", "获得100金币, 继续")
    ids = {v.rule_id for v in hits}
    assert "EN-ZH-01" in ids            # half-width comma
    assert "EN-ZH-03" in ids            # 获得100 needs a space
    assert check_mechanical(EN_ZH, "Gain gold", "获得 100 金币。") == []


def test_source_parity_rule():
    # source ends a sentence, target doesn't → flagged
    hits = check_mechanical(ZH_EN, "恢复生命值。", "Restore Health")
    assert "ZH-EN-03" in {v.rule_id for v in hits}
    # both are labels → fine
    assert not [v for v in check_mechanical(ZH_EN, "恢复生命值",
                                            "Restore Health")
                if v.rule_id == "ZH-EN-03"]


def test_prompt_shows_llm_and_advisory_rules_with_ids():
    prompt = ZH_EN.render_prompt("dialogue")
    assert "[ZH-EN-12]" in prompt            # llm rule for dialogue
    assert "[ZH-EN-20]" in prompt            # advisory, global
    assert "[ZH-EN-10]" not in prompt        # UI-only, wrong domain
    assert "[ZH-EN-01]" not in prompt        # mechanical: gate, not prompt
    assert "START the message with the rule id" in prompt


def test_roundtrip_and_markdown(tmp_path: Path):
    path = tmp_path / "zh-CN-en.json"
    ZH_EN.save(path)
    raw = json.loads(path.read_text("utf-8"))
    assert raw["metadata"]["by_enforcement"]["mechanical"] >= 3
    reloaded = StyleGuide.load(path)
    assert {r.id for r in reloaded.rules} == {r.id for r in ZH_EN.rules}
    assert check_mechanical(reloaded, "测试", "test，bad") != []
    doc = render_markdown(reloaded)
    assert "ZH-EN-01" in doc and "| enforcement |" in doc


def test_default_guide_lookup_tolerates_locale_form():
    assert default_guide("zh-CN", "en") is ZH_EN
    assert default_guide("zh", "en-US") is ZH_EN
    assert default_guide("en", "zh") is EN_ZH
    assert default_guide("ja", "ko") is None      # not authored yet


def test_unknown_check_kind_never_fires():
    guide = StyleGuide(source_lang="a", target_lang="b", rules=[
        StyleRule(id="X-1", text="nonsense", enforcement="mechanical",
                  check="not_a_real_check", value="x")])
    assert check_mechanical(guide, "src", "tgt") == []


def test_english_morphology_plural_both_directions():
    morph = ZH_EN.morphology
    # locked singular satisfied by a plural target …
    assert morph.matches("Plague Node", "Destroy all Plague Nodes")
    # … and locked plural satisfied by a singular target (the asymmetry
    # that produced false violations before)
    assert morph.matches("Plague Nodes", "Destroy the Plague Node")
    # -y → -ies
    assert morph.matches("Ability", "Two Abilities remain")
    assert morph.matches("Abilities", "One Ability remains")
    # irregulars come from the profile, not from code
    assert morph.matches("Wolf", "three Wolves appear")
    assert morph.matches("Staff", "a rack of Staves")
    # capitalization is not identity
    assert morph.matches("Plague Node", "destroy the plague node")
    # a genuinely different term is still a violation
    assert not morph.matches("Plague Node", "Infection Point")


def test_chinese_morphology_is_exact():
    morph = EN_ZH.morphology
    assert morph.strategy == "none"
    assert morph.matches("瘟疫点", "清除所有瘟疫点")
    assert not morph.matches("瘟疫点", "清除所有感染点")


def test_morphology_survives_roundtrip(tmp_path: Path):
    path = tmp_path / "g.json"
    ZH_EN.save(path)
    raw = json.loads(path.read_text("utf-8"))
    assert raw["morphology"]["strategy"] == "suffix"
    assert "wolf" in raw["morphology"]["irregular"]
    reloaded = StyleGuide.load(path)
    assert reloaded.morphology.matches("Plague Node", "Plague Nodes")
    assert "Morphology" in render_markdown(reloaded)


def test_validate_catches_unrunnable_rules():
    guide = StyleGuide(source_lang="a", target_lang="b", rules=[
        StyleRule(id="A-1", text="ok", enforcement="mechanical",
                  check="forbid_pattern", value="["),        # bad regex
        StyleRule(id="A-2", text="ok", enforcement="mechanical"),  # no check
        StyleRule(id="A-3", text="ok", enforcement="mechanical",
                  check="not_real", value="x"),              # unknown kind
        StyleRule(id="A-4", text="", enforcement="llm"),      # empty text
        StyleRule(id="A-5", text="ok", enforcement="steering"),  # bad bin
        StyleRule(id="A-6", text="ok", enforcement="llm",
                  check="forbid_pattern", value="x"),        # never runs
        StyleRule(id="A-1", text="dup", enforcement="advisory"),
    ])
    problems = " | ".join(guide.validate())
    for expect in ("invalid regex", "need a 'check'", "unknown check",
                   "empty rule text", "unknown enforcement",
                   "will never run", "duplicate rule id"):
        assert expect in problems, expect
    # the shipped guides must themselves be clean
    assert ZH_EN.validate() == [] and EN_ZH.validate() == []


def test_prompt_contract_excludes_mechanical_rules():
    prompt = ZH_EN.render_prompt("ui")
    assert "STYLE GUIDE — zh-CN → en" in prompt and "· ui" in prompt
    assert "START the message with the rule id" in prompt
    mechanical = {r.id for r in ZH_EN.rules
                  if r.enforcement == "mechanical"}
    assert mechanical and not any(f"[{rid}]" in prompt
                                  for rid in mechanical)
    # llm rules for this domain ARE present
    assert "[ZH-EN-10]" in prompt
    # and advisory can be dropped when only the rubric is wanted
    rubric = ZH_EN.render_prompt("ui", include_advisory=False)
    assert "[ZH-EN-20]" not in rubric
