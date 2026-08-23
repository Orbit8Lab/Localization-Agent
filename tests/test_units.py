"""Unit tests for the deterministic core: gate checks, ratchet math, store
attempt versioning, rules-based classifier, ingest dedup, TM guard."""
import json
from pathlib import Path

import pytest

from orbit8.gate_checks import GateConfig, run_gate
from orbit8.glossary import Glossary
from orbit8.graphs.context import classify_rules_v0
from orbit8.graphs.translate import _badness
from orbit8.ingest import dedup, ingest_po, run_ingest
from orbit8.memory import RunDB, TranslationMemory
from orbit8.schemas import (BugType, Domain, Finding, IngestReport, Severity,
                            SourceString, UniqueString)
from orbit8.store import JobStore


# ------------------------------------------------------------------- gate

def test_gate_placeholder_mismatch():
    cfg = GateConfig(source_lang="zh", target_lang="ko")
    findings = run_gate("u1", "攻击力+{0}", "공격력 증가", cfg)
    assert any(f.bug_type == BugType.PLACEHOLDER for f in findings)


def test_gate_locked_term():
    cfg = GateConfig(source_lang="zh", target_lang="ko",
                     locked_terms={"狼人": "늑대인간"})
    bad = run_gate("u1", "狼人出现了", "여우가 나타났다", cfg)
    assert any(f.bug_type == BugType.TERMINOLOGY for f in bad)
    good = run_gate("u1", "狼人出现了", "늑대인간이 나타났다", cfg)
    assert not any(f.bug_type == BugType.TERMINOLOGY for f in good)


def test_gate_leakage_zh_to_ko():
    cfg = GateConfig(source_lang="zh", target_lang="ko")
    findings = run_gate("u1", "传说之剑，攻击力提升", "传说之剑 공격력", cfg)
    assert any(f.bug_type == BugType.LEAKAGE for f in findings)


def test_gate_clean():
    cfg = GateConfig(source_lang="zh", target_lang="ko")
    assert run_gate("u1", "退出", "종료", cfg) == []


def test_gate_length_ratio_needs_max_len():
    """Without a hard UI budget the ratio check is noise (logographic
    targets legitimately compress: 'REFLECTIONS' → '反射')."""
    cfg = GateConfig(source_lang="en", target_lang="zh")
    silent = run_gate("u1", "REFLECTIONS", "反射", cfg)
    assert not any(f.bug_type == BugType.LENGTH for f in silent)
    flagged = run_gate("u1", "REFLECTIONS", "反射", cfg, max_len=40)
    assert any(f.bug_type == BugType.LENGTH for f in flagged)


def test_badness_ordering():
    high = [Finding(key="u", bug_type=BugType.PLACEHOLDER,
                    severity=Severity.HIGH, message="m", evidence="e")]
    two_med = [Finding(key="u", bug_type=BugType.LENGTH,
                       severity=Severity.MEDIUM, message="m", evidence="e")] * 2
    assert _badness(high) > _badness(two_med) > _badness([])


# ------------------------------------------------------------------ store

def test_attempt_versioning(tmp_path: Path):
    store = JobStore(tmp_path, "j1")
    report = IngestReport(total_records=1, unique_strings=1, total_chars=2,
                          dedup_ratio=0.0)
    first = store.new_attempt(4)
    store.write(4, "x", report, produced_by="code:t@1", attempt=first)
    second = store.new_attempt(4)
    store.write(4, "x", report, produced_by="code:t@1", attempt=second)
    assert first == 1 and second == 2
    assert store.latest_attempt(4) == 2
    assert (store.job_dir / "s4" / "attempt-01" / "x.json").exists()
    assert (store.job_dir / "s4" / "attempt-02" / "x.json").exists()


def test_schema_mismatch_refused(tmp_path: Path):
    store = JobStore(tmp_path, "j1")
    report = IngestReport(total_records=1, unique_strings=1, total_chars=2,
                          dedup_ratio=0.0)
    store.write(1, "r", report, produced_by="code:t@1")
    from orbit8.store import ArtifactError
    from orbit8.schemas import StyleBrief
    with pytest.raises(ArtifactError):
        store.read(1, "r", StyleBrief)


# ------------------------------------------------------------- classifier

def test_rules_classifier_fails_expensive():
    uniques = [UniqueString(uid="u0", text="开始", keys=["UI_START"]),
               UniqueString(uid="u1", text="你好", keys=["DLG_HELLO"]),
               UniqueString(uid="u2", text="谜之文本", keys=["WEIRD_KEY"])]
    labels = {i.key: i for i in classify_rules_v0(uniques).items}
    assert labels["u0"].domain == Domain.UI and labels["u0"].confidence >= 0.9
    assert labels["u1"].domain == Domain.DIALOGUE
    # unknown convention → low confidence → routes TO MTPE, never away
    assert labels["u2"].confidence < 0.6


# ----------------------------------------------------------------- ingest

def test_dedup_stable_uids():
    records = [SourceString(key="A", text="同"),
               SourceString(key="B", text="同"),
               SourceString(key="C", text="异")]
    uniques = dedup(records)
    assert len(uniques) == 2
    assert uniques[0].keys == ["A", "B"]


def test_ingest_po(tmp_path: Path):
    po = tmp_path / "game.po"
    po.write_text('msgctxt "K1"\nmsgid "你好"\nmsgstr ""\n\n'
                  'msgctxt "K2"\nmsgid "再见"\nmsgstr ""\n', encoding="utf-8")
    records = ingest_po(po)
    assert [(r.key, r.text) for r in records] == [("K1", "你好"), ("K2", "再见")]


def test_ingest_po_keeps_ue_location(tmp_path: Path):
    """UE `#:` reference comments (widget/asset paths) ride along as
    context — they are what makes a bug row actionable for the dev."""
    po = tmp_path / "game.po"
    po.write_text(
        '#. Key:\tAAAA\n'
        '#: /Game/UI/WDG_Menu.WDG_Menu_C:WidgetTree.TextBlock_0.Text\n'
        'msgctxt ",AAAA"\nmsgid "BACK"\nmsgstr ""\n\n'
        'msgctxt ",BBBB"\nmsgid "Quit"\nmsgstr ""\n', encoding="utf-8")
    records = ingest_po(po)
    assert records[0].context == ("/Game/UI/WDG_Menu.WDG_Menu_C:"
                                  "WidgetTree.TextBlock_0.Text")
    assert records[1].context is None


def test_ingest_report_wordcount(tmp_path: Path):
    src = tmp_path / "s.json"
    src.write_text(json.dumps({"a": "xx", "b": "xx", "c": "yyy"}),
                   encoding="utf-8")
    _, uniques, report = run_ingest([src])
    assert report.total_records == 3
    assert report.unique_strings == 2
    assert report.total_chars == 5


# ----------------------------------------------------------------- memory

def test_tm_stub_guard_and_human_priority(tmp_path: Path):
    tm = TranslationMemory(tmp_path / "tm.db")
    tm.store("你好", "[ko]你好", "ko")               # stub: silently dropped
    assert tm.lookup("你好", "ko") is None
    tm.store("你好", "안녕(기계)", "ko")
    tm.store("你好", "안녕하세요", "ko", origin="human")
    assert tm.lookup("你好", "ko") == "안녕하세요"     # human wins


def test_run_db_roundtrip(tmp_path: Path):
    db = RunDB(tmp_path / "run.db")
    db.seed([UniqueString(uid="u0", text="你好", keys=["K1", "K2"])])
    db.label("u0", Domain.DIALOGUE, 0.9)
    db.record("u0", status="accepted", target="안녕",
              findings=[Finding(key="u0", bug_type=BugType.LENGTH,
                                severity=Severity.LOW, message="m",
                                evidence="e")])
    row = db.get("u0")
    assert row["domain"] == "dialogue" and row["target"] == "안녕"
    assert row["findings"][0].severity == Severity.LOW
    assert db.counts() == {"accepted": 1}


# --------------------------------------------------------------- glossary

def test_glossary_precedence_t1_wins():
    glossary = Glossary.from_layers(
        "g", "ko",
        t1={"terms": {"狼人": {"translation": "늑대인간"}}},
        t2={"狼人": "울프맨", "村庄": "마을"})
    locked = glossary.locked_map()
    assert locked == {"狼人": "늑대인간"}            # T1 only, T1 wins
    brief = glossary.brief_for(["狼人来到村庄"])
    renderings = {t.term: (t.translation, t.tier) for t in brief.terms}
    assert renderings["狼人"] == ("늑대인간", 1)
    assert renderings["村庄"] == ("마을", 2)


def test_glossary_prefill():
    glossary = Glossary.from_layers(
        "g", "ko", t1={"terms": {"退出": {"translation": "종료"}}})
    assert glossary.prefill(" 退出 ") == "종료"
    assert glossary.prefill("退出游戏") is None


def test_locked_in_target_authority_order():
    """Exact form > operator-approved variant > morphology; and a
    profile replaces the hardcoded language list."""
    from orbit8.gate_checks import locked_in_target
    from orbit8.style_defaults import EN_MORPHOLOGY, ZH_MORPHOLOGY

    # 1. exact
    assert locked_in_target("Plague Node", "Clear the Plague Node", "en")
    # 2. a VARIANT is a recorded decision, not a guess
    assert not locked_in_target("Plague Source", "the Source of Plague",
                                "en")
    assert locked_in_target("Plague Source", "the Source of Plague", "en",
                            variants=["Source of Plague"])
    # 3. morphology comes from the profile
    assert not locked_in_target("Plague Node", "many Plague Nodes exist",
                                "en", morphology=ZH_MORPHOLOGY)
    assert locked_in_target("Plague Node", "many Plague Nodes exist",
                            "en", morphology=EN_MORPHOLOGY)
    # a profile that says "no inflection" still rejects a different term
    assert not locked_in_target("瘟疫点", "感染点", "zh-CN",
                                morphology=ZH_MORPHOLOGY)
