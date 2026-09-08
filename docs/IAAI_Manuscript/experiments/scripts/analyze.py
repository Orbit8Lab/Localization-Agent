"""Turn the two result files into the paper's tables.

Reports effect sizes AND the sample size behind them. With n=174 and 57
positives, a 5-point F1 move is roughly three strings, so a table that
prints only percentages invites the reader to over-read it — every row
carries raw counts and a Wilson interval for that reason.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RESULTS = HERE / "results"


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score interval — correct near 0 and 1, where the normal
    approximation produces bounds outside [0,1] on samples this size."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def table_lqa(path: Path) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    n, pos = d["n_rows"], d["n_rejected"]
    lines = [f"### LQA detection accuracy (n={n}, {pos} human-rejected)",
             "",
             "| Condition | TP | FP | FN | Precision | Recall | F1 | FP rate | Tokens | Time |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in d["results"]:
        lo, hi = wilson(r["tp"], r["tp"] + r["fn"])
        lines.append(
            f"| {r['condition']} | {r['tp']} | {r['fp']} | {r['fn']} "
            f"| {r['precision']:.1%} | {r['recall']:.1%} "
            f"[{lo:.0%}–{hi:.0%}] | {r['f1']:.3f} | {r['fp_rate']:.1%} "
            f"| {r['tokens']:,.0f} | {r['seconds']:.0f}s |")
    return "\n".join(lines)


def table_translation(path: Path) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    n = d["n_rows"]
    # The production MT and the human post-edit are fixed artifacts, not
    # runs, but the table is unreadable without them: they are the
    # baseline the paper compares against and the ceiling that makes the
    # other rows interpretable.
    rows = list(d["results"])
    # Dataset-specific baselines; round1's are July-era corrected.
    stem = path.stem.replace("translation_", "")
    base = RESULTS / f"baseline_rows_{'round1' if 'r1' in stem else 'mtpe'}.json"
    if not base.exists():
        base = RESULTS / "baseline_rows_mtpe.json"
    if base.exists():
        fixed = json.loads(base.read_text(encoding="utf-8"))
        rows = ([f for f in fixed if f["condition"].startswith("A_")]
                + rows
                + [f for f in fixed if f["condition"].startswith("PE_")])
    lines = [f"### Translation quality (n={n})",
             "",
             "Primary metrics are defect counts against the client's own",
             "written rules and locked glossary; chrF is reported for",
             "continuity with MT practice but no claim rests on it.",
             "",
             "| System | Calls | Rule viol. | By rule | Locked-term err | Inconsist. | chrF | Tokens | Time |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        by = ", ".join(f"{k}:{v}" for k, v in
                       sorted(r.get("violations_by_rule", {}).items())) or "—"
        lines.append(
            f"| {r['condition']} | {r['calls'] or '—'} "
            f"| {r['violation_strings']}/{r['scored']} "
            f"({r['violation_rate']:.1%}) | {by} "
            f"| {r['term_errors']} | {r['inconsistent_terms']} "
            f"| {r['chrf']:.3f} "
            f"| {r['tokens']:,.0f} | {r['seconds']:.0f}s |")
    return "\n".join(lines)


def table_lqa_views(path: Path) -> str:
    """All three scoring views side by side.

    On the round-1 corpus 70% of rejections carry no bug label (F15), so
    the full-view recall measures coincidental agreement with fluency
    rewrites rather than detection. The labelled view is the detector
    score; the actionable view excludes LOW advisories. Which one is
    "the" number depends on the consumer, so all three are shown.
    """
    d = json.loads(path.read_text(encoding="utf-8"))
    if not any(r.get("labelled_only") for r in d["results"]):
        return ""
    lines = ["### LQA, three scoring views "
             f"(n={d['n_rows']}, {d['n_rejected']} rejections)",
             "",
             "| Condition | all: P | all: R | all: F1 | lab: P | lab: R | lab: F1 | Tokens | Min |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in d["results"]:
        lab = r.get("labelled_only") or {}
        lines.append(
            f"| {r['condition']} | {r['precision']:.1%} | {r['recall']:.1%} "
            f"| {r['f1']:.3f} | {lab.get('precision', 0):.1%} "
            f"| {lab.get('recall', 0):.1%} | {lab.get('f1', 0):.3f} "
            f"| {r['tokens']:,.0f} | {r['seconds']/60:.0f} |")
    return "\n".join(lines)


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "main"
    out = ["# IAAI experiment results", "",
           f"Run tag: `{tag}`. Corpus: project002 (绯月杀), zh→en.",
           "Ground truth is the professional post-editor's verdict.", ""]
    for name, fn in ((f"translation_{tag}.json", table_translation),
                     (f"lqa_{tag}.json", table_lqa),
                     (f"lqa_{tag}.json", table_lqa_views)):
        tag_, fn = name, fn
        path = RESULTS / tag_
        if path.exists():
            rendered = fn(path)
            if rendered:
                out += [rendered, ""]
        else:
            out += [f"_{tag_} not present yet_", ""]
    text = "\n".join(out)
    (RESULTS / f"TABLES_{tag}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
