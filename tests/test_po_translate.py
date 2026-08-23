"""Incremental translation of a received drop's untranslated strings."""
import json
from pathlib import Path

import openpyxl

from orbit8.po_translate import translate_untranslated
from orbit8.standards import MTPE_FORM_HEADERS

PO = '''﻿msgid ""
msgstr ""
"Project-Id-Version: test\\n"
"Content-Type: text/plain; charset=utf-8\\n"
"Language: en\\n"

msgctxt ",K1"
msgid "秘火使徒"
msgstr "Secret Fire Apostle"

msgctxt ",K2"
msgid "瘟疫点"
msgstr ""

msgctxt ",K3"
msgid "摧毁所有瘟疫点获得胜利"
msgstr ""

msgctxt ",K4"
msgid "一把破旧的太刀"
msgstr ""
'''

T1 = {"metadata": {"game": "测试", "locale": "en",
                   "family_rules": {"致幻": {"translation":
                                             "Hallucination"}}},
      "terms": {"瘟疫点": {"translation": "Plague Node", "locked": True},
                "太刀": {"translation": "Tachi", "locked": True},
                # unlocked mined entry — steers the prompt but must NOT
                # be gate-enforced (K3 says Destroy, not Demolish)
                "摧毁": {"translation": "Demolish", "locked": False}}}


class FakeProvider:
    """First pass violates the 太刀 ruling; repair pass fixes it.
    Keys arrive as opaque aliases (s0, s1, …) — the fake answers by
    SOURCE CONTENT, like a real model."""
    name, model = "fake", "t"

    def __init__(self):
        self.tokens_spent = 0.0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        import re
        blocks = re.findall(r"^### (\S+)\n(.*?)(?=\n### |\Z)", user,
                            flags=re.M | re.S)
        items = []
        for key, text in blocks:
            if key == "END":
                continue
            if "摧毁所有瘟疫点" in text:
                target = "Destroy all Plague Nodes to win"
            elif "破旧的太刀" in text:
                target = ("A worn-out Tachi" if temperature == 0.0
                          else "A worn-out greatsword")
            else:
                target = f"[en]{text.strip()}"
            items.append({"key": key, "target_text": target})
        return json.dumps({"items": items})


def _setup(tmp_path: Path):
    po = tmp_path / "Game.po"
    po.write_text(PO, encoding="utf-8", newline="")
    t1 = tmp_path / "glossary_terms.json"
    t1.write_text(json.dumps(T1, ensure_ascii=False), encoding="utf-8")
    return po, t1


def test_translate_flow(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    out = tmp_path / "out"
    run = translate_untranslated(po, t1, out, provider=FakeProvider(),
                                 game="测试")
    # K2 is an exact glossary hit → prefilled, zero LLM
    assert run.prefilled == {",K2": "Plague Node"}
    assert run.translated[",K3"] == "Destroy all Plague Nodes to win"
    # K4 violated 太刀=Tachi on pass 1, repaired on retry
    assert run.translated[",K4"] == "A worn-out Tachi"
    assert run.repaired == [",K4"]
    assert run.violations == []
    assert run.sanity == "ok"


def test_patched_po_fidelity(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    out = tmp_path / "out"
    translate_untranslated(po, t1, out, provider=FakeProvider(),
                           game="测试")
    original = po.read_text(encoding="utf-8")
    patched = (out / "Game.po").read_text(encoding="utf-8")
    # untouched entry byte-identical, BOM preserved, only empties filled
    assert 'msgstr "Secret Fire Apostle"' in patched
    assert patched.startswith("﻿")
    assert 'msgstr "Plague Node"' in patched
    for line in original.splitlines():
        if "msgstr" not in line:
            assert line in patched                # non-msgstr lines intact


def test_reuse_from_previous_run(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    prev = tmp_path / "prev.po"
    prev.write_text('''﻿msgid ""
msgstr ""
"Content-Type: text/plain; charset=utf-8\\n"

msgctxt ",K3"
msgid "摧毁所有瘟疫点获得胜利"
msgstr "Destroy every Plague Node to claim victory"

msgctxt ",OTHERKEY"
msgid "一把破旧的太刀"
msgstr "A battered Tachi"

msgctxt ",K2"
msgid "感染点"
msgstr "Old stale rendering"
''', encoding="utf-8", newline="")
    out = tmp_path / "out"
    run = translate_untranslated(po, t1, out, provider=FakeProvider(),
                                 game="测试", reuse_from=prev)
    # K3 carried by exact key; K4 carried by identical source text
    # (different key in prev run) — neither costs an LLM call.
    # K2's key exists in prev BUT with a DIFFERENT source (感染点 vs
    # 瘟疫点) → stale, must NOT be carried (glossary prefill wins).
    assert run.reused == {
        ",K3": "Destroy every Plague Node to claim victory",
        ",K4": "A battered Tachi"}
    assert run.translated == {} and run.violations == []
    assert run.prefilled == {",K2": "Plague Node"}
    report = json.loads((out / "translate_report.json").read_text())
    assert report["reused"] == 2


def test_mtpe_form_and_report(tmp_path: Path):
    po, t1 = _setup(tmp_path)
    out = tmp_path / "out"
    translate_untranslated(po, t1, out, provider=FakeProvider(),
                           game="测试")
    book = openpyxl.load_workbook(out / "mtpe_form.xlsx")
    sheet = book["MTPE"]
    rows = list(sheet.iter_rows(values_only=True))
    assert list(rows[0]) == list(MTPE_FORM_HEADERS)
    assert len(rows) - 1 == 3                     # K2 K3 K4
    decision_col = list(MTPE_FORM_HEADERS).index("PE_Decision")
    assert all(not r[decision_col] for r in rows[1:])   # PE cols empty
    report = json.loads((out / "translate_report.json").read_text())
    assert report["prefilled"] == 1 and report["translated"] == 2
    assert "NOT A DELIVERABLE" in (out / "translate_report.md").read_text()
