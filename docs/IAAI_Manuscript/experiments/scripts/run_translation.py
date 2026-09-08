"""Experiment 1 — does batching + style guide improve translation quality?

Four conditions over the SAME 174 human-labelled strings, differing only
in what the workflow supplies to the model. Every condition calls the
production `agents.translate_batch`, so what is measured is the shipped
pipeline, not a reimplementation of it:

  B1  sentence      n=1, no glossary, no style guide   (LLM per-segment)
  B2  batch         n=20 blind slice, no glossary/style
  B3  batch+gloss   n=20 + glossary brief
  B4  agentic       n=20 template/conversation grouping + glossary + style

B1→B2 isolates batching. B2→B3 isolates terminology. B3→B4 isolates the
style guide plus context-aware grouping — the paper's actual claim. Order
matters: each step adds exactly one mechanism so a gain is attributable.

Scoring is against the post-editor's verdict, not BLEU. For every string
the human either accepted the original MT or wrote a correction, so:

  exact      output == the human's final target (normalised whitespace)
  pe_needed  output differs from the human's final target → an editor
             would still have to touch it
  regress    the human ACCEPTED the original MT and we changed it anyway

`regress` is reported because a condition can raise exact-match on the
rejected rows while quietly rewriting rows that were already correct,
which in production costs review time rather than saving it.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics
from orbit8 import agents
from orbit8.glossary import Glossary
from orbit8.grouping import plan_similarity_batches, plan_story_batches
from orbit8.llm import build_provider
from orbit8.style_guide import StyleGuide

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
RESULTS = HERE / "results"
DRIVE = Path("/Users/maotian/Library/CloudStorage/"
             "GoogleDrive-info@orbit8lab.com/My Drive/Orbit8LabInternal/"
             "project")
GLOSSARY = DRIVE / "project002-绯月杀/40-reference/glossary/glossary_terms.json"

STORY_LIKE = {"dialogue", "marketing"}


def norm(s: str) -> str:
    """Compare on content, not on incidental spacing.

    Deliberately does NOT fold case: casing IS the defect in 13 of the
    27 client style rules, so folding it would score the exact errors
    the experiment exists to measure as successes.
    """
    return re.sub(r"\s+", " ", (s or "").strip())


def load_rows() -> List[dict]:
    path = DATA / "p002_mtpe_groundtruth.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def make_batches(rows: List[dict], *, size: int, agentic: bool) -> List[List[dict]]:
    if size == 1:
        return [[r] for r in rows]
    if not agentic:
        return [rows[i:i + size] for i in range(0, len(rows), size)]
    # Agentic: group by domain first (the prompt is domain-aware), then
    # by conversation for story text and by template family for strings.
    out: List[List[dict]] = []
    by_domain: Dict[str, List[dict]] = {}
    for r in rows:
        by_domain.setdefault(r["domain"], []).append(r)
    for domain, group in sorted(by_domain.items()):
        segs = [dict(r, uid=r["key"], text=r["source"]) for r in group]
        if domain in STORY_LIKE:
            planned = plan_story_batches(segs, max_size=5)
        else:
            planned = plan_similarity_batches(segs, max_size=size)
        by_key = {r["key"]: r for r in group}
        for batch in planned:
            out.append([by_key[s["uid"]] for s in batch if s["uid"] in by_key])
    return [b for b in out if b]


def run_condition(name: str, rows: List[dict], *, provider, size: int,
                  use_glossary: bool, use_style: bool, agentic: bool,
                  glossary: Optional[Glossary],
                  guide: Optional[StyleGuide],
                  locked: Dict[str, str],
                  forms: Dict[str, Dict[str, str]]) -> dict:
    batches = make_batches(rows, size=size, agentic=agentic)
    got: Dict[str, str] = {}
    calls = 0
    t0 = time.time()
    tokens_before = provider.tokens_spent

    for i, batch in enumerate(batches, 1):
        # Surrogate ids, not the raw game keys. 153 of the 174 keys in
        # this client's form carry a leading comma (a CSV-export
        # artifact, e.g. ",6C18DD27..."), and the model silently
        # normalises it away — which trips translate_batch's coverage
        # guard and drops the whole batch. That guard is RIGHT: a
        # translator returning keys that don't match what it was given
        # is exactly the corruption it exists to catch. So the harness
        # hands the model clean ids and maps back itself, rather than
        # relaxing a production invariant to make an experiment pass.
        ids = {f"s{j:04d}": r for j, r in enumerate(batch)}
        items = [(sid, r["source"]) for sid, r in ids.items()]
        brief = (glossary.brief_for([r["source"] for r in batch])
                 if use_glossary and glossary else None)
        domains = {r["domain"] for r in batch}
        domain = domains.pop() if len(domains) == 1 else None
        # Only claim "one conversation" when the batch really is one
        # exchange, matching what graphs/translate.py does in production.
        conversation = bool(agentic and domain in STORY_LIKE and len(batch) > 1)
        try:
            out, _fp = agents.translate_batch(
                provider, items, source_lang="zh", target_lang="en",
                game="绯月杀", domain=domain, glossary_brief=brief,
                style_guide=guide if use_style else None,
                conversation=conversation)
            for it in out.items:
                row = ids.get(it.key)
                if row is not None:
                    got[row["key"]] = it.target_text
            calls += 1
        except Exception as exc:            # a failed batch is data, not a crash
            print(f"    [{name}] batch {i}/{len(batches)} FAILED: "
                  f"{type(exc).__name__}: {str(exc)[:120]}")
        if i % 5 == 0 or i == len(batches):
            print(f"    [{name}] {i}/{len(batches)} batches, "
                  f"{len(got)}/{len(rows)} strings, {time.time()-t0:.0f}s",
                  flush=True)

    elapsed = time.time() - t0
    tokens = provider.tokens_spent - tokens_before

    # Scoring. Exact match against the editor's wording is reported for
    # completeness but nothing rests on it — free translation is
    # underdetermined, so it reads ~0% for every condition (see
    # metrics.py). The claims rest on defect counts against the client's
    # own written rules and locked glossary.
    exact = missing = regress = 0
    viol_strings = 0
    viol_by_rule: Dict[str, int] = {}
    term_err = 0
    chrf_sum = 0.0
    per_row, pairs = [], []
    for r in rows:
        out = got.get(r["key"])
        if out is None:
            missing += 1
            continue
        ref = r["reference"]
        ok = norm(out) == norm(ref) if ref else False
        exact += ok
        if not r["human_rejected"] and norm(out) != norm(r["mt"]):
            regress += 1
        vio = metrics.rule_violations(r["source"], out)
        viol_strings += bool(vio)
        for v in vio:
            viol_by_rule[v] = viol_by_rule.get(v, 0) + 1
        terr = metrics.term_errors(r["source"], out, locked, forms)
        term_err += len(terr)
        chrf_sum += metrics.chrf(out, ref) if ref else 0.0
        pairs.append((r["source"], out))
        per_row.append({"key": r["key"], "domain": r["domain"],
                        "human_rejected": r["human_rejected"],
                        "output": out, "reference": ref, "exact": ok,
                        "violations": vio,
                        "term_errors": [t[0] for t in terr]})

    conflicts = metrics.consistency_conflicts(pairs, locked, forms)
    scored = len(rows) - missing
    return {
        "condition": name, "batch_size": size, "agentic": agentic,
        "glossary": use_glossary, "style_guide": use_style,
        "calls": calls, "batches": len(batches), "seconds": round(elapsed, 1),
        "tokens": tokens, "n": len(rows), "scored": scored, "missing": missing,
        # primary: defects against the client's own rules and glossary
        "violation_strings": viol_strings,
        "violation_rate": round(viol_strings / scored, 4) if scored else 0.0,
        "violations_by_rule": viol_by_rule,
        "term_errors": term_err,
        "inconsistent_terms": len(conflicts),
        "inconsistent_detail": {k: sorted(v) for k, v in
                                list(conflicts.items())[:15]},
        # secondary / reported but not claimed on
        "chrf": round(chrf_sum / scored, 4) if scored else 0.0,
        "exact": exact,
        "exact_rate": round(exact / scored, 4) if scored else 0.0,
        "regressions": regress,
        "per_row": per_row,
    }


CONDITIONS = {
    "B1_sentence":   dict(size=1,  use_glossary=False, use_style=False, agentic=False),
    "B2_batch":      dict(size=20, use_glossary=False, use_style=False, agentic=False),
    "B3_batch_gloss":dict(size=20, use_glossary=True,  use_style=False, agentic=False),
    "B4_agentic":    dict(size=20, use_glossary=True,  use_style=True,  agentic=True),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model")
    ap.add_argument("--conditions", default=",".join(CONDITIONS))
    ap.add_argument("--limit", type=int, help="first N rows (smoke test)")
    ap.add_argument("--tag", default="main")
    args = ap.parse_args()

    rows = load_rows()
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows)} strings; provider={args.provider} "
          f"model={args.model or 'preset default'}")

    t1 = Glossary.load_t1_file(GLOSSARY)
    glossary = Glossary.from_layers("绯月杀", "en", t1=t1)
    # Only LOCKED terms are law (the rest are suggestions), so only
    # locked terms are scored as errors.
    locked = {zh: e["translation"] for zh, e in t1.get("terms", {}).items()
              if e.get("locked")}
    # Declared part-of-speech renderings. Any of them satisfies the term:
    # 合成 is locked as "Crafting" but declares verb "craft", so
    # "Craft using materials" follows the ruling and must not be scored
    # as a defect.
    forms = {zh: e["forms"] for zh, e in t1.get("terms", {}).items()
             if e.get("forms")}
    guide = StyleGuide.load(DATA / "style_guide_zh_en.json")
    print(f"glossary: {len(glossary.terms)} terms ({len(locked)} locked, "
          f"{len(forms)} with forms) · style: {len(guide.rules)} rules")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / f"translation_{args.tag}.json"
    all_results = []
    for name in args.conditions.split(","):
        name = name.strip()
        if name not in CONDITIONS:
            print(f"unknown condition {name!r}; known: {list(CONDITIONS)}")
            return 2
        print(f"\n=== {name} ===", flush=True)
        provider = build_provider(args.provider, model=args.model)
        res = run_condition(name, rows, provider=provider,
                            glossary=glossary, guide=guide, locked=locked,
                            forms=forms,
                            **CONDITIONS[name])
        print(f"  rule-violating strings={res['violation_strings']}"
              f"/{res['scored']} ({res['violation_rate']:.1%}) "
              f"{res['violations_by_rule']}")
        print(f"  locked-term errors={res['term_errors']}  "
              f"inconsistent terms={res['inconsistent_terms']}  "
              f"chrF={res['chrf']:.3f}  exact={res['exact']}")
        print(f"  calls={res['calls']}  {res['seconds']}s  "
              f"tokens={res['tokens']:.0f}  missing={res['missing']}")
        all_results.append(res)
        # Written after EVERY condition: a run that dies at condition 4
        # must not throw away the three that already cost money.
        out_path.write_text(json.dumps(
            {"provider": args.provider, "model": args.model,
             "n_rows": len(rows), "results": all_results},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
