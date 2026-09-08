"""Figure data for the manuscript.

Emits tidy CSV per figure rather than images: the manuscript is written
in LaTeX and the user's plotting standard is BoutrosLab.plotting.general
in R, so the R script consumes these. Keeping data and rendering
separate also means a re-run of the experiment does not require
re-running the plots by hand.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RESULTS, FIG = HERE / "results", HERE / "figures"


def write(name: str, header: list, rows: list) -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    with (FIG / name).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"  {name}  ({len(rows)} rows)")


def main() -> int:
    print("figure data:")

    lqa = RESULTS / "lqa_main.json"
    if lqa.exists():
        d = json.loads(lqa.read_text(encoding="utf-8"))
        write("fig_lqa_accuracy.csv",
              ["condition", "precision", "recall", "f1", "tp", "fp", "fn",
               "tokens", "seconds"],
              [[r["condition"], r["precision"], r["recall"], r["f1"],
                r["tp"], r["fp"], r["fn"], round(r["tokens"]),
                r["seconds"]] for r in d["results"]])
        # FP composition — the §7.3 limitation figure
        rows = []
        for r in d["results"]:
            comp = {}
            for f in r.get("false_positives", []):
                for t in f["bug_types"]:
                    comp[t] = comp.get(t, 0) + 1
            for t, n in sorted(comp.items(), key=lambda x: -x[1]):
                rows.append([r["condition"], t, n])
        write("fig_fp_composition.csv", ["condition", "bug_type", "n"], rows)

    tr = RESULTS / "translation_main.json"
    if tr.exists():
        d = json.loads(tr.read_text(encoding="utf-8"))
        write("fig_translation_quality.csv",
              ["condition", "batch_size", "agentic", "glossary",
               "style_guide", "violation_strings", "violation_rate",
               "term_errors", "inconsistent_terms", "chrf", "calls",
               "tokens", "seconds"],
              [[r["condition"], r["batch_size"], int(r["agentic"]),
                int(r["glossary"]), int(r["style_guide"]),
                r["violation_strings"], r["violation_rate"],
                r["term_errors"], r["inconsistent_terms"], r["chrf"],
                r["calls"], round(r["tokens"]), r["seconds"]]
               for r in d["results"]])
        # cost/quality frontier — §6.2
        write("fig_cost_quality.csv",
              ["condition", "calls", "tokens", "seconds", "violation_rate",
               "term_errors"],
              [[r["condition"], r["calls"], round(r["tokens"]),
                r["seconds"], r["violation_rate"], r["term_errors"]]
               for r in d["results"]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
