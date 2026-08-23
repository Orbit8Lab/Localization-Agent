"""Consistency checks on the canonical document standards."""
from orbit8 import standards as st


ALL_FORMS = ("MTPE_FORM_HEADERS", "LQA_PE_FORM_HEADERS",
             "GLOSSARY_PE_FORM_HEADERS")


def test_pe_columns_present_in_all_forms():
    for name in ALL_FORMS:
        headers = getattr(st, name)
        for col in st.PE_FILLED_COLUMNS:
            assert col in headers, (name, col)


def test_headers_unique():
    for name in ALL_FORMS:
        headers = getattr(st, name)
        assert len(set(headers)) == len(headers), name


def test_test_kit_headers_unique():
    assert (len(set(st.TEST_KIT_FORM_HEADERS))
            == len(st.TEST_KIT_FORM_HEADERS))


def test_test_kit_owner_prefixes_partition_the_form():
    """Every column belongs to exactly one owner, and the prefix says which:
    TEST_* is the tester's, AI_* is a prediction, bare is deterministic."""
    for col in st.TEST_KIT_FORM_HEADERS:
        assert not (col.startswith("TEST_") and col.startswith("AI_"))
        if col.startswith("TEST_"):
            assert col in st.TEST_FILLED_COLUMNS, col
        else:
            assert col not in st.TEST_FILLED_COLUMNS, col
    for col in st.TEST_FILLED_COLUMNS:
        assert col in st.TEST_KIT_FORM_HEADERS, col


def test_test_kit_error_types_match_bug_report_vocabulary():
    """Predictions must be countable against confirmed bugs — so the kit's
    vocabulary is the bug report's, minus the client-facing prefix."""
    from orbit8.bug_report import CATEGORY_LABELS
    assert set(st.TEST_ERROR_TYPE_BY_TOKEN) == set(CATEGORY_LABELS)
    for token, label in st.TEST_ERROR_TYPE_BY_TOKEN.items():
        expected = CATEGORY_LABELS[token].replace("Localization - ", "")
        assert label == expected, (token, label, expected)
        assert label in st.TEST_ERROR_TYPES


def test_blank_decision_is_not_a_test_result():
    """'No Issue' is affirmative; blank means untested. Coverage math
    depends on those never collapsing into one value."""
    assert "No Issue" in st.TEST_DECISIONS
    assert "" not in st.TEST_DECISIONS
    for decision in st.TEST_BUG_DECISIONS:
        assert decision in st.TEST_DECISIONS
        assert st.TEST_DECISION_REQUIRES[decision] in st.TEST_FILLED_COLUMNS


def test_dropdown_columns_exist_in_matching_form():
    assert set(st.FORM_DROPDOWNS["mtpe"]) <= set(st.MTPE_FORM_HEADERS)
    assert set(st.FORM_DROPDOWNS["lqa"]) <= set(st.LQA_PE_FORM_HEADERS)
    assert (set(st.FORM_DROPDOWNS["glossary"])
            <= set(st.GLOSSARY_PE_FORM_HEADERS))
    assert set(st.FORM_DROPDOWNS["testkit"]) <= set(st.TEST_KIT_FORM_HEADERS)
    assert st.FORM_DROPDOWNS["testkit"]["TEST_Decision"] == st.TEST_DECISIONS
    # multi-value column → enforced at write time, never an Excel dropdown
    assert "AI_ErrorTypes" not in st.FORM_DROPDOWNS["testkit"]
    assert st.FORM_DROPDOWNS["mtpe"]["PE_Decision"] == st.MTPE_DECISIONS
    assert st.FORM_DROPDOWNS["lqa"]["PE_Decision"] == st.LQA_PE_DECISIONS
    # glossary adjudicates original vs suggested → LQA decision vocabulary
    assert (st.FORM_DROPDOWNS["glossary"]["PE_Decision"]
            == st.LQA_PE_DECISIONS)


def test_artifact_naming_convention():
    from datetime import datetime
    when = datetime(2026, 8, 2, 17, 45)
    name = st.artifact_name("asset", "glossary-refresh", when)
    assert name == "asset-glossary-refresh-20260802-1745"
    assert st.is_standard_name(name)
    assert st.is_standard_name("lqa-bugreport-zhCN-20260802-1745")
    assert not st.is_standard_name("20260802-glossary-update")  # date-first
    assert not st.is_standard_name("results-foo-20260802-1745")  # bad stage
    assert not st.is_standard_name("lqa-foo-20260802")  # no minutes
    try:
        st.artifact_name("results", "x", when)
        assert False, "unknown stage must raise"
    except ValueError:
        pass


def test_conditional_requirements_reference_real_columns_and_decisions():
    all_decisions = set(st.MTPE_DECISIONS) | set(st.LQA_PE_DECISIONS)
    for decision, required_col in st.DECISION_REQUIRES.items():
        assert decision in all_decisions
        assert required_col in st.PE_FILLED_COLUMNS
    for decision in st.UNTOUCHED_DECISIONS:
        assert decision in all_decisions
