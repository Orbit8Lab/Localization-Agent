"""External LQA as a real side entrance to the pipeline.

`orbit8 lqa run` audits translations SOMEBODY ELSE produced. It reads the
intake and a pairs file and nothing else — no ingest, no context, no
glossary lock, no translation. That is the design, and two things quietly
prevented it:

1. `emit_bilingual_jsonl` hard-rejected anything but `.po`, so a client
   sending an xlsx/csv of translations for review had no way in — while
   the identical file as a SOURCE was handled fine by the Adapter-Writer.
   An asymmetry, not a decision.
2. `run_external_lqa` read the s2 style brief unconditionally, an artifact
   only CONTEXT produces — a stage an external audit never runs.

Together they made a documented entry point unreachable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.controller import Job
from orbit8.exports import emit_bilingual_jsonl
from orbit8.schemas import IntakeBrief


@pytest.fixture
def job(tmp_path) -> Job:
    return Job.init(tmp_path / "jobs", "audit",
                    intake=IntakeBrief(game="G", source_lang="en",
                                       target_locales=["zh-CN"]),
                    source_files=[])


def _po(path: Path, pairs: list) -> Path:
    path.write_text(
        "\n\n".join(f'msgctxt ",{k}"\nmsgid "{s}"\nmsgstr "{t}"'
                    for k, s, t in pairs) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------ the style-brief blocker

def test_the_style_brief_is_optional_for_an_external_audit(job):
    """An external audit enters at S5, so no s2 artifact exists. The LQA
    context already types style_brief as Optional and the cascade handles
    None — the hard read was the only thing making the entry point
    impossible."""
    assert job._style_or_none() is None


def test_the_strict_reader_still_raises_where_it_should(job):
    """Only the external path tolerates a missing brief. A normal
    lifecycle run reaching CONTEXT-dependent work with no brief is a real
    error and must stay loud."""
    from orbit8.store import ArtifactError
    with pytest.raises(ArtifactError):
        job._style()


# ------------------------------------------- bilingual formats beyond .po

def test_a_po_still_works_without_any_fallback(tmp_path):
    """The native path must not regress: .po needs no adapter."""
    source = _po(tmp_path / "t.po",
                 [("A", "Start", "开始"), ("B", "Quit", "退出")])
    written, empty = emit_bilingual_jsonl(
        [source], tmp_path / "out.jsonl",
        source_lang="en", target_lang="zh-CN")
    assert written == 2 and empty == 0


def test_a_non_po_without_a_fallback_explains_itself(tmp_path):
    """Dry-run cannot generate an adapter, so the refusal has to say what
    to do rather than just naming the suffix."""
    csv = tmp_path / "pairs.csv"
    csv.write_text("Key,English,Chinese\nA,Start,开始\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        emit_bilingual_jsonl([csv], tmp_path / "out.jsonl",
                             source_lang="en", target_lang="zh-CN")
    assert "adapter-writer" in str(excinfo.value)


def test_a_non_po_goes_through_the_fallback(tmp_path):
    """The fix: any format the adapter-writer can read is a valid
    bilingual input, exactly as it already is for source ingest."""
    csv = tmp_path / "pairs.csv"
    csv.write_text("Key,English,Chinese\nA,Start,开始\nB,Quit,退出\n",
                   encoding="utf-8")

    def fake_adapter(paths, *, context=""):
        assert isinstance(paths, list)      # the WHOLE set, not one file
        assert "en" in context and "zh-CN" in context
        return [("A", "Start", "开始", str(paths[0])),
                ("B", "Quit", "退出", str(paths[0]))]

    written, empty = emit_bilingual_jsonl(
        [csv], tmp_path / "out.jsonl", source_lang="en",
        target_lang="zh-CN", fallback=fake_adapter)
    assert written == 2 and empty == 0

    rows = [json.loads(line) for line in
            (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["source_text"] == "Start"
    assert rows[0]["target_text"] == "开始"


def test_an_untranslated_row_survives_alongside_translated_ones(tmp_path):
    """An untranslated string is exactly what a review must flag —
    dropping it at export is a recall hole, and that rule has to hold on
    the adapter path too."""
    csv = tmp_path / "pairs.csv"
    csv.write_text("Key,English,Chinese\nA,Start,开始\nB,Quit,\n",
                   encoding="utf-8")

    written, empty = emit_bilingual_jsonl(
        [csv], tmp_path / "out.jsonl", source_lang="en",
        target_lang="zh-CN",
        fallback=lambda p, *, context="": [
            ("A", "Start", "开始", str(p[0])),
            ("B", "Quit", "", str(p[0]))])
    assert written == 2 and empty == 1


def test_an_export_with_NO_translations_is_refused(tmp_path):
    """The bug that produced 400 unusable rows: a per-locale file holds
    ONE language, so an adapter shown it alone emits that language as
    `source` with every target empty. Correctly shaped, nothing to
    review, and silent — an LQA run would have found no defects because
    there was no translation to inspect."""
    csv = tmp_path / "ja_only.csv"
    csv.write_text("Key,Japanese\nA,ゲーム開始\n", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        emit_bilingual_jsonl(
            [csv], tmp_path / "out.jsonl", source_lang="en",
            target_lang="ja",
            fallback=lambda p, *, context="": [
                (f"K{n}", "ゲーム開始", "", str(p[0])) for n in range(5)])
    message = str(excinfo.value)
    assert "empty target" in message
    assert "pass BOTH" in message           # and how to fix it


def test_two_files_are_handed_to_the_adapter_together(tmp_path):
    """THE fix. A per-locale layout is only legible when the adapter sees
    the source export and the target export at once — shown one at a
    time it can find a source with no target and nothing else."""
    source = tmp_path / "Game (Source).xlsx"
    target = tmp_path / "Game_ja.xlsx"
    for path in (source, target):
        path.write_bytes(b"PK\x03\x04stub")

    seen = {}

    def adapter(paths, *, context=""):
        seen["files"] = [p.name for p in paths]
        seen["context"] = context
        return [("A", "Start Game", "ゲーム開始", str(paths[0]))]

    written, _empty = emit_bilingual_jsonl(
        [source, target], tmp_path / "out.jsonl", source_lang="en",
        target_lang="ja", fallback=adapter)

    assert written == 1
    assert seen["files"] == ["Game (Source).xlsx", "Game_ja.xlsx"]
    # the context names the languages AND the argv positions, which is
    # what lets the adapter tell which file holds which language
    assert "en → ja" in seen["context"]
    assert "argv[1]=Game (Source).xlsx" in seen["context"]


def test_a_source_language_file_is_still_refused(tmp_path):
    """The sanity guard must survive the new path: a file whose 'targets'
    equal its sources is a SOURCE export, and pairing it produces useless
    en→en rows."""
    csv = tmp_path / "english.csv"
    csv.write_text("Key,A,B\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        emit_bilingual_jsonl(
            [csv], tmp_path / "out.jsonl", source_lang="en",
            target_lang="zh-CN",
            fallback=lambda p, *, context="": [
                (f"K{n}", "Start", "Start", str(p[0])) for n in range(10)])
    assert "source-language file" in str(excinfo.value)


# ------------------------------------------- inspecting a spreadsheet

def _chat(tmp_path):
    from orbit8.llm import EchoProvider
    from orbit8.orchestrator import ChatOrchestrator
    job = Job.init(tmp_path / "proj" / "jobs", "j",
                   intake=IntakeBrief(game="G", source_lang="en",
                                      target_locales=["ja"]),
                   source_files=[])
    return ChatOrchestrator(job, EchoProvider("ja"), operator="t",
                            dry_run=True)


def test_inspecting_an_xlsx_shows_columns_not_zip_bytes(tmp_path):
    """An xlsx is a zip. Text-peeking it handed the model `PK\\x03\\x04…`,
    which answers nothing about the columns — so it called inspect again
    and again and burned the whole step budget on a tool that "succeeded"
    every time."""
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Strings"
    sheet.append(["Key", "English", "Japanese"])
    sheet.append(["UI_START", "Start Game", "ゲーム開始"])
    path = tmp_path / "proj" / "10-received"
    path.mkdir(parents=True)
    book.save(path / "Bilingual.xlsx")

    result = json.loads(_chat(tmp_path)._t_inspect_file(
        {"path": "10-received/Bilingual.xlsx"}))
    assert "PK" not in str(result.get("head", ""))
    preview = result["sheet_preview"][0]
    assert preview["header"] == ["Key", "English", "Japanese"]
    assert preview["sample_rows"][0][2] == "ゲーム開始"


def test_inspecting_a_csv_shows_its_header(tmp_path):
    path = tmp_path / "proj" / "10-received"
    path.mkdir(parents=True)
    (path / "pairs.csv").write_text(
        "Key,English,Japanese\nUI_START,Start Game,ゲーム開始\n",
        encoding="utf-8")

    result = json.loads(_chat(tmp_path)._t_inspect_file(
        {"path": "10-received/pairs.csv"}))
    assert result["header"] == ["Key", "English", "Japanese"]


def test_an_unreadable_spreadsheet_reports_the_error(tmp_path):
    """Never fall back to a byte peek: binary noise is what caused the
    loop this exists to prevent."""
    path = tmp_path / "proj" / "10-received"
    path.mkdir(parents=True)
    (path / "broken.xlsx").write_bytes(b"not really a spreadsheet")

    result = json.loads(_chat(tmp_path)._t_inspect_file(
        {"path": "10-received/broken.xlsx"}))
    assert "error" in result
    assert "head" not in result


# ---------------------------------------------- the bilingual contract

def test_the_pair_validator_requires_all_three_fields():
    from orbit8.codegen import validate_pairs
    with pytest.raises(ValueError):
        validate_pairs('[{"key": "A", "text": "Start"}]', "f")


def test_the_pair_validator_keeps_empty_targets():
    """Opposite of the ingest validator, on purpose: for ingest an empty
    string is noise, for LQA it is the finding."""
    from orbit8.codegen import validate_pairs
    pairs = validate_pairs('[{"key":"A","source":"Start","target":""}]', "f")
    assert pairs == [("A", "Start", "", "f")]


def test_the_pair_validator_drops_rows_with_no_source():
    from orbit8.codegen import validate_pairs
    with pytest.raises(ValueError):
        validate_pairs('[{"key":"A","source":"","target":"开始"}]', "f")


def test_the_pair_validator_refuses_duplicate_keys():
    from orbit8.codegen import validate_pairs
    with pytest.raises(ValueError):
        validate_pairs('[{"key":"A","source":"x","target":"y"},'
                       ' {"key":"A","source":"z","target":"w"}]', "f")


def test_a_stored_adapter_is_re_run_behind_the_same_wall():
    """Reuse must not become a way around validation: a stored bilingual
    adapter is re-validated against the pair contract, not the ingest
    one."""
    import inspect

    from orbit8.codegen import run_adapter
    assert "validate" in inspect.signature(run_adapter).parameters
