"""PO drop comparison: bucket classification, red flags (lost/stale),
duplicate handling, report files, and the orchestrator compare_po tool."""
import json
from pathlib import Path

from orbit8.po_compare import compare_po, word_diff, write_compare_report

OLD_PO = (
    'msgctxt ",K_UNCHANGED"\nmsgid "Quit"\nmsgstr "退出"\n\n'
    'msgctxt ",K_REMOVED"\nmsgid "Old feature"\nmsgstr "旧功能"\n\n'
    'msgctxt ",K_SRC_CHANGED"\nmsgid "Attack power"\nmsgstr "攻击力"\n\n'
    'msgctxt ",K_STALE"\nmsgid "HP"\nmsgstr "生命值"\n\n'
    'msgctxt ",K_MODIFIED"\nmsgid "Settings"\nmsgstr "设定"\n\n'
    'msgctxt ",K_NEWLY"\nmsgid "Crouch"\nmsgstr ""\n\n'
    'msgctxt ",K_LOST"\nmsgid "Jump"\nmsgstr "跳跃"\n')

NEW_PO = (
    'msgctxt ",K_UNCHANGED"\nmsgid "Quit"\nmsgstr "退出"\n\n'
    'msgctxt ",K_SRC_CHANGED"\nmsgid "Attack damage"\nmsgstr "攻击伤害"\n\n'
    'msgctxt ",K_STALE"\nmsgid "Max HP"\nmsgstr "生命值"\n\n'
    'msgctxt ",K_MODIFIED"\nmsgid "Settings"\nmsgstr "设置"\n\n'
    'msgctxt ",K_NEWLY"\nmsgid "Crouch"\nmsgstr "蹲下"\n\n'
    'msgctxt ",K_LOST"\nmsgid "Jump"\nmsgstr ""\n\n'
    'msgctxt ",K_ADDED"\nmsgid "Sprint"\nmsgstr ""\n')


def _write(tmp_path: Path):
    old = tmp_path / "old.po"
    new = tmp_path / "new.po"
    old.write_text(OLD_PO, encoding="utf-8")
    new.write_text(NEW_PO, encoding="utf-8")
    return old, new


def test_buckets_and_red_flags(tmp_path: Path):
    old, new = _write(tmp_path)
    result = compare_po(old, new)
    assert result.counts() == {
        "added": 1, "removed": 1, "source_changed": 2,
        "translation_modified": 1, "newly_translated": 1,
        "translation_lost": 1, "unchanged": 1, "stale_translations": 1}
    assert result.added[0]["key"] == ",K_ADDED"
    assert result.added[0]["untranslated"] is True
    assert result.removed[0]["key"] == ",K_REMOVED"
    assert result.translation_lost[0]["key"] == ",K_LOST"
    assert result.translation_lost[0]["old_target"] == "跳跃"
    stale = {e["key"]: e.get("stale") for e in result.source_changed}
    # K_STALE: source moved, translation identical → stale.
    # K_SRC_CHANGED: source moved AND translation retranslated → not stale.
    assert stale == {",K_STALE": True, ",K_SRC_CHANGED": False}
    assert result.needs_attention          # lost + stale present
    words = result.word_counts()
    assert words["added"]["source_words"] == 1          # "Sprint"
    assert words["newly_translated"]["translation_words"] == 2  # 蹲下


def test_word_diff_cjk_and_latin():
    # a 2-word edit in a longer sentence counts 2, not the whole string
    assert word_diff("Return to the main city", "Return to the safe city"
                     ) == {"added": 1, "removed": 1}
    assert word_diff("HP", "Max HP") == {"added": 1, "removed": 0}
    # CJK: per-character tokens; 攻击力 → 攻击伤害 keeps 攻击
    assert word_diff("攻击力", "攻击伤害") == {"added": 2, "removed": 1}
    # mixed script, rename inside a sentence
    assert word_diff("夜间SAN值会加速消耗", "夜间精神值会加速消耗"
                     ) == {"added": 2, "removed": 1}
    assert word_diff("same", "same") == {"added": 0, "removed": 0}


def test_work_summary_correspondence(tmp_path: Path):
    """The user-facing question: source words added/edited, target words
    already updated in the drop, and the outstanding target backlog."""
    old, new = _write(tmp_path)
    ws = compare_po(old, new).work_summary()
    # source side: "Sprint" new (1w); two edited strings — "HP"→"Max HP"
    # (+1) and "Attack power"→"Attack damage" (+1/-1)
    assert ws["source"]["new"] == {"entries": 1, "words": 1}
    assert ws["source"]["edited"] == {"entries": 2, "words_added": 2,
                                      "words_removed": 1}
    assert ws["source"]["removed"] == {"entries": 1, "words": 2}
    # target already updated in the same drop: 攻击伤害 retranslation (+2),
    # 设定→设置 (+1), newly translated 蹲下 (+2)
    assert ws["target_updated"] == {"entries": 3, "words_added": 5,
                                    "words_removed": 2}
    # outstanding: 1 new untranslated (Sprint), 1 stale ("Max HP": 2 words
    # re-review scope, 1 word minimal edit), 1 lost (Jump)
    out = ws["target_outstanding"]
    assert out["new_strings"] == {"entries": 1, "source_words": 1}
    assert out["stale_translations"] == {"entries": 1,
                                         "source_words_full": 2,
                                         "source_words_edited": 1}
    assert out["lost_translations"] == {"entries": 1, "source_words": 1}
    # of 2 source-edited strings, 1 was retranslated, 1 went stale
    assert ws["correspondence"] == {"source_edited_entries": 2,
                                    "target_also_updated": 1,
                                    "target_stale": 1}


def test_no_attention_when_clean(tmp_path: Path):
    old = tmp_path / "a.po"
    new = tmp_path / "b.po"
    old.write_text('msgctxt "K"\nmsgid "Hi"\nmsgstr ""\n', encoding="utf-8")
    new.write_text('msgctxt "K"\nmsgid "Hi"\nmsgstr "你好"\n',
                   encoding="utf-8")
    result = compare_po(old, new)
    assert not result.needs_attention
    assert result.counts()["newly_translated"] == 1


def test_duplicate_keys_reported(tmp_path: Path):
    old = tmp_path / "a.po"
    new = tmp_path / "b.po"
    old.write_text('msgctxt "K"\nmsgid "A"\nmsgstr "甲"\n', encoding="utf-8")
    new.write_text('msgctxt "K"\nmsgid "A"\nmsgstr "甲"\n\n'
                   'msgctxt "K"\nmsgid "A"\nmsgstr "乙"\n', encoding="utf-8")
    result = compare_po(old, new)
    assert result.duplicate_keys == ["K"]
    # last occurrence wins → counts as modified, matching engine import
    assert result.counts()["translation_modified"] == 1


def test_report_files(tmp_path: Path):
    old, new = _write(tmp_path)
    result = compare_po(old, new)
    md = write_compare_report(result, tmp_path / "out")
    assert md.name == "new_compare.md"
    text = md.read_text(encoding="utf-8")
    assert "Needs attention" in text
    assert "Translations LOST" in text and ",K_LOST" in text
    assert "Stale translations" in text and ",K_STALE" in text
    data = json.loads((tmp_path / "out" / "new_compare.json").read_text())
    assert data["counts"]["translation_lost"] == 1
    assert data["needs_attention"] is True


def test_orchestrator_compare_po_tool(tmp_path: Path):
    from orbit8.controller import Job
    from orbit8.orchestrator import ChatOrchestrator
    from orbit8.schemas import IntakeBrief

    class ScriptedProvider:
        name, model, tokens_spent = "scripted", "test", 0.0

        def __init__(self, outputs):
            self.outputs = list(outputs)

        def complete(self, system, user, *, temperature=0.3,
                     max_tokens=2000):
            return self.outputs.pop(0)

    project = tmp_path / "projectX"
    received = project / "10-received"
    received.mkdir(parents=True)
    old, new = (received / "old.po"), (received / "new.po")
    old.write_text(OLD_PO, encoding="utf-8")
    new.write_text(NEW_PO, encoding="utf-8")
    src = project / "s.json"
    src.write_text(json.dumps({"K": "x"}), encoding="utf-8")
    job = Job.init(project / "20-work", "j1",
                   intake=IntakeBrief(game="G", source_lang="en",
                                      target_locales=["zh-CN"]),
                   source_files=[str(src)])
    provider = ScriptedProvider([
        json.dumps({"tool": "compare_po",
                    "args": {"old": str(old), "new": str(new)}}),
        json.dumps({"tool": "respond", "args": {},
                    "message": "1 translation lost, 1 stale — review "
                               "before ingesting."}),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian")
    reply = chat.turn("we received an updated po, compare with previous")
    assert "lost" in reply
