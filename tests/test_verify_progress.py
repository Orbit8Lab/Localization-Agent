"""The verifier must report that it is alive.

On a 1,233-row run the verify phase went silent for 40+ minutes while
making one LLM call per finding. `on_progress` fired only for
`t3_batch`, so a working verifier and a wedged one produced identical
output: nothing. Diagnosing it needed `lsof` to prove a socket was
open — the same "silence looks like success" failure the glossary
coverage gap had, in a different place.
"""
from __future__ import annotations

from orbit8.graphs.lqa import LQAConfig, LQAContext, run_lqa_stage
from orbit8.memory import RunDB
from orbit8.po_scan import _seed

_FINDING = ('{"findings": [{"key": "u0000", "bug_type": "terminology",'
            ' "severity": "medium", "message": "stub",'
            ' "evidence": "stub"}]}')
_VERDICT = ('{"decision": "confirm", "confidence": 0.9,'
            ' "reasoning": "stub", "suggested_target": null}')


class _Provider:
    """One finding per batch, so the verifier has work to do.

    Routes on "second-layer", which only the verifier's system prompt
    contains. Sniffing "reviewer" does NOT work — both the Critic and
    the verifier describe themselves that way, so the verifier received
    the Critic's response shape and failed Verdict validation.
    """
    name, model, tokens_spent = "stub", "stub-1", 0.0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        if "second-layer" in system:
            return _VERDICT
        return _FINDING


class _Clean(_Provider):
    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        if "second-layer" in system:
            return _VERDICT
        return '{"findings": []}'


def _ctx(tmp_path, provider, events):
    db = RunDB(tmp_path / "s.db")
    _seed(db, [("k1", "确定", "Confirm", "/Game/UI/DT_A.DT_A.0.Text")])
    cfg = LQAConfig(game="G", source_lang="zh", locale="en", full_scan=True)
    return LQAContext(provider=provider, cfg=cfg, run_db=db,
                      on_progress=lambda ev, info=None: events.append(ev))


def test_the_verifier_announces_its_workload(tmp_path):
    events: list = []
    run_lqa_stage(_ctx(tmp_path, _Provider(), events), "j")
    assert "verify_start" in events, (
        f"a verify phase with findings must announce itself; got {events}")


def test_no_findings_means_no_verify_noise(tmp_path):
    """Silence has to be earned: a run with nothing to verify must not
    emit a phase banner, or operators learn to ignore the channel."""
    events: list = []
    run_lqa_stage(_ctx(tmp_path, _Clean(), events), "j")
    assert "verify_start" not in events
