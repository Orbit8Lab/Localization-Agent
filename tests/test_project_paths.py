"""Canonical project asset resolution."""
import json
from pathlib import Path

from orbit8.project_paths import (canonical_glossary, find_project_root,
                                  promote_glossary, resolve_glossary)

T1 = {"metadata": {"game": "x"}, "terms": {}}


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "P00N-demo"
    for d in ("10-received", "20-work", "30-deliverables",
              "40-reference"):
        (root / d).mkdir(parents=True)
    return root


def _write(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(T1), encoding="utf-8")
    return path


def test_find_project_root_from_nested_path(tmp_path: Path):
    root = _project(tmp_path)
    nested = root / "20-work" / "asset-glossary-merge-20260802-2127"
    nested.mkdir()
    assert find_project_root(nested) == root
    assert find_project_root(tmp_path) is None


def test_canonical_wins_over_run_folders(tmp_path: Path):
    root = _project(tmp_path)
    _write(root / "20-work/asset-glossary-merge-20260101-0000"
                  "/glossary_terms.json")
    canonical = _write(canonical_glossary(root))
    path, notes = resolve_glossary(project_root=root)
    assert path == canonical and notes == []


def test_falls_back_to_newest_run_with_warning(tmp_path: Path):
    root = _project(tmp_path)
    _write(root / "20-work/asset-glossary-merge-20260101-0000"
                  "/glossary_terms.json")
    newest = _write(root / "20-work/asset-glossary-merge-20260802-2127"
                           "/glossary_terms.json")
    path, notes = resolve_glossary(project_root=root)
    assert path == newest
    assert notes and notes[0].startswith("WARNING")
    assert "promote" in notes[0]


def test_explicit_hint_wins_and_accepts_directory(tmp_path: Path):
    root = _project(tmp_path)
    _write(canonical_glossary(root))
    run = _write(root / "20-work/run-20260803-1200/glossary_terms.json")
    assert resolve_glossary(hint=run, project_root=root)[0] == run
    # a directory hint resolves to its glossary_terms.json
    assert resolve_glossary(hint=run.parent,
                            project_root=root)[0] == run
    missing, notes = resolve_glossary(hint=tmp_path / "nope.json")
    assert missing is None and "not found" in notes[0]


def test_promote_copies_json_and_xlsx(tmp_path: Path):
    root = _project(tmp_path)
    run = _write(root / "20-work/run-20260803-1200/glossary_terms.json")
    run.with_suffix(".xlsx").write_bytes(b"fake xlsx")
    target = promote_glossary(run, root)
    assert target == canonical_glossary(root)
    assert json.loads(target.read_text()) == T1
    assert target.with_suffix(".xlsx").read_bytes() == b"fake xlsx"
    assert run.exists()                      # run folder left intact
    assert resolve_glossary(project_root=root) == (target, [])
