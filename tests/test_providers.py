"""The multi-provider layer: presets, the shared retry/deadline base, and
the Anthropic client.

Every test here pins something that fails SILENTLY in production. A wrong
`extra_body` 400s on a live key but never in a dry run; a provider that
skips the shared base still works until a call stalls overnight; a tool
that builds its own client bills the wrong vendor and reports success.
"""
from __future__ import annotations

import pytest

from orbit8 import llm
from orbit8.llm import (PROVIDER_NAMES, PROVIDER_PRESETS, AnthropicProvider,
                        OpenAICompatProvider, _ResilientProvider,
                        build_provider)


# --------------------------------------------------------------- presets

def test_only_deepseek_sends_reasoning_effort():
    """`reasoning_effort` is DeepSeek's spelling, not a portable parameter.

    Gemini's OpenAI-compat surface REJECTS it, so the shared boolean this
    replaced made every Gemini call a 400 — invisible until someone had a
    Gemini key, because no dry run ever sends a body.
    """
    assert PROVIDER_PRESETS["deepseek"].body() == {
        "extra_body": {"reasoning_effort": "low"}}
    for name in ("gemini", "openai", "qwen", "huggingface"):
        assert PROVIDER_PRESETS[name].body() == {}, (
            f"{name} must not send reasoning_effort")


def test_headroom_only_where_reasoning_tokens_are_billed():
    """Headroom tracks the DEFAULT MODEL, not the vendor: a thinking model
    spends tokens the caller never sees, so without headroom the visible
    budget is silently eaten by reasoning and the answer truncates."""
    for name in ("deepseek", "huggingface"):     # reasoning defaults
        assert PROVIDER_PRESETS[name].headroom == llm.REASONING_HEADROOM
    for name in ("gemini", "openai", "qwen"):    # non-reasoning defaults
        assert PROVIDER_PRESETS[name].headroom == 0


def test_presets_are_frozen_dataclasses_not_tuples():
    """The positional unpack this replaced broke silently when a field was
    added; a frozen dataclass makes both failures loud."""
    preset = PROVIDER_PRESETS["deepseek"]
    assert preset.api_key_env == "DEEPSEEK_API"
    with pytest.raises(Exception):
        preset.default_model = "something-else"


def test_anthropic_is_not_in_the_openai_compat_table():
    """It does not speak that protocol. Keeping it out of the table is
    what stops OpenAICompatProvider from being handed a base_url that
    would fail at request time rather than construction."""
    assert "anthropic" not in PROVIDER_PRESETS
    assert "anthropic" in PROVIDER_NAMES


# ------------------------------------------------- the shared resilience

def test_both_providers_share_the_retry_and_deadline_base():
    """A provider that reimplements `complete` reopens the ~50-minute
    stall and the 3-hour exit hang the base class exists to prevent."""
    for cls in (OpenAICompatProvider, AnthropicProvider):
        assert issubclass(cls, _ResilientProvider)
        assert "complete" not in vars(cls), (
            f"{cls.__name__} overrides complete() and so bypasses the "
            f"shared retry/deadline policy")


def test_missing_key_names_the_env_var(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API", raising=False)
    monkeypatch.setattr(llm, "autoload_env", lambda: None)
    with pytest.raises(RuntimeError, match=r"\$DEEPSEEK_API"):
        OpenAICompatProvider("deepseek")


def test_unknown_provider_lists_the_real_choices(monkeypatch):
    with pytest.raises(ValueError, match="unknown provider"):
        OpenAICompatProvider("gpt5-turbo-ultra")


# -------------------------------------------------------- build_provider

def test_build_provider_routes_by_name(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API", "test-key")
    assert isinstance(build_provider("deepseek"), OpenAICompatProvider)


def test_build_provider_reaches_anthropic(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = build_provider("anthropic")
    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"


def test_anthropic_import_error_names_the_extra(monkeypatch):
    """The dependency is optional, so the failure must say how to get it
    rather than surfacing a bare ImportError."""
    import builtins
    real_import = builtins.__import__

    def no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    with pytest.raises(RuntimeError, match="uv sync --extra anthropic"):
        AnthropicProvider()


# --------------------------------------------- the Anthropic wire shape

class _Block:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class _Usage:
    input_tokens, output_tokens = 30, 12


class _AnthropicResponse:
    def __init__(self, blocks):
        self.content = blocks
        self.usage = _Usage()


def _anthropic(monkeypatch, response) -> AnthropicProvider:
    pytest.importorskip("anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = AnthropicProvider()
    captured: dict = {}

    def create(**kwargs):
        captured.update(kwargs)
        return response

    provider.client = type("C", (), {"messages": type("M", (), {
        "create": staticmethod(create)})()})()
    provider._captured = captured
    return provider


def test_system_prompt_is_top_level_not_a_message(monkeypatch):
    """Anthropic has no system ROLE; sending one as a message would make
    the instructions read as user text."""
    provider = _anthropic(monkeypatch, _AnthropicResponse([_Block("text", "hi")]))
    provider.complete("SYSTEM RULES", "the user text")
    sent = provider._captured
    assert sent["system"] == "SYSTEM RULES"
    assert sent["messages"] == [{"role": "user", "content": "the user text"}]


def test_thinking_blocks_are_not_concatenated_into_the_json(monkeypatch):
    """`content` is a list of typed blocks. Joining them blindly puts
    reasoning prose in front of the JSON `complete_json` must parse."""
    provider = _anthropic(monkeypatch, _AnthropicResponse([
        _Block("thinking", "let me reason about this at length"),
        _Block("text", '{"ok": true}')]))
    assert provider.complete("sys", "user") == '{"ok": true}'


def test_usage_sums_input_and_output(monkeypatch):
    """There is no `total_tokens` field; the Stage-4 budget reads
    `tokens_spent`, so a missing half silently doubles the budget."""
    provider = _anthropic(monkeypatch, _AnthropicResponse([_Block("text", "x")]))
    provider.complete("sys", "user")
    assert provider.tokens_spent == 42


def test_temperature_is_not_forwarded(monkeypatch):
    """Current Claude models reject sampling parameters alongside
    thinking (400). Callers still pass temperature; it stops here."""
    provider = _anthropic(monkeypatch, _AnthropicResponse([_Block("text", "x")]))
    provider.complete("sys", "user", temperature=0.0)
    assert "temperature" not in provider._captured


def test_effort_is_sent_only_when_asked(monkeypatch):
    provider = _anthropic(monkeypatch, _AnthropicResponse([_Block("text", "x")]))
    provider.complete("sys", "user")
    assert "output_config" not in provider._captured

    provider.effort = "low"
    provider.complete("sys", "user")
    assert provider._captured["output_config"] == {"effort": "low"}


def test_satisfies_the_provider_protocol(monkeypatch):
    """`Provider` is a plain (non-runtime-checkable) Protocol, so this
    checks the members structurally — which is what the call sites in
    agents.py and graphs/ actually depend on."""
    provider = _anthropic(monkeypatch, _AnthropicResponse([_Block("text", "x")]))
    assert isinstance(provider.name, str) and provider.name
    assert isinstance(provider.model, str) and provider.model
    assert provider.tokens_spent == 0.0
    assert callable(provider.complete)
    assert llm.model_fingerprint(provider, "sys").startswith(
        f"anthropic/{provider.model}#")


# ------------------------------------------------------ configured defaults

def test_deepseek_defaults_to_flash():
    """The operator's choice for the chat agent: flash routes tool calls,
    which is constant cheap work rather than a translation batch."""
    assert PROVIDER_PRESETS["deepseek"].default_model == "deepseek-v4-flash"


def test_huggingface_default_is_a_router_qualified_id():
    """Router ids are exactly `org/model`; a bare name 404s at request
    time, which is the wrong place to find out."""
    model = PROVIDER_PRESETS["huggingface"].default_model
    assert model == "Qwen/Qwen3.8-27B"
    assert model.count("/") == 1 and not model.startswith("/")
