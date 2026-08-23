"""Translate the untranslated strings of a received bilingual .po.

Targeted incremental translation (not the full job pipeline): the received
drop already carries most translations; only entries with an empty msgstr
are processed. The glossary (T1 shape, family rules included) is law:

1. exact-hit prefill — a source string that IS a glossary term costs
   zero LLM calls;
2. batched DeepSeek translation via ``agents.translate_batch``, each batch
   carrying only the glossary slice its sources match;
3. deterministic gate — every locked term / family rule present in a
   source must appear in the target; violators get ONE repair retry, then
   stay flagged for the post-editor;
4. outputs: a stream-patched copy of the received file (untouched entries
   byte-identical — format fidelity), the standard MTPE form (§4.1) for
   post-editing, and a run report. ``po_sanity.check_format`` verifies the
   patched file imports cleanly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .agents import translate_batch
from .exports import read_po_entries
from .gate_checks import term_in_text
from .glossary import Glossary
from .glossary_update import _en_has
from .pe_form import emit_pe_form
from .po_patch import patch_po_file
from .schemas import TermBrief

# Order is significant: "ui" is LAST because it is a container folder in
# UE export paths (/Game/Program/UI/... holds dialogue, wiki and skill
# text alike), so a generic parent must never outrank a specific leaf.
# Matching it first labelled every dialogue string "UI" and judged prose
# against a button's display-width budget.
_TYPE_HINTS = (("skill", "Skill"), ("item", "Item"), ("dialog", "Dialogue"),
               ("subtitle", "Dialogue"), ("wiki", "Dialogue"),
               ("marketing", "Marketing"), ("system", "System"),
               ("ui", "UI"))


def _string_type(location: str) -> str:
    """The widget class implied by a SourceLocation path.

    Used both for the PE form's StringType column and to select the
    display-width budget, so the two can never disagree.
    """
    low = location.lower()
    for needle, label in _TYPE_HINTS:
        if needle in low:
            return label
    return "Others"


@dataclass
class TranslateRun:
    total: int
    todo: int
    reused: Dict[str, str] = field(default_factory=dict)
    prefilled: Dict[str, str] = field(default_factory=dict)
    translated: Dict[str, str] = field(default_factory=dict)
    violations: List[dict] = field(default_factory=list)
    repaired: List[str] = field(default_factory=list)
    tokens: float = 0.0
    model: str = ""
    sanity: str = ""

    @property
    def all_targets(self) -> Dict[str, str]:
        return {**self.translated, **self.prefilled, **self.reused}


def _load_glossary(t1_path: Path, game: str, locale: str
                   ) -> Tuple[Glossary, Dict[str, str]]:
    """Returns (glossary for prompt steering, hard constraints for the
    gate). The WHOLE glossary steers the prompt, but only entries with
    ``locked: true`` plus family rules are enforced — mined/draft entries
    are evidence, not law."""
    t1 = Glossary.load_t1_file(t1_path)
    glossary = Glossary.from_layers(t1.get("metadata", {}).get("game")
                                    or game, locale, t1=t1)
    constraints = {zh: entry["translation"]
                   for zh, entry in t1.get("terms", {}).items()
                   if entry.get("locked")}
    for zh, rule in (t1.get("metadata", {}).get("family_rules")
                     or {}).items():
        constraints.setdefault(zh, rule["translation"])
        if zh.lower() not in glossary.terms:
            glossary.terms[zh.lower()] = TermBrief(
                term=zh, translation=rule["translation"], tier=1,
                sense_note="family rule — applies to every term "
                           "containing it")
    return glossary, constraints


def _gate(constraints: Dict[str, str], source: str,
          target: str) -> List[dict]:
    out = []
    for term, rendering in constraints.items():
        if not term_in_text(term, source):
            continue
        # "Infection / Infect" style alternatives: any variant satisfies
        variants = [v.strip() for v in rendering.split("/") if v.strip()]
        if not any(_en_has(v, target) for v in variants):
            out.append({"term": term, "expected": rendering})
    return out


def translate_untranslated(po_path: Path, t1_path: Path, out_dir: Path, *,
                           provider, game: str, locale: str = "en",
                           source_lang: str = "zh-CN",
                           batch_size: int = 12,
                           reuse_from: Optional[Path] = None
                           ) -> TranslateRun:
    po_path, out_dir = Path(po_path), Path(out_dir)
    entries = [(k, zh, en, loc)
               for k, zh, en, loc in read_po_entries(po_path) if k]
    todo = [(k, zh, loc) for k, zh, en, loc in entries
            if zh.strip() and not en.strip()]
    glossary, constraints = _load_glossary(Path(t1_path), game, locale)
    run = TranslateRun(total=len(entries), todo=len(todo))

    # translations carried over from a previous run/delivery. A carry
    # requires the SOURCE to be identical — a key whose source text
    # changed between drops must NOT inherit the old translation (that
    # is exactly the stale-translation defect po_compare flags); it goes
    # to the LLM instead.
    reuse_by_key: Dict[str, Tuple[str, str]] = {}   # key -> (zh, en)
    reuse_by_zh: Dict[str, str] = {}
    if reuse_from:
        for key, zh, en, _loc in read_po_entries(Path(reuse_from)):
            if key and en.strip():
                reuse_by_key[key] = (zh, en)
                reuse_by_zh.setdefault(zh, en)

    remaining: List[Tuple[str, str, str]] = []
    for key, zh, loc in todo:
        hit = reuse_by_key.get(key)
        carried = (hit[1] if hit and hit[0] == zh
                   else reuse_by_zh.get(zh))
        if carried:
            run.reused[key] = carried
            continue
        hit = glossary.prefill(zh)
        if hit is not None:
            run.prefilled[key] = hit
        else:
            remaining.append((key, zh, loc))

    # Keys go to the model as opaque batch-local aliases (s0, s1, …):
    # real msgctxt keys (",<hash>" in UE exports) get "cleaned" by LLMs,
    # which trips the coverage check. Aliases make that impossible.
    def _run_batch(batch: List[Tuple[str, str, str]],
                   temperature: float = 0.3) -> str:
        alias = {f"s{i}": key for i, (key, _, _) in enumerate(batch)}
        brief = glossary.brief_for([zh for _, zh, _ in batch])
        result, fingerprint = translate_batch(
            provider, [(f"s{i}", zh)
                       for i, (_, zh, _) in enumerate(batch)],
            source_lang=source_lang, target_lang=locale, game=game,
            glossary_brief=brief, temperature=temperature)
        for item in result.items:
            run.translated[alias[item.key]] = item.target_text
        return fingerprint

    for start in range(0, len(remaining), batch_size):
        run.model = _run_batch(remaining[start:start + batch_size])

    # deterministic glossary gate + one repair retry
    src_by_key = {k: zh for k, zh, _ in todo}
    loc_map = {k: loc for k, zh, loc in todo}
    flagged = [k for k, tgt in run.translated.items()
               if _gate(constraints, src_by_key[k], tgt)]
    if flagged:
        before = {k: run.translated[k] for k in flagged}
        _run_batch([(k, src_by_key[k], loc_map.get(k, ""))
                    for k in flagged], temperature=0.0)
        for key in flagged:
            if _gate(constraints, src_by_key[key], run.translated[key]):
                run.translated[key] = before[key]   # retry no better
            elif run.translated[key] != before[key]:
                run.repaired.append(key)
    for key, target in run.all_targets.items():
        for violation in _gate(constraints, src_by_key[key], target):
            run.violations.append({"key": key, **violation,
                                   "target": target})
    run.tokens = getattr(provider, "tokens_spent", 0.0)

    # outputs -------------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / po_path.name
    patch_po_file(po_path, dst, run.all_targets)
    from .po_sanity import check_format
    issues = check_format(dst, expect_bom=po_path.read_bytes()[:3]
                          == b"\xef\xbb\xbf")
    errors = [i for i in issues if getattr(i, "severity", "") == "ERROR"]
    run.sanity = "ok" if not errors else "; ".join(
        getattr(i, "message", str(i)) for i in errors[:5])

    loc_by_key = {k: loc for k, zh, loc in todo}
    flagged_keys = {v["key"] for v in run.violations}
    # form order: violations first, fresh MT next, reused wording last
    rows = [{"StringID": key,
             "StringType": _string_type(loc_by_key.get(key, "")),
             "Source": src_by_key[key], "Target_MT": target}
            for key, target in sorted(
                run.all_targets.items(),
                key=lambda kv: (kv[0] not in flagged_keys,
                                kv[0] in run.reused, kv[0]))]
    emit_pe_form(out_dir / "mtpe_form.xlsx", "mtpe", rows)

    report = {
        "po": str(po_path), "glossary": str(t1_path),
        "entries_total": run.total, "untranslated": run.todo,
        "reused": len(run.reused),
        "prefilled": len(run.prefilled),
        "translated": len(run.translated),
        "repaired": run.repaired, "violations": run.violations,
        "model": run.model, "tokens_spent": run.tokens,
        "sanity_format": run.sanity}
    (out_dir / "translate_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1),
        encoding="utf-8")
    lines = ["# Incremental translation report", "",
             f"- source drop: `{po_path}`",
             f"- glossary: `{t1_path}`",
             f"- {run.todo} untranslated of {run.total} entries; "
             f"{len(run.reused)} reused from previous run, "
             f"{len(run.prefilled)} glossary prefills (no LLM), "
             f"{len(run.translated)} machine-translated",
             f"- model: {run.model}; tokens: {int(run.tokens)}",
             f"- repair retries that succeeded: {len(run.repaired)}",
             f"- REMAINING glossary violations: {len(run.violations)} "
             "(sorted first in mtpe_form.xlsx)",
             f"- patched file format check: {run.sanity}", "",
             "NOT A DELIVERABLE — route mtpe_form.xlsx through "
             "post-editing, then deliver via `orbit8 lqa deliver`."]
    for violation in run.violations[:20]:
        lines.append(f"  - ⚠ {violation['key']}: {violation['term']} → "
                     f"{violation['expected']!r} missing")
    (out_dir / "translate_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return run
