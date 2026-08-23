"""Glossary integrity gate."""
import json
from pathlib import Path

from orbit8.glossary_check import (check_glossary, check_glossary_file,
                                   write_check_report)
from orbit8.style_defaults import EN_MORPHOLOGY


def _g(terms, **meta):
    return {"metadata": meta, "terms": terms}


def test_clean_glossary_passes():
    report = check_glossary(_g({
        "瘟疫点": {"translation": "Plague Point", "locked": True},
        "生命值": {"translation": "Health", "locked": True}}))
    assert report.issues == [] and not report.blocked
    assert report.terms_total == 2 and report.locked_total == 2


def test_undeclared_variant_group_with_competing_rulings_is_an_error():
    """Messy source is normal; UNDECLARED competing rulings are not."""
    report = check_glossary(_g({
        "魔力值": {"translation": "Mana", "locked": True},
        "魔法值": {"translation": "Mana", "locked": True},
        "法力值": {"translation": "Mana", "locked": False}}))
    assert report.blocked
    issue = report.errors[0]
    assert issue.kind == "undeclared_variant_group"
    assert set(issue.terms) == {"魔力值", "魔法值", "法力值"}
    assert "glossary unify" in issue.message


def test_declared_variant_group_is_correct_not_a_defect():
    """One canonical spelling + variant_of pointers = the intended way
    to normalize an inconsistent source."""
    report = check_glossary(_g({
        "魔法值": {"translation": "Mana", "locked": True},
        "魔力值": {"translation": "Mana", "locked": True,
                   "variant_of": "魔法值"},
        "法力值": {"translation": "Mana", "locked": True,
                   "variant_of": "魔法值"}}))
    assert not report.blocked and not report.warnings
    info = [i for i in report.issues if i.kind == "source_variants"]
    assert info and "canonical '魔法值'" in info[0].message


def test_undeclared_group_without_competing_rulings_is_a_warning():
    report = check_glossary(_g({
        "生命值": {"translation": "Health", "locked": True},
        "血量": {"translation": "Health", "locked": False}}))
    assert not report.blocked
    assert report.warnings[0].kind == "undeclared_variant_group"


def test_variant_of_must_point_somewhere_real():
    report = check_glossary(_g({
        "车队": {"translation": "Convoy", "locked": True},
        "马车": {"translation": "Convoy", "locked": True,
                 "variant_of": "运输队"}}))            # not a term
    assert report.blocked
    assert report.errors[0].kind == "dangling_variant_of"


def test_variant_colliding_with_another_terms_rendering():
    report = check_glossary(_g({
        "瘟疫源": {"translation": "Plague Source", "locked": True,
                   "variants": ["Infection Source"]},
        "感染来源": {"translation": "Infection Source", "locked": True}}))
    kinds = {i.kind for i in report.errors}
    assert "variant_collision" in kinds
    assert report.blocked


def test_structural_problems():
    report = check_glossary(_g({
        "长弓/一级长弓": {"translation": "Longbow", "locked": True},
        "空的": {"translation": "  ", "locked": False},
        "Iron": {"translation": "iron", "locked": False}}))
    kinds = {i.kind for i in report.issues}
    assert "unsplit_alias" in kinds            # one key, two aliases
    assert "empty_translation" in kinds        # ERROR
    assert "untranslated" in kinds             # zh == en
    assert report.blocked


def test_rename_chain_must_actually_retire_the_old_term():
    report = check_glossary(_g({
        "车队": {"translation": "Convoy", "locked": True,
                 "supersedes": "马车"},
        "马车": {"translation": "Convoy", "locked": True}}))
    kinds = {i.kind for i in report.issues}
    assert "superseded_still_present" in kinds
    # …and the undeclared group is the deeper contradiction
    assert "undeclared_variant_group" in {i.kind for i in report.errors}


def test_morphological_variant_is_flagged_as_redundant():
    report = check_glossary(_g({
        "瘟疫点": {"translation": "Plague Point", "locked": True,
                   "variants": ["Plague Points"]}}),
        morphology=EN_MORPHOLOGY)
    infos = [i for i in report.issues if i.severity == "INFO"]
    assert any(i.kind == "morphological_variant" for i in infos)
    assert not report.blocked          # harmless, just noise


def test_family_and_locked_consistency():
    report = check_glossary(_g(
        {"致幻陷阱": {"translation": "Hallucinogenic Trap",
                      "locked": False},
         "太刀": {"translation": "Tachi", "locked": True},
         "一级太刀": {"translation": "Lv.1 Longsword", "locked": True}},
        family_rules={"致幻": {"translation": "Hallucination"}}))
    kinds = {i.kind for i in report.issues}
    assert "family_violation" in kinds        # mined term vs 族 rule
    assert "locked_inconsistency" in kinds    # locked term vs locked term


def test_report_files(tmp_path: Path):
    path = tmp_path / "g.json"
    path.write_text(json.dumps(_g({
        "魔力值": {"translation": "Mana", "locked": True},
        "魔法值": {"translation": "Mana", "locked": True}}),
        ensure_ascii=False), encoding="utf-8")
    report = check_glossary_file(path)
    md = write_check_report(report, tmp_path / "out")
    assert "publishable: NO" in md.read_text("utf-8")
    data = json.loads(
        (tmp_path / "out/glossary_check.json").read_text("utf-8"))
    assert data["blocked"] is True and data["counts"]["ERROR"] == 1
