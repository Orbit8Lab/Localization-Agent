"""Standalone LQA scan of a bilingual .po."""
import json
from pathlib import Path

import openpyxl

from orbit8.po_scan import scan_po
from orbit8.standards import LQA_PE_FORM_HEADERS

PO = '''﻿msgid ""
msgstr ""
"Content-Type: text/plain; charset=utf-8\\n"
"Language: en\\n"

#: /Game/UI/WDG_Hud.Text
msgctxt ",K1"
msgid "瘟疫点"
msgstr "Infection Point"

msgctxt ",K2"
msgid "清除瘟疫点"
msgstr "Clear the Plague Node"

#: /Game/Skill/S_Heal.Desc
msgctxt ",K3"
msgid "恢复生命值"
msgstr "Restore Health"

msgctxt ",K4"
msgid "破旧的太刀"
msgstr "Worn-out Tachi"

msgctxt ",K5"
msgid "破旧的太刀"
msgstr "Battered Longsword"
'''

T1 = {"metadata": {"game": "测试", "locale": "en"},
      "terms": {"瘟疫点": {"translation": "Plague Node", "locked": True},
                "太刀": {"translation": "Tachi", "locked": True},
                "生命值": {"translation": "Health", "locked": True}}}


def _setup(tmp_path: Path):
    po = tmp_path / "Game.po"
    po.write_text(PO, encoding="utf-8", newline="")
    t1 = tmp_path / "glossary_terms.json"
    t1.write_text(json.dumps(T1, ensure_ascii=False), encoding="utf-8")
    return po, t1


def test_scan_flags_locked_term_violation(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    result = scan_po(po, t1, tmp_path / "out", game="测试",
                     deterministic_only=True)      # T1+T2 only, no LLM
    # K1 renders 瘟疫点 as "Infection Point" — a locked-term violation
    flagged_sources = {i.source for i in result.report.items}
    assert "瘟疫点" in flagged_sources
    assert result.report.flagged_strings >= 1
    # cascade ledger present and telescoping (verify_cascade passed —
    # run_lqa_stage raises otherwise)
    assert result.report.cascade_ledger["accepted"] >= 4
    # every finding carries a code-stamped tier
    tiers = {vf.finding.tier for i in result.report.items
             for vf in i.findings}
    assert tiers and None not in tiers


def test_scan_detects_inconsistent_renderings(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    result = scan_po(po, t1, tmp_path / "out", game="测试",
                     deterministic_only=True)
    # 破旧的太刀 ships as both "Worn-out Tachi" and "Battered Longsword"
    assert len(result.inconsistent) == 1
    entry = result.inconsistent[0]
    assert entry["source"] == "破旧的太刀"
    assert {r["target"] for r in entry["renderings"]} == {
        "Worn-out Tachi", "Battered Longsword"}
    assert (tmp_path / "out/inconsistent_renderings.json").exists()


def test_scan_emits_standard_pe_form_and_bug_report(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    result = scan_po(po, t1, tmp_path / "out", game="测试",
                     deterministic_only=True)
    book = openpyxl.load_workbook(result.outputs["pe_form"])
    sheet = book["LQA PE"]
    rows = list(sheet.iter_rows(values_only=True))
    # standard shape preserved, extended with an agent context column
    assert list(rows[0]) == list(LQA_PE_FORM_HEADERS) + ["Findings"]
    assert len(rows) - 1 == result.pe_rows >= 1
    idx = {h: i for i, h in enumerate(rows[0])}
    # agent columns filled, PE columns empty and awaiting the reviewer
    assert all(r[idx["Source"]] for r in rows[1:])
    assert all(not r[idx["PE_Decision"]] for r in rows[1:])
    assert all(not r[idx["PE_Note"]] for r in rows[1:])
    # the reviewer sees WHY each row is flagged, with its tier
    assert any("T1" in str(r[idx["Findings"]]) for r in rows[1:])
    assert Path(result.outputs["bug_report"]).exists()
    assert Path(result.outputs["tech_summary"]).exists()
    summary = json.loads(
        (tmp_path / "out/scan_report.json").read_text("utf-8"))
    assert summary["entries"] == 5 and summary["inconsistent_sources"] == 1


def test_scan_without_glossary_still_runs(tmp_path: Path):
    po, _ = _setup(tmp_path)
    result = scan_po(po, None, tmp_path / "out", game="测试",
                     deterministic_only=True)
    assert result.report.checked >= 1        # mechanical checks only


class FlakyProvider:
    """T3 provider whose SECOND batch always returns prose, not JSON."""
    name, model = "flaky", "test"

    def __init__(self):
        self.tokens_spent = 0.0
        self.calls = 0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.calls += 1
        if 3 <= self.calls <= 4:          # batch 2 + its repair attempt
            return "I'm sorry, I cannot review this content."
        return json.dumps({"findings": []})


def test_t3_batch_failure_does_not_kill_the_run(tmp_path: Path):
    """One malformed completion must not discard every other batch —
    and the unreviewed strings must be VISIBLE, not silently clean."""
    po = tmp_path / "Game.po"
    entries = "".join(
        f'msgctxt ",K{i}"\nmsgid "文本{i}号内容"\nmsgstr "Text {i} body"\n\n'
        for i in range(12))
    po.write_text('﻿msgid ""\nmsgstr ""\n"Language: en\\n"\n\n' + entries,
                  encoding="utf-8", newline="")
    result = scan_po(po, None, tmp_path / "out", game="测试",
                     provider=FlakyProvider(), batch_string=5,
                     suggestions=False)
    led = result.report.cascade_ledger
    # the run COMPLETED despite a failing batch
    assert led["t3_ran"] == 1 and led["t3_batches"] >= 2
    assert led["t3_batches_failed"] == 1
    # the coverage gap is declared in the ledger AND itemized (the failing
    # call lands on the 3rd batch: 12 strings at n=5 → 5 + 5 + 2)
    assert led["t3_unreviewed"] == 2
    assert sum(len(e["uids"]) for e in result.report.t3_errors) == 2
    assert led["t3_input"] - led["t3_unreviewed"] == 10   # rest reviewed
    assert "cannot review" in result.report.t3_errors[0]["error"].lower() \
        or "RuntimeError" in result.report.t3_errors[0]["error"]
    # failed batches are NOT finding-audit entries (they produced none)
    assert all(a.get("uid") for a in result.report.t3_audit)


class HangingProvider:
    """Every call times out — the worst case that hung the real run."""
    name, model = "hanging", "test"

    def __init__(self):
        self.tokens_spent = 0.0
        self.calls = 0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.calls += 1
        err = TimeoutError("Request timed out.")
        err.__class__.__name__ = "APITimeoutError"
        raise err


def test_total_llm_outage_still_produces_a_report(tmp_path: Path):
    """A dead API must yield a REPORT with an explicit coverage gap —
    never a hang, never a crash, never a falsely clean result."""
    po, t1 = _setup(tmp_path)
    provider = HangingProvider()
    result = scan_po(po, t1, tmp_path / "out", game="测试",
                     provider=provider, batch_string=5,
                     suggestions=False)
    led = result.report.cascade_ledger
    # T1/T2 findings survive — mechanical checks never needed the API
    assert result.report.flagged_strings >= 1
    # every T3 batch failed, and the gap is declared
    assert led["t3_batches_failed"] == led["t3_batches"] >= 1
    assert led["t3_unreviewed"] == led["t3_input"] > 0
    assert sum(len(e["uids"]) for e in result.report.t3_errors) == \
        led["t3_unreviewed"]
    # the deliverables still exist for the reviewer
    assert Path(result.outputs["bug_report"]).exists()
    assert Path(result.outputs["pe_form"]).exists()


def test_style_rules_fire_in_the_gate_scoped_by_domain(tmp_path: Path):
    """A mechanical style rule becomes a T1 finding, and UI-only rules
    must not fire on dialogue."""
    from orbit8.style_guide import StyleGuide, StyleRule
    guide = StyleGuide(source_lang="zh-CN", target_lang="en", rules=[
        StyleRule(id="T-01", enforcement="mechanical",
                  check="forbid_chars", value="，。",
                  text="No CJK punctuation in English.", severity="high"),
        StyleRule(id="T-02", enforcement="mechanical", check="max_chars",
                  value=10, domains=["ui"],
                  text="UI labels stay under 10 characters.",
                  severity="medium"),
    ])
    po = tmp_path / "Game.po"
    po.write_text(
        '﻿msgid ""\nmsgstr ""\n"Language: en\\n"\n\n'
        '#: /Game/UI/WDG_Menu.Back.Text\n'
        'msgctxt ",K1"\nmsgid "返回"\n'
        'msgstr "This label is far too long for a button"\n\n'
        '#: /Game/Dialogue/NPC_01.Line\n'
        'msgctxt ",K2"\nmsgid "你好"\n'
        'msgstr "Hello there, friend — a long line of dialogue"\n\n'
        '#: /Game/UI/WDG_Hud.Tip\n'
        'msgctxt ",K3"\nmsgid "提示"\nmsgstr "Tip，bad"\n',
        encoding="utf-8", newline="")
    result = scan_po(po, None, tmp_path / "out", game="测试",
                     deterministic_only=True, style_guide=guide)
    by_source = {i.source: i for i in result.report.items}
    ui_msgs = [vf.finding.message for vf in by_source["返回"].findings]
    # the UI length rule fired, and cites its id
    assert any(m.startswith("[T-02]") for m in ui_msgs)
    # the same rule must NOT fire on the (longer) dialogue string
    dlg = by_source.get("你好")
    assert dlg is None or not any(
        "[T-02]" in vf.finding.message for vf in dlg.findings)
    # the global punctuation rule fires regardless of domain
    tip_msgs = [vf.finding.message for vf in by_source["提示"].findings]
    assert any(m.startswith("[T-01]") for m in tip_msgs)


class RepairProvider:
    """T3 finds nothing; the REPAIR pass first proposes a fix that
    violates the glossary, then corrects itself on the retry."""
    name, model = "repair", "test"

    def __init__(self, ever_fix=True):
        self.tokens_spent = 0.0
        self.repairs = 0
        self.ever_fix = ever_fix

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        import re
        if "QA reviewer" in system:
            return json.dumps({"findings": []})
        self.repairs += 1
        keys = [k for k in re.findall(r"^### (\S+)$", user, flags=re.M)
                if k != "END"]
        fixed = self.ever_fix and self.repairs > 1
        return json.dumps({"items": [
            {"key": k,
             "target_text": ("Clear the Plague Point" if fixed
                             else "Clear the Infection Point"),
             "term_decisions": {}, "notes": None} for k in keys]})


def _one_bad_string(tmp_path: Path):
    po = tmp_path / "Game.po"
    po.write_text('﻿msgid ""\nmsgstr ""\n"Language: en\\n"\n\n'
                  'msgctxt ",K1"\nmsgid "清除瘟疫点"\n'
                  'msgstr "Clear the Infection Point"\n',
                  encoding="utf-8", newline="")
    t1 = tmp_path / "g.json"
    t1.write_text(json.dumps({
        "metadata": {"game": "测试", "locale": "en"},
        "terms": {"瘟疫点": {"translation": "Plague Point",
                             "locked": True}}}, ensure_ascii=False),
        encoding="utf-8")
    return po, t1


def test_suggestion_must_itself_satisfy_the_glossary(tmp_path: Path):
    po, t1 = _one_bad_string(tmp_path)
    provider = RepairProvider(ever_fix=True)
    result = scan_po(po, t1, tmp_path / "out", game="测试",
                     provider=provider)
    # the first proposal repeated the violation and was re-repaired
    assert provider.repairs == 2
    assert list(result.suggestions.values()) == ["Clear the Plague Point"]
    assert result.rejected_suggestions == []


def test_incorrigible_suggestion_is_dropped_not_shipped(tmp_path: Path):
    po, t1 = _one_bad_string(tmp_path)
    result = scan_po(po, t1, tmp_path / "out", game="测试",
                     provider=RepairProvider(ever_fix=False))
    # a fix that keeps violating the locked term never reaches the client
    assert result.suggestions == {}
    assert len(result.rejected_suggestions) == 1
    rejected = result.rejected_suggestions[0]
    assert "Infection Point" in rejected["candidate"]
    assert any("瘟疫点" in v for v in rejected["violations"])
    assert (tmp_path / "out/rejected_suggestions.json").exists()
    # …and the original defect is still reported to the reviewer
    assert result.report.flagged_strings == 1
