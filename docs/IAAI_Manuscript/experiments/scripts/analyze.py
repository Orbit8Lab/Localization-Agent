"""Turn the two result files into the paper's tables.

Reports effect sizes AND the sample size behind them. With n=174 and 57
positives, a 5-point F1 move is roughly three strings, so a table that
prints only percentages invites the reader to over-read it — every row
carries raw counts and a Wilson interval for that reason.
"""
from __future__ import annotations

import json
import math
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
    base = RESULTS / "baseline_rows.json"
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


def main() -> int:
    out = ["# IAAI experiment results", "",
           "Corpus: project002 (绯月杀), zh→en. Ground truth is the",
           "professional post-editor's verdict in `mtpe_form.xlsx`.", ""]
    for tag, fn in (("translation_main.json", table_translation),
                    ("lqa_main.json", table_lqa)):
        path = RESULTS / tag
        if path.exists():
            out += [fn(path), ""]
        else:
            out += [f"_{tag} not present yet_", ""]
    text = "\n".join(out)
    (RESULTS / "TABLES.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
