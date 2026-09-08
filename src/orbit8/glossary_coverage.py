"""Are the glossary's locked terms actually REACHABLE in this corpus?

Motivated by a bug that shipped through eleven client rounds. The
project002 glossary locked ``BOSS -> "Boss"``, but ``term_spans``
guarded ASCII term edges with ``\\b`` and Python's ``\\w`` includes Han,
so the term matched nothing inside 摧毁BOSS. It never entered the
translator's glossary brief and never raised a gate finding, and every
occurrence shipped wrong — 23% of that round's human rejections.

What made it survive so long is the shape of the failure: a term that
matches nothing produces no finding, no warning and no log line, so
**absence of a finding was indistinguishable from compliance.** Every
other observability surface in the pipeline reports what it DID; nothing
reported what it silently never attempted.

Hence this module. It answers one question the rest of the system
cannot: of the terms the operator locked, how many were ever seen? A
locked term with zero source matches is either dead weight in the
termbase or a matcher bug, and the operator must be told which — both
are actionable, and neither is visible from a scan report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from .gate_checks import term_spans


@dataclass
class TermCoverage:
    term: str
    rendering: str
    sources: int = 0          # source strings where the term matched
    targets: int = 0          # of those, how many rendered it correctly


@dataclass
class CoverageReport:
    locked: int = 0
    matched: int = 0          # locked terms seen in at least one source
    unmatched: List[str] = field(default_factory=list)
    # Terms present in source but never rendered as locked: a real
    # compliance gap, distinct from a term that never appears at all.
    never_applied: List[str] = field(default_factory=list)
    per_term: Dict[str, TermCoverage] = field(default_factory=dict)
    corpus_size: int = 0

    @property
    def reach(self) -> float:
        """Fraction of locked terms this corpus can even exercise.

        NOTE this is a WEAK signal on its own, and the motivating bug
        proves it: with the broken matcher, reach on the full 1,349-string
        corpus was 90.2% against 92.7% after the fix. The aggregate barely
        moved because BOSS still matched at string edges — while its
        detected occurrences went from 2 to 28. A term found 7% of the
        time is indistinguishable from a healthy one by reach alone.

        `occurrences` is the number that actually exposes this, which is
        why it is reported per term and why `compare` exists.
        """
        return self.matched / self.locked if self.locked else 1.0

    @property
    def occurrences(self) -> int:
        """Total (term, source) matches. The quantity a matcher bug moves
        sharply while `reach` stays flat."""
        return sum(c.sources for c in self.per_term.values())

    def warnings(self) -> List[str]:
        """Operator-facing lines. Empty when there is nothing to say —
        this must not become noise the operator learns to skip."""
        out: List[str] = []
        if self.locked and not self.matched:
            out.append(
                f"NO locked term matched any source string ({self.locked} "
                f"locked). This is a matcher or encoding fault, not a "
                f"terminology gap — a glossary is never 0% relevant.")
            return out
        # A locked term found in exactly one or two strings, in a corpus
        # large enough that a real term recurs, is the signature of a
        # matcher that is *mostly* failing. It is a suspicion, not a
        # verdict, so it is phrased as one.
        thin = [t for t, c in sorted(self.per_term.items())
                if 0 < c.sources <= 2]
        if thin and self.corpus_size >= 200:
            shown = ", ".join(thin[:8])
            more = f" (+{len(thin) - 8} more)" if len(thin) > 8 else ""
            out.append(
                f"{len(thin)} locked term(s) matched only 1-2 of "
                f"{self.corpus_size} strings: {shown}{more}. Verify these "
                f"are genuinely rare rather than partly unmatchable — a "
                f"Latin term in CJK source, an encoding variant, or a "
                f"full-width/half-width mismatch all look like this.")
        if self.never_applied:
            shown = ", ".join(self.never_applied[:8])
            more = (f" (+{len(self.never_applied) - 8} more)"
                    if len(self.never_applied) > 8 else "")
            out.append(
                f"{len(self.never_applied)} locked term(s) occur in the "
                f"source but were NEVER rendered as locked: {shown}{more}. "
                f"Either the translation ignores them or the term's `case` "
                f"rule makes the check vacuous.")
        return out


def measure(pairs: Sequence[tuple], locked: Dict[str, str], *,
            target_lang: str = "en",
            term_case: Dict[str, str] = None,
            term_forms: Dict[str, Dict[str, str]] = None
            ) -> CoverageReport:
    """``pairs`` is ``[(source, target), ...]``; ``locked`` is term → rendering.

    Two distinct signals, deliberately not merged:

    - **unmatched** — the term never appears in this corpus. Usually
      benign (a termbase spans many files), but ALL terms unmatched is a
      matcher bug, which `warnings()` calls out as such.
    - **never_applied** — the term appears and is never rendered
      correctly. Always worth the operator's attention.
    """
    from .gate_checks import locked_in_target

    term_case = term_case or {}
    term_forms = term_forms or {}
    report = CoverageReport(locked=len(locked), corpus_size=len(pairs))
    for term, rendering in locked.items():
        report.per_term[term] = TermCoverage(term=term, rendering=rendering)

    for source, target in pairs:
        for term, rendering in locked.items():
            if not term_spans(term, source or ""):
                continue
            cov = report.per_term[term]
            cov.sources += 1
            if locked_in_target(rendering, target or "", target_lang,
                                forms=term_forms.get(term),
                                case=term_case.get(term, "context")):
                cov.targets += 1

    for term, cov in report.per_term.items():
        if cov.sources == 0:
            report.unmatched.append(term)
        else:
            report.matched += 1
            if cov.targets == 0:
                report.never_applied.append(term)
    return report


def compare(before: CoverageReport, after: CoverageReport) -> List[str]:
    """Per-term occurrence deltas between two coverage runs.

    This is the check that would actually have caught the motivating
    bug. Aggregate reach moved 90.2% -> 92.7% (invisible); BOSS's
    occurrence count moved 2 -> 28 (unmissable). Any change to the
    matcher, the glossary, or the corpus should be diffed this way
    rather than compared on a single summary number.
    """
    out: List[str] = []
    for term in sorted(set(before.per_term) | set(after.per_term)):
        b = before.per_term.get(term)
        a = after.per_term.get(term)
        b_n = b.sources if b else 0
        a_n = a.sources if a else 0
        if b_n != a_n:
            out.append(f"{term}: {b_n} -> {a_n} source matches")
    return out
