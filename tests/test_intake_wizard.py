"""Conversational job creation (intake_wizard.py).

`job init` takes fourteen flags; describing a project in prose is a better
human interface. But the intake form is the job's constitution — every
later stage reads `source_lang` and `target_locales` from it — so the model
PROPOSES and a human COMMITS, with deterministic validation in between.

That middle layer is what these tests are mostly about. It has to catch the
mistakes a model makes confidently, because a plausible-looking wrong
locale is invisible until a stage runs against a language nobody meant.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from orbit8.intake_wizard import (COMMON_MISTAKES, IntakeProposal, normalize_locale,
                                  render, review, to_intake)
from orbit8.schemas import IntakeBrief


@pytest.fixture
def source(tmp_path) -> list:
    path = tmp_path / "strings.json"
    path.write_text('{"K":"开始"}', encoding="utf-8")
    return [str(path)]


def _proposal(**kw) -> IntakeProposal:
    base = dict(job_id="examplegame-en-ja", game="ExampleGame",
                source_lang="zh-CN",
                target_locales=["en", "ja"], genre=["survival"],
                client_lang="zh-CN", engine="unity")
    base.update(kw)
    return IntakeProposal(**base)


# ------------------------------------------------------ locale checking

def test_a_clean_proposal_passes(source):
    assert review(_proposal(), source).ok


@pytest.mark.parametrize("wrong,right", sorted(COMMON_MISTAKES.items()))
def test_common_locale_mistakes_are_caught(wrong, right, source):
    """`jp` for Japanese is the classic, and a model will produce whatever
    the prose suggested. Every one of these is a plausible string that is
    not the code."""
    result = review(_proposal(target_locales=[wrong]), source)
    assert not result.ok
    assert any(right in message for message in result.errors)


def test_the_correction_is_suggested_not_applied(source):
    """Silently rewriting would hide the problem the check exists to
    surface — the operator should see that the model got it wrong."""
    result = review(_proposal(target_locales=["jp"]), source)
    assert "did you mean 'ja'" in " ".join(result.errors)
    assert result.proposal.target_locales == ["jp"]      # unchanged


def test_a_language_name_is_not_a_locale(source):
    assert not review(_proposal(target_locales=["Japanese"]), source).ok


def test_a_malformed_code_is_rejected(source):
    assert not review(_proposal(target_locales=["jp-Japan"]), source).ok


def test_real_regional_codes_pass(source):
    # source_lang is `en` here so a zh-CN TARGET is not also caught by the
    # self-translation rule — this test is about code SHAPE only.
    for code in ("zh-CN", "zh-TW", "pt-BR", "es-419", "en-GB"):
        result = review(
            _proposal(source_lang="en", target_locales=[code]), source)
        assert result.ok, (code, result.errors)


def test_the_source_language_is_checked_too(source):
    assert not review(_proposal(source_lang="chinese"), source).ok


# ----------------------------------------------------- structural checks

def test_translating_a_language_into_itself_is_refused(source):
    """A model that reads 'Chinese game for global release' can list zh-CN
    as both. The pipeline would dutifully translate it to itself."""
    result = review(_proposal(target_locales=["en", "zh-CN"]), source)
    assert not result.ok
    assert any("itself" in message for message in result.errors)


def test_no_targets_is_an_error(source):
    assert not review(_proposal(target_locales=[]), source).ok


def test_an_unsafe_job_id_is_refused(source):
    """job_id becomes a directory name."""
    for bad in ("My Project!", "../escape", "has space", "UPPER"):
        assert not review(_proposal(job_id=bad), source).ok, bad


def test_a_missing_source_file_is_caught(source):
    result = review(_proposal(), ["/nonexistent/strings.json"])
    assert not result.ok
    assert any("not found" in message for message in result.errors)


def test_no_source_file_warns_but_does_not_block():
    """A job with no strings yet is legitimate: it sits at INTAKE/G0, and
    only S1 (INGEST) needs the source — which is the right place to stop.
    Blocking here would prevent setting a project up before the client's
    drop arrives, which is exactly when a project folder gets created."""
    result = review(_proposal(), [])
    assert result.ok
    assert any("no source file yet" in message
               for message in result.warnings)


# ------------------------------------------------- warnings, not errors

def test_a_missing_genre_warns_without_blocking(source):
    result = review(_proposal(genre=[]), source)
    assert result.ok
    assert any("genre" in message for message in result.warnings)


def test_a_missing_client_lang_warns(source):
    """The client may not read the target language — a real failure that
    surfaces only when someone receives an unreadable bug report."""
    result = review(_proposal(client_lang=None), source)
    assert result.ok
    assert any("client_lang" in message for message in result.warnings)


def test_warnings_never_block_creation(source):
    result = review(_proposal(genre=[], client_lang=None, engine="unknown"),
                    source)
    assert result.ok and len(result.warnings) == 3


# --------------------------------------------------------- the handoff

def test_a_reviewed_proposal_becomes_the_artifact_schema(source):
    intake = to_intake(_proposal())
    assert isinstance(intake, IntakeBrief)
    assert intake.target_locales == ["en", "ja"]
    assert intake.source_lang == "zh-CN"


def test_the_tenant_is_carried_through(source):
    assert to_intake(_proposal(), tenant_id="client-a").tenant_id == "client-a"


def test_every_field_is_shown_for_confirmation(source):
    """An omission is a decision too. The failure mode is an operator
    confirming a form whose blank client_lang they never noticed."""
    text = render(review(_proposal(client_lang=None, platforms=[]), source),
                  source)
    assert "client lang   (none)" in text
    assert "platforms     (none)" in text


def test_errors_are_visible_in_the_rendering(source):
    text = render(review(_proposal(target_locales=["jp"]), source), source)
    assert "ERROR" in text and "ja" in text


def test_normalize_leaves_a_good_code_alone():
    assert normalize_locale("zh-CN") == "zh-CN"
    assert normalize_locale("ja") == "ja"


def test_normalize_does_not_guess_at_the_unrecognized():
    """Guessing further would hide the problem: an unknown string reaches
    the BCP-47 check and is reported, rather than being silently mapped."""
    assert normalize_locale("kl-ingon") == "kl-ingon"
