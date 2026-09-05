"""Which provider a chat tool actually calls.

`orbit8 chat --provider X` configures the session, but two tools built
their own `OpenAICompatProvider("deepseek")` instead of using it. The
failure is silent in the worst way: the run SUCCEEDS, reports tokens
spent, and bills a vendor the operator did not choose — with a
`model_fingerprint` on the output naming that vendor, so the artifact
disagrees with the session that produced it.
"""
from __future__ import annotations

import json
from pathlib import Path

from orbit8.controller import Job
from orbit8.orchestrator import ChatOrchestrator
from orbit8.schemas import IntakeBrief


class _Provider:
    name, model, tokens_spent = "chosen-vendor", "chosen-model", 0.0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        raise AssertionError("no model call expected")


def _chat(tmp_path: Path, **kwargs) -> ChatOrchestrator:
    project = tmp_path / "projectX"
    (project / "10-received").mkdir(parents=True)
    src = project / "s.json"
    src.write_text(json.dumps({"K": "x"}), encoding="utf-8")
    job = Job.init(project / "20-work", "j1",
                   intake=IntakeBrief(game="G", source_lang="zh",
                                      target_locales=["en"]),
                   source_files=[str(src)])
    return ChatOrchestrator(job, _Provider(), operator="tian", **kwargs)


def test_session_provider_is_used_when_there_is_no_factory(tmp_path: Path):
    chat = _chat(tmp_path)
    assert chat._locale_provider({}).name == "chosen-vendor"


def test_factory_wins_and_receives_the_locale(tmp_path: Path):
    """A per-locale factory is how the CLI passes `--provider` down; it
    must still reach the tools that take a locale argument."""
    seen = []

    def factory(locale):
        seen.append(locale)
        return _Provider()

    chat = _chat(tmp_path, provider_factory=factory)
    assert chat._locale_provider({"locale": "ja"}).name == "chosen-vendor"
    assert seen == ["ja"]


def test_dry_run_yields_no_provider(tmp_path: Path):
    """A --dry-run session must not construct a real client, which is
    also what makes these tools runnable with no API key at all."""
    chat = _chat(tmp_path, dry_run=True)
    assert chat._locale_provider({"locale": "ja"}) is None


def test_translate_po_refuses_in_dry_run_instead_of_billing(tmp_path: Path):
    chat = _chat(tmp_path, dry_run=True)
    result = json.loads(chat._t_translate_po({"po": "x.po", "locale": "en"}))
    assert "dry-run" in result["error"]


def test_no_tool_constructs_its_own_client():
    """The regression, pinned at the source: a hardcoded vendor name in a
    tool body silently overrides the operator's --provider."""
    source = Path("src/orbit8/orchestrator.py").read_text(encoding="utf-8")
    assert "OpenAICompatProvider" not in source, (
        "a chat tool builds its own provider; use _locale_provider so the "
        "session's --provider is honoured")


def test_extract_glossary_stays_deterministic_without_llm_filter(tmp_path: Path):
    """The LLM filter is opt-in. Without it the tool must ask for no
    provider at all, so it needs no key and cannot spend."""
    chat = _chat(tmp_path)
    out = tmp_path / "projectX" / "20-work" / "out"
    result = json.loads(chat._t_extract_glossary(
        {"po": [], "out_dir": str(out), "locale": "en"}))
    assert "stats" in result


# ------------------------------------------- switching the work model

class _Other:
    name, model, tokens_spent = "other-vendor", "other-model", 0.0

    def complete(self, system, user, *, temperature=0.3, max_tokens=2000):
        raise AssertionError("no model call expected")


def test_work_model_reports_what_the_factory_builds(tmp_path: Path):
    chat = _chat(tmp_path, provider_factory=lambda locale: _Other())
    assert chat.work_model() == "other-vendor/other-model"


def test_work_model_falls_back_to_the_session_provider(tmp_path: Path):
    chat = _chat(tmp_path)
    assert chat.work_model() == "chosen-vendor/chosen-model"


def test_set_work_model_rebinds_what_tools_use(tmp_path: Path):
    chat = _chat(tmp_path)
    assert chat._locale_provider({}).name == "chosen-vendor"
    after = chat.set_work_model(lambda locale: _Other())
    assert after == "other-vendor/other-model"
    assert chat._locale_provider({}).name == "other-vendor"


def test_switching_leaves_the_chat_agents_own_model_alone(tmp_path: Path):
    """The operator chose stage/tool scope: a session that changed its own
    reasoning model halfway would answer from two behaviours with nothing
    in the transcript marking the seam."""
    chat = _chat(tmp_path)
    chat.set_work_model(lambda locale: _Other())
    assert chat.provider.name == "chosen-vendor"


def test_the_switch_is_recorded_in_the_trace(tmp_path: Path):
    """Two artifacts from one session can now carry different
    fingerprints; the trace is what explains why."""
    chat = _chat(tmp_path)
    chat.set_work_model(lambda locale: _Other())
    switches = [r for r in chat.trace if r["event"] == "work_model"]
    assert len(switches) == 1
    assert switches[0]["before"] == "chosen-vendor/chosen-model"
    assert switches[0]["after"] == "other-vendor/other-model"


def test_the_agent_cannot_switch_its_own_model():
    """Operator-only, per design §7: a missing tool is a guarantee, a
    prompt rule is only a suggestion."""
    names = ChatOrchestrator.tool_names()
    for forbidden in ("set_model", "set_work_model", "set_provider"):
        assert forbidden not in names
