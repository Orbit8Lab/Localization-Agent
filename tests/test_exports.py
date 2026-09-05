"""Exporters + the orchestrator's file/standardize tools (scripted LLM)."""
import json
from pathlib import Path

import pytest

from orbit8.controller import Job
from orbit8.exports import (emit_bilingual_jsonl, emit_flat_json,
                            read_po_entries)
from orbit8.orchestrator import ChatOrchestrator
from orbit8.schemas import IntakeBrief, SourceString

ZH_PO = '''msgctxt "K1"
msgid "New journal entry added."
msgstr "已添加新的日志条目。"

msgctxt "K2"
msgid "Press any key"
msgstr ""

msgctxt "K3"
msgid "Multi"
"-line source"
msgstr "多行"
"目标"
'''


def test_read_po_entries_and_bilingual(tmp_path: Path):
    po = tmp_path / "Chinese.po"
    po.write_text(ZH_PO, encoding="utf-8")
    entries = read_po_entries(po)
    assert entries[2] == ("K3", "Multi-line source", "多行目标", "")
    out = tmp_path / "pairs.jsonl"
    written, empty = emit_bilingual_jsonl([po], out, source_lang="en",
                                          target_lang="zh-CN")
    # K2 (empty msgstr) is INCLUDED with empty target — untranslated
    # strings must reach LQA, not vanish at export
    assert (written, empty) == (3, 1)
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert rows[0] == {"key": "K1", "source_language": "en",
                       "target_language": "zh-CN",
                       "source_text": "New journal entry added.",
                       "target_text": "已添加新的日志条目。"}
    assert rows[1]["key"] == "K2" and rows[1]["target_text"] == ""


def test_bilingual_carries_ue_locations(tmp_path: Path):
    po = tmp_path / "Chinese.po"
    po.write_text(
        '#. Key:\tAAAA\n'
        '#: /Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.TextBlock_0.Text\n'
        'msgctxt ",AAAA"\nmsgid "BACK"\nmsgstr "返回"\n\n'
        'msgctxt ",BBBB"\nmsgid "Quit"\nmsgstr "退出"\n', encoding="utf-8")
    entries = read_po_entries(po)
    assert entries[0] == (",AAAA", "BACK", "返回",
                          "/Game/UI/WDG_Menu.WDG_Menu_C:"
                          "WidgetTree.TextBlock_0.Text")
    assert entries[1][3] == ""
    out = tmp_path / "pairs.jsonl"
    emit_bilingual_jsonl([po], out, source_lang="en", target_lang="zh-CN")
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    assert rows[0]["location"].startswith("/Game/UI/WDG_Menu")
    assert "location" not in rows[1]          # absent, never empty-string


def test_bilingual_rejects_source_language_po(tmp_path: Path):
    po = tmp_path / "English.po"
    po.write_text('msgctxt "K1"\nmsgid "Hello"\nmsgstr "Hello"\n\n'
                  'msgctxt "K2"\nmsgid "Bye"\nmsgstr "Bye"\n',
                  encoding="utf-8")
    with pytest.raises(ValueError, match="source-language file"):
        emit_bilingual_jsonl([po], tmp_path / "p.jsonl",
                             source_lang="en", target_lang="zh-CN")
    assert not (tmp_path / "p.jsonl").exists()   # nothing half-written


def test_flat_json_duplicate_conflict(tmp_path: Path):
    with pytest.raises(ValueError, match="duplicate key"):
        emit_flat_json([SourceString(key="A", text="x"),
                        SourceString(key="A", text="y")],
                       tmp_path / "f.json")


class ScriptedProvider:
    name, model, tokens_spent = "scripted", "test", 0.0

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.prompts.append(user)
        return self.outputs.pop(0)


def _call(tool, args=None, message=None):
    return json.dumps({"tool": tool, "args": args or {},
                       "message": message})


def test_agent_inspects_then_standardizes(tmp_path: Path):
    """The full loop the operator asked for: agent peeks at the .po, sees
    filled msgstr, exports bilingual JSONL — via tools, inside the project
    boundary."""
    project = tmp_path / "projectX"
    received = project / "10-received"
    received.mkdir(parents=True)
    (received / "Chinese.po").write_text(ZH_PO, encoding="utf-8")
    src = project / "s.json"
    src.write_text(json.dumps({"K1": "hello"}), encoding="utf-8")
    intake = IntakeBrief(game="G", source_lang="en",
                         target_locales=["zh-CN"])
    job = Job.init(project / "20-work", "j1", intake=intake,
                   source_files=[str(src)])

    po_path = str(received / "Chinese.po")
    provider = ScriptedProvider([
        _call("inspect_file", {"path": po_path}),
        _call("standardize", {"files": [po_path],
                              "output": "bilingual_jsonl"}),
        _call("respond", message="Exported 2 bilingual pairs; K2 is "
                                 "untranslated."),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian", dry_run=True)
    reply = chat.turn("standardize the developer files for LQA")
    assert "untranslated" in reply
    assert '"msgstr_filled": 2' in provider.prompts[1]     # it saw the data
    out = job.store.job_dir / "exports" / "pairs_en-zh-CN.jsonl"
    assert out.exists() and len(out.read_text().splitlines()) == 3


def test_file_tools_confined_to_project(tmp_path: Path):
    project = tmp_path / "projectX"
    src = project / "s.json"
    project.mkdir()
    src.write_text(json.dumps({"K": "t"}), encoding="utf-8")
    intake = IntakeBrief(game="G", source_lang="en",
                         target_locales=["zh-CN"])
    job = Job.init(project / "20-work", "j1", intake=intake,
                   source_files=[str(src)])
    provider = ScriptedProvider([
        _call("inspect_file", {"path": "/etc/passwd"}),
        _call("respond", message="cannot read that"),
    ])
    chat = ChatOrchestrator(job, provider, operator="t", dry_run=True)
    chat.turn("read /etc/passwd")
    # Assert the REFUSAL and the path, not the wording: the boundary moved
    # from "the project folder" to "the organization workspace"
    # (tenancy.py) and the message moved with it, but a path outside both
    # must still be refused and the model must be told which path failed.
    observation = provider.prompts[1]
    assert "error" in observation.lower()
    assert "/etc/passwd" in observation
    assert "outside" in observation


def test_an_input_file_path_is_not_a_location(tmp_path: Path):
    """A generated adapter has no widget information, and some fill
    `location` with the source workbook's own path. It is identical on
    every row, useless to a developer, and it rode into the client bug
    report's String ID column carrying our internal directory layout:

        u0525 :: /workspace/project/project004-Nomori/10-received/
                 20260829-drop/Nomori_Yarn (Source).xlsx :: line:04b3c45
    """
    from orbit8.exports import _is_file_path
    assert _is_file_path("/workspace/project/p004/10-received/Src.xlsx")
    assert _is_file_path("C:\\clients\\Strings.csv")
    assert _is_file_path("strings.po")


def test_a_real_ue_reference_survives(tmp_path: Path):
    """The discriminator is the DOCUMENT EXTENSION, not a leading slash:
    a UE reference is absolute too, so "starts with /" would have
    discarded exactly the locations this column exists for."""
    from orbit8.exports import _is_file_path
    assert not _is_file_path(
        "/Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.TextBlock_0.Text")
    assert not _is_file_path("Content/UI/Menu.uasset")
    assert not _is_file_path("WBP_Inventory/Text_Qty")
