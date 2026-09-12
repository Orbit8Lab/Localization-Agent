"""A third casing mode, for terms the client has ruled on partially.

The glossary had two modes and neither fits the commonest real case:

  context — any casing satisfies the term. Correct when capitalization
            genuinely depends on position, wrong when the client has
            banned a specific form.
  exact   — one casing only. Correct for proper names, wrong for a word
            that is Title Case as a system name and lowercase as a
            common noun.

project002's BOSS is the second kind, and measuring both modes showed
the gap. Locked to "Boss":

  case=context  ->  "BOSS" accepted. 8 casing defects the post-editor
                    rejected went unflagged.
  case=exact    ->  "BOSS" flagged (good), but lowercase "boss" ALSO
                    flagged -- and the post-editor ACCEPTED "killed the
                    boss" and "a boss fight", which are CAP-02/13
                    common-noun usage.

So `exact` bought 8 true positives at the cost of 2 false positives on
text a professional signed off. `sanctioned` is the mode that wants
neither: reject the form the client banned (all-caps screaming), accept
any casing a human actually wrote.
"""
from __future__ import annotations

from orbit8.gate_checks import locked_in_target


def _ok(target: str, case: str = "sanctioned", locked: str = "Boss") -> bool:
    return locked_in_target(locked, target, "en", case=case)


# ------------------------------------------------- what must be rejected

def test_screaming_form_is_rejected():
    """The defect the post-editor fixed 13 times."""
    assert not _ok("Destroying the BOSS can reduce Infection")
    assert not _ok("if all BOSSES are killed you lose")


# ------------------------------------------------- what must be accepted

def test_title_case_is_accepted():
    assert _ok("Destroying the Boss can reduce Infection")


def test_lower_case_is_accepted_as_a_common_noun():
    """CAP-02/13: the same word lowercases when it is not a system name,
    and the post-editor accepted exactly these two strings."""
    assert _ok("The Knight killed the boss, he must be an Apostle")
    assert _ok("interfere when you are focusing on a boss fight")


def test_the_screaming_plural_is_rejected():
    """"BOSSES" is as common a defect as "BOSS", so the check reads
    inflected spans too, not only exact ones.

    Note what is NOT asserted here: that "Bosses" SATISFIES the term.
    It does not, and that is pre-existing behaviour unrelated to
    casing — `locked_in_target` only tolerates inflection for languages
    in INFLECTED_TARGETS, and English is not one of them, so an English
    plural is judged absent in every mode. A first draft of this test
    asserted the plural was accepted; it was asserting a capability the
    system has never had. Left recorded because the distinction matters:
    this mode governs CASING, and it must not be credited with fixing
    English morphology."""
    assert not _ok("all BOSSES are killed")
    from orbit8.gate_checks import _screams
    assert _screams("Boss", "all BOSSES are killed")
    assert not _screams("Boss", "all Bosses are killed")
    assert not _screams("Boss", "all bosses are killed")


# ---------------------------------------------- the other modes are intact

def test_exact_still_demands_one_casing():
    assert locked_in_target("Boss", "the Boss fell", "en", case="exact")
    assert not locked_in_target("Boss", "the boss fell", "en", case="exact")
    assert not locked_in_target("Boss", "the BOSS fell", "en", case="exact")


def test_context_still_accepts_anything():
    for t in ("the Boss", "the boss", "the BOSS"):
        assert locked_in_target("Boss", t, "en", case="context"), t


def test_an_absent_term_is_still_absent_in_every_mode():
    for mode in ("context", "exact", "sanctioned"):
        assert not locked_in_target("Boss", "nothing relevant here", "en",
                                    case=mode), mode


# ------------------------------------------------------------- edge cases

def test_an_all_caps_locked_term_is_not_its_own_violation():
    """A term whose MANDATED form is all-caps (an initialism like HP)
    must not be flagged for being all-caps."""
    assert locked_in_target("HP", "restores 5 HP", "en", case="sanctioned")


def test_a_single_letter_term_is_not_treated_as_screaming():
    """'A' is trivially all-caps; the rule must not fire on it."""
    assert locked_in_target("A", "press A to continue", "en",
                            case="sanctioned")


def test_mixed_case_multiword_term():
    """'Plague Node' screaming is 'PLAGUE NODE'; 'plague node' and
    'Plague Node' are both human-written forms."""
    for good in ("the Plague Node", "a plague node here"):
        assert _ok(good, locked="Plague Node"), good
    assert not _ok("the PLAGUE NODE", locked="Plague Node")


# ------------------------------------------------- invalid values are loud

def test_an_unknown_case_mode_raises_instead_of_degrading():
    """A typo used to fall through to `context`, which enforces nothing.

    That is the same failure shape as the bug that motivated this work:
    a check that quietly does not run is indistinguishable from a check
    that passes. `sanctionned` must be an error, not a silent downgrade.
    """
    import pytest
    with pytest.raises(ValueError, match="unknown glossary case mode"):
        locked_in_target("Boss", "the BOSS fell", "en", case="sanctionned")


def test_every_declared_mode_is_actually_handled():
    """Guards against adding a name to TERM_CASE_MODES without wiring
    it up — which would accept the value and then ignore it."""
    from orbit8.gate_checks import TERM_CASE_MODES
    for mode in TERM_CASE_MODES:
        # must not raise, and must give a definite answer
        assert isinstance(
            locked_in_target("Boss", "the Boss fell", "en", case=mode), bool)
