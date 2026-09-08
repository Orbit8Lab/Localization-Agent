"""Scoring for the translation experiment.

Exact match against the post-editor's wording was tried first and
discarded: it scores ~0% for every condition. The reason is not that the
outputs are bad but that free translation is underdetermined — "Destroy
the Boss to lower World Infection" and "Destroying the Boss can reduce
World Infection value" are both correct, and an exact-match metric calls
both wrong while saying nothing about the defect that actually matters.
Averaged over conditions that noise swamps the effect being measured.

So each mechanism is scored against the specific defect it targets, and
the defects are the ones THIS post-editor actually fixed (measured, not
assumed: of 57 rejections, 23% were casing-only and a further 35% were
minor lexical edits dominated by casing and term choice):

  defect_rate  — how often a KNOWN client rule is broken. Every element
                 is a rule the post-editor wrote down, checked
                 mechanically, so it is objective and cheap.
  term_error   — locked glossary terms rendered as something other than
                 their locked target. The glossary is law, so this is a
                 hard error, not a preference.
  consistency  — one source term rendered two ways ACROSS the corpus.
                 This is the batching claim: it is only detectable
                 corpus-wide, and it is what per-sentence translation
                 structurally cannot get right.

`chrF` is also reported as a soft similarity to the post-editor's target
so the tables carry one conventional MT number, but no claim rests on
it: it is a paraphrase-sensitive metric on n=174.
"""
from __future__ import annotations

import collections
import re
from typing import Dict, List, Optional, Tuple

# Casing defects the client's CAP rules forbid, as literal surface forms.
# Only terms whose ruling is unambiguous in the rules table are listed:
# BOSS is CAP-03/05 (a named entity → Title Case, never all-caps).
SCREAMING = re.compile(r"\b(BOSS(?:es)?|NPC|HP|MP)\b")

# Rules that a regex can settle. Anything requiring judgment (CAP-12/13/
# 14, "same word, two casings") is deliberately absent: a wrong
# mechanical verdict would be worse than no verdict.
def rule_violations(source: str, target: str) -> List[str]:
    out = []
    # STY-01 — full-width colon must become "half-width + space"
    if "：" in target:
        out.append("STY-01")
    # STY-02 — no exclamation stacking
    if re.search(r"!{2,}", target):
        out.append("STY-02")
    # CAP-03/05 — a named entity left in all-caps (BOSS) where the
    # client's rules require Title Case (Boss).
    #
    # Note the source is NOT used as an escape hatch here. A first cut
    # skipped the check when the source also contained "BOSS", which
    # silenced it on every row that matters: Chinese source text writes
    # 摧毁BOSS in caps as a matter of CJK convention, and the client's
    # ruling is precisely that English must still render it "Boss". The
    # post-editor made that edit 13 times in 57 rejections, so a check
    # that defers to the source measures nothing.
    #
    # HP/MP are excluded from this rule — they are conventional
    # abbreviations, not named entities, and the rules table does not
    # ask for them to be recased.
    if re.search(r"\b(BOSS(?:es|ES)?|NPCs?)\b", target):
        out.append("CAP-03")
    # STY-03 — placeholders preserved verbatim
    src_ph = sorted(re.findall(r"\{[^}]*\}|%[sd]|\[[A-Za-z_]+\]", source))
    tgt_ph = sorted(re.findall(r"\{[^}]*\}|%[sd]|\[[A-Za-z_]+\]", target))
    if src_ph != tgt_ph:
        out.append("STY-03")
    # STY-05 (numbers verbatim) is NOT scored here, though the client
    # rule is real. Validated against ground truth it moved the WRONG
    # way — 18 violations on the raw MT vs 25 on the human post-edit —
    # because zh→en legitimately converts spelled-out numerals to
    # digits: the editor's own "Mission Three" → "Mission 3" trips a
    # naive digit-set comparison. A check that penalises the target
    # state cannot be used to rank conditions against it, and
    # gate_checks.py already enforces the real rule where the source
    # digits are present. Left visible rather than deleted so the
    # omission is a recorded decision, not an oversight.
    # STY-08 — line-break structure preserved
    if source.count("\n") != target.count("\n"):
        out.append("STY-08")
    return out


def term_errors(source: str, target: str,
                locked: Dict[str, str]) -> List[Tuple[str, str]]:
    """Locked source terms present but rendered as something else.

    Matching is longest-source-first so 感染值 is judged before 感染,
    which otherwise steals the match and reports a false error.
    """
    out = []
    tl = target.lower()
    for zh in sorted(locked, key=len, reverse=True):
        if zh and zh in source:
            want = locked[zh]
            if want and want.lower() not in tl:
                out.append((zh, want))
    return out


def consistency_conflicts(pairs: List[Tuple[str, str]],
                          locked: Dict[str, str]) -> Dict[str, set]:
    """Source terms rendered inconsistently across the whole corpus.

    The batching claim lives here: a per-sentence system has no way to
    know how it rendered the same term 300 lines earlier, so this is the
    error class context is supposed to remove.
    """
    seen: Dict[str, set] = collections.defaultdict(set)
    for source, target in pairs:
        for zh in locked:
            if zh and zh in source:
                # Record the rendering actually used, when recognisable.
                want = locked[zh]
                if want and want.lower() in (target or "").lower():
                    seen[zh].add(want.lower())
                else:
                    # An unrecognised rendering still counts as a variant:
                    # that is precisely the inconsistency being measured.
                    seen[zh].add("<other>")
    return {zh: v for zh, v in seen.items() if len(v) > 1}


def _ngrams(s: str, n: int) -> collections.Counter:
    s = re.sub(r"\s+", " ", s.strip())
    return collections.Counter(s[i:i + n] for i in range(len(s) - n + 1))


def chrf(hyp: str, ref: str, n: int = 6, beta: float = 2.0) -> float:
    """chrF — character n-gram F-score. Standard for MT, and unlike BLEU
    it degrades gracefully on single sentences, which is all we have."""
    if not hyp or not ref:
        return 0.0
    ps, rs = [], []
    for k in range(1, n + 1):
        h, r = _ngrams(hyp, k), _ngrams(ref, k)
        overlap = sum((h & r).values())
        ps.append(overlap / max(1, sum(h.values())))
        rs.append(overlap / max(1, sum(r.values())))
    p, r = sum(ps) / n, sum(rs) / n
    if p + r == 0:
        return 0.0
    b2 = beta * beta
    return (1 + b2) * p * r / (b2 * p + r)
