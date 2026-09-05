"""Splitting the chat agent's model from the work it drives.

`orbit8 chat --provider deepseek --work-provider huggingface` runs the
agent's reasoning on one vendor and the language work on another. Two
things must hold: the default stays single-provider, and an explicit
`--api-key` never reaches a vendor it was not issued for.
"""
from __future__ import annotations

from orbit8.cli import main, resolve_work_provider


class _Args:
    """The parsed-args surface `_cmd_chat` reads for provider wiring."""
    def __init__(self, **kw):
        self.provider = "deepseek"
        self.model = None
        self.work_provider = None
        self.work_model = None
        self.api_key = None
        self.__dict__.update(kw)


_resolve = resolve_work_provider     # the real thing, not a copy


def test_work_defaults_to_the_chat_provider():
    assert _resolve(_Args()) == ("deepseek", None, None)


def test_work_model_alone_applies_to_the_same_provider():
    assert _resolve(_Args(work_model="deepseek-v4-pro")) == (
        "deepseek", "deepseek-v4-pro", None)


def test_the_configured_split():
    """The operator's setup: flash drives the agent, Qwen does the work."""
    assert _resolve(_Args(
        provider="deepseek",
        work_provider="huggingface",
        work_model="Qwen/Qwen3.8-27B")) == (
            "huggingface", "Qwen/Qwen3.8-27B", None)


def test_chat_model_does_not_leak_into_the_work_provider():
    """`--model` qualifies `--provider`. Handing a DeepSeek model id to
    HuggingFace would 404 at request time, hours in."""
    provider, model, _ = _resolve(_Args(
        model="deepseek-v4-flash", work_provider="huggingface"))
    assert provider == "huggingface" and model is None


def test_api_key_never_crosses_vendors():
    """The real hazard: `--api-key` is one vendor's secret. Forwarding it
    to another service would send that service someone else's credential."""
    _, _, key = _resolve(_Args(api_key="sk-deepseek-secret",
                               work_provider="huggingface"))
    assert key is None


def test_api_key_still_applies_within_one_vendor():
    _, _, key = _resolve(_Args(api_key="sk-deepseek-secret"))
    assert key == "sk-deepseek-secret"


def test_chat_exposes_both_provider_pairs():
    """The flags must exist and be discoverable, or the split is
    unreachable without editing code."""
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.suppress(SystemExit):
        main(["chat", "--help"])
    text = out.getvalue()
    for flag in ("--provider", "--model", "--work-provider", "--work-model"):
        assert flag in text
