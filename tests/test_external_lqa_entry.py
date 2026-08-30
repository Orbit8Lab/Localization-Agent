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

    def fake_adapter(path):
        return [("A", "Start", "开始", str(path)),
                ("B", "Quit", "退出", str(path))]

    written, empty = emit_bilingual_jsonl(
        [csv], tmp_path / "out.jsonl", source_lang="en",
        target_lang="zh-CN", fallback=fake_adapter)
    assert written == 2 and empty == 0

    rows = [json.loads(line) for line in
            (tmp_path / "out.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["source_text"] == "Start"
    assert rows[0]["target_text"] == "开始"


def test_an_empty_target_survives_the_fallback(tmp_path):
    """An untranslated string is exactly what a review must flag —
    dropping it at export is a recall hole, and that rule has to hold on
    the adapter path too."""
    csv = tmp_path / "pairs.csv"
    csv.write_text("Key,English,Chinese\nA,Start,\n", encoding="utf-8")

    written, empty = emit_bilingual_jsonl(
        [csv], tmp_path / "out.jsonl", source_lang="en",
        target_lang="zh-CN",
        fallback=lambda p: [("A", "Start", "", str(p))])
    assert written == 1 and empty == 1


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
            fallback=lambda p: [(f"K{n}", "Start", "Start", str(p))
                                for n in range(10)])
    assert "source-language file" in str(excinfo.value)


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
