"""Building the client deliverable from chat.

`lqa_run` was reachable from the agent and `lqa report` was not, so every
audit ended with the operator dropping to a shell to produce the xlsx.
That shell is also where the mistakes happened — a report read out of the
wrong attempt, an audit whose stored locale disagreed with its name, a
locations file from another language — because the CLI's guard rails were
the only ones and `--force` was one keystroke away.

The tool carries the same rules as the CLI, plus one the CLI cannot: on a
refusal it tells the MODEL, in the result, to relay the warnings and stop
rather than retry with force. A mislabelled bug report cannot be recalled
once the client has it.
"""
from __future__ import annotations

import json

import pytest

from orbit8.controller import Job
from orbit8.external_lqa import run_external_lqa
from orbit8.llm import EchoProvider
from orbit8.orchestrator import ChatOrchestrator
from orbit8.schemas import IntakeBrief, LQAReport

INTAKE = IntakeBrief(game="Nomori", source_lang="en",
                     target_locales=["zh-CN", "ja", "ko"])


@pytest.fixture
def chat(tmp_path):
    job = Job.init(tmp_path / "proj" / "jobs", "j", intake=INTAKE,
                   source_files=[])
    return ChatOrchestrator(job, EchoProvider("ja"), operator="t",
                            dry_run=True)


def _pairs(chat, locale, rows=(("a", "Start Game", "ゲーム開始"),)):
    project = chat.job.store.root.parent
    path = project / f"{locale}.jsonl"
    path.write_text("\n".join(json.dumps(
        {"key": k, "source_language": "en", "target_language": locale,
         "source_text": s, "target_text": t}, ensure_ascii=False)
        for k, s, t in rows) + "\n", encoding="utf-8")
    return path


def _audit(chat, locale, name):
    run_external_lqa(chat.job, None, _pairs(chat, locale), name=name,
                     deterministic_only=True)


def _store(chat, name, locale):
    """A stored report with a locale that does not match its name."""
    attempt = chat.job.store.new_attempt(5)
    chat.job.store.write(5, f"lqa_report.{name}",
                         LQAReport(job_id="j", locale=locale, checked=1,
                                   flagged_strings=0, findings_total=0,
                                   confirmed=0, overturned=0, uncertain=0,
                                   block_ship=False),
                         produced_by="test", attempt=attempt)


# --------------------------------------------------------- it is reachable

def test_the_tool_is_in_the_tool_set():
    assert "lqa_report" in ChatOrchestrator.tool_names()


def test_it_builds_the_deliverable(chat):
    _audit(chat, "ja", "lqa-ja")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja",
                                         "no_suggestions": True}))
    assert out["status"] == "complete"
    assert out["locale"] == "ja"
    assert out["xlsx"].endswith("Nomori_Bug_Report_ja.xlsx")


def test_the_deliverable_goes_to_30_deliverables(chat):
    _audit(chat, "ja", "lqa-ja")
    chat._t_lqa_report({"name": "lqa-ja", "no_suggestions": True})
    project = chat.job.store.root.parent
    assert list((project / "30-deliverables").glob("*/*Bug_Report_ja.xlsx"))


def test_it_finds_a_report_in_an_older_attempt(chat):
    """Each audit opens its own attempt, so the newest is usually the
    wrong place to look."""
    _audit(chat, "ja", "lqa-ja")
    _audit(chat, "ko", "lqa-ko")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja",
                                         "no_suggestions": True}))
    assert out["attempt"] < chat.job.store.latest_attempt(5)


# ----------------------------------------------------------- recoverability

def test_no_name_lists_the_available_audits(chat):
    """A model that cannot discover the names will guess at them."""
    _audit(chat, "ja", "lqa-ja")
    _audit(chat, "ko", "lqa-ko")
    out = chat._t_lqa_report({})
    assert "lqa-ja" in out and "lqa-ko" in out


def test_an_unknown_name_lists_the_available_audits(chat):
    _audit(chat, "ja", "lqa-ja")
    out = chat._t_lqa_report({"name": "nope"})
    assert out.startswith("error:") and "lqa-ja" in out


def test_it_says_when_no_audit_has_run(chat):
    out = chat._t_lqa_report({"name": "anything"})
    assert "lqa_run" in out


# ------------------------------------------------------------- the refusals

def test_a_name_locale_mismatch_is_refused(chat):
    """The real artifact: named ja, scored as zh-CN."""
    _store(chat, "lqa-ja-20260830", "zh-CN")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja-20260830",
                                         "no_suggestions": True}))
    assert out["status"] == "refused"
    assert "SCORED against zh-CN" in out["warnings"][0]


def test_a_refusal_tells_the_model_to_stop_not_retry(chat):
    """Without this the model's next move is force:true."""
    _store(chat, "lqa-ja", "zh-CN")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja",
                                         "no_suggestions": True}))
    advice = out["advice"]
    assert "VERBATIM" in advice and "cannot be recalled" in advice


def test_a_refusal_writes_nothing(chat):
    _store(chat, "lqa-ja", "zh-CN")
    chat._t_lqa_report({"name": "lqa-ja", "no_suggestions": True})
    project = chat.job.store.root.parent
    assert not (project / "30-deliverables").exists()


def test_a_locations_file_for_another_locale_is_refused(chat):
    _audit(chat, "ja", "lqa-ja")
    ko = _pairs(chat, "ko", (("a", "Start", "시작"),))
    out = json.loads(chat._t_lqa_report({
        "name": "lqa-ja", "locations_from": str(ko),
        "no_suggestions": True}))
    assert out["status"] == "refused"
    assert "ko export" in out["warnings"][0]


def test_force_overrides_the_refusal(chat):
    """Reachable, but only on the operator's explicit say-so."""
    _store(chat, "lqa-ja", "zh-CN")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja", "force": True,
                                         "no_suggestions": True}))
    assert out["status"] == "complete"
    assert out["warnings"]                 # still reported, not silenced


def test_a_missing_locations_file_is_reported(chat):
    _audit(chat, "ja", "lqa-ja")
    out = chat._t_lqa_report({"name": "lqa-ja", "locations_from": "nope.jsonl"})
    assert "no locations file" in out


# ------------------------------------------------------------- what it says

def test_reviewer_fixes_are_counted_separately(chat):
    """A --no-suggestions run has zero Repair-agent output but can still
    fill the column from the reviewer's own fixes. Reporting only the
    agent's total made a useful report look empty."""
    _audit(chat, "ja", "lqa-ja")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja",
                                         "no_suggestions": True}))
    assert "repair_agent_suggestions" in out
    assert "reviewer_suggestions" in out


def test_block_ship_is_surfaced(chat):
    """The agent must be able to warn the operator before delivery."""
    _audit(chat, "ja", "lqa-ja")
    out = json.loads(chat._t_lqa_report({"name": "lqa-ja",
                                         "no_suggestions": True}))
    assert "block_ship" in out


def test_the_timestamp_can_be_pinned(chat):
    _audit(chat, "ja", "lqa-ja")
    chat._t_lqa_report({"name": "lqa-ja", "no_suggestions": True,
                        "timestamp": "20260830"})
    project = chat.job.store.root.parent
    assert (project / "30-deliverables" / "20260830-lqa-report").is_dir()


def test_out_dir_cannot_escape_the_project(chat):
    _audit(chat, "ja", "lqa-ja")
    out = chat._t_lqa_report({"name": "lqa-ja", "no_suggestions": True,
                              "out_dir": "/etc"})
    assert "error" in out.lower()
