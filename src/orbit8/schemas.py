"""Artifact contract — the normative Pydantic models for every stage output.

Ported from localization-pipeline `locpipe/schemas.py` (the V2 step contracts,
docs/agents/README.md) and extended with the Orbit8 envelope from the design
doc §3: every artifact carries schema_version, job_id, stage, attempt,
produced_by, and (for agent-produced artifacts) a model_fingerprint.

Rule carried over: schema mismatch is a step failure, never best-effort
parsing. The Controller validates on write and refuses to advance the stage
on a validation failure.
"""
from __future__ import annotations

import warnings as _warnings
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


class Strict(BaseModel):
    """Base for LLM-facing schemas: unknown keys are errors, not noise."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- envelope

class Envelope(Strict):
    """Design §3: the artifact wrapper. `produced_by` is either
    "code:<module>@<version>" or "agent:<name>@<prompt_version>";
    `model_fingerprint` (model id + prompt hash) is set for agents only —
    it is what makes a disputed translation attributable six weeks later.
    """
    schema_version: int = SCHEMA_VERSION
    schema_name: str
    job_id: str
    stage: int
    attempt: int = 1
    produced_at: datetime
    produced_by: str
    model_fingerprint: Optional[str] = None
    payload: dict


# ---------------------------------------------------------------- ingestion

class SourceString(Strict):
    """One source record, normalized from any ingest adapter."""
    key: str
    text: str
    context: Optional[str] = None
    max_len: Optional[int] = None
    file_ref: Optional[str] = None


class UniqueString(Strict):
    """A deduplicated source string with a stable synthetic id (`u0000`, …).

    ``keys`` holds every real game key sharing this source text; fan-out back
    to game keys happens only at emission.
    """
    uid: str
    text: str
    keys: List[str]
    context: Optional[str] = None


class SourceBatch(Strict):
    records: List[SourceString]


class IngestReport(Strict):
    """Wordcount is load-bearing: it drives quoting at G0 revisions and
    tester-hour allocation in Stage 6."""
    total_records: int
    unique_strings: int
    total_chars: int
    dedup_ratio: float
    per_file: Dict[str, int] = Field(default_factory=dict)


# ------------------------------------------------------------------ intake

class IntakeBrief(Strict):
    """Stage 0: the job's constitution. Everything downstream reads from
    here instead of re-asking the operator."""
    game: str
    genre: List[str] = Field(default_factory=list)
    engine: str = "unknown"
    source_lang: str
    target_locales: List[str]
    reference_titles: List[str] = Field(default_factory=list)
    volume_estimate: Optional[int] = None
    deadline: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    client_lang: Optional[str] = None
    tenant_id: str = "default"
    notes: Optional[str] = None


class MarketAssessment(Strict):
    locale: str
    demand: str = "unknown"        # strong | moderate | weak | unknown
    comparable_titles: List[str] = Field(default_factory=list)
    recommendation: str
    notes: Optional[str] = None


class MarketReport(Strict):
    """Market Analyst output — advisory input to locale selection at G0."""
    assessments: List[MarketAssessment]
    summary: str


# ----------------------------------------------------------------- domains

class Domain(str, Enum):
    DIALOGUE = "dialogue"
    UI = "ui"
    MAP = "map"
    ITEM_DESC = "item_desc"
    SYSTEM = "system"
    MARKETING = "marketing"


# Domains whose policy mandates human post-editing regardless of gate results.
MTPE_DOMAINS = {Domain.DIALOGUE, Domain.MARKETING}


class DomainLabelItem(Strict):
    key: str
    domain: Domain
    confidence: float = 1.0        # rules-based v0 sets <1.0 on weak matches


class DomainLabels(Strict):
    """Per-string domain labels — drive prompt emphasis, MTPE routing, and
    test-case generation. Safety property (design §8): a low-confidence
    label routes TO MTPE, never away from it."""
    items: List[DomainLabelItem]


# ------------------------------------------------------------------- style

with _warnings.catch_warnings():
    # "register" is the linguistics term (formality level) — pydantic warns
    # that it shadows a BaseModel attribute, which we accept knowingly.
    _warnings.filterwarnings("ignore", message='Field name "register"')

    class StyleBrief(Strict):
        genre: List[str] = Field(default_factory=list)
        tone: str = "neutral"
        register: str = "standard"
        audience: str = "general"
        do: List[str] = Field(default_factory=list)
        dont: List[str] = Field(default_factory=list)
        per_locale_notes: Dict[str, str] = Field(default_factory=dict)
        confidence: str = "medium"
        sample_size: int = 0


# ---------------------------------------------------------------- glossary

class TermBrief(Strict):
    """One glossary term as presented to the Translator/Critic."""
    term: str
    translation: str
    type: str = "other"
    tier: int = 1                  # 1 game · 2 genre · 3 standard UI
    # Only a LOCKED term is law. Mined/draft entries are the termbase's
    # current best guess: presenting them to the reviewer as mandatory
    # makes it report "locked term violated" for a preference nobody
    # ratified, which is how correct strings end up on a bug report.
    locked: bool = False
    # Declared part-of-speech forms and casing policy (see gate_checks).
    forms: Dict[str, str] = Field(default_factory=dict)
    case: str = "context"
    en_anchor: Optional[str] = None
    sense_note: Optional[str] = None
    distinct_from: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)


class GlossaryBrief(Strict):
    """The per-batch glossary slice: only terms matched in the batch source.
    Merged T1 > T2 > T3 view — T1 wins conflicts."""
    game: str
    locale: str
    asset_version: int = 1
    terms: List[TermBrief] = Field(default_factory=list)


class TermProposal(Strict):
    term: str
    type: str = "other"
    frequency: int = 0
    proposed: Dict[str, str] = Field(default_factory=dict)   # locale -> term
    context_sample: Optional[str] = None


class TermConflict(Strict):
    term: str
    issue: str                     # variant_cluster | polysemy | collision
    variants: List[str] = Field(default_factory=list)
    recommendation: str


class GlossaryDelta(Strict):
    """Terminologist extraction — staged for the review sheet; nothing enters
    the locked glossary without human gate G1."""
    new_terms: List[TermProposal] = Field(default_factory=list)
    conflicts: List[TermConflict] = Field(default_factory=list)


class HealthIssue(Strict):
    check: str
    term: Optional[str] = None
    message: str
    proposal: Optional[Dict[str, str]] = None


class HealthReport(Strict):
    """Non-empty ``blockers`` ⇒ the Controller refuses to pass G1."""
    blockers: List[HealthIssue] = Field(default_factory=list)
    warnings: List[HealthIssue] = Field(default_factory=list)
    stats: Dict[str, float] = Field(default_factory=dict)


class AuditedFixRequest(Strict):
    """The ONLY write path toward a locked glossary (design §7): an agent
    files this request; a human re-opens G1. Repair has no other tool."""
    term: str
    current: str
    proposed: str
    reason: str
    requested_by: str


# ------------------------------------------------------------- translation

class TranslationItem(Strict):
    key: str
    target_text: str
    term_decisions: Dict[str, str] = Field(default_factory=dict)
    notes: Optional[str] = None


class BatchTranslation(Strict):
    """Output of both the Translator and the Repair agent."""
    items: List[TranslationItem]


# ----------------------------------------------------------------- review

class BugType(str, Enum):
    PLACEHOLDER = "placeholder"
    MARKUP = "markup"
    TERMINOLOGY = "terminology"
    UNTRANSLATED = "untranslated"
    LEAKAGE = "leakage"
    MISTRANSLATION = "mistranslation"
    OMISSION = "omission"
    GRAMMAR = "grammar"
    REGISTER = "register"
    LENGTH = "length"
    PUNCTUATION = "punctuation"
    COMPLIANCE = "compliance"
    CONSISTENCY = "consistency"
    REGRESSION = "regression"


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Finding(Strict):
    key: str
    bug_type: BugType
    severity: Severity
    message: str
    evidence: str
    suggested_fix: Optional[str] = None
    # Which LQA tier produced this finding (1/2/3). Stamped by the cascade
    # NODES, never by the LLM — it is provenance, not judgment. None outside
    # the S5 cascade (e.g. the S4 translate-loop critic).
    tier: Optional[int] = None

    def identity(self) -> tuple:
        """Identity used for repeat-detection across repair iterations."""
        return (self.key, self.bug_type, self.evidence)


class Review(Strict):
    """Findings from the deterministic gate OR the LLM Critic — same schema
    on purpose; consumers must not care which produced it."""
    findings: List[Finding] = Field(default_factory=list)


# -------------------------------------------------------------------- MTPE

class MTPEReason(str, Enum):
    DOMAIN_POLICY = "domain_policy"    # story/marketing route by policy
    FAILURE = "failure"                # hit max_iterations without converging
    LOW_CONFIDENCE = "low_confidence"  # classifier failed expensive (§8)


class MTPEItem(Strict):
    """Tagged distinctly (design §4): a translator post-editing by policy
    needs different framing from one repairing a 4x-failed string."""
    uid: str
    source: str
    target: str
    domain: Domain
    reason: MTPEReason
    findings: List[Finding] = Field(default_factory=list)


class MTPEQueue(Strict):
    locale: str
    items: List[MTPEItem] = Field(default_factory=list)


# -------------------------------------------------------------------- lqa

class VerdictDecision(str, Enum):
    CONFIRM = "confirm"
    OVERTURN = "overturn"
    UNCERTAIN = "uncertain"


class Verdict(Strict):
    decision: VerdictDecision
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    suggested_target: Optional[str] = None


class VerifiedFinding(Strict):
    finding: Finding
    verdict: Optional[Verdict] = None


class LQAItem(Strict):
    uid: str
    game_keys: List[str]
    source: str
    target: str
    findings: List[VerifiedFinding]


class LQAReport(Strict):
    """Tier cascade output (design §5). T3 precision beats recall — studios
    abandon LQA tooling that cries wolf."""
    job_id: str
    locale: str
    checked: int
    flagged_strings: int
    findings_total: int
    confirmed: int
    overturned: int
    uncertain: int
    block_ship: bool
    by_severity: Dict[str, int] = Field(default_factory=dict)
    by_bug_type: Dict[str, int] = Field(default_factory=dict)
    items: List[LQAItem] = Field(default_factory=list)
    # T3 observability: raw/kept/dropped-by-reason counts, plus the full
    # per-finding audit (kept + dropped, with verdicts) for calibration.
    t3_stats: Dict[str, int] = Field(default_factory=dict)
    t3_audit: List[dict] = Field(default_factory=list)
    # T3 batches whose LLM call failed: those strings were NOT reviewed.
    # A coverage gap must be visible in the artifact — "no findings" and
    # "never looked" are different claims.
    t3_errors: List[dict] = Field(default_factory=list)
    # Per-tier in/out counts recorded by the cascade nodes themselves.
    # The numbers must telescope (accepted → t1 → t2 → t3); verify_cascade
    # (graphs/lqa.py) refuses the report when they don't — proof the four
    # steps ran in order, independent of any agent's claims.
    cascade_ledger: Dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------- testing

class TestCase(Strict):
    id: str
    domain: Domain
    title: str
    steps: List[str]
    expected: str
    keys: List[str] = Field(default_factory=list)


class TestPlan(Strict):
    game: str
    target_lang: str
    tester_hours: float
    cases: List[TestCase]
    coverage_note: str = ""


# ---------------------------------------------------------------- release

class StoreCopy(Strict):
    platform: str
    short_description: str
    long_description: str
    char_limit_short: int
    char_limit_long: int
    within_limits: bool = True


class MarketingKit(Strict):
    """Studio-voice materials only — never fabricated player reviews or
    comments presented as organic (platforms ban astroturfing)."""
    game: str
    target_locale: str
    key_messages: List[str]
    store_copy: List[StoreCopy]
    social_posts: List[str] = Field(default_factory=list)
    press_blurb: str = ""


class DeliverablesManifest(Strict):
    job_id: str
    locales: List[str]
    files: Dict[str, str] = Field(default_factory=dict)   # logical name -> path
    glossary_asset_version: int = 1
    changelog: List[str] = Field(default_factory=list)


# ------------------------------------------------------------ translate run

class SegmentRef(Strict):
    """Stage-4 graph state holds IDs, never full text (design §4) — text
    lives in the run DB; checkpointing 40k strings per superstep would bury
    the checkpointer."""
    uid: str
    domain: Domain = Domain.UI


class TranslateRunSummary(Strict):
    """Artifact emitted when a Stage-4 graph run completes; the Controller
    derives PILOT/PRODUCTION completion from it."""
    job_id: str
    locale: str
    kind: str                       # pilot | production | flagged
    segments_total: int
    accepted: int
    prefilled: int
    reused: int
    escalated: int
    mtpe_policy: int
    tokens_spent: float = 0.0
    iterations_max: int = 0
