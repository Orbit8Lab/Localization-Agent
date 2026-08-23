"""Post-editing delivery: decision parsing, key matching across all
Location/String ID shapes, surgical .po patching, conflict/undecided
handling, TM write-back, and the orchestrator deliver_po tool."""
import json
from pathlib import Path

import openpyxl
import pytest

from orbit8.memory import TranslationMemory
from orbit8.po_patch import (deliver_from_review, match_keys,
                             normalize_decision, patch_po_file,
                             patch_po_text)

UE_PO = (
    '#. Key:\tAAAA1111AAAA1111AAAA1111AAAA1111\n'
    '#: /Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.Back.Text\n'
    'msgctxt ",AAAA1111AAAA1111AAAA1111AAAA1111"\n'
    'msgid "BACK"\nmsgstr ""\n\n'
    '#: /Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.Lore.Text\n'
    'msgctxt ",BBBB2222BBBB2222BBBB2222BBBB2222"\n'
    'msgid "Line one.\\r\\nLine two."\nmsgstr "旧一\\r\\n旧二"\n\n'
    'msgctxt ",CCCC3333CCCC3333CCCC3333CCCC3333"\n'
    'msgid "Quit"\nmsgstr "退出"\n\n'
    'msgctxt ",DDDD4444DDDD4444DDDD4444DDDD4444"\n'
    'msgid "Settings"\nmsgstr "设置"\n')


def _review_xlsx(path: Path, rows: list) -> Path:
    headers = ["Bug#", "Location/String ID", "Source Text",
               "Current Translation",
               "Expected Result / Suggested Translation", "Severity",
               "Decision", "Modify Version"]
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(h, "") for h in headers])
    book.save(path)
    return path


def test_normalize_decision_tolerates_freeform():
    assert normalize_decision("Accept") == "accept"
    assert normalize_decision("Decline & Modify ") == "modify"     # sic
    assert normalize_decision("Decline & Keep-as-it") == "keep"
    assert normalize_decision(None) == "undecided"
    assert normalize_decision("  ") == "undecided"


def test_match_keys_all_id_shapes():
    keys = {",AAAA1111AAAA1111AAAA1111AAAA1111",
            ",BBBB2222BBBB2222BBBB2222BBBB2222", "K1"}
    # pipeline: <path> :: <GUID>
    assert match_keys("/Game/UI/X.Text :: AAAA1111AAAA1111AAAA1111AAAA1111",
                      keys) == [",AAAA1111AAAA1111AAAA1111AAAA1111"]
    # agent: uid :: <path> :: <key>
    assert match_keys(
        "u0001 :: /Game/UI/X :: ,BBBB2222BBBB2222BBBB2222BBBB2222",
        keys) == [",BBBB2222BBBB2222BBBB2222BBBB2222"]
    # legacy merged uid :: ,G1,,G2
    assert set(match_keys(
        "u0001 :: ,AAAA1111AAAA1111AAAA1111AAAA1111,"
        ",BBBB2222BBBB2222BBBB2222BBBB2222", keys)) == {
            ",AAAA1111AAAA1111AAAA1111AAAA1111",
            ",BBBB2222BBBB2222BBBB2222BBBB2222"}
    assert match_keys("u9 :: K1", keys) == ["K1"]
    assert match_keys("nothing here", keys) == []


def test_patch_po_is_surgical_and_keeps_crlf_convention():
    patched, applied = patch_po_text(UE_PO, {
        ",AAAA1111AAAA1111AAAA1111AAAA1111": "返回",
        ",BBBB2222BBBB2222BBBB2222BBBB2222": "新一\n新二"})
    assert applied == [",AAAA1111AAAA1111AAAA1111AAAA1111",
                       ",BBBB2222BBBB2222BBBB2222BBBB2222"]
    assert 'msgstr "返回"' in patched
    assert 'msgstr "新一\\r\\n新二"' in patched     # file convention: \r\n
    assert 'msgstr "退出"' in patched               # untouched entries stay
    assert '#: /Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.Back.Text' in patched
    # nothing else changed
    assert patched.count("msgctxt") == 4


def test_patch_stream_byte_fidelity(tmp_path: Path):
    """The structural guarantee: untouched lines are forwarded verbatim —
    BOM, CRLF line endings, odd spacing all survive byte-for-byte, and
    the source file itself is never modified."""
    raw = ("﻿# header comment\r\n"
           'msgid ""\r\n'
           'msgstr ""\r\n'
           '"Project-Id-Version: X\\n"\r\n'
           "\r\n"
           'msgctxt ",AAAA1111AAAA1111AAAA1111AAAA1111"\r\n'
           'msgid "BACK"\r\n'
           'msgstr ""\r\n'
           "\r\n"
           'msgctxt ",CCCC3333CCCC3333CCCC3333CCCC3333"\r\n'
           'msgid "Quit"\r\n'
           'msgstr "退出"\r\n').encode("utf-8")
    src = tmp_path / "crlf.po"
    src.write_bytes(raw)
    dst = tmp_path / "out.po"
    applied = patch_po_file(src, dst,
                            {",AAAA1111AAAA1111AAAA1111AAAA1111": "返回"})
    assert applied == [",AAAA1111AAAA1111AAAA1111AAAA1111"]
    assert src.read_bytes() == raw                 # source never written
    out = dst.read_bytes()
    expected = raw.replace(
        'msgid "BACK"\r\nmsgstr ""'.encode("utf-8"),
        'msgid "BACK"\r\nmsgstr "返回"'.encode("utf-8"))
    assert out == expected            # every other byte identical, incl.
    assert out.startswith(b"\xef\xbb\xbf")          # BOM
    assert out.count(b"\r\n") == raw.count(b"\r\n")  # CRLF endings


def test_deliver_applies_decisions_and_reports(tmp_path: Path):
    po = tmp_path / "Chinese.po"
    po.write_text(UE_PO, encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "/Game/UI/... :: AAAA1111AAAA1111AAAA1111AAAA1111",
         "Source Text": "BACK",
         "Expected Result / Suggested Translation": "返回",
         "Decision": "Accept"},
        {"Bug#": 2, "Location/String ID":
            "/Game/UI/... :: BBBB2222BBBB2222BBBB2222BBBB2222",
         "Source Text": "Line one...",
         "Expected Result / Suggested Translation": "建议版",
         "Decision": "Decline & Modify ", "Modify Version": "修订一\n修订二"},
        {"Bug#": 3, "Location/String ID":
            "/Game/UI/... :: CCCC3333CCCC3333CCCC3333CCCC3333",
         "Source Text": "Quit",
         "Expected Result / Suggested Translation": "退出游戏",
         "Decision": "Decline & Keep-as-it"},
        {"Bug#": 4, "Location/String ID":
            "/Game/UI/... :: DDDD4444DDDD4444DDDD4444DDDD4444",
         "Source Text": "Settings",
         "Expected Result / Suggested Translation": "设置项",
         "Decision": ""},                                    # undecided
        {"Bug#": 5, "Location/String ID": "/Game/Unknown :: FFFF",
         "Source Text": "Ghost",
         "Expected Result / Suggested Translation": "鬼",
         "Decision": "Accept"},                              # unmatched
    ])
    tm = TranslationMemory(tmp_path / "tm.db")
    report = deliver_from_review(review, [po], tmp_path / "deliver",
                                 timestamp="20260801", tm=tm,
                                 locale="zh-CN")
    assert report.counts() == {"applied": 2, "kept": 1, "undecided": 1,
                               "unmatched": 1, "conflicts": 0,
                               "inconsistent_sources": 0}
    out = Path(report.delivery_dir)
    assert out.name == "20260801-po-delivery"
    text = (out / "Chinese.po").read_text(encoding="utf-8")
    assert 'msgstr "返回"' in text                    # Accept → suggestion
    assert 'msgstr "修订一\\r\\n修订二"' in text       # Modify OVERRIDES
    assert "建议版" not in text
    assert 'msgstr "退出"' in text                    # keep + undecided
    assert 'msgstr "设置"' in text
    assert (out / "DELIVERY_REPORT.md").exists()
    data = json.loads((out / "DELIVERY_REPORT.json").read_text())
    assert data["counts"]["applied"] == 2
    # human decisions became TM ground truth
    assert tm.lookup("BACK", "zh-CN") == "返回"
    assert tm.lookup("Line one.\r\nLine two.", "zh-CN") == "修订一\n修订二"


def test_deliver_conflicting_decisions_apply_nothing(tmp_path: Path):
    po = tmp_path / "Chinese.po"
    po.write_text(UE_PO, encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: CCCC3333CCCC3333CCCC3333CCCC3333",
         "Expected Result / Suggested Translation": "版本甲",
         "Decision": "Accept"},
        {"Bug#": 2, "Location/String ID":
            "y :: CCCC3333CCCC3333CCCC3333CCCC3333",
         "Expected Result / Suggested Translation": "版本乙",
         "Decision": "Accept"},
    ])
    report = deliver_from_review(review, [po], tmp_path / "d",
                                 timestamp="20260801")
    assert report.counts()["conflicts"] == 1
    assert report.counts()["applied"] == 0
    text = (Path(report.delivery_dir) / "Chinese.po").read_text()
    assert 'msgstr "退出"' in text            # original kept on conflict


def test_modify_without_text_is_surfaced_not_guessed(tmp_path: Path):
    po = tmp_path / "Chinese.po"
    po.write_text(UE_PO, encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: AAAA1111AAAA1111AAAA1111AAAA1111",
         "Expected Result / Suggested Translation": "返回",
         "Decision": "Decline & Modify", "Modify Version": ""},
    ])
    report = deliver_from_review(review, [po], tmp_path / "d",
                                 timestamp="20260801")
    assert report.counts()["undecided"] == 1
    assert "Modify Version is empty" in report.undecided[0]["note"]
    text = (Path(report.delivery_dir) / "Chinese.po").read_text()
    assert 'msgstr ""' in text                # untouched


def test_split_decisions_on_duplicate_source_warn(tmp_path: Path):
    """Reviewers accepting one location and modifying another of the SAME
    source ships two renderings — applied faithfully, but warned."""
    po = tmp_path / "Chinese.po"
    po.write_text(
        'msgctxt ",AAAA1111AAAA1111AAAA1111AAAA1111"\n'
        'msgid "Crouch"\nmsgstr ""\n\n'
        'msgctxt ",BBBB2222BBBB2222BBBB2222BBBB2222"\n'
        'msgid "Crouch"\nmsgstr ""\n', encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: AAAA1111AAAA1111AAAA1111AAAA1111",
         "Expected Result / Suggested Translation": "下蹲",
         "Decision": "Accept"},
        {"Bug#": 2, "Location/String ID":
            "y :: BBBB2222BBBB2222BBBB2222BBBB2222",
         "Expected Result / Suggested Translation": "下蹲",
         "Decision": "Decline & Modify", "Modify Version": "蹲下"},
    ])
    report = deliver_from_review(review, [po], tmp_path / "d",
                                 timestamp="20260801")
    assert report.counts()["applied"] == 2
    assert report.counts()["conflicts"] == 0        # different keys: legal
    assert report.counts()["inconsistent_sources"] == 1
    assert report.inconsistent[0]["source"] == "Crouch"
    assert set(report.inconsistent[0]["renderings"]) == {"下蹲", "蹲下"}
    md = (Path(report.delivery_dir) / "DELIVERY_REPORT.md").read_text()
    assert "Consistency warnings" in md


HEADERED_PO = (
    "\ufeff# ExampleGame zh-CN\n"
    'msgid ""\n'
    'msgstr ""\n'
    '"Project-Id-Version: SOK\\n"\n'
    '"POT-Creation-Date: 2026-07-16 02:56\\n"\n'
    '"PO-Revision-Date: 2026-07-16 02:56\\n"\n'
    '"Language-Team: \\n"\n'
    '"Language: zh-Hans\\n"\n'
    '"MIME-Version: 1.0\\n"\n'
    '"Content-Type: text/plain; charset=UTF-8\\n"\n'
    '"Content-Transfer-Encoding: 8bit\\n"\n'
    "\n"
    'msgctxt ",AAAA1111AAAA1111AAAA1111AAAA1111"\n'
    'msgid "BACK"\nmsgstr ""\n\n'
    'msgctxt ",CCCC3333CCCC3333CCCC3333CCCC3333"\n'
    'msgid "Quit"\nmsgstr "退出"\n')


def test_sanity_gate_and_relabel_pass(tmp_path: Path):
    """Default flow on a well-formed file: header labels refreshed
    (revision date + Orbit8 branding), po_sanity passes, not blocked."""
    po = tmp_path / "Chinese.po"
    po.write_text(HEADERED_PO, encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: AAAA1111AAAA1111AAAA1111AAAA1111",
         "Expected Result / Suggested Translation": "返回",
         "Decision": "Accept"},
    ])
    report = deliver_from_review(review, [po], tmp_path / "d",
                                 timestamp="20260801")
    assert not report.blocked
    result = report.sanity["Chinese.po"]
    assert result["verdict"].startswith("PASS"), result["error_details"]
    assert result["summary"]["newly_translated"] == 1
    labels = report.relabeled["Chinese.po"]
    assert labels["Language-Team"] == "Orbit8 Lab"
    assert "PO-Revision-Date" in labels
    text = (Path(report.delivery_dir) / "Chinese.po").read_text(
        encoding="utf-8")
    assert '"Language-Team: Orbit8 Lab\\n"' in text
    assert '"PO-Revision-Date: 2026-07-16 02:56\\n"' not in text
    assert '"Last-Translator: Orbit8 Lab\\n"' in text
    assert text.count("msgctxt") == 2          # entries untouched


def test_sanity_gate_blocks_leaked_review_note(tmp_path: Path):
    """A reviewer annotation pasted into Modify Version must not ship:
    the gate flags it as an ERROR and the delivery is blocked."""
    po = tmp_path / "Chinese.po"
    po.write_text(HEADERED_PO, encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: CCCC3333CCCC3333CCCC3333CCCC3333",
         "Expected Result / Suggested Translation": "退出游戏",
         "Decision": "Decline & Modify",
         "Modify Version": "待定，需要跟开发确认"},
    ])
    report = deliver_from_review(review, [po], tmp_path / "d",
                                 timestamp="20260801")
    assert report.blocked
    result = report.sanity["Chinese.po"]
    assert result["verdict"].startswith("FAIL")
    assert any("reviewer/QA note" in detail
               for detail in result["error_details"])
    md = (Path(report.delivery_dir) / "DELIVERY_REPORT.md").read_text()
    assert "DO NOT DELIVER" in md


def test_no_relabel_keeps_header_bytes(tmp_path: Path):
    po = tmp_path / "Chinese.po"
    po.write_text(HEADERED_PO, encoding="utf-8")
    review = _review_xlsx(tmp_path / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: AAAA1111AAAA1111AAAA1111AAAA1111",
         "Expected Result / Suggested Translation": "返回",
         "Decision": "Accept"},
    ])
    report = deliver_from_review(review, [po], tmp_path / "d",
                                 timestamp="20260801", relabel=False)
    text = (Path(report.delivery_dir) / "Chinese.po").read_text(
        encoding="utf-8")
    assert '"PO-Revision-Date: 2026-07-16 02:56\\n"' in text
    assert "Orbit8 Lab" not in text
    assert report.relabeled == {}
    # unbranded + stale label now correctly FAILS the gate
    assert report.blocked


def test_orchestrator_deliver_po_tool(tmp_path: Path):
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

    def _call(tool, args=None, message=None):
        return json.dumps({"tool": tool, "args": args or {},
                           "message": message})

    project = tmp_path / "projectX"
    received = project / "10-received"
    received.mkdir(parents=True)
    po = received / "Chinese.po"
    po.write_text(UE_PO, encoding="utf-8")
    review = _review_xlsx(received / "review.xlsx", [
        {"Bug#": 1, "Location/String ID":
            "x :: AAAA1111AAAA1111AAAA1111AAAA1111",
         "Expected Result / Suggested Translation": "返回",
         "Decision": "Accept"},
    ])
    src = project / "s.json"
    src.write_text(json.dumps({"K": "x"}), encoding="utf-8")
    job = Job.init(project / "20-work", "j1",
                   intake=IntakeBrief(game="G", source_lang="en",
                                      target_locales=["zh-CN"]),
                   source_files=[str(src)])
    provider = ScriptedProvider([
        _call("deliver_po", {"review": str(review),
                             "po_files": [str(po)],
                             "timestamp": "20260801"}),
        _call("respond", message="Delivered 1 accepted fix."),
    ])
    chat = ChatOrchestrator(job, provider, operator="tian")
    reply = chat.turn("apply the post-editing decisions and deliver the po")
    assert "Delivered" in reply
    delivered = (project / "30-deliverables" / "20260801-po-delivery"
                 / "Chinese.po")
    assert delivered.exists()
    assert 'msgstr "返回"' in delivered.read_text(encoding="utf-8")


def _mtpe_form(path: Path, rows: list) -> Path:
    """Filled standard MTPE form (standards.MTPE_FORM_HEADERS)."""
    from orbit8.standards import MTPE_FORM_HEADERS
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "MTPE"
    sheet.append(list(MTPE_FORM_HEADERS))
    for row in rows:
        sheet.append([row.get(h, "") for h in MTPE_FORM_HEADERS])
    book.save(path)
    return path


def test_parse_review_accepts_filled_mtpe_form(tmp_path: Path):
    from orbit8.po_patch import parse_review
    path = _mtpe_form(tmp_path / "mtpe.xlsx", [
        {"StringID": ",AAAA1111AAAA1111AAAA1111AAAA1111",
         "Source": "BACK", "Target_MT": "返回",
         "PE_Decision": "Accept Translation"},
        {"StringID": ",CCCC3333CCCC3333CCCC3333CCCC3333",
         "Source": "Quit", "Target_MT": "退出",
         "PE_Decision": "Reject&Modification",
         "PE_Modification": "离开游戏", "PE_Note": "更自然"},
        {"StringID": ",DDDD4444DDDD4444DDDD4444DDDD4444",
         "Source": "Settings", "Target_MT": "设定",
         "PE_Decision": "Reject&Keep-as-it-is"},
        {"StringID": ",BBBB2222BBBB2222BBBB2222BBBB2222",
         "Source": "Line one.", "Target_MT": "一",
         "PE_Decision": "Reject&Cannot Answer",
         "PE_Query": "这个词指什么？"},
        {"StringID": ",EEEE5555EEEE5555EEEE5555EEEE5555",
         "Source": "Later", "Target_MT": "稍后"},          # blank decision
    ])
    rows = parse_review(path)
    by_id = {r.location_id: r for r in rows}
    assert by_id[",AAAA1111AAAA1111AAAA1111AAAA1111"].decision == "accept"
    assert by_id[",AAAA1111AAAA1111AAAA1111AAAA1111"].replacement == "返回"
    assert by_id[",CCCC3333CCCC3333CCCC3333CCCC3333"].decision == "modify"
    assert by_id[",CCCC3333CCCC3333CCCC3333CCCC3333"].replacement == "离开游戏"
    assert by_id[",DDDD4444DDDD4444DDDD4444DDDD4444"].decision == "keep"
    cannot = by_id[",BBBB2222BBBB2222BBBB2222BBBB2222"]
    assert cannot.decision == "keep" and "dev query" in cannot.note
    assert by_id[",EEEE5555EEEE5555EEEE5555EEEE5555"].decision == "undecided"


def test_mtpe_form_incomplete_rows_are_undecided(tmp_path: Path):
    from orbit8.po_patch import parse_review
    path = _mtpe_form(tmp_path / "mtpe.xlsx", [
        {"StringID": ",A", "Source": "x", "Target_MT": "",
         "PE_Decision": "Accept Translation"},           # nothing to accept
        {"StringID": ",B", "Source": "y", "Target_MT": "z",
         "PE_Decision": "Reject&Modification"},          # no modification
    ])
    rows = parse_review(path)
    assert all(r.decision == "undecided" for r in rows)
    assert "Target_MT is empty" in rows[0].note
    assert "PE_Modification is empty" in rows[1].note


def test_deliver_from_filled_mtpe_form(tmp_path: Path):
    source = tmp_path / "Game.po"
    source.write_text(UE_PO, encoding="utf-8", newline="")
    review = _mtpe_form(tmp_path / "mtpe.xlsx", [
        {"StringID": ",AAAA1111AAAA1111AAAA1111AAAA1111",
         "Source": "BACK", "Target_MT": "返回",
         "PE_Decision": "Accept Translation"},
        {"StringID": ",CCCC3333CCCC3333CCCC3333CCCC3333",
         "Source": "Quit", "Target_MT": "退出",
         "PE_Decision": "Reject&Modification",
         "PE_Modification": "离开游戏"},
        {"StringID": ",DDDD4444DDDD4444DDDD4444DDDD4444",
         "Source": "Settings", "Target_MT": "设定",
         "PE_Decision": "Reject&Keep-as-it-is"},
    ])
    report = deliver_from_review(review, [source], tmp_path / "out",
                                 timestamp="20260803", sanity_check=False)
    delivered = (tmp_path / "out" / "20260803-po-delivery"
                 / "Game.po").read_text(encoding="utf-8")
    assert 'msgstr "返回"' in delivered              # accepted MT applied
    assert 'msgstr "离开游戏"' in delivered           # modification wins
    assert 'msgstr "设置"' in delivered              # keep = untouched
    assert len(report.applied) == 2
