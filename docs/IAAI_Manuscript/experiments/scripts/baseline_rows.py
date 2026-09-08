"""Score the two fixed systems in the corpus: production MT and human PE.

Neither is a condition we can run — the production MT is a delivered
artifact and the post-edit is the ground truth — but both must appear in
the results table. The MT is condition A (the conventional baseline the
paper compares against) and the post-edit is the ceiling, which is what
makes the other rows readable: without it a reader cannot tell whether
"11 locked-term errors" is good.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics

HERE = Path(__file__).resolve().parents[1]
DRIVE = Path("/Users/maotian/Library/CloudStorage/"
             "GoogleDrive-info@orbit8lab.com/My Drive/Orbit8LabInternal/"
             "project")


_DATASETS = {"mtpe": "p002_mtpe_groundtruth.jsonl",
             "round1": "p002_round1_groundtruth.jsonl"}


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "mtpe"
    rows = [json.loads(l) for l in
            (HERE / "data" / _DATASETS[tag]).read_text(
                encoding="utf-8").splitlines() if l.strip()]
    t1 = json.loads((DRIVE / "project002-绯月杀/40-reference/glossary/"
                     "glossary_terms.json").read_text(encoding="utf-8"))
    locked = {zh: e["translation"] for zh, e in t1["terms"].items()
              if e.get("locked")}
    forms = {zh: e["forms"] for zh, e in t1["terms"].items()
             if e.get("forms")}
    # 22 of 41 locked terms were ratified AFTER the 2026-07-19 PE pass
    # (F17). Scoring round-1 output against them measures the glossary's
    # evolution, not the translation: it put the human post-edit at 155
    # term errors against the raw MT's 180, i.e. destroyed the metric's
    # ability to tell good from bad. Excluded for that dataset only.
    excl_path = HERE / "data" / "round1_anachronistic_terms.json"
    if tag == "round1" and excl_path.exists():
        excluded = set(json.loads(excl_path.read_text(encoding="utf-8")))
        locked = {zh: v for zh, v in locked.items() if zh not in excluded}
        print(f"July-era glossary: {len(locked)} locked terms "
              f"({len(excluded)} post-dated terms excluded)")

    out = []
    for field, name in (("mt", "A_production_mt"),
                        ("reference", "PE_human_ceiling")):
        v = collections.Counter()
        nb = te = 0
        ch = 0.0
        pairs = []
        for r in rows:
            t = r[field] or ""
            vio = metrics.rule_violations(r["source"], t)
            v.update(vio)
            nb += bool(vio)
            te += len(metrics.term_errors(r["source"], t, locked, forms))
            ch += metrics.chrf(t, r["reference"] or "")
            pairs.append((r["source"], t))
        conflicts = metrics.consistency_conflicts(pairs, locked, forms)
        out.append({
            "condition": name,
            # These are artifacts, not runs: no calls, no tokens, no time.
            "batch_size": None, "agentic": None, "glossary": None,
            "style_guide": None, "calls": 0, "tokens": 0.0, "seconds": 0.0,
            "n": len(rows), "scored": len(rows), "missing": 0,
            "violation_strings": nb,
            "violation_rate": round(nb / len(rows), 4),
            "violations_by_rule": dict(v),
            "term_errors": te,
            "inconsistent_terms": len(conflicts),
            "chrf": round(ch / len(rows), 4),
        })
        print(f"{name:18s} violations={nb:3d}/{len(rows)} {dict(v)} "
              f"term_err={te:3d} inconsistent={len(conflicts)} "
              f"chrF={ch/len(rows):.3f}")

    path = HERE / "results" / f"baseline_rows_{tag}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
