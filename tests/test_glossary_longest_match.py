"""A nested glossary term must not be enforced inside a longer one.

A termbase legitimately contains a term inside a longer one: "Spirit" and
"Spirit Guardian", "Season Seed" and "Autumn Season Seed", "Festival" and
"Firework Festival". The gate checked every entry independently, so
"Find your Spirit Guardian" was held to BOTH "Spirit Guardian" -> 守护灵
AND "Spirit" -> 灵体 — and a correct translation was reported as a
terminology defect because 灵体 was absent.

The Nomori termbase nests 29 pairs this way, most of them the seasonal
families (Spring/Summer/Autumn/Winter × Spirit Guardian / Season Seed /
Season Trial), so the noise was systematic. On the zh-CN corpus it was 79
of 96 terminology findings — 82% false positives.

Some nested cases passed by luck, not by logic: "Goo Puddle" -> 黏液洼
satisfied the short "Goo" -> 黏液 check because 黏液 happens to be a
substring of 黏液洼. That is not something to rely on, and it fails the
moment a longer term's rendering does not contain the shorter one's.
"""
from __future__ import annotations

import pytest

from orbit8.gate_checks import GateConfig, applicable_terms, run_gate
from orbit8.schemas import BugType

# The real shape of the problem, from the Nomori termbase.
LOCKED = {
    "spirit": "灵体",
    "Guardian": "守护灵",
    "Spirit Guardian": "守护灵",
    "Autumn Spirit Guardian": "秋之守护灵",
    "Season Seed": "季节灵种",
    "Autumn Season Seed": "秋季灵种",
    "Festival": "庆典",
    "Firework Festival": "花火大会",
}


def _cfg():
    return GateConfig(source_lang="en", target_lang="zh-CN",
                      locked_terms=LOCKED)


def _types(source, target):
    return [f.bug_type for f in run_gate("k", source, target, _cfg())]


# ------------------------------------------------- which terms apply

def test_the_longest_term_claims_the_span():
    """"Spirit" and "Guardian" are inside "Spirit Guardian" and must not
    be required separately."""
    assert applicable_terms("Find your Spirit Guardian.", LOCKED) == {
        "Spirit Guardian": "守护灵"}


def test_a_three_word_term_beats_a_two_word_one():
    assert applicable_terms("The Autumn Spirit Guardian awaits.",
                            LOCKED) == {"Autumn Spirit Guardian": "秋之守护灵"}


def test_a_shorter_term_still_applies_where_it_stands_alone():
    """Suppression is per-SPAN, not per-string: the same word outside the
    longer term's span is still subject to its own rule."""
    got = applicable_terms("The spirit fled from the Spirit Guardian.",
                           LOCKED)
    assert got == {"Spirit Guardian": "守护灵", "spirit": "灵体"}


def test_an_unnested_term_is_unaffected():
    assert applicable_terms("A lone spirit appeared.", LOCKED) == {
        "spirit": "灵体"}


def test_no_terms_in_the_source_means_none_apply():
    assert applicable_terms("Press start to continue.", LOCKED) == {}


def test_resolution_does_not_depend_on_dict_order():
    """The glossary is merged from three tiers, so its insertion order
    carries no meaning and must not decide which term wins."""
    forward = applicable_terms("Find your Spirit Guardian.", LOCKED)
    reversed_map = dict(reversed(list(LOCKED.items())))
    assert applicable_terms("Find your Spirit Guardian.",
                            reversed_map) == forward


def test_repeated_occurrences_are_each_resolved():
    got = applicable_terms(
        "The Spirit Guardian and the Autumn Spirit Guardian.", LOCKED)
    assert got == {"Autumn Spirit Guardian": "秋之守护灵",
                   "Spirit Guardian": "守护灵"}


# ------------------------------------------------- through the gate

@pytest.mark.parametrize("source,target", [
    ("Find your Spirit Guardian.", "找到你的守护灵。"),
    ("The Autumn Spirit Guardian awaits.", "秋之守护灵在等着你。"),
    ("Plant the Autumn Season Seed.", "种下秋季灵种。"),
    ("The Firework Festival begins!", "花火大会开始了！"),
])
def test_a_correct_translation_of_a_nested_term_is_clean(source, target):
    """The regression: every one of these reported a terminology defect."""
    assert _types(source, target) == []


def test_a_real_defect_on_the_longer_term_is_still_caught():
    """Suppressing the short term must not suppress the long one."""
    assert _types("The Firework Festival begins!",
                  "烟花节开始了！") == [BugType.TERMINOLOGY]


def test_a_real_defect_on_a_standalone_short_term_is_still_caught():
    assert _types("A lone spirit appeared.",
                  "出现了一个东西。") == [BugType.TERMINOLOGY]


def test_the_shorter_term_is_enforced_outside_the_longer_span():
    assert _types("The spirit fled from the Spirit Guardian.",
                  "东西逃离了守护灵。") == [BugType.TERMINOLOGY]


def test_the_message_names_the_term_that_actually_applies():
    """Reporting 'spirit' for a Spirit Guardian string sent the client to
    fix the wrong word."""
    findings = run_gate("k", "The Firework Festival begins!",
                        "烟花节开始了！", _cfg())
    assert "Firework Festival" in findings[0].message
    assert "'Festival'" not in findings[0].message


def test_a_lucky_substring_rendering_is_not_relied_on():
    """"Goo Puddle" -> 黏液洼 used to pass only because 黏液 is a
    substring of 黏液洼. With a rendering that shares nothing, the old
    behaviour would have flagged it; the new one must not."""
    locked = {"Goo": "黏液", "Goo Puddle": "泥沼"}
    cfg = GateConfig(source_lang="en", target_lang="zh-CN",
                     locked_terms=locked)
    assert run_gate("k", "Collect a Goo Puddle.", "收集泥沼。", cfg) == []


# ------------------------------------------- what the model is shown

def _glossary():
    from orbit8.glossary import Glossary
    return Glossary.from_layers(
        "Nomori", "zh-CN",
        t1={"terms": {term: {"translation": rendering, "locked": True}
                      for term, rendering in LOCKED.items()}})


def test_the_prompt_shows_only_the_governing_terms():
    """The other half of the same bug. A sentence containing "Autumn
    Spirit Guardian" showed the model SIX renderings — four of which do
    not apply — inviting the translator to write 灵体 inside 秋之守护灵
    and the reviewer to file a defect when it is absent."""
    brief = _glossary().brief_for(
        ["Find your Autumn Spirit Guardian and plant the Autumn Season Seed."])
    assert {t.term for t in brief.terms} == {"Autumn Spirit Guardian",
                                             "Autumn Season Seed"}


def test_the_prompt_still_shows_a_standalone_shorter_term():
    brief = _glossary().brief_for(
        ["The spirit fled from the Spirit Guardian."])
    assert {t.term for t in brief.terms} == {"Spirit Guardian", "spirit"}


def test_a_batch_unions_each_strings_governing_terms():
    """Resolution is per string: a term suppressed in one line still
    applies in another line of the same batch."""
    brief = _glossary().brief_for(["A lone spirit appeared.",
                                   "Find your Spirit Guardian."])
    assert {t.term for t in brief.terms} == {"Spirit Guardian", "spirit"}


def test_the_prompt_and_the_gate_agree():
    """They must resolve identically, or the model is told to do one
    thing and graded on another."""
    text = "Find your Autumn Spirit Guardian."
    from_gate = set(applicable_terms(text, LOCKED))
    from_prompt = {t.term for t in _glossary().brief_for([text]).terms}
    assert from_gate == from_prompt


def test_longest_first_ordering_is_preserved():
    """Compound terms must still surface before their parts."""
    brief = _glossary().brief_for(
        ["The spirit fled from the Spirit Guardian."])
    assert [t.term for t in brief.terms] == ["Spirit Guardian", "spirit"]


# ------------------------------------------------------------- plurals

def test_a_plural_longer_term_still_claims_its_span():
    """"Spirit Guardian" does not match inside "Spirit Guardians" — the
    trailing \\b fails against the plural s — so the longer term claimed
    nothing and the nested "Spirit" won the span by default. That is the
    exact false positive this resolution exists to prevent, and it
    survived the first fix: three of them were still in the zh-CN report
    after the singular case was handled."""
    got = applicable_terms("Look. <h>Spirit Guardians</h> are powerful.",
                           LOCKED)
    assert got == {"Spirit Guardian": "守护灵"}


def test_several_plural_terms_in_one_line():
    got = applicable_terms(
        "<h>Season Seeds</h> and <h>Spirit Guardians</h>.", LOCKED)
    assert got == {"Season Seed": "季节灵种",
                   "Spirit Guardian": "守护灵"}


def test_a_standalone_short_term_survives_a_plural_long_one():
    got = applicable_terms("The spirit fled from the Spirit Guardians.",
                           LOCKED)
    assert got == {"Spirit Guardian": "守护灵", "spirit": "灵体"}


def test_an_es_plural_is_recognised():
    locked = {"Torch": "火把", "Wall Torch": "壁炬"}
    assert applicable_terms("Light the Wall Torches.", locked) == {
        "Wall Torch": "壁炬"}


def test_a_plural_does_not_invent_a_match():
    """The suffix probe only decides COVERAGE for a term already in the
    glossary; it must not make an absent term applicable."""
    assert applicable_terms("The guard stood watch.",
                            {"Guardian": "守护灵"}) == {}


def test_an_interior_word_may_be_inflected():
    """The glossary holds "Firework Festival"; the script writes
    "Fireworks Festival". The inflection is on the FIRST word, so a
    trailing-suffix probe could not see it and the nested "Festival"
    claimed the span — six such rows survived two earlier point-fixes for
    trailing plurals."""
    locked = {"Festival": "庆典", "Firework Festival": "花火大会"}
    assert applicable_terms("We arrived at the Fireworks Festival!",
                            locked) == {"Firework Festival": "花火大会"}


def test_the_bare_short_term_still_applies_on_its_own():
    """Suppression must not swallow the short term where it is the only
    thing present."""
    locked = {"Festival": "庆典", "Firework Festival": "花火大会"}
    assert applicable_terms("A festival?", locked) == {"Festival": "庆典"}


def test_inflection_matching_does_not_invent_terms():
    """The probe decides COVERAGE only; a term absent from the source
    must still match nothing."""
    assert applicable_terms("The guard stood watch.",
                            {"Guardian": "守护灵"}) == {}
    assert applicable_terms("Festive mood tonight.",
                            {"Festival": "庆典"}) == {}
