"""Experiment 2 — does batching + style guide improve LQA accuracy?

The scanner's job is to find the strings a professional post-editor would
reject. The MTPE form says exactly which 57 of 174 those are, so the
scanner can be scored as a detector:

  TP  flagged   and human rejected      FN  not flagged but human rejected
  FP  flagged   and human accepted      TN  not flagged and human accepted

Conditions vary one mechanism at a time, all through the production
`scan_po`:

  L1  deterministic  T1/T2 only, no glossary, no style guide, no LLM
  L2  +glossary      adds the locked-term layer
  L3  +LLM batched   full cascade, n=20 semantic batches
  L4  +style guide   L3 plus the client's 27 rules in the Critic prompt

L1 is the conventional-tooling analogue (regex/QA-check localization
tooling); L4 is the full workflow. FP rate is reported alongside recall
because a scanner that flags everything has perfect recall and zero
value — the client pays reviewer time for every false positive.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Dict, List

from orbit8.llm import build_provider
from orbit8.po_scan import scan_po
from orbit8.style_guide import StyleGuide

HERE = Path(__file__).resolve().parents[1]
DATA, RESULTS = HERE / "data", HERE / "results"
DRIVE = Path("/Users/maotian/Library/CloudStorage/"
             "GoogleDrive-info@orbit8lab.com/My Drive/Orbit8LabInternal/"
             "project")
GLOSSARY = DRIVE / "project002-绯月杀/40-reference/glossary/glossary_terms.json"


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def write_po(rows: List[dict], path: Path) -> None:
    """Rebuild a .po holding the ORIGINAL machine targets.

    The scanner must see what the post-editor saw — the raw MT — so that
    a flag can be compared against their verdict on that same text.
    """
    out = ['msgid ""', 'msgstr ""',
           '"Content-Type: text/plain; charset=UTF-8\\n"', ""]
    for r in rows:
        out += [f'#: {r["string_type"]}',
                f'msgctxt "{_esc(r["key"])}"',
                f'msgid "{_esc(r["source"])}"',
                f'msgstr "{_esc(r["mt"])}"', ""]
    path.write_text("\n".join(out), encoding="utf-8")


def flagged_keys(report, *, min_severity: str = "low") -> Dict[str, List[str]]:
    """game key → bug types.

    Keyed on `game_keys`, not `uid`: the scanner dedups by (source,
    target) and a uid is the hash of that pair, so one flagged item can
    cover several game keys. Scoring on uid would silently under-count
    every duplicated string.

    `min_severity` matters because severity is a real product decision,
    not presentation. The width-ratio check is deliberately LOW and
    labelled "unverified — confirm in-game", so a reviewer triages those
    as a block. Scoring every advisory as a flag measures a report
    nobody acts on that way; scoring only actionable findings measures
    what the client actually reviews. Both are reported, since which one
    is "the" precision depends on the consumer (see the L3-vs-L4
    operating-point discussion).
    """
    rank = {"low": 0, "medium": 1, "high": 2}
    floor = rank[min_severity]
    out: Dict[str, List[str]] = {}
    for item in report.items:
        # bug_type lives on the wrapped Finding, not the verified
        # wrapper, so reach through `.finding` to get a real label.
        types = [str(getattr(f.finding, "bug_type", "?").value
                     if hasattr(getattr(f.finding, "bug_type", None), "value")
                     else getattr(f.finding, "bug_type", "?"))
                 for f in item.findings]
        keep = [t for t, f in zip(types, item.findings)
                if rank.get(str(getattr(f.finding, "severity", "low").value
                                 if hasattr(getattr(f.finding, "severity",
                                                    None), "value")
                                 else getattr(f.finding, "severity", "low")),
                            0) >= floor]
        if not keep:
            continue
        for key in (item.game_keys or [item.uid]):
            out.setdefault(str(key), []).extend(keep)
    return out


# `style` selects WHICH guide, not whether one exists.
#
# Passing style_guide=None does NOT disable style rules: scan_po falls
# back to the built-in zh→en starter guide (12 rules, 4 mechanical),
# so an early version of this experiment was silently comparing
# "starter guide" against "client guide" while claiming to compare
# "no guide" against "guide". Discovered by reading the progress log
# ("using the built-in starter guide"), not from the results — the
# numbers looked perfectly plausible.
#
#   "none"    → an EMPTY StyleGuide, which suppresses the fallback and
#               genuinely enforces nothing
#   "starter" → the shipped default (what a new project gets on day one)
#   "client"  → project002's own 27 hand-written rules
CONDITIONS = {
    "L1_deterministic": dict(glossary=False, llm=False, style="none"),
    "L2_glossary":      dict(glossary=True,  llm=False, style="none"),
    "L3_llm_batched":   dict(glossary=True,  llm=True,  style="none"),
    "L3b_starter_style":dict(glossary=True,  llm=True,  style="starter"),
    "L4_style_guide":   dict(glossary=True,  llm=True,  style="client"),
}


def score(flags: Dict[str, List[str]], rows: List[dict]) -> dict:
    tp = fp = fn = tn = 0
    misses, false_pos = [], []
    for r in rows:
        hit = r["key"] in flags
        if r["human_rejected"]:
            if hit:
                tp += 1
            else:
                fn += 1
                misses.append({"key": r["key"], "source": r["source"],
                               "mt": r["mt"], "reference": r["reference"]})
        else:
            if hit:
                fp += 1
                false_pos.append({"key": r["key"], "mt": r["mt"],
                                  "bug_types": flags[r["key"]]})
            else:
                tn += 1
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    # Composition counted over ALL findings, not the truncated example
    # lists. An earlier version reported "16 of 25 FPs are length"
    # from the capped list, which silently became a sample statistic
    # once a condition exceeded 25 FPs.
    fp_by_type: Dict[str, int] = {}
    for f in false_pos:
        for t in set(f["bug_types"]):
            fp_by_type[t] = fp_by_type.get(t, 0) + 1
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 4), "recall": round(rec, 4),
            "f1": round(f1, 4),
            "fp_rate": round(fp / (fp + tn), 4) if fp + tn else 0.0,
            "fp_by_type": fp_by_type,
            # Example lists stay truncated (they carry full strings);
            # the counts above are complete.
            "misses": misses[:25], "false_positives": false_pos[:25]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model")
    ap.add_argument("--batch-string", type=int, default=20)
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tag", default="main")
    args = ap.parse_args()

    rows = [json.loads(l) for l in
            (DATA / "p002_mtpe_groundtruth.jsonl").read_text(
                encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    n_rej = sum(r["human_rejected"] for r in rows)
    print(f"{len(rows)} strings, {n_rej} human-rejected "
          f"({n_rej/len(rows):.0%} defect rate)")

    work = HERE / "work"
    work.mkdir(parents=True, exist_ok=True)
    po = work / "p002_eval.po"
    write_po(rows, po)
    from orbit8.style_defaults import default_guide
    guides = {
        # An empty guide, not None: None triggers scan_po's fallback.
        "none": StyleGuide(source_lang="zh-CN", target_lang="en",
                           version="empty", rules=[]),
        "starter": default_guide("zh-CN", "en"),
        "client": StyleGuide.load(DATA / "style_guide_zh_en.json"),
    }
    for label, g in guides.items():
        print(f"  style[{label}]: {len(g.rules) if g else 0} rules")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"lqa_{args.tag}.json"
    results = []
    for name in args.conditions.split(","):
        name = name.strip()
        cfg = CONDITIONS[name]
        print(f"\n=== {name} ===", flush=True)
        out_dir = work / name
        if out_dir.exists():
            shutil.rmtree(out_dir)
        provider = build_provider(args.provider, model=args.model) \
            if cfg["llm"] else None
        t0 = time.time()
        res = scan_po(
            po, GLOSSARY if cfg["glossary"] else None, out_dir,
            provider=provider, game="绯月杀", locale="en",
            source_lang="zh-CN", job_id=f"iaai-{name}",
            deterministic_only=not cfg["llm"],
            batch_string=args.batch_string, suggestions=False,
            full_scan=True,
            style_guide=guides[cfg["style"]],
            on_progress=lambda ev, info=None: print(f"    {ev} {info}", flush=True))
        elapsed = time.time() - t0
        flags = flagged_keys(res.report)
        sc = score(flags, rows)
        # Actionable-only view: excludes LOW advisories, which the
        # report presents as a separate triage block.
        sc_act = score(flagged_keys(res.report, min_severity="medium"), rows)
        tokens = provider.tokens_spent if provider else 0.0
        print(f"  P={sc['precision']:.1%} R={sc['recall']:.1%} "
              f"F1={sc['f1']:.3f}  TP={sc['tp']} FP={sc['fp']} "
              f"FN={sc['fn']}  {elapsed:.0f}s tokens={tokens:.0f}")
        print(f"  actionable-only (excl. LOW): P={sc_act['precision']:.1%} "
              f"R={sc_act['recall']:.1%} F1={sc_act['f1']:.3f} "
              f"TP={sc_act['tp']} FP={sc_act['fp']}")
        results.append({"condition": name, **cfg, "seconds": round(elapsed, 1),
                        "tokens": tokens, "flagged": len(flags),
                        "batch_string": args.batch_string, **sc,
                        "actionable": {k: v for k, v in sc_act.items()
                                       if k not in ("misses",
                                                    "false_positives")}})
        out_path.write_text(json.dumps(
            {"provider": args.provider, "model": args.model,
             "n_rows": len(rows), "n_rejected": n_rej, "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
