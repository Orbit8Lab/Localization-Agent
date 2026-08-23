"""Canonical document standards — single source of truth for column schemas
and option vocabularies shared between form writers and form readers.

Human-readable companion: docs/STANDARDS.md. Any module that emits or parses
a PE form MUST import these constants; never re-declare column names inline.
"""
from __future__ import annotations

# ---------------------------------------------------------------- string type
STRING_TYPES = (
    "Skill", "Item", "UI", "System", "Dialogue", "Marketing", "Others",
)

# ------------------------------------------------------------- PE vocabularies
PE_CATEGORIZATIONS = (
    "Accuracy", "Terminology", "Tone", "Fluency", "Technical",
)
PE_SEVERITIES = ("Critical", "Major", "Minor")

# Decisions — MTPE form (machine-translation post-editing).
MTPE_DECISIONS = (
    "Accept Translation",
    "Reject&Modification",
    "Reject&Cannot Answer",
)

# Decisions — LQA PE form (bug-fix adjudication).
LQA_PE_DECISIONS = (
    "Reject&Keep-as-it-is",
    "Accept Suggested Translation",
    "Reject&Modification",
    "Reject&Cannot Answer",
)

# ------------------------------------------------------------------ PE forms
# Agent emits these headers with all PE_* columns empty (dropdowns attached);
# the post-editor fills them; the agent reads them back.

MTPE_FORM_HEADERS = (
    "StringID",
    "StringType",
    "Source",
    "Target_MT",
    "PE_Decision",
    "PE_Modification",
    "PE_Note",
    "PE_Query",
    "PE_Categorization",
    "PE_Severity",
)

LQA_PE_FORM_HEADERS = (
    "StringID",
    "StringType",
    "Source",
    "Target_Original",
    "Target_Suggested",
    "PE_Decision",
    "PE_Modification",
    "PE_Note",
    "PE_Query",
    "PE_Categorization",
    "PE_Severity",
)

# Glossary review form — same family as the LQA PE form (identical target
# pair + PE_* block and decision vocabulary), with term-specific agent
# columns. EntryType is agent-authored, not a PE dropdown.
GLOSSARY_ENTRY_TYPES = ("Conflict", "Violation", "Suggestion", "Update")

GLOSSARY_PE_FORM_HEADERS = (
    "TermID",
    "EntryType",
    "Source",
    "Target_Original",
    "Target_Suggested",
    "Alternatives",
    "Evidence",
    "PE_Decision",
    "PE_Modification",
    "PE_Note",
    "PE_Query",
    "PE_Categorization",
    "PE_Severity",
)

# Columns the post-editor owns; everything else is agent-authored and must
# come back unchanged (a diff there is an intake error, not an edit).
PE_FILLED_COLUMNS = (
    "PE_Decision",
    "PE_Modification",
    "PE_Note",
    "PE_Query",
    "PE_Categorization",
    "PE_Severity",
)

# ------------------------------------------------------------- test kit form
# In-game test kit (§4.5): agent → tester → agent. Unlike the PE forms this
# has THREE owners, so the prefix says who fills a column:
#   (none)  agent, deterministic — from the corpus/classifier
#   AI_*    agent, model-predicted — a hint, never a verdict
#   TEST_*  the tester, in-game — empty on emit, dropdowns attached
#
# The AI_* block is advisory ON PURPOSE. A kit that only shipped predicted-bug
# rows could never catch a missed prediction, so every string ships with its
# prediction visible and the tester's verdict always outranks it.

# Predicted error types. Deliberately the SAME vocabulary as the bug report's
# CATEGORY_LABELS (bug_report.py) so a prediction and a confirmed bug are
# countable against each other — prediction precision is measurable only if
# both sides speak one language.
TEST_ERROR_TYPES = (
    "Tag/Markup",
    "Terminology",
    "Untranslated",
    "Source Leakage",
    "Mistranslation",
    "Omission",
    "Grammar",
    "Tone/Register",
    "Length/Truncation",
    "Punctuation",
    "Compliance",
    "Terminology Inconsistency",
    "Regression",
)

# Internal bug_report token -> test-kit label, for turning gate/LQA findings
# into predictions. Mirrors bug_report.CATEGORY_LABELS minus its client-facing
# "Localization - " prefix (a tester reads the short form on a phone screen).
TEST_ERROR_TYPE_BY_TOKEN = {
    "placeholder": "Tag/Markup",
    "markup": "Tag/Markup",
    "terminology": "Terminology",
    "untranslated": "Untranslated",
    "leakage": "Source Leakage",
    "mistranslation": "Mistranslation",
    "omission": "Omission",
    "grammar": "Grammar",
    "register": "Tone/Register",
    "length": "Length/Truncation",
    "punctuation": "Punctuation",
    "compliance": "Compliance",
    "consistency": "Terminology Inconsistency",
    "regression": "Regression",
}

TEST_EXPECTED_BUG = ("Yes", "No")

TEST_ASSIGNED_TEAMS = (
    "Localization", "Engineering", "UI", "Design", "QA",
)

# The tester's verdict. "No Issue" is an affirmative result, not a blank —
# a row that comes back empty was never tested, and the two must never be
# conflated when measuring coverage.
TEST_DECISIONS = (
    "No Issue",
    "Localization Bug",
    "Internationalization Bug",
    "Needs Investigation",
    "Cannot Reproduce",
)

TEST_KIT_FORM_HEADERS = (
    "StringID",
    "StringType",
    "StringLocation",
    "Source",
    "Target_Current",
    "AI_ExpectedBug",
    "AI_ErrorTypes",
    "TEST_Reproduction",
    "TEST_Context",
    "TEST_AssignedTeam",
    "TEST_Decision",
    "TEST_Suggestion",
    "TEST_Notes",
)

# Columns the tester owns; everything else must come back unchanged.
TEST_FILLED_COLUMNS = (
    "TEST_Reproduction",
    "TEST_Context",
    "TEST_AssignedTeam",
    "TEST_Decision",
    "TEST_Suggestion",
    "TEST_Notes",
)

# Decisions that assert a defect exists and therefore need evidence on
# read-back (checked when the reader lands; see docs/STANDARDS.md §4.5).
TEST_DECISION_REQUIRES = {
    "Localization Bug": "TEST_Reproduction",
    "Internationalization Bug": "TEST_Reproduction",
    "Needs Investigation": "TEST_Notes",
}

# Decisions that open a bug downstream (feed the LQA bug report).
TEST_BUG_DECISIONS = ("Localization Bug", "Internationalization Bug")

# Dropdown restrictions per column, per form kind.
FORM_DROPDOWNS = {
    "mtpe": {
        "StringType": STRING_TYPES,
        "PE_Decision": MTPE_DECISIONS,
        "PE_Categorization": PE_CATEGORIZATIONS,
        "PE_Severity": PE_SEVERITIES,
    },
    "lqa": {
        "StringType": STRING_TYPES,
        "PE_Decision": LQA_PE_DECISIONS,
        "PE_Categorization": PE_CATEGORIZATIONS,
        "PE_Severity": PE_SEVERITIES,
    },
    "glossary": {
        "PE_Decision": LQA_PE_DECISIONS,
        "PE_Categorization": PE_CATEGORIZATIONS,
        "PE_Severity": PE_SEVERITIES,
    },
    # AI_ErrorTypes carries no dropdown: it is multi-value (a string can be
    # at risk of both overflow and a broken placeholder) and Excel list
    # validation cannot express that. The vocabulary is enforced at write
    # time by emit_test_kit instead.
    "testkit": {
        "StringType": STRING_TYPES,
        "AI_ExpectedBug": TEST_EXPECTED_BUG,
        "TEST_AssignedTeam": TEST_ASSIGNED_TEAMS,
        "TEST_Decision": TEST_DECISIONS,
    },
}

# Conditional requirements on read-back: decision -> column that must be
# non-empty for the row to be actionable.
DECISION_REQUIRES = {
    "Reject&Modification": "PE_Modification",
    "Reject&Cannot Answer": "PE_Query",
}

# Decisions that leave the delivered string byte-identical to the original.
UNTOUCHED_DECISIONS = ("Reject&Keep-as-it-is", "Reject&Cannot Answer")

# ---------------------------------------------------------- glossary standard
# Canonical glossary storage is the pipeline T1 JSON shape (loads via
# Glossary.load_t1_file); xlsx renderings are human views, JSON is the record.
GLOSSARY_T1_KEYS = ("metadata", "terms")

# ------------------------------------------------------------- path templates
# Project workspace layout — canonical skeleton is _templates/P000-template
# (Google Drive; see docs/STANDARDS.md §1.1).
ADMIN_DIR = "00-admin"                # quotes, contracts, QA plans
RECEIVED_DIR = "10-received"          # client drops — immutable, byte-preserved
WORK_DIR = "20-work"                  # all work products, indexed in WORKLOG.md
DELIVERABLES_DIR = "30-deliverables"  # exactly what was sent; immutable
REFERENCE_DIR = "40-reference"        # project glossaries, style guides
TEMP_DIR = "90-temp"                  # agent scratch — retention-limited
TEMP_RETENTION_DAYS = 90              # standard plans; permanent-retention
                                      # plans keep forever

# Naming convention for agent-created folders/files:
#   <stage>-<slug>-YYYYMMDD-HHMM   (stage first, minute timestamp last)
# Exception: 10-received drops are named by receipt date
# ("YYYYMMDD-<source-label>", original filenames unchanged).
STAGE_TOKENS = ("intake", "ingest", "context", "asset", "translate",
                "pe", "lqa", "test", "release")
TIMESTAMP_FMT = "%Y%m%d-%H%M"
RECEIVED_DROP_FMT = "%Y%m%d"

_ARTIFACT_NAME_RE = None


def artifact_name(stage: str, slug: str, when) -> str:
    """Standard name for an agent-created folder/file stem:
    ``<stage>-<slug>-YYYYMMDD-HHMM`` (docs/STANDARDS.md §1.1).
    ``when`` is a datetime; ``slug`` is lowercase kebab-case."""
    if stage not in STAGE_TOKENS:
        raise ValueError(f"unknown lifecycle stage {stage!r}; "
                         f"expected one of {STAGE_TOKENS}")
    return f"{stage}-{slug}-{when.strftime(TIMESTAMP_FMT)}"


def is_standard_name(name: str) -> bool:
    """True if ``name`` (folder or file stem) follows the stage-first,
    minute-timestamp-last convention."""
    global _ARTIFACT_NAME_RE
    if _ARTIFACT_NAME_RE is None:
        import re
        _ARTIFACT_NAME_RE = re.compile(
            r"^(" + "|".join(STAGE_TOKENS) + r")"
            r"-[a-z0-9][a-zA-Z0-9-]*"
            r"-\d{8}-\d{4}$")
    return bool(_ARTIFACT_NAME_RE.match(name))
