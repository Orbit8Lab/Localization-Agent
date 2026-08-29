"""Conversational job creation — describe a project, confirm, create.

`job init` takes fourteen flags, four of them required. That is a fine
machine interface and a poor human one, especially for the first job on a
new box where an operator does not yet know what the pipeline wants.

## Why a model proposes but never commits

The intake form is the job's constitution (schemas.IntakeBrief): the first
artifact, authoritative, and the source every later stage reads its
configuration from. `target_locales` decides what gets translated;
`source_lang` decides how the gate reads every string. A wrong value here
is not a bad answer to one question — it points an entire pipeline run at
the wrong thing, and the artifact tree will faithfully record that it was
asked for.

So this module splits the job in two:

    the model      turns prose into a STRUCTURED PROPOSAL
    the schema     rejects anything malformed before a human sees it
    the human      reads the proposal and commits it

The model never writes. `propose_intake` returns a value; creating the job
is a separate call the caller makes only after confirmation. That ordering
is the point — an LLM that can scaffold a job unprompted has been handed
the one decision this architecture reserves for people.

Locale codes get particular scrutiny: `ja` and `ja-JP` and `jp` are not
interchangeable, a model will produce whichever the prose suggested, and
the mistake is invisible until a stage runs against a locale nobody meant.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from pydantic import Field

from .llm import Provider, complete_json
from .schemas import IntakeBrief, Strict

# BCP-47-ish: a language subtag, optionally a script, and optionally a
# region that is EITHER two letters (US, BR) or a three-digit UN M.49 code.
# The numeric form is not exotic here — es-419 (Latin American Spanish) is
# a normal game-localization target, and a two-letter-only pattern rejects
# it. Not a registry lookup: the point is to catch shapes that are
# obviously not locales ("english", "jp-Japan") before they reach an
# artifact, not to be authoritative about which codes exist.
_LOCALE_RE = re.compile(
    r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-([A-Z]{2}|\d{3}))?$")

# Codes people reach for that are not the code. Left as a correction map
# rather than silent rewriting: the operator confirms the fix.
COMMON_MISTAKES = {
    "jp": "ja", "cn": "zh-CN", "kr": "ko", "english": "en",
    "japanese": "ja", "korean": "ko", "chinese": "zh-CN",
    "zh-cn": "zh-CN", "zh-tw": "zh-TW", "pt-br": "pt-BR",
    "en-us": "en", "en-gb": "en-GB",
}

SYSTEM = """You turn a description of a game-localization project into a \
structured intake form. You do NOT create anything — a human reviews your \
proposal and decides.

Rules:
- target_locales and source_lang MUST be BCP-47 codes (en, ja, ko, zh-CN, \
zh-TW, pt-BR, fr, de, es-419). Never a language name, never "jp".
- source_lang is the language the game is WRITTEN in; target_locales are \
what it is being translated INTO. Never include source_lang in targets.
- job_id: lowercase, hyphenated, no spaces — a directory name.
- genre: short lowercase tags (survival, roguelike, visual-novel, rpg).
- client_lang: the language the CLIENT reads reports in. Often the same as \
source_lang for a Chinese or Japanese studio. Omit if not stated.
- Leave a field out rather than inventing it. Unstated is not unknown-so-\
guess; the operator fills gaps.

Respond with ONE JSON object, no prose, no code fences."""


class IntakeProposal(Strict):
    """What the model suggests. Not yet a job, and not yet an artifact."""
    job_id: str
    game: str
    source_lang: str
    target_locales: List[str]
    genre: List[str] = Field(default_factory=list)
    engine: str = "unknown"
    client_lang: Optional[str] = None
    platforms: List[str] = Field(default_factory=list)
    reference_titles: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


@dataclass
class Review:
    """A proposal plus everything wrong or questionable about it."""
    proposal: IntakeProposal
    errors: List[str]                  # must be fixed before creating
    warnings: List[str]                # worth a human glance

    @property
    def ok(self) -> bool:
        return not self.errors


def normalize_locale(code: str) -> str:
    """Map a common near-miss onto the real code. Returns the input
    unchanged when it is already fine or not a recognized mistake —
    guessing further would hide the problem the check exists to surface."""
    stripped = code.strip()
    return COMMON_MISTAKES.get(stripped.lower(), stripped)


def review(proposal: IntakeProposal,
           source_files: List[str]) -> Review:
    """Everything checkable about a proposal, before a human reads it.

    Deterministic: this is the layer that must not depend on a model being
    careful, because the failure it catches (a plausible-looking wrong
    locale) is exactly what a model produces confidently.
    """
    errors: List[str] = []
    warnings: List[str] = []

    if not proposal.target_locales:
        errors.append("no target locales — nothing would be translated")

    for code in [proposal.source_lang, *proposal.target_locales]:
        fixed = normalize_locale(code)
        if fixed != code:
            errors.append(f"{code!r} is not a locale code — did you mean "
                          f"{fixed!r}?")
        elif not _LOCALE_RE.match(code):
            errors.append(f"{code!r} does not look like a BCP-47 locale "
                          f"(expected e.g. en, ja, zh-CN)")

    if proposal.source_lang in proposal.target_locales:
        errors.append(f"source_lang {proposal.source_lang!r} is also a "
                      f"target — the pipeline would translate it to itself")

    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", proposal.job_id):
        errors.append(f"job_id {proposal.job_id!r} is not a safe directory "
                      f"name (lowercase, digits, . _ -)")

    if not source_files:
        errors.append("no source files given — S1 would have nothing to "
                      "ingest")
    for path in source_files:
        if not Path(path).exists():
            errors.append(f"source file not found: {path}")

    if not proposal.genre:
        warnings.append("no genre — the T2 genre wording layer will be "
                        "empty and the style brief has less to work from")
    if proposal.client_lang is None:
        warnings.append("no client_lang — bug reports default to the "
                        "target language, which the client may not read")
    if proposal.engine == "unknown":
        warnings.append("engine unknown — fine, but a known engine helps "
                        "the ingest adapter and placeholder rules")
    return Review(proposal=proposal, errors=errors, warnings=warnings)


def propose_intake(provider: Provider, description: str, *,
                   source_files: List[str]) -> Review:
    """Ask the model for a structured intake, then check it.

    Returns a `Review`, never a job: the caller confirms with a human and
    calls `to_intake` separately. Splitting propose from create is what
    keeps the model out of a decision the artifact tree treats as
    constitutional.
    """
    prompt = (f"Project description:\n{description}\n\n"
              f"Source files provided: {', '.join(source_files) or 'none'}\n"
              "Propose the intake form as JSON.")
    proposal = complete_json(provider, SYSTEM, prompt, IntakeProposal,
                             temperature=0.0, max_tokens=800)
    return review(proposal, source_files)


def to_intake(proposal: IntakeProposal, *,
              tenant_id: str = "default") -> IntakeBrief:
    """Convert a REVIEWED proposal into the artifact schema."""
    return IntakeBrief(
        game=proposal.game, source_lang=proposal.source_lang,
        target_locales=list(proposal.target_locales),
        genre=list(proposal.genre), engine=proposal.engine,
        client_lang=proposal.client_lang,
        platforms=list(proposal.platforms),
        reference_titles=list(proposal.reference_titles),
        tenant_id=tenant_id, notes=proposal.notes)


def render(review_result: Review, source_files: List[str]) -> str:
    """The proposal as a human reads it before deciding.

    Shows every field including the empty ones: an omission is a decision
    too, and the failure mode here is an operator confirming a form whose
    blank `client_lang` they never noticed.
    """
    proposal = review_result.proposal
    lines = [
        "",
        f"  job id        {proposal.job_id}",
        f"  game          {proposal.game}",
        f"  source lang   {proposal.source_lang}",
        f"  targets       {', '.join(proposal.target_locales) or '(none)'}",
        f"  genre         {', '.join(proposal.genre) or '(none)'}",
        f"  engine        {proposal.engine}",
        f"  client lang   {proposal.client_lang or '(none)'}",
        f"  platforms     {', '.join(proposal.platforms) or '(none)'}",
        f"  sources       {', '.join(source_files) or '(none)'}",
    ]
    if proposal.notes:
        lines.append(f"  notes         {proposal.notes}")
    for message in review_result.errors:
        lines.append(f"  ERROR   {message}")
    for message in review_result.warnings:
        lines.append(f"  warning {message}")
    return "\n".join(lines)
