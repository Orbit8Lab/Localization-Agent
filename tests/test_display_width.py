"""Display width as the UI-overflow metric.

Overflow is geometry, so it is measured in rendered columns. The two
obvious alternatives both mislead on zh→en, and the word one inverts the
answer on exactly the strings that break:

    弓 → "Longbow"        chars 7.0x   words 1.0x   width 3.5x
    攻击力 → "Attack Power"  chars 4.0x   words 0.7x   width 2.0x

Word counting stays correct for QUOTING (analysis.count_words) — this is
a separate metric for a separate question.
"""
from __future__ import annotations

from orbit8.gate_checks import (GateConfig, display_width, run_gate,
                                width_ratio)
from orbit8.schemas import BugType, Severity


# ------------------------------------------------------------ the metric

def test_cjk_glyphs_cost_two_columns():
    assert display_width("攻击力") == 6
    assert display_width("确定") == 4


def test_latin_costs_one_column_each():
    assert display_width("Attack Power") == 12
    assert display_width("Confirm") == 7


def test_the_ratio_reflects_what_the_widget_sees():
    """'攻击力' is 6 columns, 'Attack Power' is 12 — genuinely 2x."""
    assert width_ratio("攻击力", "Attack Power") == 2.0


def test_word_counting_would_have_missed_this():
    """The regression this metric exists to prevent: one CJK glyph to one
    English word is 'no growth' by word count, 3.5x on screen."""
    assert width_ratio("弓", "Longbow") == 3.5


def test_fullwidth_punctuation_counts_as_wide():
    assert display_width("（）") == 4
    assert display_width("()") == 2


def test_combining_marks_do_not_advance_the_cursor():
    """A combining accent renders inside its base glyph."""
    assert display_width("é") == 1        # e + combining acute
    assert display_width("café") == 4


def test_empty_string_has_no_width():
    assert display_width("") == 0


def test_ratio_is_zero_when_the_source_has_no_width():
    """A pure-placeholder source cannot define an expansion budget."""
    assert width_ratio("", "anything") == 0.0


# ------------------------------------------------- the gate: width budget

CFG = GateConfig(source_lang="zh", target_lang="en")


def _length_findings(source, target, **kw):
    return [f for f in run_gate("k", source, target, CFG, **kw)
            if f.bug_type is BugType.LENGTH]


def test_ui_overflow_is_flagged():
    """8 source columns → 29: 3.6x, past the 2.4x UI budget."""
    found = _length_findings("确定取消", "Confirm and Cancel Everything",
                             string_type="UI")
    assert len(found) == 1
    assert found[0].severity is Severity.MEDIUM
    assert "overflow risk" in found[0].message


def test_a_normal_expansion_passes():
    """Median zh→en is ~1.8x — the budget must not fire on the norm."""
    assert _length_findings("攻击力提升", "Attack Power Up",
                            string_type="UI") == []


def test_the_finding_reports_columns_not_characters():
    found = _length_findings("确定取消", "Confirm and Cancel Everything",
                             string_type="UI")
    assert "columns" in found[0].message
    assert "8" in found[0].message          # source: 4 glyphs = 8 columns


def test_dialogue_has_no_budget_and_never_fires():
    """Prose reflows; a width finding there is pure noise."""
    long_en = "This is a very considerably longer rendering indeed, truly."
    assert _length_findings("对话文本内容", long_en,
                            string_type="Dialogue") == []


def test_an_unknown_string_type_is_exempt():
    assert _length_findings("确定取消", "Confirm and Cancel Everything",
                            string_type="Whatever") == []


def test_no_string_type_means_silence_not_a_guess():
    assert _length_findings("确定取消", "Confirm and Cancel Everything") == []


def test_short_labels_are_exempt_from_the_ratio():
    """A 1-glyph source hits 3.5x on any ordinary word. That is a fact
    about short labels, not a defect — those need a real max_len."""
    assert _length_findings("弓", "Longbow", string_type="UI") == []


def test_the_budget_is_per_type_configurable_data():
    """Same string, same width — only the budget differs. Thresholds are a
    per-project precision choice, so they must live in config, not code."""
    source, target = "攻击力量", "Attack Power Rating X"      # 8 -> 20 = 2.5x
    loose = GateConfig(source_lang="zh", target_lang="en",
                       width_budget={"UI": 3.0})
    tight = GateConfig(source_lang="zh", target_lang="en",
                       width_budget={"UI": 2.0})

    def length(cfg):
        return [f for f in run_gate("k", source, target, cfg,
                                    string_type="UI")
                if f.bug_type is BugType.LENGTH]

    assert length(loose) == []
    assert len(length(tight)) == 1


def test_default_budgets_pass_a_typical_string():
    """Budgets sit at the p95 of shipped games (median there is 1.65x), so
    ordinary expansion must not fire — a scan that cries wolf gets
    switched off."""
    assert _length_findings("攻击力量", "Attack Power Up",       # 1.9x
                            string_type="UI") == []


def test_budgets_are_anchored_below_the_projects_own_tail():
    """The anchor is external ON PURPOSE. The scanned corpus's own p95 is
    2.80x; fitting to that would define its own tail as normal. A 2.6x UI
    string passes a self-fitted budget and must NOT pass this one."""
    # a real in-corpus string at 2.62x — under its own 2.80x p95
    found = _length_findings("屏蔽昵称", "Hide All Player Names",
                             string_type="UI")
    assert found and found[0].bug_type is BugType.LENGTH


# --------------------------------------------------------- the hard limit

def test_hard_limit_is_measured_in_columns():
    """A budget of 10 columns is blown by 6 CJK glyphs (12 columns) even
    though a naive character count reads 6."""
    found = _length_findings("一二三四五六", "一二三四五六", max_len=10)
    assert found and found[0].severity is Severity.HIGH
    assert "12 > 10 columns" in found[0].message


def test_hard_limit_passes_when_it_fits():
    assert _length_findings("Short", "Short", max_len=10) == []


def test_hard_limit_and_budget_are_independent():
    """Both can fire; they answer different questions."""
    found = _length_findings("确定取消", "Confirm and Cancel Everything",
                             max_len=10, string_type="UI")
    assert {f.severity for f in found} == {Severity.HIGH, Severity.MEDIUM}


# --------------------------------------------- widget class from the path

def test_a_generic_ui_folder_does_not_outrank_a_specific_leaf():
    """UE exports nest everything under /Game/Program/UI/, so matching
    "ui" first labelled dialogue as UI and judged prose against a button's
    budget. Specific hints must win."""
    from orbit8.po_translate import _string_type
    assert _string_type("/Game/Program/UI/Wiki/WBP_Dialogue.Text") \
        == "Dialogue"
    assert _string_type("/Game/Program/UI/Subtitles/WBP_Sub.Text") \
        == "Dialogue"
    assert _string_type("/Game/Program/UI/Skill/WBP_Skill.Text") == "Skill"
    assert _string_type("/Game/Program/UI/WBP_Button.Text") == "UI"


def test_an_unclassifiable_path_is_exempt_rather_than_guessed():
    from orbit8.po_translate import _string_type
    assert _string_type("/Game/Whatever/Thing.Text") == "Others"
    assert _length_findings("确定取消", "Confirm and Cancel Everything",
                            string_type="Others") == []


def test_markup_and_placeholders_do_not_count_toward_width():
    """Tags are not rendered text; counting them would flag styled strings
    that fit perfectly."""
    assert _length_findings("<b>攻击力提升</b>", "<b>Attack Power Up</b>",
                            string_type="UI") == []
