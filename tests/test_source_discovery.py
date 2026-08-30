"""Finding the source file from the project directory (project_paths.py).

`--source` made an operator type a path they had already organized on
disk. Discovery removes the typing — but deliberately NOT the decision: a
project folder holds several drops, previous deliverables and reference
material, and silently ingesting last month's drop produces a job that
looks correct and translates the wrong text.

So discovery returns CANDIDATES with enough context to choose between
them, and reads every file rather than trusting its name.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from orbit8.project_paths import SOURCE_SUFFIXES, discover_sources


def _project(tmp_path: Path) -> Path:
    """A standard workspace: two markers is what makes it a project."""
    for name in ("10-received", "20-work", "40-reference"):
        (tmp_path / name).mkdir(parents=True)
    return tmp_path


def _drop(root: Path, name: str) -> Path:
    path = root / "10-received" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_source(directory: Path, name: str, count: int = 3) -> Path:
    path = directory / name
    path.write_text(
        json.dumps({f"K{n}": f"文本{n}" for n in range(count)},
                   ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------ scaffolding

def test_the_standard_structure_is_created(tmp_path):
    """A project folder is routinely made before there is anything to put
    in it — the client is signed, the folder exists, the strings arrive a
    week later. Requiring the layout to pre-exist made the tool useless at
    exactly that moment."""
    from orbit8.project_paths import scaffold_project

    created = scaffold_project(tmp_path)
    assert set(created) == {"10-received", "20-work", "30-deliverables",
                            "40-reference"}
    for name in created:
        assert (tmp_path / name).is_dir()


def test_scaffolding_only_adds(tmp_path):
    """Existing directories are left alone — this must never reshape
    someone's folder, only fill in what is missing."""
    from orbit8.project_paths import scaffold_project

    (tmp_path / "10-received").mkdir()
    (tmp_path / "10-received" / "keep.po").write_text("x", encoding="utf-8")

    created = scaffold_project(tmp_path)
    assert "10-received" not in created
    assert (tmp_path / "10-received" / "keep.po").exists()


def test_scaffolding_is_idempotent(tmp_path):
    from orbit8.project_paths import scaffold_project

    scaffold_project(tmp_path)
    assert scaffold_project(tmp_path) == []


def test_a_scaffolded_folder_is_recognised_as_a_project(tmp_path):
    """The point of scaffolding: discovery works afterwards."""
    from orbit8.project_paths import scaffold_project

    scaffold_project(tmp_path)
    _json_source(tmp_path / "10-received", "Strings.json")
    assert discover_sources(tmp_path).found


# --------------------------------------------------------- the happy path

def test_a_single_source_is_found(tmp_path):
    root = _project(tmp_path)
    _json_source(_drop(root, "20260828"), "Game.json")

    found = discover_sources(root)
    assert found.found
    assert found.unambiguous is not None
    assert found.unambiguous.path.name == "Game.json"


def test_the_strings_are_counted_not_guessed(tmp_path):
    """Reading the file is the point: the name says nothing, and an empty
    drop is exactly what an operator needs told BEFORE a job is built."""
    root = _project(tmp_path)
    _json_source(_drop(root, "20260828"), "Game.json", count=17)
    assert discover_sources(root).candidates[0].entries == 17


def test_the_drop_it_came_from_is_reported(tmp_path):
    root = _project(tmp_path)
    _json_source(_drop(root, "20260828-drop"), "Game.json")
    assert "20260828-drop" in discover_sources(root).candidates[0].describe()


def test_loose_files_directly_in_received_are_found(tmp_path):
    """Not every project uses dated drop folders."""
    root = _project(tmp_path)
    _json_source(root / "10-received", "Game.json")
    assert discover_sources(root).found


# ----------------------------------------------- ambiguity is a question

def test_several_candidates_are_not_silently_ranked(tmp_path):
    """THE failure this avoids. Picking one silently builds a job that
    looks correct and translates the wrong text."""
    root = _project(tmp_path)
    _json_source(_drop(root, "20260810"), "Old.json")
    _json_source(_drop(root, "20260828"), "New.json")

    found = discover_sources(root)
    assert len(found.candidates) == 2
    assert found.unambiguous is None       # a human decides


def test_the_newest_drop_is_offered_first(tmp_path):
    """Ordering is a convenience, not a decision — the newest drop is what
    a new job is almost always about, and both stay visible."""
    root = _project(tmp_path)
    _json_source(_drop(root, "20260810"), "Old.json")
    _json_source(_drop(root, "20260828"), "New.json")
    assert discover_sources(root).candidates[0].path.name == "New.json"


# ------------------------------------------------------ what gets skipped

@pytest.mark.parametrize("name", [
    "dedup_index.json", "compare_report.json", "glossary_terms.json",
    "run_summary.json", "bug_report.json", "README.json",
])
def test_pipeline_outputs_are_not_mistaken_for_sources(tmp_path, name):
    """A drop folder accumulates our own outputs. Ingesting a dedup index
    as source strings would be nonsense the pipeline cannot detect."""
    root = _project(tmp_path)
    drop = _drop(root, "20260828")
    _json_source(drop, "Game.json")
    (drop / name).write_text('{"x":"y"}', encoding="utf-8")

    names = [c.path.name for c in discover_sources(root).candidates]
    assert names == ["Game.json"]


def test_an_unsupported_format_is_ignored(tmp_path):
    root = _project(tmp_path)
    drop = _drop(root, "20260828")
    _json_source(drop, "Game.json")
    (drop / "notes.txt").write_text("hello", encoding="utf-8")
    (drop / "sheet.xlsx").write_bytes(b"PK\x03\x04")

    assert len(discover_sources(root).candidates) == 1


def test_the_supported_formats_are_the_ones_s1_ingests(tmp_path):
    """Discovery must not offer a format the ingest stage would reject."""
    from orbit8.ingest import ADAPTERS
    assert set(SOURCE_SUFFIXES) == set(ADAPTERS)


def test_a_malformed_file_is_reported_not_silently_dropped(tmp_path):
    """An unreadable drop is a finding. Silence would leave the operator
    wondering why their file was not offered."""
    root = _project(tmp_path)
    drop = _drop(root, "20260828")
    _json_source(drop, "Game.json")
    (drop / "broken.json").write_text("not json at all", encoding="utf-8")

    found = discover_sources(root)
    assert len(found.candidates) == 1
    assert any("broken.json" in note for note in found.notes)


def test_a_nested_json_is_rejected_with_a_reason(tmp_path):
    """S1 wants a flat {key: text} object; a nested export is a common
    near-miss that would fail at ingest instead of here."""
    root = _project(tmp_path)
    drop = _drop(root, "20260828")
    (drop / "nested.json").write_text(
        json.dumps({"ui": {"start": "开始"}}), encoding="utf-8")

    found = discover_sources(root)
    assert not found.candidates
    assert any("nested" in note.lower() or "not all strings" in note
               for note in found.notes)


# -------------------------------------------- the target-vs-source trap

def test_a_translated_po_is_flagged_as_probably_a_target(tmp_path):
    """A .po with filled msgstr is a TARGET file. Fed in as a source it
    would push already-translated text through the pipeline — which
    produces plausible output and is wrong."""
    root = _project(tmp_path)
    drop = _drop(root, "20260828")
    (drop / "Translated.po").write_text(
        'msgctxt ",A"\nmsgid "开始"\nmsgstr "Start"\n', encoding="utf-8")

    candidate, = discover_sources(root).candidates
    assert "target" in candidate.note


def test_an_untranslated_po_carries_no_warning(tmp_path):
    root = _project(tmp_path)
    drop = _drop(root, "20260828")
    (drop / "Source.po").write_text(
        'msgctxt ",A"\nmsgid "开始"\nmsgstr ""\n', encoding="utf-8")

    candidate, = discover_sources(root).candidates
    assert candidate.note == ""


# ------------------------------------------------------- no project found

def test_a_folder_that_is_not_a_project_says_so(tmp_path):
    found = discover_sources(tmp_path)
    assert not found.found
    assert found.project_root is None
    assert any("no project workspace" in note for note in found.notes)


def test_a_project_with_no_received_dir_says_so(tmp_path):
    (tmp_path / "20-work").mkdir()
    (tmp_path / "40-reference").mkdir()
    found = discover_sources(tmp_path)
    assert found.project_root == tmp_path
    assert any("10-received" in note for note in found.notes)


def test_an_empty_received_dir_says_so(tmp_path):
    root = _project(tmp_path)
    found = discover_sources(root)
    assert not found.found
    assert any("no .json or .po" in note for note in found.notes)


def test_an_explicit_project_root_skips_the_upward_walk(tmp_path):
    root = _project(tmp_path)
    _json_source(_drop(root, "20260828"), "Game.json")
    nested = root / "20-work" / "somewhere" / "deep"
    nested.mkdir(parents=True)
    assert discover_sources(nested, project_root=root).found
