"""A finding must name the CAUSE, not a consequence of it.

Two real cases from the Nomori zh-CN client report.

1. **Misaligned rows reported as terminology.** Three rows in the client's
   own export pair an English line with the Chinese translation of a
   DIFFERENT line. The gate checked the pair component by component —
   is the locked term rendered, is the markup present — and never asked
   whether the target was a translation of the source at all, so it
   reported "Locked term 'Yuki' must render as '小雪'". Literally true,
   and actively harmful: a client following that advice edits a
   translation that was never Yuki's line. Worse, a Tier-1 flag REMOVES
   the string from the cascade (`t2_input == accepted - t1_flagged`), so
   the semantic reviewer that would have caught it never ran. Every one
   of the five findings on those rows was tier 1.

2. **Untranslated rows reported twice.** `PLACEHOLDER KODAMA` →
   `PLACEHOLDER KODAMA` produced both "untranslated" and "Locked term
   'Kodama' must render as '木灵'" — two HIGH rows for one defect, and the
   terminology one is unfixable on its own, since you cannot render the
   term without translating the string.
"""
from __future__ import annotations

import pytest

from orbit8.gate_checks import GateConfig, run_gate, speaker_mismatch
from orbit8.schemas import BugType

# The speaker map comes from LOCKED GLOSSARY TERMS — character names are
# already tier-1 entries, so the check needs no new configuration.
LOCKED = {"Yuki": "小雪", "Kiko": "希子", "Kodama": "木灵", "Goo": "黏液"}


def _cfg(**kw):
    return GateConfig(source_lang="en", target_lang="zh-CN",
                      locked_terms=LOCKED, **kw)


def _types(source, target):
    return [f.bug_type for f in run_gate("k", source, target, _cfg())]


# ------------------------------------------------------- misalignment

MISALIGNED = [
    # the three real rows, verified against the client's export
    ("Yuki: Honestly? I have no idea. I'm kind of figuring things out.",
     "希子:我不是灵体。我叫希子。你呢？"),
    ("Yuki: After you!",
     "木灵:我是<h>木灵</h>！很久没见过我的<h>守护灵</h>了，我好害怕！"),
    ("Yuki: Ooohh... I remember this purple <h>Goo</h> being bouncy!",
     "木灵:我能跟你走吗？应该能塞进你的背包。"),
]


@pytest.mark.parametrize("source,target", MISALIGNED)
def test_a_misaligned_row_is_not_a_terminology_defect(source, target):
    """The regression. These reported terminology, and one also reported
    a placeholder mismatch."""
    types = _types(source, target)
    assert BugType.TERMINOLOGY not in types
    assert BugType.PLACEHOLDER not in types


@pytest.mark.parametrize("source,target", MISALIGNED)
def test_a_misaligned_row_reports_exactly_one_cause(source, target):
    """Five findings across three rows became three. The component
    failures are consequences and must not be shipped as separate bugs."""
    types = _types(source, target)
    assert types == [BugType.MISTRANSLATION]


def test_the_message_says_it_is_not_a_terminology_fix(source=MISALIGNED[0]):
    """The client acts on the message text, so it has to say what the
    problem actually is and what to do about it."""
    findings = run_gate("k", *source, _cfg())
    message = findings[0].message
    assert "misalignment" in message.lower()
    assert "Yuki" in message and "希子" in message   # both speakers named
    assert "not a terminology fix" in message
    assert "line mapping" in message


def test_a_correctly_paired_row_stays_clean():
    assert _types("Yuki: And there's a lot of <h>Goo</h> in Nomori.",
                  "小雪:而且野森有的是<h>黏液</h>。") == []


def test_a_real_terminology_defect_is_still_terminology():
    """The check must not swallow the defect class it sits in front of."""
    assert _types("Yuki: Look at the <h>Goo</h>!",
                  "小雪:看那<h>东西</h>！") == [BugType.TERMINOLOGY]


# -------------------------------------------- when it must stay silent

def test_a_line_with_no_speaker_prefix_is_not_judged():
    assert speaker_mismatch("Start Game", "開始", LOCKED) is None


def test_an_unmapped_speaker_is_not_judged():
    """A name absent from the glossary carries no expectation."""
    assert speaker_mismatch("Narrator: It begins.", "旁白:开始了。",
                            LOCKED) is None


def test_a_target_prefix_that_is_not_a_character_is_not_judged():
    """Only a DIFFERENT KNOWN character is evidence. An arbitrary prefix
    could be anything — a label, a timestamp, a mistranslation."""
    assert speaker_mismatch("Yuki: Hello!", "警告:你好！", LOCKED) is None


def test_a_correct_speaker_is_not_judged():
    assert speaker_mismatch("Yuki: Hello!", "小雪:你好！", LOCKED) is None


def test_a_fullwidth_colon_target_is_still_read():
    """Chinese typography uses '：'; the check must see through it or it
    would miss every correctly punctuated line."""
    assert speaker_mismatch("Yuki: Hello!", "希子：你好！", LOCKED)


def test_no_locked_terms_means_no_speaker_check():
    """Without a glossary there is no mapping, so nothing to compare."""
    assert speaker_mismatch("Yuki: Hello!", "希子:你好！", {}) is None


# ------------------------------------------------------- untranslated

def test_an_untranslated_string_does_not_also_report_terminology():
    """The regression: bug 76 and bug 77 were one defect."""
    types = _types("PLACEHOLDER KODAMA", "PLACEHOLDER KODAMA")
    assert types == [BugType.UNTRANSLATED]


def test_untranslated_wins_over_several_terms():
    """A dev placeholder can name many locked terms; none of them is the
    fixable problem."""
    types = _types("KODAMA AND GOO LINES", "KODAMA AND GOO LINES")
    assert types == [BugType.UNTRANSLATED]


def test_a_translated_string_still_gets_its_terminology_finding():
    """Suppression is scoped to untranslated rows only."""
    assert _types("The <h>Goo</h> is here.",
                  "这里有<h>东西</h>。") == [BugType.TERMINOLOGY]


def test_an_empty_target_is_still_a_single_omission():
    types = _types("Yuki: Hello!", "")
    assert types == [BugType.OMISSION]
