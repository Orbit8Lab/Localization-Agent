"""Is the display-width budget the dominant false-positive source?

gate_checks.py carries an explicit caveat on `width_budget`: it was
fitted on en→zh corpora (Factorio, Endless Legend) while this pipeline
runs zh→en, and the comment asks for re-fitting once real data exists.
The MTPE form IS that data — 117 strings a professional accepted, so any
width finding on those is a false positive by the only standard the
client recognises.
"""
from __future__ import annotations

import collections
import json
from pathlib import Path

from orbit8.gate_checks import display_width

HERE = Path(__file__).resolve().parents[1]
rows = [json.loads(l) for l in
        (HERE / "data/p002_mtpe_groundtruth.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()]

BUDGET = {"UI": 2.4, "Skill": 2.5, "Item": 2.5, "System": 2.45}

by_type = collections.defaultdict(lambda: {"acc": [], "rej": []})
for r in rows:
    sw = display_width(r["source"])
    if sw < 6:                      # below the noise floor the check uses
        continue
    ratio = display_width(r["mt"]) / sw
    bucket = "rej" if r["human_rejected"] else "acc"
    by_type[r["string_type"]][bucket].append((ratio, r))

print("Observed target/source display-width ratio, by string type")
print("(only strings the check would actually judge: source width >= 6)\n")
print(f"{'type':8s} {'budget':>7s} {'n_acc':>6s} {'acc p50':>8s} "
      f"{'acc p95':>8s} {'acc max':>8s} {'FP@budget':>10s}")
for stype in sorted(by_type):
    b = BUDGET.get(stype)
    acc = sorted(x[0] for x in by_type[stype]["acc"])
    if not acc:
        continue
    p50 = acc[len(acc) // 2]
    p95 = acc[min(len(acc) - 1, int(len(acc) * 0.95))]
    fp = sum(1 for x in acc if b and x > b)
    print(f"{stype:8s} {b if b else '—':>7} {len(acc):>6d} {p50:>8.2f} "
          f"{p95:>8.2f} {acc[-1]:>8.2f} {fp:>10d}")

# What ceiling would clear the accepted strings this corpus actually has?
print("\nCeiling needed to stop flagging strings the human ACCEPTED:")
for stype in sorted(by_type):
    acc = sorted(x[0] for x in by_type[stype]["acc"])
    if acc and BUDGET.get(stype):
        print(f"  {stype:8s} current {BUDGET[stype]:.2f} → "
              f"observed accepted max {acc[-1]:.2f}")

# And does width actually separate good from bad here at all?
print("\nDoes width separate accepted from rejected?")
for stype in sorted(by_type):
    acc = [x[0] for x in by_type[stype]["acc"]]
    rej = [x[0] for x in by_type[stype]["rej"]]
    if len(acc) >= 5 and len(rej) >= 5:
        print(f"  {stype:8s} accepted mean {sum(acc)/len(acc):.2f} "
              f"vs rejected mean {sum(rej)/len(rej):.2f} "
              f"(n={len(acc)}/{len(rej)})")
