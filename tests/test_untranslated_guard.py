"""`zh-CN` was not in ALLOWED_SCRIPTS, which disabled the guard.

The untranslated check exists to catch a target echoed verbatim from
the source. It already guards against dev-English placeholders ("Error",
"Text Block") by requiring the SOURCE to actually contain its own
script. But the lookup was by exact locale tag: `ALLOWED_SCRIPTS` has
`zh` and `zh-tw`, and `po_scan` passes **`zh-CN`**. The lookup missed,
`src_scripts_expected` came back empty, and `source_has_own_script`
defaulted to True — so the guard never applied for the very language
pair this project runs.

Measured on 1,233 rows: 6 false positives on strings whose source and
target are legitimately identical because there is nothing to
translate — `(0/3)`, `(0/35)`, `Lv.1`. The post-editor accepted all of
them.

Regional tags are the norm, not the exception (`pt-BR`, `zh-Hans`,
`en-GB`), so the fix normalises the tag rather than adding `zh-CN` to
the table.
"""
from __future__ import annotations

from orbit8.gate_checks import GateConfig, run_gate
from orbit8.schemas import BugType


def _untranslated(source: str, target: str, source_lang: str = "zh-CN"):
    return [f for f in run_gate(key="k", source=source, target=target,
                                cfg=GateConfig(source_lang=source_lang,
                                               target_lang="en"))
            if f.bug_type is BugType.UNTRANSLATED]


def test_a_numeric_counter_is_not_untranslated():
    """The exact false positive: a fraction has nothing to translate."""
    assert _untranslated("(0/3)", "(0/3)") == []
    assert _untranslated("(0/35)", "(0/35)") == []


def test_a_level_prefix_is_not_untranslated():
    assert _untranslated("Lv.1", "Lv.1") == []


def test_real_untranslated_chinese_is_still_caught():
    """The check must keep working — this is its whole purpose."""
    assert _untranslated("确定取消操作", "确定取消操作") != []


def test_the_regional_tag_behaves_like_the_base_tag():
    """zh-CN and zh must agree; a locale suffix is not a new language."""
    for src in ("(0/3)", "Lv.1"):
        assert (_untranslated(src, src, "zh-CN")
                == _untranslated(src, src, "zh")), src
    assert (bool(_untranslated("确定取消操作", "确定取消操作", "zh-CN"))
            is bool(_untranslated("确定取消操作", "确定取消操作", "zh")))


def test_other_regional_tags_normalise_too():
    """pt-BR, zh-Hans, en-GB are all normal in localization."""
    assert _untranslated("(0/3)", "(0/3)", "zh-Hans") == []
    assert _untranslated("确定取消操作", "确定取消操作", "zh-Hans") != []


def test_a_genuinely_translated_string_is_never_flagged():
    assert _untranslated("确定取消操作", "Confirm cancellation") == []


# ------------------------------------------- the same bug, worse effect

def _leakage(source: str, target: str, source_lang: str = "zh-CN"):
    from orbit8.schemas import BugType as _BT
    return [f for f in run_gate(key="k", source=source, target=target,
                                cfg=GateConfig(source_lang=source_lang,
                                               target_lang="en"))
            if f.bug_type is _BT.LEAKAGE]


def test_han_leaking_into_english_is_detected_for_regional_tags():
    """The same exact-match lookup disabled the script-LEAKAGE check
    entirely: with source_lang="zh-CN", src_scripts was empty, so
    `src - tgt` was empty and the check never ran. Untranslated Chinese
    sitting in a shipped English target — a HIGH-severity defect — went
    unreported for the project's real locale."""
    assert _leakage("净化瘟疫点", "Purify the 瘟疫点 now", "zh-CN") != []
    assert _leakage("净化瘟疫点", "Purify the 瘟疫点 now", "zh") != []


def test_a_clean_english_target_raises_no_leakage():
    assert _leakage("净化瘟疫点", "Purify the Plague Node", "zh-CN") == []
