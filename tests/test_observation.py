"""The observation layer (PLAN §3) — Phase 1 of the self-evolution plan.

Two properties carry the whole design and are asserted hardest here:

**The signature must be string-independent** (PLAN §4.2). `Finding.identity()`
includes the segment uid, so anything derived from it accumulates a count of
one, forever. That is the difference between a cache and a skill, and it is
invisible at runtime — the registry simply stays empty.

**The log must never be able to break a run.** It observes the ratchet, the
single most important decision in the pipeline. A logging bug that fails a
translate batch would make this "risk-free" phase the most expensive one.
"""
from __future__ import annotations

import sqlite3

import pytest

from orbit8.observation import (ACCEPTED, EVIDENCE_MAX, FIRST, G3_ACCEPTED,
                                G3_EDITED, G3_PENDING, G3_REJECTED, REJECTED,
                                Observation, ObservationLog,
                                normalize_evidence, signature, signatures)
from orbit8.schemas import BugType, Finding, Severity


def _finding(bug_type=BugType.TERMINOLOGY, evidence="魂石", key="uid-1",
             message="Locked term '魂石' must render as 'Soulstone'.",
             severity=Severity.HIGH) -> Finding:
    return Finding(key=key, bug_type=bug_type, severity=severity,
                   message=message, evidence=evidence)


# ------------------------------------------------- the signature (PLAN §4.2)

def test_the_same_defect_on_two_strings_shares_one_signature():
    """The property the whole plan rests on. Two segments violating the
    same locked term are ONE defect class; if they signed differently
    nothing could ever accumulate."""
    a = _finding(key="uid-aaa")
    b = _finding(key="uid-bbb")
    assert signature(a) == signature(b)


def test_the_segment_uid_never_appears_in_the_signature():
    """Finding.identity() is (key, bug_type, evidence) and key IS the uid.
    Deriving the signature from it would key a 'skill' to one string."""
    assert "uid-aaa" not in signature(_finding(key="uid-aaa"))


def test_different_terms_are_different_signatures():
    """Coarse enough to accumulate, still specific enough to act on: two
    different locked terms are not interchangeable defects."""
    assert signature(_finding(evidence="魂石")) != \
        signature(_finding(evidence="灵力"))


def test_different_bug_types_are_different_signatures():
    assert signature(_finding(bug_type=BugType.TERMINOLOGY)) != \
        signature(_finding(bug_type=BugType.PLACEHOLDER))


def test_a_style_rule_id_sharpens_the_signature():
    """gate_checks emits style findings as '[RULE-ID] message' — the one
    place a real rule id survives into a Finding, so it is the most
    specific component available and must not be discarded."""
    sig = signature(_finding(bug_type=BugType.PUNCTUATION, evidence="!!",
                             message="[CAP-TITLE] Title case required."))
    assert "CAP-TITLE" in sig


def test_two_rules_flagging_the_same_evidence_stay_distinct():
    left = _finding(bug_type=BugType.PUNCTUATION, evidence="!!",
                    message="[CAP-TITLE] Title case required.")
    right = _finding(bug_type=BugType.PUNCTUATION, evidence="!!",
                    message="[PUNCT-BANG] No double exclamation.")
    assert signature(left) != signature(right)


def test_prose_evidence_is_dropped_rather_than_making_a_unique_key():
    """Long evidence is a quotation of ONE string. Keeping it would make
    the signature a uid by another name — the §4.2 failure mode with extra
    steps — so the signature falls back to the defect class alone."""
    prose = "x" * (EVIDENCE_MAX + 1)
    sig = signature(_finding(bug_type=BugType.MISTRANSLATION, evidence=prose))
    assert sig == BugType.MISTRANSLATION.value


def test_placeholder_indices_do_not_split_one_defect_class():
    """{0} and {1} are the same defect wearing different numbers."""
    assert normalize_evidence("{0}") == normalize_evidence("{1}")


def test_evidence_matching_is_case_and_whitespace_insensitive():
    assert normalize_evidence("  Soul   Stone ") == normalize_evidence(
        "soul stone")


def test_signatures_are_deduped_and_order_stable():
    findings = [_finding(evidence="魂石"), _finding(evidence="灵力"),
                _finding(evidence="魂石")]
    assert signatures(findings) == [signature(findings[0]),
                                    signature(findings[1])]


def test_a_clean_candidate_has_no_signatures():
    assert signatures([]) == []


# --------------------------------------------------------------- the log

@pytest.fixture
def log(tmp_path):
    return ObservationLog(tmp_path / "obs.db")


def _obs(**kw) -> Observation:
    base = dict(job_id="job-1", locale="en", attempt=1, uid="uid-1",
                signatures=["terminology/魂石"], strategy="translate",
                iteration=0, badness_before=None, badness_after=100,
                verdict=FIRST, target="Soul Stone")
    base.update(kw)
    return Observation(**base)


def test_an_observation_round_trips(log):
    log.record(_obs())
    row, = log.all_rows()
    assert row["uid"] == "uid-1"
    assert row["signatures"] == ["terminology/魂石"]
    assert row["badness_after"] == 100
    assert row["verdict"] == FIRST


def test_rejections_are_recorded_not_discarded(log):
    """PLAN §4.3/§6.3: a rolled-back repair is the negative example that
    bounds where a skill applies. Logging only winners is how a skill
    generalizes past the cases it actually works on."""
    log.record(_obs(verdict=REJECTED, strategy="repair", badness_before=10,
                    badness_after=100))
    row, = log.all_rows()
    assert row["verdict"] == REJECTED
    assert row["badness_before"] == 10 and row["badness_after"] == 100


def test_the_log_is_append_only(log):
    """Two observations of the same string are two rows: the history IS
    the data, so a later attempt must not overwrite an earlier one."""
    log.record(_obs(badness_after=100))
    log.record(_obs(badness_after=0, verdict=ACCEPTED, badness_before=100,
                    strategy="repair", iteration=1))
    rows = log.all_rows()
    assert len(rows) == 2
    assert [r["badness_after"] for r in rows] == [100, 0]


def test_the_attempt_is_stamped(log):
    """PLAN §5.7: an observation that cannot be tied to the attempt that
    produced it is not auditable."""
    log.record(_obs(attempt=3))
    assert log.all_rows()[0]["attempt"] == 3


def test_there_is_no_store_revision_column(log):
    """PLAN §5.7 also asks for a store revision, and Phase 1 deliberately
    omits it: nothing here reads the store, so the column could only ever
    hold 0 — advertising an audit coordinate it does not have. Phase 4 adds
    it alongside the counter that gives it a value. Pinned so it comes back
    on purpose rather than as a silently-zero field."""
    log.record(_obs())
    assert "revision" not in log.all_rows()[0]


def test_rows_are_queryable_by_signature(log):
    log.record(_obs(uid="uid-1", signatures=["terminology/魂石"]))
    log.record(_obs(uid="uid-2", signatures=["terminology/魂石"]))
    log.record(_obs(uid="uid-3", signatures=["placeholder/{#}"]))
    assert {r["uid"] for r in log.for_signature("terminology/魂石")} == \
        {"uid-1", "uid-2"}


def test_the_tally_separates_recurrence_from_a_flapping_loop(log):
    """PLAN §4.1: 5 observations on ONE string is a repair loop retried 5
    times; 5 on five strings is a defect class. Opposite conclusions, so
    the tally must not collapse them into one count."""
    for _ in range(5):
        log.record(_obs(uid="same-string", signatures=["terminology/魂石"]))
    for n in range(5):
        log.record(_obs(uid=f"uid-{n}", signatures=["placeholder/{#}"]))

    tally = {r["signature"]: r for r in log.signature_tally()}
    assert tally["terminology/魂石"]["n"] == 5
    assert tally["terminology/魂石"]["distinct_strings"] == 1
    assert tally["placeholder/{#}"]["distinct_strings"] == 5


def test_the_tally_counts_accepts_and_rejects_separately(log):
    log.record(_obs(verdict=ACCEPTED))
    log.record(_obs(verdict=REJECTED))
    log.record(_obs(verdict=REJECTED))
    row, = log.signature_tally()
    assert row["accepted"] == 1 and row["rejected"] == 2


# ------------------------------------------------ the G3 verdict (§5.6)

def test_a_row_starts_pending_not_accepted(log):
    """An unreviewed string must never read as an approving human. This is
    the difference between a held-out signal and a fabricated one."""
    log.record(_obs())
    assert log.all_rows()[0]["g3_verdict"] == G3_PENDING


def test_a_g3_verdict_attaches_to_every_observation_of_the_string(log):
    """The human ruled on the STRING, so every candidate that led to it
    inherits the ruling — including the rejected ones, which is what makes
    a rejection interpretable later."""
    log.record(_obs(verdict=FIRST))
    log.record(_obs(verdict=REJECTED, strategy="repair"))
    log.record(_obs(uid="other-uid"))

    assert log.record_g3("uid-1", "en", G3_EDITED, "Soulstone") == 2
    assert {r["g3_verdict"] for r in log.for_uid("uid-1")} == {G3_EDITED}
    assert log.for_uid("other-uid")[0]["g3_verdict"] == G3_PENDING


def test_an_edit_keeps_what_the_human_actually_wrote(log):
    log.record(_obs())
    log.record_g3("uid-1", "en", G3_EDITED, "Soulstone")
    assert log.all_rows()[0]["g3_text"] == "Soulstone"


# ------------------------------------------ the multi-locale uid collision

def test_a_ruling_in_one_locale_does_not_touch_another(log):
    """`uid` hashes the SOURCE string, so it is identical across every
    target locale, and this log is job-scoped — one file for all locales.
    Keying a verdict on uid alone stamped one reviewer's ruling onto every
    locale, which fabricated human-agreement data in the one place the
    design says it must be real (PLAN §5.6)."""
    log.record(_obs(uid="u1", locale="ko", target="시작"))
    log.record(_obs(uid="u1", locale="ja", target="開始"))

    assert log.record_g3("u1", "ja", G3_EDITED, "ゲーム開始") == 1

    by_locale = {r["locale"]: r for r in log.all_rows()}
    assert by_locale["ja"]["g3_verdict"] == G3_EDITED
    assert by_locale["ko"]["g3_verdict"] == G3_PENDING


def test_one_locales_translation_never_lands_on_another(log):
    """The sharper half of the same bug: the ruling carried the reviewer's
    TEXT, so Korean rows ended up holding Japanese strings."""
    log.record(_obs(uid="u1", locale="ko", target="시작"))
    log.record(_obs(uid="u1", locale="ja", target="開始"))
    log.record_g3("u1", "ja", G3_EDITED, "ゲーム開始")

    korean = next(r for r in log.all_rows() if r["locale"] == "ko")
    assert korean["g3_text"] is None


def test_for_uid_can_be_scoped_to_one_locale(log):
    log.record(_obs(uid="u1", locale="ko"))
    log.record(_obs(uid="u1", locale="ja"))
    assert len(log.for_uid("u1")) == 2               # every locale
    assert len(log.for_uid("u1", "ko")) == 1         # just this one


def test_each_locale_can_be_ruled_on_independently(log):
    """Two reviewers, two languages, two different conclusions about the
    same source string — which is the normal case, not an edge case."""
    log.record(_obs(uid="u1", locale="ko", target="시작"))
    log.record(_obs(uid="u1", locale="ja", target="開始"))
    log.record_g3("u1", "ko", G3_ACCEPTED)
    log.record_g3("u1", "ja", G3_REJECTED)

    by_locale = {r["locale"]: r["g3_verdict"] for r in log.all_rows()}
    assert by_locale == {"ko": G3_ACCEPTED, "ja": G3_REJECTED}


def test_an_unknown_g3_verdict_is_refused(log):
    """Vocabulary drift here silently corrupts every agreement rate
    computed downstream."""
    log.record(_obs())
    with pytest.raises(ValueError):
        log.record_g3("uid-1", "en", "looks-fine")


def test_the_tally_reports_agreement_and_overturns(log):
    """PLAN §5.6: badness improved AND G3 overturned is the row that says
    a gate check is miscalibrated, so it has to be countable."""
    log.record(_obs(uid="a"))
    log.record(_obs(uid="b"))
    log.record(_obs(uid="c"))
    log.record_g3("a", "en", G3_ACCEPTED)
    log.record_g3("b", "en", G3_EDITED, "better")
    log.record_g3("c", "en", G3_REJECTED)

    row, = log.signature_tally()
    assert row["g3_accepted"] == 1
    assert row["g3_overturned"] == 2      # edited + rejected


def test_reopening_the_log_sees_prior_rows(tmp_path):
    """The log outlives one stage-run: cross-attempt comparison is the
    reason it is job-scoped rather than attempt-scoped."""
    path = tmp_path / "obs.db"
    ObservationLog(path).record(_obs())
    assert len(ObservationLog(path).all_rows()) == 1


def test_a_new_column_would_not_misalign_existing_reads(log):
    """Guards the bug this schema already invited once: 18 positional
    columns, where an inserted field shifts every later one and the
    corruption looks like bad data rather than a schema change."""
    log.record(_obs())
    row, = log.all_rows()
    assert isinstance(row["tokens"], float)
    assert row["target"] == "Soul Stone"
    assert row["strategy"] == "translate"
