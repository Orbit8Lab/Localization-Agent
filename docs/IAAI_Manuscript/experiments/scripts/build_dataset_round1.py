"""Extract the round-1 PE master table — 7x the labelled data.

`pe-en-round1-20260719-0000/…_PE_complete.csv` is a full post-edit pass
over the whole corpus, not the 174-row sample the MTPE form covers:

    1,233 rows with source + target
      397 PE-Flag=yes  (32% defect rate)
      393 of those carry the editor's rewrite
      245 carry an explicit bug label and severity
      836 PE-Flag=no  (accepted)

This is INDEPENDENT of the MTPE set, not a superset of it. Only 23 ids
appear in both, and on those the two rounds disagree 8/23 — expected,
because the MT differs: round 1 (2026-07-19) reviewed an earlier
translation than the 2026-08-03 MTPE round. So the two form two
separate evaluations rather than one larger one, and combining their
verdicts would be comparing judgments about different text.

It also carries something the MTPE form does not: a human bug
CATEGORY and SEVERITY per finding (Terminology 130, Terminology
Inconsistency 57, Untranslated 39, Mistranslation 21, …). That makes
per-bug-type precision measurable for the first time, which no other
artifact in the archive supports.

Keys: the `unique id (po hash)` matches the PO `msgctxt` after
stripping the leading comma the client's export adds (1,056 of 1,233
resolve). Rows that do not resolve are kept — they are still scoreable
pairs — but flagged so a location-dependent metric can skip them.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

from orbit8.exports import read_po_entries

HERE = Path(__file__).resolve().parents[1]
DRIVE = Path("/Users/maotian/Library/CloudStorage/"
             "GoogleDrive-info@orbit8lab.com/My Drive/Orbit8LabInternal/"
             "project")
SRC = (DRIVE / "project002-绯月杀/20-work/pe-en-round1-20260719-0000/"
       "20260719_绯月杀_EN_QA_Master_Table_PE_complete.csv")
PO = (DRIVE / "project002-绯月杀/20-work/pe-applied-20260803/"
      "20260803-po-delivery/Game.po")
OUT = HERE / "data" / "p002_round1_groundtruth.jsonl"

_BUG_CAT = re.compile(r"\[([^\]]+)\]")


def _nz(value) -> bool:
    return value is not None and str(value).strip() not in ("", "NA", "N/A", "-")


def main() -> int:
    if not SRC.exists():
        print(f"missing: {SRC}", file=sys.stderr)
        return 1

    # location per PO key, so the asset-path grouping axes still apply.
    loc_by_key = {}
    if PO.exists():
        for key, _s, _t, loc in read_po_entries(PO):
            loc_by_key[key.lstrip(",")] = loc

    with SRC.open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))

    out = []
    for row in rows:
        src = (row.get("chinese (latest)") or "").strip()
        mt = (row.get("english (latest)") or "").strip()
        if not src or not mt:
            continue
        key = (row.get("unique id (po hash)") or "").strip()
        rejected = str(row.get("PE-Flag") or "").strip().lower() == "yes"
        pe = (row.get("post-editing") or "").strip()
        bug = (row.get("bug") or "").strip()
        cats = _BUG_CAT.findall(bug) if _nz(bug) else []
        out.append({
            "key": key,
            "location": loc_by_key.get(key, ""),
            "source": src,
            "mt": mt,
            "human_rejected": rejected,
            # A rejection with no rewrite says the row is wrong but not
            # what right looks like: keep the LQA label, exclude it from
            # the translation reference.
            "reference": pe if _nz(pe) else (None if rejected else mt),
            "bug_categories": cats,
            "bug_level": (row.get("bug level") or "").strip()
                         if _nz(row.get("bug level")) else "",
            "glossary_note": (row.get("glossary PE") or "").strip()
                             if _nz(row.get("glossary PE")) else "",
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as fh:
        for record in out:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    rej = sum(r["human_rejected"] for r in out)
    print(f"wrote {len(out)} rows → {OUT}")
    print(f"  rejected        : {rej} ({rej/len(out):.0%} defect rate)")
    print(f"  with a reference: {sum(r['reference'] is not None for r in out)}")
    print(f"  with a location : {sum(bool(r['location']) for r in out)}")
    print(f"  with bug labels : {sum(bool(r['bug_categories']) for r in out)}")
    cats = {}
    for r in out:
        for c in r["bug_categories"]:
            cats[c] = cats.get(c, 0) + 1
    for c, n in sorted(cats.items(), key=lambda x: -x[1])[:8]:
        print(f"      {n:4d}  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
