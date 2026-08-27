"""Skill registry and promotion policy (PLAN §4, §5.8, §6.2, §6.3, §6.5).

This module holds the *machinery* of learned repair skills. It deliberately
holds none of the *numbers*: every threshold is a field on `PromotionPolicy`
with no calibrated default, because the values are what a batch calibration
run over real observation data is supposed to determine (PLAN open
questions). The plan opens by criticizing a guessed threshold of 3; picking
one here would repeat that mistake in the code meant to fix it.

Structure, and the bug each piece exists to prevent:

- `SkillRegistry.observe()` — the tally lives in its OWN namespace and
  increments unconditionally (§4.1: the sketched `promote()` only wrote
  when the count was already high enough, reading the key it refused to
  write, so nothing was ever promoted).
- Namespaces are per-tenant (§5.4: a global `("skills", "repair")` would
  apply a fix mined from one client's asset to another's).
- Accepted and rejected applications are tracked SEPARATELY (§4.3, §6.3):
  the accepted track is what the skill does, the rejected track is where it
  stops applying. A skill with only the first generalizes exactly as far as
  its first few successes reached, then misfires silently.
- `promotion_candidate()` returns a *request*, never an active skill
  (§5.8): promotion is audited, so nothing here can make a skill live.

Nothing in this module retrieves or applies a skill. Wiring it into the
repair loop is the second half of Phase 4 (PLAN §6, step 2) and must not
happen until step 1's retrieval-as-hints has measured match precision.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .observation import (ACCEPTED, G3_ACCEPTED, G3_EDITED, G3_PENDING,
                          G3_REJECTED, REJECTED)
from .schemas import SkillCounterExample, SkillPromotionRequest

SCHEMA_VERSION = 1


# ----------------------------------------------------------------- policy

@dataclass
class PromotionPolicy:
    """Every constant Phase 4 needs, in one injectable place.

    The defaults here are **deliberately conservative placeholders, not
    calibrated values**. They are set so that an uncalibrated system
    promotes nothing rather than promoting on guesswork: `min_samples` and
    `min_g3_reviewed` are high enough that no small pilot trips them by
    accident. A calibration run is expected to LOWER them to whatever the
    observation data supports.

    PLAN's open questions list every one of these as unresolved. Treat a
    value you did not measure as unset.
    """
    # §4.1: distinct accepted applications before a signature is even a
    # candidate. Counted over DISTINCT STRINGS, never observations — 9
    # accepts on one string is a flapping repair loop, not a defect class.
    min_samples: int = 20

    # §5.6: promotion additionally requires human agreement, not just a
    # badness improvement our own gate scored. Both a floor on the rate and
    # a floor on how many reviewed cases that rate is computed from —
    # 3-for-3 is not evidence.
    min_g3_reviewed: int = 10
    min_g3_agreement: float = 0.9

    # §6.2: retrieval ranks on relevance × utility. Candidates below this
    # utility are not offered at all.
    min_utility: float = 0.5

    # §6.5: decay is scored over a WINDOW of recent applications, never a
    # lifetime count — a lifetime counter is dominated by history and
    # cannot fall, which makes it structurally unable to detect decay.
    decay_window: int = 50
    decay_floor: float = 0.4

    # §6.5: a skill nobody exercised is unvalidated, not validated. Flagged
    # for re-audit rather than trusted indefinitely.
    staleness_limit: int = 500

    def __post_init__(self) -> None:
        """Reject configurations that cannot mean what they appear to.

        Every field is validated, not just the obvious rates. A policy is
        set by hand or from a CLI flag during calibration, and the failure
        modes here are silent rather than loud: `min_utility=2.0` is an
        unsatisfiable floor that blocks every skill forever while looking
        like a strict-but-reasonable setting, and `decay_window=0` makes
        `rows[-0:]` the WHOLE history — the exact lifetime-scoring bug §6.5
        exists to avoid, reintroduced through a config value.
        """
        if not 0.0 <= self.min_g3_agreement <= 1.0:
            raise ValueError("min_g3_agreement is a rate in [0, 1]")
        if not 0.0 <= self.decay_floor <= 1.0:
            raise ValueError("decay_floor is a rate in [0, 1]")
        # Utility is a product of factors each in [0, 1] (see utility_for),
        # so it cannot exceed 1.0 — a floor above that is unreachable.
        if not 0.0 <= self.min_utility <= 1.0:
            raise ValueError("min_utility is a rate in [0, 1]")
        if self.min_samples < 1 or self.min_g3_reviewed < 1:
            raise ValueError("sample floors must be >= 1")
        if self.decay_window < 1:
            raise ValueError("decay_window must be >= 1 (0 would score the "
                             "whole history, not a window)")
        if self.staleness_limit < 1:
            raise ValueError("staleness_limit must be >= 1")


# ---------------------------------------------------------------- utility

@dataclass(frozen=True)
class Utility:
    """What the observation log says about one signature's fix (§6.2).

    `score` combines the three signals; `confidence` is the sample weight
    that keeps a 2-observation skill from outranking a 200-observation one
    just because it is semantically closer. That weighting is what makes
    cold start fall out of the ranking instead of needing a warm-up rule.
    """
    signature: str
    samples: int                 # distinct strings, not observations
    accept_rate: float           # ratchet: accepted / (accepted + rejected)
    mean_badness_delta: float    # >0 means it reduced badness
    g3_reviewed: int
    g3_agreement: float          # upheld / reviewed; 0.0 when unreviewed
    score: float
    confidence: float

    @property
    def is_evidence_backed(self) -> bool:
        """True when a human — not only our scorer — has weighed in."""
        return self.g3_reviewed > 0


def rows_for(rows: Sequence[dict], signature: str) -> List[dict]:
    """The observations that actually belong to `signature`.

    Every function here that takes a signature filters through this. The
    alternative — trusting callers to pre-filter — already produced a real
    bug: a healthy skill scoring 1.0 dropped to 0.111 when handed the full
    log, because another signature's rejections were counted against it,
    and `boundary_for` attributed that other signature's counter-examples
    to it as a fabricated applicability boundary.

    A row carries a LIST of signatures: one candidate can exhibit several
    defect classes at once, and it is evidence about each of them.
    """
    return [row for row in rows if signature in row.get("signatures", ())]


def _confidence(samples: int, target: int) -> float:
    """Saturating sample weight in [0, 1]. Deliberately not a hard cutoff:
    a cutoff makes one observation flip a skill from invisible to fully
    trusted, whereas this makes evidence accumulate."""
    if target <= 0:
        return 1.0
    return min(1.0, samples / float(target))


def utility_for(rows: Sequence[dict], signature: str,
                policy: PromotionPolicy) -> Utility:
    """Score one signature from raw observation rows (§6.2).

    A pure function over the log: no I/O, no state. That is what lets a
    calibration run sweep policy parameters over a fixed dataset without
    re-running Stage 4, which is the difference between a parameter sweep
    that takes minutes and one that costs API spend.

    `rows` is filtered to `signature` here rather than trusted to arrive
    pre-filtered — see `rows_for`. Taking a signature and ignoring it made
    the full log a valid-looking argument that silently mixed every
    signature's evidence together.
    """
    rows = rows_for(rows, signature)
    applied = [r for r in rows if r["verdict"] in (ACCEPTED, REJECTED)]
    accepted = [r for r in applied if r["verdict"] == ACCEPTED]

    # Distinct STRINGS (§4.1): repeated attempts on one segment are one
    # piece of evidence, not many.
    samples = len({r["uid"] for r in accepted})
    accept_rate = (len(accepted) / len(applied)) if applied else 0.0

    deltas = [(r["badness_before"] - r["badness_after"])
              for r in applied
              if r["badness_before"] is not None]
    mean_delta = (sum(deltas) / len(deltas)) if deltas else 0.0

    ruled = [r for r in rows if r["g3_verdict"] != G3_PENDING]
    upheld = [r for r in ruled if r["g3_verdict"] == G3_ACCEPTED]
    g3_reviewed = len({r["uid"] for r in ruled})
    g3_agreement = (len(upheld) / len(ruled)) if ruled else 0.0

    # The score is the product of "did it work" and "did a human agree",
    # weighted by how much evidence stands behind each. G3 agreement is
    # NOT averaged in as a co-equal term: an unreviewed skill must not be
    # able to reach a high score on badness improvement alone (§5.6), so
    # it acts as a multiplier that is neutral-low until a human rules.
    gate_signal = accept_rate * _confidence(samples, policy.min_samples)
    human_signal = (g3_agreement
                    * _confidence(g3_reviewed, policy.min_g3_reviewed))
    score = gate_signal * (human_signal if ruled else 0.5)

    return Utility(
        signature=signature, samples=samples, accept_rate=accept_rate,
        mean_badness_delta=mean_delta, g3_reviewed=g3_reviewed,
        g3_agreement=g3_agreement, score=score,
        confidence=min(_confidence(samples, policy.min_samples),
                       _confidence(g3_reviewed, policy.min_g3_reviewed)))


# ------------------------------------------------------------- boundaries

@dataclass(frozen=True)
class Boundary:
    """Where a skill stops applying (§6.3) — the negative track.

    Built from applications the ratchet rejected or a human overturned.
    Carried on the promotion request so a reviewer sees the skill's LIMIT,
    not just its win count: a win count invites a rubber-stamp, a boundary
    invites "is that the right limit?", which is the question a human
    answers better than the log.
    """
    signature: str
    counter_examples: List[dict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.counter_examples

    def summary(self) -> Dict[str, int]:
        by_reason: Dict[str, int] = {}
        for case in self.counter_examples:
            by_reason[case["reason"]] = by_reason.get(case["reason"], 0) + 1
        return by_reason


def boundary_for(rows: Sequence[dict], signature: str) -> Boundary:
    """Collect the counter-examples for one signature (§6.3).

    Filtered through `rows_for`: an unfiltered call attributed OTHER
    signatures' failures to this one, which is the worst possible thing to
    get wrong here — the boundary is what a human reviewer reads to decide
    where a skill stops applying, and a fabricated one argues against a
    skill using evidence that has nothing to do with it.
    """
    cases: List[dict] = []
    for row in rows_for(rows, signature):
        reason = None
        if row["verdict"] == REJECTED:
            reason = "ratchet_rejected"
        elif row["g3_verdict"] in (G3_EDITED, G3_REJECTED):
            # A human overruling a candidate our gate accepted is the
            # strongest counter-example there is: it says the gate itself
            # is miscalibrated here (§5.6).
            reason = "g3_overturned"
        if reason:
            cases.append({
                "uid": row["uid"], "locale": row["locale"],
                "reason": reason, "attempt": row["attempt"],
                "badness_before": row["badness_before"],
                "badness_after": row["badness_after"],
                "human_text": row.get("g3_text"),
            })
    return Boundary(signature=signature, counter_examples=cases)


# ------------------------------------------------------------- the registry

class SkillRegistry:
    """Per-tenant tally and candidate store.

    NOT a live skill store: nothing here is consulted by the repair loop.
    `promotion_candidate()` produces a request for a human (§5.8), and a
    skill only becomes active once that request is approved — which is a
    Phase 4 step 2 concern, not this module's.
    """

    def __init__(self, path: Path, tenant_id: str,
                 policy: Optional[PromotionPolicy] = None):
        if not tenant_id:
            # §5.4: an empty tenant would collapse every client into one
            # shared namespace, which is the exact leak this guards.
            raise ValueError("tenant_id is required (PLAN §5.4)")
        self.tenant_id = tenant_id
        self.policy = policy or PromotionPolicy()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tally (
                tenant_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                accepted INTEGER NOT NULL DEFAULT 0,
                rejected INTEGER NOT NULL DEFAULT 0,
                last_fix TEXT,
                observed_at TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (tenant_id, signature)
            )""")
        self.conn.commit()

    # -- write -------------------------------------------------------

    def observe(self, signature: str, *, fix_ref: str, job_id: str,
                attempt: int, uid: str, accepted: bool) -> dict:
        """Record one application of a fix. Increments UNCONDITIONALLY.

        §4.1: the sketched `promote()` wrote only when the count was
        already at the threshold, but derived that count from the key it
        was refusing to write — so the count stayed at 1 forever and
        nothing was ever promoted. The tally is therefore its own
        namespace, always written; promotion READS it (see
        `promotion_candidate`) and never gates the write.

        §4.3: accepted and rejected are separate counters, because a
        rejection is evidence about the skill's boundary rather than a
        failure to discard.
        """
        row = self.conn.execute(
            "SELECT * FROM tally WHERE tenant_id = ? AND signature = ?",
            (self.tenant_id, signature)).fetchone()
        trail = json.loads(row["observed_at"]) if row else []
        # The audit trail (§4.1): without it a count of 5 is unfalsifiable,
        # because 5 distinct defects and one loop retried 5 times warrant
        # opposite decisions and look identical as an integer.
        trail.append({"job": job_id, "attempt": attempt, "uid": uid,
                      "accepted": bool(accepted)})
        accepted_n = (row["accepted"] if row else 0) + (1 if accepted else 0)
        rejected_n = (row["rejected"] if row else 0) + (0 if accepted else 1)
        self.conn.execute(
            "INSERT INTO tally (tenant_id, signature, accepted, rejected, "
            "last_fix, observed_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(tenant_id, signature) DO UPDATE SET "
            "accepted = excluded.accepted, rejected = excluded.rejected, "
            "last_fix = excluded.last_fix, "
            "observed_at = excluded.observed_at",
            (self.tenant_id, signature, accepted_n, rejected_n, fix_ref,
             json.dumps(trail, ensure_ascii=False)))
        self.conn.commit()
        return self.tally(signature)

    # -- read --------------------------------------------------------

    def tally(self, signature: str) -> dict:
        row = self.conn.execute(
            "SELECT * FROM tally WHERE tenant_id = ? AND signature = ?",
            (self.tenant_id, signature)).fetchone()
        if row is None:
            return {"signature": signature, "accepted": 0, "rejected": 0,
                    "last_fix": None, "observed_at": [],
                    "distinct_strings": 0}
        out = dict(row)
        out["observed_at"] = json.loads(out["observed_at"])
        out["distinct_strings"] = len(
            {e["uid"] for e in out["observed_at"] if e["accepted"]})
        return out

    def all_tallies(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT signature FROM tally WHERE tenant_id = ? "
            "ORDER BY signature", (self.tenant_id,)).fetchall()
        return [self.tally(r["signature"]) for r in rows]

    # -- promotion (a REQUEST, never an activation) -------------------

    def eligibility(self, signature: str,
                    utility: Utility) -> Tuple[bool, List[str]]:
        """Is this signature promotable, and if not, exactly why?

        Returns the blocking reasons rather than a bare False: "not yet"
        and "never" are different states, and a calibration run needs to
        know WHICH floor a signature is failing to know whether the floor
        or the skill is wrong.
        """
        policy, blockers = self.policy, []
        tally = self.tally(signature)
        if tally["distinct_strings"] < policy.min_samples:
            blockers.append(
                f"samples {tally['distinct_strings']} < "
                f"{policy.min_samples} distinct strings")
        # §5.6: badness improvement is necessary, NOT sufficient. A skill
        # that no human has reviewed cannot promote at any accept rate.
        if utility.g3_reviewed < policy.min_g3_reviewed:
            blockers.append(
                f"G3-reviewed {utility.g3_reviewed} < "
                f"{policy.min_g3_reviewed}")
        elif utility.g3_agreement < policy.min_g3_agreement:
            blockers.append(
                f"G3 agreement {utility.g3_agreement:.2f} < "
                f"{policy.min_g3_agreement:.2f}")
        if utility.mean_badness_delta <= 0:
            blockers.append("no mean badness improvement")
        return (not blockers), blockers

    def promotion_candidate(self, signature: str, rows: Sequence[dict],
                            ) -> SkillPromotionRequest:
        """Build the audited request for one signature (§5.8).

        Always returns a request, even when it is not yet eligible: the
        `blockers` field is what a calibration run reads to learn WHICH
        floor is binding. Filing it is a separate, explicit act — see
        `file_promotion_request`.
        """
        tally = self.tally(signature)
        utility = utility_for(rows, signature, self.policy)
        boundary = boundary_for(rows, signature)
        blockers = self.eligibility(signature, utility)[1]
        return SkillPromotionRequest(
            signature=signature, tenant_id=self.tenant_id,
            fix_ref=tally["last_fix"] or "",
            accepted=tally["accepted"], rejected=tally["rejected"],
            distinct_strings=tally["distinct_strings"],
            mean_badness_delta=utility.mean_badness_delta,
            g3_reviewed=utility.g3_reviewed,
            g3_agreement=utility.g3_agreement,
            utility_score=utility.score,
            counter_examples=[SkillCounterExample(**case)
                              for case in boundary.counter_examples],
            observed_at=tally["observed_at"],
            blockers=blockers)

    # -- decay (§6.5) -------------------------------------------------

    def decay_check(self, signature: str,
                    rows: Sequence[dict]) -> Optional[str]:
        """Should an already-promoted skill be demoted? Returns a reason,
        or None to leave it active.

        Scored over the LAST `decay_window` applications, never the
        lifetime tally: a cumulative counter is dominated by history and
        cannot fall, so it is structurally incapable of noticing that a
        skill stopped working. Content drifts — a new engine version
        changes the markup, a glossary update invalidates a term fix — and
        the skill keeps matching and firing regardless.

        This only RECOMMENDS. Demotion follows the §5.8 asymmetry: filed
        automatically, effective on human approval, because automatically
        removing a working skill is its own outage. The one exception is
        collapsing G3 agreement, which is evidence of active harm and is
        reported as such so the caller can stop it firing immediately.

        `rows` is filtered to `signature` here rather than trusted to be
        pre-filtered. Taking the signature and ignoring it would make a
        caller passing the whole log get a confident answer about the wrong
        skill, with nothing at the call site to reveal it.
        """
        rows = rows_for(rows, signature)
        applied = [r for r in rows if r["verdict"] in (ACCEPTED, REJECTED)]
        if not applied:
            return None
        window = applied[-self.policy.decay_window:]
        recent_ruled = [r for r in window if r["g3_verdict"] != G3_PENDING]
        if recent_ruled:
            agreement = (sum(1 for r in recent_ruled
                             if r["g3_verdict"] == G3_ACCEPTED)
                         / len(recent_ruled))
            if agreement < self.policy.decay_floor:
                return (f"g3_agreement_collapsed:{agreement:.2f}"
                        f"<{self.policy.decay_floor:.2f}")
        accept_rate = (sum(1 for r in window if r["verdict"] == ACCEPTED)
                       / len(window))
        if accept_rate < self.policy.decay_floor:
            return (f"accept_rate_decayed:{accept_rate:.2f}"
                    f"<{self.policy.decay_floor:.2f}")
        return None

    def staleness_check(self, observations_since: int) -> Optional[str]:
        """A skill nobody exercised recently is UNVALIDATED, not validated
        (§6.5). Flag for re-audit rather than trusting it indefinitely.

        Takes only a count: unlike `decay_check` there is nothing here to
        filter, so accepting a signature would be decoration that implies a
        lookup this never performs. The caller names the skill.
        """
        if observations_since > self.policy.staleness_limit:
            return (f"stale:{observations_since} observations since last "
                    f"match (limit {self.policy.staleness_limit})")
        return None


# ---------------------------------------------------------- calibration

def replay(rows_by_signature: Dict[str, List[dict]],
           policy: PromotionPolicy) -> List[dict]:
    """What WOULD have promoted under `policy`, given observations already
    collected. Pure: no registry, no writes, no API calls.

    This is the function a calibration run sweeps. Because the observation
    log is the fixed input and the policy is the variable, a parameter
    sweep costs milliseconds instead of a Stage 4 re-run — which is the
    difference between exploring the space and guessing at it.

    Each result reports the binding blocker as well as the verdict, because
    "which floor is stopping this" is the actual output of a sweep: a
    signature blocked only by `min_g3_reviewed` is waiting for reviewers,
    while one blocked by agreement is telling you the fix is wrong.
    """
    out: List[dict] = []
    for signature, rows in sorted(rows_by_signature.items()):
        utility = utility_for(rows, signature, policy)
        accepted = [r for r in rows if r["verdict"] == ACCEPTED]
        distinct = len({r["uid"] for r in accepted})
        blockers: List[str] = []
        if distinct < policy.min_samples:
            blockers.append("min_samples")
        if utility.g3_reviewed < policy.min_g3_reviewed:
            blockers.append("min_g3_reviewed")
        elif utility.g3_agreement < policy.min_g3_agreement:
            blockers.append("min_g3_agreement")
        if utility.mean_badness_delta <= 0:
            blockers.append("no_badness_gain")
        if utility.score < policy.min_utility:
            blockers.append("min_utility")
        out.append({
            "signature": signature, "would_promote": not blockers,
            "blockers": blockers, "distinct_strings": distinct,
            "utility": round(utility.score, 4),
            "accept_rate": round(utility.accept_rate, 4),
            "g3_reviewed": utility.g3_reviewed,
            "g3_agreement": round(utility.g3_agreement, 4),
            "mean_badness_delta": round(utility.mean_badness_delta, 2),
            "counter_examples": len(
                boundary_for(rows, signature).counter_examples),
        })
    return out


def group_by_signature(rows: Sequence[dict]) -> Dict[str, List[dict]]:
    """Bucket raw observation rows by signature (rows carry a list, since
    one candidate can exhibit several defect classes at once)."""
    grouped: Dict[str, List[dict]] = {}
    for row in rows:
        for signature in row.get("signatures", ()):
            grouped.setdefault(signature, []).append(row)
    return grouped
