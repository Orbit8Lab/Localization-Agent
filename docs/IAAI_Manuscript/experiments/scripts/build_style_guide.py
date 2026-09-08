"""Convert the client's real StyleRules.xlsx into a pipeline style guide.

These rules are not invented for the paper. A professional post-editor
wrote them DURING the project002 post-edit, one row per regularity they
found themselves correcting repeatedly — which is exactly the artifact
the paper argues should enter the prompt at generation time instead of
being applied afterwards by hand.

Enforcement is assigned by whether code can decide the rule:
  mechanical — a regex/parity check settles it (punctuation, placeholders)
  llm        — needs judgment about what a word IS in this sentence
               (CAP-12/13/14 are exactly "same word, two casings")
Rules the client still marks `Proposed` are carried as advisory: they are
real observations, but the client has not ratified them, so they inform
the model without being scored as violations.
"""
from __future__ import annotations

import json
from pathlib import Path

import openpyxl

DRIVE = Path("/Users/maotian/Library/CloudStorage/"
             "GoogleDrive-info@orbit8lab.com/My Drive/Orbit8LabInternal/"
             "project")
SRC = DRIVE / "project002-绯月杀/40-reference/style/绯月杀_风格规则表_StyleRules.xlsx"
OUT = Path(__file__).resolve().parents[1] / "data" / "style_guide_zh_en.json"

# Mechanical rules, hand-mapped to the checks style_guide.py implements.
# Only rules a check can actually settle appear here; anything else stays
# in the LLM bin rather than being given a check that misfires.
MECHANICAL = {
    "STY-01": ("forbid_chars", "：", "high"),
    "STY-02": ("forbid_pattern", r"[!]{2,}", "medium"),
    "STY-05": ("forbid_pattern", None, None),   # handled by gate_checks
}


def main() -> int:
    wb = openpyxl.load_workbook(SRC, data_only=True)
    rules = []

    for row in wb["①Casing大小写"].iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        rid, scope, text, good, bad, _how, status, note = row[:8]
        confirmed = str(status or "").strip() == "Confirmed"
        rules.append({
            "id": str(rid),
            "text": f"{scope}: {text}",
            # Casing is a judgment call about what the word denotes here,
            # so no regex can enforce it — it goes to the model.
            "enforcement": "llm" if confirmed else "advisory",
            "severity": "high" if rid in ("CAP-12", "CAP-13", "CAP-14")
                        else "medium",
            "rationale": str(note or ""),
            "examples": {k: str(v) for k, v in
                         (("good", good), ("bad", bad))
                         if v and str(v).strip() not in ("—", "")},
        })

    for row in wb["②其他风格规则"].iter_rows(min_row=2, values_only=True):
        if not row[0]:
            continue
        rid, cat, text, good, bad, status, note = row[:7]
        rid = str(rid)
        confirmed = str(status or "").strip() == "Confirmed"
        rule = {
            "id": rid,
            "text": f"{cat}: {text}",
            "enforcement": "llm" if confirmed else "advisory",
            "severity": "high" if rid in ("STY-03", "STY-05", "STY-08")
                        else "medium",
            "rationale": str(note or ""),
            "examples": {k: str(v) for k, v in
                         (("good", good), ("bad", bad))
                         if v and str(v).strip() not in ("—", "", "(待定)")},
        }
        mech = MECHANICAL.get(rid)
        if mech and mech[1] is not None:
            check, value, sev = mech
            rule.update(enforcement="mechanical", check=check,
                        value=value, severity=sev)
        rules.append(rule)

    guide = {
        "metadata": {
            "source_lang": "zh", "target_lang": "en", "version": "1",
            "notes": "Derived from the client's 绯月杀_风格规则表 "
                     "(StyleRules.xlsx), written by the post-editor "
                     "during the project002 PE pass.",
        },
        "morphology": {},
        "rules": rules,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(guide, ensure_ascii=False, indent=2),
                   encoding="utf-8")

    bins = {}
    for r in rules:
        bins[r["enforcement"]] = bins.get(r["enforcement"], 0) + 1
    print(f"wrote {len(rules)} rules → {OUT}")
    for b, n in sorted(bins.items()):
        print(f"  {b:12s} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
