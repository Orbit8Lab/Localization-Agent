"""Skill registry and promotion policy (PLAN §4, §5.4, §5.6, §5.8, §6.3, §6.5).

The bugs this machinery exists to prevent are all silent — a deadlocked
counter, a leaked namespace, a skill promoted on our own scorer's opinion.
None of them raise; they just quietly produce a system that either never
learns or learns the wrong thing. So each gets a test that would fail if
the fix were undone.

Every threshold here is passed in explicitly. The production defaults are
uncalibrated placeholders (see PromotionPolicy) and a test that depended on
them would silently start failing the moment a calibration run tightened
one.
"""
from __future__ import annotations

import pytest

from orbit8.observation import (ACCEPTED, FIRST, G3_ACCEPTED, G3_EDITED,
                                G3_PENDING, G3_REJECTED, REJECTED)
from orbit8.skills import (Boundary, PromotionPolicy, SkillRegistry,
                           boundary_for, utility_for)

# A policy tuned for readable tests: small floors so a handful of rows can
# cross them. NOT a recommendation — see PromotionPolicy's docstring.
TEST_POLICY = PromotionPolicy(min_samples=3, min_g3_reviewed=2,
                              min_g3_agreement=0.6, decay_window=4,
                              decay_floor=0.5)


@pytest.fixture
def registry(tmp_path):
    return SkillRegistry(tmp_path / "skills.db", "tenant-a",
                         policy=TEST_POLICY)


def _row(uid="u1", locale="ko", verdict=ACCEPTED, before=100, after=0,
         g3=G3_PENDING, g3_text=None, attempt=1, sig="t/x") -> dict:
    """A row as the observation log returns it. `signatures` is a LIST
    because one candidate can exhibit several defect classes at once —
    and decay_check filters on it, so omitting it here would mean the
    decay tests silently scored an empty window."""
    return {"uid": uid, "locale": locale, "verdict": verdict,
            "badness_before": before, "badness_after": after,
            "g3_verdict": g3, "g3_text": g3_text, "attempt": attempt,
            "signatures": [sig]}


# ------------------------------------------------- §4.1 the deadlock bug

def test_the_tally_increments_from_the_very_first_observation(registry):
    """THE §4.1 bug. The sketched promote() only wrote when the count was
    already >= threshold, but derived that count from the key it was
    refusing to write — so it stayed at 1 forever and nothing was ever
    promoted. The tally must therefore be written unconditionally."""
    for n in range(3):
        registry.observe("terminology/x", fix_ref="fix:1", job_id="j",
                         attempt=1, uid=f"u{n}", accepted=True)
    tally = registry.tally("terminology/x")
    assert tally["accepted"] == 3
    assert tally["distinct_strings"] == 3


def test_promotion_never_gates_the_tally_write(registry):
    """The deadlock in one assertion: an observation nowhere near the
    threshold must still be counted, or the threshold is unreachable."""
    registry.observe("terminology/x", fix_ref="f", job_id="j", attempt=1,
                     uid="u1", accepted=True)
    assert registry.tally("terminology/x")["accepted"] == 1


def test_the_audit_trail_records_which_job_and_attempt_voted(registry):
    """§4.1: without the trail a count of 5 is unfalsifiable — 5 distinct
    defects and one loop retried 5 times look identical as an integer and
    warrant opposite decisions."""
    registry.observe("t/x", fix_ref="f", job_id="job-a", attempt=1,
                     uid="u1", accepted=True)
    registry.observe("t/x", fix_ref="f", job_id="job-b", attempt=2,
                     uid="u2", accepted=False)
    trail = registry.tally("t/x")["observed_at"]
    assert [e["job"] for e in trail] == ["job-a", "job-b"]
    assert [e["attempt"] for e in trail] == [1, 2]
    assert [e["accepted"] for e in trail] == [True, False]


def test_repeated_attempts_on_one_string_are_one_piece_of_evidence(registry):
    """§4.1: a flapping repair loop must not look like a recurring defect
    class. `accepted` counts applications; `distinct_strings` is what the
    promotion floor reads."""
    for _ in range(5):
        registry.observe("t/x", fix_ref="f", job_id="j", attempt=1,
                         uid="same", accepted=True)
    tally = registry.tally("t/x")
    assert tally["accepted"] == 5
    assert tally["distinct_strings"] == 1


# ------------------------------------------------- §4.3/§6.3 dual track

def test_rejections_are_counted_separately_not_dropped(registry):
    registry.observe("t/x", fix_ref="f", job_id="j", attempt=1, uid="u1",
                     accepted=True)
    registry.observe("t/x", fix_ref="f", job_id="j", attempt=1, uid="u2",
                     accepted=False)
    tally = registry.tally("t/x")
    assert (tally["accepted"], tally["rejected"]) == (1, 1)


def test_a_rejection_becomes_a_counter_example(registry):
    """§6.3: the negative track. A rolled-back repair defines where the
    skill stops applying, so it must survive as a case, not a tally."""
    rows = [_row(uid="u1", verdict=ACCEPTED),
            _row(uid="u2", verdict=REJECTED, before=10, after=100)]
    boundary = boundary_for(rows, "t/x")
    assert not boundary.is_empty
    assert boundary.summary() == {"ratchet_rejected": 1}


def test_a_human_overturn_is_the_strongest_counter_example(registry):
    """Our gate accepted it and a human overruled it — that is evidence the
    gate is miscalibrated here (§5.6), not merely that the fix failed."""
    rows = [_row(uid="u1", verdict=ACCEPTED, g3=G3_EDITED,
                 g3_text="what the human wrote")]
    boundary = boundary_for(rows, "t/x")
    assert boundary.summary() == {"g3_overturned": 1}
    assert boundary.counter_examples[0]["human_text"] == "what the human wrote"


def test_a_clean_signature_has_an_empty_boundary():
    rows = [_row(uid=f"u{n}", g3=G3_ACCEPTED) for n in range(3)]
    assert boundary_for(rows, "t/x").is_empty


# --------------------------------------------------- §5.4 tenant leakage

def test_two_tenants_do_not_share_a_tally(tmp_path):
    """§5.4: the sketch used a global ("skills", "repair") namespace, so a
    fix mined from one client's asset would apply to another's. Given the
    existing TenantMemory separation that is a policy break."""
    path = tmp_path / "skills.db"
    a = SkillRegistry(path, "tenant-a", policy=TEST_POLICY)
    b = SkillRegistry(path, "tenant-b", policy=TEST_POLICY)
    for n in range(3):
        a.observe("t/x", fix_ref="f", job_id="j", attempt=1, uid=f"u{n}",
                  accepted=True)

    assert a.tally("t/x")["accepted"] == 3
    assert b.tally("t/x")["accepted"] == 0          # never sees a's evidence
    assert b.all_tallies() == []


def test_an_empty_tenant_is_refused(tmp_path):
    """An empty tenant id would collapse every client into one namespace —
    the leak this guards, arriving through a default rather than a bug."""
    with pytest.raises(ValueError):
        SkillRegistry(tmp_path / "s.db", "")


# ---------------------------------- §5.6 badness alone cannot promote

def test_badness_improvement_alone_does_not_promote(registry):
    """THE §5.6 correction. Our gate's accept/reject and _badness() are the
    same scorer the repair is trying to satisfy. A skill that reliably
    lowers it is reliably satisfying our checks, which is not the same
    claim as improving the translation."""
    rows = [_row(uid=f"u{n}", verdict=ACCEPTED, before=100, after=0)
            for n in range(10)]           # perfect by our own metric...
    for n in range(10):
        registry.observe("t/x", fix_ref="f", job_id="j", attempt=1,
                         uid=f"u{n}", accepted=True)

    utility = utility_for(rows, "t/x", TEST_POLICY)
    ok, blockers = registry.eligibility("t/x", utility)
    assert not ok                          # ...and still not promotable
    assert any("G3-reviewed" in b for b in blockers)


def test_human_agreement_unlocks_promotion(registry):
    rows = [_row(uid=f"u{n}", verdict=ACCEPTED, g3=G3_ACCEPTED)
            for n in range(4)]
    for n in range(4):
        registry.observe("t/x", fix_ref="f", job_id="j", attempt=1,
                         uid=f"u{n}", accepted=True)
    ok, blockers = registry.eligibility(
        "t/x", utility_for(rows, "t/x", TEST_POLICY))
    assert ok, blockers


def test_a_high_overturn_rate_blocks_promotion(registry):
    """Reviewed, but the humans mostly disagreed — the case that must not
    promote no matter how good the badness delta looks."""
    rows = [_row(uid="u0", verdict=ACCEPTED, g3=G3_ACCEPTED),
            _row(uid="u1", verdict=ACCEPTED, g3=G3_EDITED, g3_text="x"),
            _row(uid="u2", verdict=ACCEPTED, g3=G3_REJECTED),
            _row(uid="u3", verdict=ACCEPTED, g3=G3_EDITED, g3_text="y")]
    for n in range(4):
        registry.observe("t/x", fix_ref="f", job_id="j", attempt=1,
                         uid=f"u{n}", accepted=True)
    ok, blockers = registry.eligibility(
        "t/x", utility_for(rows, "t/x", TEST_POLICY))
    assert not ok
    assert any("agreement" in b for b in blockers)


def test_a_small_sample_of_agreement_is_not_enough(registry):
    """§5.6 requires a floor on the SAMPLE as well as the rate: 1-for-1 is
    100% agreement and no evidence at all."""
    rows = [_row(uid="u0", verdict=ACCEPTED, g3=G3_ACCEPTED)]
    registry.observe("t/x", fix_ref="f", job_id="j", attempt=1, uid="u0",
                     accepted=True)
    ok, blockers = registry.eligibility(
        "t/x", utility_for(rows, "t/x", TEST_POLICY))
    assert not ok
    assert any("G3-reviewed" in b or "samples" in b for b in blockers)


def test_blockers_say_which_floor_is_binding(registry):
    """'not yet' and 'never' are different states. A calibration run needs
    to know WHICH floor a signature fails to tell whether the floor or the
    skill is wrong."""
    ok, blockers = registry.eligibility(
        "t/x", utility_for([], "t/x", TEST_POLICY))
    assert not ok and len(blockers) >= 2


# ------------------------------------------------------ §6.2 utility

def test_utility_counts_distinct_strings_not_observations():
    rows = [_row(uid="same", verdict=ACCEPTED) for _ in range(6)]
    assert utility_for(rows, "t/x", TEST_POLICY).samples == 1


def test_an_unreviewed_skill_cannot_reach_a_high_score():
    """§5.6 as arithmetic: without a human ruling, the score is damped so
    an unreviewed skill never outranks a reviewed one."""
    unreviewed = [_row(uid=f"u{n}", verdict=ACCEPTED) for n in range(4)]
    reviewed = [_row(uid=f"u{n}", verdict=ACCEPTED, g3=G3_ACCEPTED)
                for n in range(4)]
    assert (utility_for(unreviewed, "t/x", TEST_POLICY).score
            < utility_for(reviewed, "t/x", TEST_POLICY).score)


def test_confidence_grows_with_evidence_rather_than_switching_on():
    """A hard cutoff would flip a skill from invisible to fully trusted on
    one observation; §6.2 wants evidence to accumulate."""
    small = utility_for([_row(uid="u0", verdict=ACCEPTED, g3=G3_ACCEPTED)],
                        "t/x", TEST_POLICY)
    big = utility_for([_row(uid=f"u{n}", verdict=ACCEPTED, g3=G3_ACCEPTED)
                       for n in range(6)], "t/x", TEST_POLICY)
    assert 0.0 < small.confidence < big.confidence <= 1.0


def test_a_signature_with_no_rows_scores_zero_not_an_error():
    utility = utility_for([], "t/x", TEST_POLICY)
    assert utility.score == 0.0 and utility.samples == 0
    assert not utility.is_evidence_backed


def test_the_accept_rate_reflects_rejections():
    rows = [_row(uid="u0", verdict=ACCEPTED),
            _row(uid="u1", verdict=REJECTED),
            _row(uid="u2", verdict=REJECTED),
            _row(uid="u3", verdict=REJECTED)]
    assert utility_for(rows, "t/x", TEST_POLICY).accept_rate == 0.25


def test_first_candidates_are_not_scored_as_applications():
    """A FIRST candidate beat nothing — counting it as an accept would
    inflate every rate computed from the log."""
    rows = [_row(uid="u0", verdict=FIRST, before=None)]
    assert utility_for(rows, "t/x", TEST_POLICY).accept_rate == 0.0


# ------------------------------------------------- §6.5 decay/demotion

def test_decay_is_scored_over_a_window_not_a_lifetime(registry):
    """§6.5: a cumulative counter is dominated by history and cannot fall,
    so it is structurally unable to detect a skill that stopped working.
    Long success followed by consistent failure must trip demotion."""
    history = [_row(uid=f"old{n}", verdict=ACCEPTED) for n in range(20)]
    recent = [_row(uid=f"new{n}", verdict=REJECTED) for n in range(4)]
    assert registry.decay_check("t/x", history + recent) is not None


def test_a_healthy_skill_is_not_demoted(registry):
    rows = [_row(uid=f"u{n}", verdict=ACCEPTED) for n in range(10)]
    assert registry.decay_check("t/x", rows) is None


def test_collapsing_human_agreement_is_reported_distinctly(registry):
    """Active harm, not gradual decay: §6.5 stops this firing immediately
    rather than waiting for an approval, so the caller must be able to tell
    the two apart."""
    rows = [_row(uid=f"u{n}", verdict=ACCEPTED, g3=G3_REJECTED)
            for n in range(4)]
    reason = registry.decay_check("t/x", rows)
    assert reason and reason.startswith("g3_agreement_collapsed")


def test_an_unexercised_skill_is_flagged_stale_not_trusted(registry):
    """§6.5: a skill nobody matched recently is unvalidated, not
    validated."""
    assert registry.staleness_check(10) is None
    assert registry.staleness_check(
        TEST_POLICY.staleness_limit + 1) is not None


def test_decay_ignores_a_signature_with_no_applications(registry):
    assert registry.decay_check("t/x", [_row(verdict=FIRST)]) is None


def test_decay_scores_only_the_named_signature(registry):
    """decay_check filters `rows` itself rather than trusting the caller to
    have done it. Handed the whole log — the natural mistake — it would
    otherwise return a confident answer about a DIFFERENT skill, with
    nothing at the call site to reveal the mix-up."""
    healthy = [_row(uid=f"ok{n}", verdict=ACCEPTED, sig="t/healthy")
               for n in range(8)]
    failing = [_row(uid=f"no{n}", verdict=REJECTED, sig="t/failing")
               for n in range(8)]
    both = healthy + failing

    assert registry.decay_check("t/healthy", both) is None
    assert registry.decay_check("t/failing", both) is not None


def test_decay_is_silent_for_a_signature_absent_from_the_rows(registry):
    rows = [_row(uid=f"u{n}", verdict=REJECTED, sig="t/other")
            for n in range(8)]
    assert registry.decay_check("t/missing", rows) is None


# ------------------------------------------- §5.8 the audited request

def test_a_promotion_request_carries_both_tracks(registry):
    """§5.8: a reviewer shown only a win count has nothing to judge but the
    win count. The boundary is what invites 'is that the right limit?'"""
    rows = [_row(uid="u0", verdict=ACCEPTED, g3=G3_ACCEPTED),
            _row(uid="u1", verdict=ACCEPTED, g3=G3_ACCEPTED),
            _row(uid="u2", verdict=REJECTED, before=10, after=100),
            _row(uid="u3", verdict=ACCEPTED, g3=G3_EDITED, g3_text="fixed")]
    for n in range(4):
        registry.observe("t/x", fix_ref="fix:v1", job_id="j", attempt=1,
                         uid=f"u{n}", accepted=n != 2)

    req = registry.promotion_candidate("t/x", rows)
    assert req.accepted == 3 and req.rejected == 1
    assert {c.reason for c in req.counter_examples} == {
        "ratchet_rejected", "g3_overturned"}
    assert req.observed_at and req.fix_ref == "fix:v1"


def test_the_request_is_scoped_to_its_tenant(registry):
    registry.observe("t/x", fix_ref="f", job_id="j", attempt=1, uid="u0",
                     accepted=True)
    assert registry.promotion_candidate("t/x", []).tenant_id == "tenant-a"


def test_an_ineligible_signature_still_produces_a_request(registry):
    """Filing is separate from eligibility: the blockers are the data a
    calibration run reads to see which floor is binding."""
    req = registry.promotion_candidate("t/x", [])
    assert req.blockers
    assert req.utility_score == 0.0


def test_building_a_request_activates_nothing(registry):
    """§5.8: a promoted skill is new latent capability making silent
    decisions on every future job. Nothing in this module may make one
    live — that requires the human approval step."""
    registry.observe("t/x", fix_ref="f", job_id="j", attempt=1, uid="u0",
                     accepted=True)
    registry.promotion_candidate("t/x", [])
    # the registry has a tally and nothing resembling an active skill set
    assert not hasattr(registry, "active_skills")
    assert registry.tally("t/x")["accepted"] == 1


# -------------------------------------------------------- the policy

def test_the_policy_rejects_impossible_rates():
    with pytest.raises(ValueError):
        PromotionPolicy(min_g3_agreement=1.5)
    with pytest.raises(ValueError):
        PromotionPolicy(decay_floor=-0.1)
    with pytest.raises(ValueError):
        PromotionPolicy(min_samples=0)


def test_the_shipped_defaults_promote_nothing_on_a_small_sample(tmp_path):
    """The defaults are uncalibrated placeholders, and they must fail
    CLOSED: an uncalibrated system promotes nothing rather than promoting
    on guesswork. A calibration run is expected to lower them."""
    registry = SkillRegistry(tmp_path / "s.db", "t")     # default policy
    rows = [_row(uid=f"u{n}", verdict=ACCEPTED, g3=G3_ACCEPTED)
            for n in range(5)]
    for n in range(5):
        registry.observe("t/x", fix_ref="f", job_id="j", attempt=1,
                         uid=f"u{n}", accepted=True)
    ok, blockers = registry.eligibility(
        "t/x", utility_for(rows, "t/x", registry.policy))
    assert not ok and blockers
