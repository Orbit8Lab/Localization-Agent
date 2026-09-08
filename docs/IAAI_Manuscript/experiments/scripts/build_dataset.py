"""Extract the evaluation dataset from the project002 MTPE form.

The MTPE form is the only artifact in the corpus that carries a HUMAN
verdict on each machine translation: a professional post-editor marked
every row Accept or Reject&Modification and, when rejecting, wrote the
corrected target. That makes it ground truth for two different questions
at once, which is why the experiment is built on it rather than on a
BLEU-style reference set:

  - translation quality — does a condition reproduce the human's
    accepted target, or does it need the same edit the human made?
  - LQA accuracy — does the scanner flag the rows the human rejected
    (true positives) and stay quiet on the rows they accepted?

`Accept Translation` rows are NOT "unflagged forever": a human accepting
a string means it shipped, so a scanner firing there is a false positive
against the only standard the client recognises.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import openpyxl

DRIVE = Path("/Users/maotian/Library/CloudStorage/"
             "GoogleDrive-info@orbit8lab.com/My Drive/Orbit8LabInternal/"
             "project")
MTPE = DRIVE / "project002-绯月杀/10-received/20260803-MTPE/mtpe_form.xlsx"
OUT = Path(__file__).resolve().parents[1] / "data"

# StringType in the client's form → the pipeline's own domain vocabulary.
# Kept explicit rather than lowercased: `Skill`/`Item` are the client's
# categories and map onto `item_desc`, which is what the style guide and
# the batch policy key off.
DOMAIN = {"UI": "ui", "System": "system", "Skill": "item_desc",
          "Item": "item_desc"}


def main() -> int:
    if not MTPE.exists():
        print(f"missing ground truth: {MTPE}", file=sys.stderr)
        return 1
    ws = openpyxl.load_workbook(MTPE, data_only=True)["MTPE"]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        key, stype, src, mt, decision, mod = r[0], r[1], r[2], r[3], r[4], r[5]
        if not key or not src or not mt:
            continue
        if not decision:
            continue          # blank = never reviewed, so it has no label
        rejected = str(decision).startswith("Reject")
        # A rejection with no rewrite tells us the row is wrong but not
        # what right looks like. Keep it for the LQA label, exclude it
        # from the translation reference.
        reference = str(mod).strip() if mod else None
        rows.append({
            "key": str(key), "string_type": str(stype or ""),
            "domain": DOMAIN.get(str(stype or ""), "system"),
            "source": str(src), "mt": str(mt),
            "human_rejected": rejected,
            "reference": reference or (None if rejected else str(mt)),
        })

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "p002_mtpe_groundtruth.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    n_rej = sum(r["human_rejected"] for r in rows)
    print(f"wrote {len(rows)} labelled rows → {path}")
    print(f"  human-rejected : {n_rej}")
    print(f"  human-accepted : {len(rows) - n_rej}")
    print(f"  with reference : {sum(r['reference'] is not None for r in rows)}")
    by = {}
    for r in rows:
        by.setdefault(r["domain"], [0, 0])
        by[r["domain"]][0] += 1
        by[r["domain"]][1] += r["human_rejected"]
    for dom, (n, rej) in sorted(by.items()):
        print(f"  {dom:10s} n={n:4d} rejected={rej:3d} ({rej/n:.0%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
