"""Re-score stored condition outputs without spending a single token.

The metric fix for declared verb/noun forms landed while B4 was already
in flight, so that condition was scored by the process's in-memory copy
of the old code. Every condition stores its per-row outputs, which means
correct numbers are a pure re-computation — no re-run, no API calls.

This also makes the scoring reproducible independently of the runs: a
future metric change can be applied to all conditions at once, and the
tables regenerated, without the results depending on when each run
happened to execute.
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


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "main"
    path = HERE / "results" / f"translation_{tag}.json"
    if not path.exists():
        print(f"no results at {path}", file=sys.stderr)
        return 1
    rows = [json.loads(l) for l in
            (HERE / "data/p002_mtpe_groundtruth.jsonl").read_text(
                encoding="utf-8").splitlines() if l.strip()]
    t1 = json.loads((DRIVE / "project002-绯月杀/40-reference/glossary/"
                     "glossary_terms.json").read_text(encoding="utf-8"))
    locked = {zh: e["translation"] for zh, e in t1["terms"].items()
              if e.get("locked")}
    forms = {zh: e["forms"] for zh, e in t1["terms"].items()
             if e.get("forms")}

    doc = json.loads(path.read_text(encoding="utf-8"))
    for res in doc["results"]:
        got = {x["key"]: x["output"] for x in res.get("per_row", [])}
        if not got:
            continue
        v = collections.Counter()
        nb = te = 0
        pairs = []
        for r in rows:
            out = got.get(r["key"])
            if out is None:
                continue
            vio = metrics.rule_violations(r["source"], out)
            v.update(vio)
            nb += bool(vio)
            te += len(metrics.term_errors(r["source"], out, locked, forms))
            pairs.append((r["source"], out))
        conflicts = metrics.consistency_conflicts(pairs, locked, forms)
        before = (res["term_errors"], res["inconsistent_terms"])
        res.update(violation_strings=nb,
                   violation_rate=round(nb / max(1, res["scored"]), 4),
                   violations_by_rule=dict(v),
                   term_errors=te,
                   inconsistent_terms=len(conflicts),
                   inconsistent_detail={k: sorted(x)
                                        for k, x in conflicts.items()})
        print(f"{res['condition']:18s} term_err {before[0]:3d} -> {te:3d}   "
              f"inconsist {before[1]:2d} -> {len(conflicts):2d}   "
              f"viol {nb:3d}/{res['scored']} {dict(v)}")

    doc["rescored"] = True
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
