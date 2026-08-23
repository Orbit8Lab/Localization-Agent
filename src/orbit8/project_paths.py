"""Canonical project asset resolution (docs/STANDARDS.md §1.1, §2.1).

The active glossary is a PROJECT asset, not a run output: it lives at
``40-reference/glossary/glossary_terms.json``. Run folders under
``20-work/`` hold the round that PRODUCED a glossary; whatever is
promoted to 40-reference is what every later stage must consume.

``resolve_glossary`` implements that rule so no caller (CLI, chat tool,
operator) has to remember a dated folder name — and so a stale run
folder can never silently become the source of truth.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple

from .standards import REFERENCE_DIR, WORK_DIR

GLOSSARY_SUBDIR = "glossary"
GLOSSARY_FILE = "glossary_terms.json"

# Directory names that mark a project root (any two is enough — projects
# migrated at different times may lack one).
_PROJECT_MARKERS = ("10-received", WORK_DIR, REFERENCE_DIR,
                    "30-deliverables")


def find_project_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` to the nearest folder that looks like a
    standard project workspace."""
    current = Path(start).resolve()
    for candidate in (current, *current.parents):
        if not candidate.is_dir():
            continue
        hits = sum(1 for marker in _PROJECT_MARKERS
                   if (candidate / marker).is_dir())
        if hits >= 2:
            return candidate
    return None


def canonical_glossary(project_root: Path) -> Path:
    return (Path(project_root) / REFERENCE_DIR / GLOSSARY_SUBDIR
            / GLOSSARY_FILE)


def resolve_glossary(hint: Optional[Path] = None,
                     project_root: Optional[Path] = None,
                     start: Optional[Path] = None
                     ) -> Tuple[Optional[Path], List[str]]:
    """Locate the glossary to use. Returns (path, notes).

    Order: an explicit ``hint`` always wins; otherwise the canonical
    ``40-reference/glossary/glossary_terms.json``; otherwise the newest
    run output under ``20-work/`` — which is reported as a WARNING,
    because consuming a run folder means the project has no promoted
    glossary yet.
    """
    notes: List[str] = []
    if hint:
        path = Path(hint)
        if path.is_dir():
            path = path / GLOSSARY_FILE
        if not path.exists():
            notes.append(f"given glossary not found: {path}")
            return None, notes
        return path, notes

    root = Path(project_root) if project_root else find_project_root(
        Path(start or Path.cwd()))
    if root is None:
        notes.append("no project workspace found (need a folder with "
                     "10-received/ 20-work/ 40-reference/)")
        return None, notes

    canonical = canonical_glossary(root)
    if canonical.exists():
        return canonical, notes

    candidates = sorted((root / WORK_DIR).glob(f"*/{GLOSSARY_FILE}"))
    if not candidates:
        notes.append(f"no glossary at {canonical} and none under "
                     f"{root / WORK_DIR}")
        return None, notes
    newest = candidates[-1]
    notes.append(
        f"WARNING: no promoted glossary at {canonical}; using the newest "
        f"run output {newest.parent.name}/{GLOSSARY_FILE}. Promote it "
        f"with `orbit8 glossary promote` so every stage agrees.")
    return newest, notes


STYLE_SUBDIR = "style"


def _lang_candidates(lang: str) -> List[str]:
    """A language tag and the regional spellings a guide may be filed
    under: 'zh' also looks for 'zh-CN', and 'zh-CN' also looks for 'zh'."""
    out = [lang]
    base = lang.split("-")[0]
    if base != lang:
        out.append(base)
    else:
        out += [f"{lang}-{region}" for region in ("CN", "TW", "HK")]
    return out


def canonical_style(project_root: Path, source_lang: str,
                    target_lang: str) -> Path:
    return (Path(project_root) / REFERENCE_DIR / STYLE_SUBDIR
            / f"{source_lang}-{target_lang}.json")


def resolve_style_guide(source_lang: str, target_lang: str, *,
                        hint: Optional[Path] = None,
                        project_root: Optional[Path] = None,
                        start: Optional[Path] = None):
    """Project style guide for a language pair, else the built-in starter
    guide, else None. Returns (guide, notes) — a project guide always
    wins, and falling back to the default is reported, never silent."""
    from .style_defaults import default_guide
    from .style_guide import StyleGuide
    notes: List[str] = []
    if hint:
        return StyleGuide.load(Path(hint)), notes
    root = Path(project_root) if project_root else find_project_root(
        Path(start or Path.cwd()))
    if root is not None:
        path = canonical_style(root, source_lang, target_lang)
        if path.exists():
            return StyleGuide.load(path), notes
        # A pipeline says "zh" where the guide is authored "zh-CN" (and
        # vice versa). Falling back to the STARTER guide because of a tag
        # spelling silently discards the project's own rules — the
        # failure is invisible in the output, so try the regional
        # variants before giving up.
        for src in _lang_candidates(source_lang):
            for tgt in _lang_candidates(target_lang):
                if (src, tgt) == (source_lang, target_lang):
                    continue
                alt = canonical_style(root, src, tgt)
                if alt.exists():
                    notes.append(
                        f"style guide for {source_lang}→{target_lang} "
                        f"resolved to {alt.name} ({src}→{tgt})")
                    return StyleGuide.load(alt), notes
    fallback = default_guide(source_lang, target_lang)
    if fallback is not None:
        notes.append(
            f"no project style guide for {source_lang}→{target_lang}; "
            f"using the built-in starter guide (v{fallback.version}). "
            f"Author one with `orbit8 style init`.")
        return fallback, notes
    notes.append(f"no style guide available for "
                 f"{source_lang}→{target_lang} — style rules not enforced")
    return None, notes


def promote_glossary(source: Path, project_root: Path) -> Path:
    """Copy a run's glossary (json + xlsx view if present) to the
    canonical project location. The run folder is left intact — it stays
    the audit record of the round that produced this glossary."""
    import shutil
    source = Path(source)
    if source.is_dir():
        source = source / GLOSSARY_FILE
    target = canonical_glossary(Path(project_root))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    xlsx = source.with_suffix(".xlsx")
    if xlsx.exists():
        shutil.copy2(xlsx, target.with_suffix(".xlsx"))
    return target
