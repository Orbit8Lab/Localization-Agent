"""T3 must be told what T1/T2 already found.

`review_batch` has always accepted `known_findings` and renders it as
"**ALREADY KNOWN (do not re-report):**". `tier3` computed the flagged
set and then used it only to FILTER rows — it never passed it. Two
consequences, and the second is the one a client sees:

1. The Critic re-reports defects the deterministic gate already raised,
   so one string arrives in the bug report twice for the same problem.
2. Worse, under full_scan the Critic sees a row that T1 already flagged
   with no indication of that, so it may report the same placeholder
   bug instead of looking for the SEMANTIC bug that also exists. A
   string with a deterministic defect AND an LLM-level defect is the
   case the tier cascade exists to catch, and it was the case most
   likely to come back with only one of the two.
"""
from __future__ import annotations

from typing import List

from orbit8.graphs.lqa import LQAConfig, LQAContext, run_lqa_stage
from orbit8.memory import RunDB
from orbit8.po_scan import _seed
from orbit8.schemas import BugType, Finding, Review, Severity


class _Provider:
    """Records what the Critic was told, and reports a DIFFERENT defect
    from the one T1 found — the two-bugs-one-string case."""
    name, model, tokens_spent = "stub", "stub-1", 0.0

    def __init__(self):
        self.prompts: List[str] = []

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        self.prompts.append(user)
        return '{"findings": []}'


def _seeded_db(tmp_path):
    """A placeholder defect T1 catches mechanically, on a row that also
    needs semantic review — the two-bugs-one-string case."""
    db = RunDB(tmp_path / "scan.db")
    uid_map, _ = _seed(db, [("k1", "剩余{0}秒", "Time remaining", "")])
    return db, list(uid_map)


def test_tier3_is_given_the_deterministic_findings(tmp_path, monkeypatch):
    """The regression, pinned where it happened: the flagged set must
    reach `review_batch`, not just filter rows."""
    seen = {}

    def fake_review(provider, items, **kwargs):
        seen["known"] = kwargs.get("known_findings")
        return Review(findings=[]), "fp"

    from orbit8 import agents
    monkeypatch.setattr(agents, "review_batch", fake_review)

    db, _uids = _seeded_db(tmp_path)
    cfg = LQAConfig(game="G", source_lang="zh", locale="en", full_scan=True)
    ctx = LQAContext(provider=_Provider(), cfg=cfg, run_db=db)
    run_lqa_stage(ctx, "j")

    known = seen.get("known")
    assert known, "T3 received no prior findings at all"
    assert any(f.bug_type is BugType.PLACEHOLDER for f in known), \
        "the placeholder finding T1 raised must be passed through"


def test_the_prompt_actually_carries_them():
    """End-to-end through the real prompt builder: a caller passing
    known_findings must see them in the text, or the wiring is
    decorative."""
    from orbit8 import agents

    provider = _Provider()
    agents.review_batch(
        provider, [("k1", "剩余{0}秒", "Time remaining")],
        source_lang="zh", target_lang="en", game="G",
        known_findings=[Finding(
            key="k1", bug_type=BugType.PLACEHOLDER,
            severity=Severity.HIGH,
            message="Placeholder {0} missing from target.",
            evidence="Time remaining")])
    prompt = provider.prompts[0]
    assert "ALREADY KNOWN" in prompt
    assert "Placeholder {0} missing" in prompt


def test_no_prior_findings_means_no_known_section():
    """A clean row must not get an empty 'already known' block — that is
    prompt weight for nothing."""
    from orbit8 import agents
    provider = _Provider()
    agents.review_batch(
        provider, [("k1", "确定", "Confirm")],
        source_lang="zh", target_lang="en", game="G")
    assert "ALREADY KNOWN" not in provider.prompts[0]
