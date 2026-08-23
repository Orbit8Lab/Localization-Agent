"""Round merge: filled Glossary PE form + previous draft glossary."""
from pathlib import Path

import openpyxl
import pytest

from orbit8.glossary_merge import (load_draft_xlsx, merge_round,
                                   read_glossary_pe_form,
                                   write_merge_outputs)
from orbit8.standards import GLOSSARY_PE_FORM_HEADERS


def _review_xlsx(tmp_path: Path, rows) -> Path:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Glossary PE"
    sheet.append(list(GLOSSARY_PE_FORM_HEADERS))
    for row in rows:
        sheet.append([row.get(h, "") for h in GLOSSARY_PE_FORM_HEADERS])
    path = tmp_path / "review.xlsx"
    book.save(path)
    return path


def _draft_xlsx(tmp_path: Path) -> Path:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "术语表 Glossary"
    sheet.append(["分类 Category", "中文 Chinese", "英文 English", "状态"])
    sheet.append(["阵营", "好人/秘火使徒", "Secret Fire Apostle", "已确认"])
    sheet.append([None, "狼人", "Werewolf", "已确认"])       # superseded
    sheet.append(["物品", "回血药水", "Healing Potion", "已确认"])  # drift
    sheet.append([None, "铁匠铺", "Blacksmith", "已确认"])   # agrees
    sheet.append([None, "月光石", "Moonstone", ""])          # new, unconfirmed
    path = tmp_path / "draft.xlsx"
    book.save(path)
    return path


CURRENT_T1 = {
    "metadata": {"game": "ExampleGame", "locale": "en"},
    "terms": {
        "冥界使徒": {"translation": "Underworld Apostle",
                     "type": "decision", "locked": True,
                     "evidence": "J-02", "supersedes": "狼人"},
        "铁匠铺": {"translation": "Blacksmith", "type": "mined",
                   "locked": False, "evidence": "corpus ×4"},
        "回血药水": {"translation": "Health Potion", "type": "mined",
                     "locked": False, "evidence": "corpus ×6"},
    }}


def test_read_form_validates_decisions(tmp_path: Path):
    path = _review_xlsx(tmp_path, [
        {"TermID": "合成", "EntryType": "Conflict", "Source": "合成",
         "PE_Decision": "Reject&Modification",
         "PE_Modification": "Crafting"},
        {"TermID": "道具", "EntryType": "Conflict", "Source": "道具",
         "PE_Decision": "Reject&Modification"},        # missing MOD
        {"TermID": "太刀", "EntryType": "Violation", "Source": "太刀",
         "Target_Suggested": "Tachi", "PE_Decision": ""},  # blank ok
    ])
    rows, problems = read_glossary_pe_form(path)
    assert len(rows) == 3
    assert len(problems) == 1
    assert problems[0]["Source"] == "道具"
    assert "PE_Modification" in problems[0]["problem"]


def test_load_draft_fill_down_and_aliases(tmp_path: Path):
    entries = load_draft_xlsx(_draft_xlsx(tmp_path))
    by_zh = {e["zh"]: e for e in entries}
    assert by_zh["好人"]["en"] == "Secret Fire Apostle"
    assert by_zh["秘火使徒"]["alias_of"] == "好人"
    assert by_zh["狼人"]["category"] == "阵营"     # fill-down
    assert by_zh["回血药水"]["category"] == "物品"
    assert by_zh["月光石"]["confirmed"] is False


def test_merge_precedence_and_supersede(tmp_path: Path):
    rows, _ = read_glossary_pe_form(_review_xlsx(tmp_path, [
        {"TermID": "合成", "EntryType": "Conflict", "Source": "合成",
         "PE_Decision": "Reject&Modification",
         "PE_Modification": "Crafting"},
        {"TermID": "道具", "EntryType": "Conflict", "Source": "道具",
         "PE_Decision": "Reject&Modification"},        # incomplete
    ]))
    draft = load_draft_xlsx(_draft_xlsx(tmp_path))
    glossary, delta = merge_round(CURRENT_T1, rows, draft,
                                  round_id="R2-20260802")
    terms = glossary["terms"]
    # review decision → locked with round provenance
    assert terms["合成"]["locked"] and \
        terms["合成"]["translation"] == "Crafting"
    assert delta["locked_from_review"] == [
        {"zh": "合成", "en": "Crafting"}]
    # incomplete decision reported, not guessed
    assert delta["incomplete"][0]["zh"] == "道具"
    assert "道具" not in terms
    # rename chain: draft 狼人 retired by 冥界使徒's supersedes
    assert "狼人" not in terms
    assert any(e["zh"] == "狼人" and e["by"] == "冥界使徒"
               for e in delta["draft_superseded"])
    # draft agrees with mined → evidence note, still one entry
    assert "draft agrees" in terms["铁匠铺"]["evidence"]
    # confirmed draft beats mined, corpus drift flagged
    assert terms["回血药水"]["translation"] == "Healing Potion"
    assert "corpus drift" in terms["回血药水"]["check"]
    # new draft terms carried with category
    assert terms["月光石"]["translation"] == "Moonstone"
    assert terms["好人"]["category"] == "阵营"
    # locked terms sorted first
    assert list(terms)[0] in ("冥界使徒", "合成")


def test_write_outputs(tmp_path: Path):
    rows, _ = read_glossary_pe_form(_review_xlsx(tmp_path, [
        {"TermID": "合成", "EntryType": "Conflict", "Source": "合成",
         "PE_Decision": "Reject&Modification",
         "PE_Modification": "Crafting"}]))
    glossary, delta = merge_round(
        CURRENT_T1, rows, load_draft_xlsx(_draft_xlsx(tmp_path)),
        round_id="R2")
    md = write_merge_outputs(glossary, delta, tmp_path / "out")
    assert (tmp_path / "out/glossary_terms.json").exists()
    assert (tmp_path / "out/glossary_terms.xlsx").exists()
    assert "locked_from_review | 1" in md.read_text()
