"""Provider layer — ported from localization-pipeline `locpipe/providers.py`.

One OpenAI-compatible client covers DeepSeek (default), OpenAI, Qwen, HF
and Gemini, driven by the `PROVIDER_PRESETS` table; Anthropic speaks a
different wire shape and gets its own class. Both satisfy the same narrow
`Provider` protocol, so nothing above this module knows which vendor ran.
`complete_json` validates against a Pydantic model and re-prompts ONCE with
the validation error before failing hard (never best-effort).

Additions for Orbit8: cumulative token accounting (the Stage-4 budget reads
it) and `model_fingerprint` (model id + prompt hash) stamped into artifact
envelopes so any output is attributable months later (design §3).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (Callable, Dict, Optional, Protocol, Type,
                    TypeVar)

from pydantic import BaseModel, ValidationError

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
REASONING_HEADROOM = 4096


@dataclass(frozen=True)
class Preset:
    """One vendor's calling convention.

    A dataclass rather than a tuple because the positional unpack this
    replaced broke silently the moment a field was added — and the field
    that mattered was per-vendor: `reasoning_effort` is DeepSeek's spelling
    of "think less", not a portable parameter. Gemini's OpenAI-compat
    endpoint rejects it, so a shared boolean could only ever be right for
    one of them.

    ``extra_body`` returns the vendor-specific body additions (or None),
    and ``headroom`` is the token allowance a reasoning model needs on top
    of the caller's budget for tokens the caller never sees.
    """
    base_url: Optional[str]
    default_model: str
    api_key_env: str
    extra_body: Optional[Callable[[], dict]] = None
    headroom: int = 0

    def body(self) -> dict:
        """The kwargs to merge into a chat-completions call."""
        extra = self.extra_body() if self.extra_body else None
        return {"extra_body": extra} if extra else {}


PROVIDER_PRESETS: Dict[str, Preset] = {
    # DeepSeek's reasoning models bill hidden reasoning tokens against
    # max_tokens, hence the headroom.
    "deepseek": Preset(DEEPSEEK_BASE_URL, "deepseek-v4-pro", "DEEPSEEK_API",
                       extra_body=lambda: {"reasoning_effort": "low"},
                       headroom=REASONING_HEADROOM),
    "openai": Preset(None, "gpt-4o-mini", "OPENAI_API_KEY"),
    "qwen": Preset("https://dashscope.aliyuncs.com/compatible-mode/v1",
                   "qwen-plus", "DASHSCOPE_API_KEY"),
    # Router model ids are strictly `org/model`; the default here is a
    # starting point, not a recommendation — pass --model explicitly.
    "huggingface": Preset("https://router.huggingface.co/v1",
                          "Qwen/Qwen2.5-72B-Instruct", "HF_API"),
    # NO extra_body: Gemini's OpenAI-compat surface rejects
    # `reasoning_effort`, so sending it 400s every call. Thinking is
    # configured on Gemini's native API, not through this shim.
    "gemini": Preset("https://generativelanguage.googleapis.com/v1beta/openai/",
                     "gemini-3.5-flash", "GEMINI_API"),
    # Anthropic has no OpenAI-compatible endpoint; `anthropic` is served by
    # AnthropicProvider below and is deliberately absent from this table.
}

M = TypeVar("M", bound=BaseModel)


def autoload_env() -> None:
    """Best-effort .ENV discovery: $ORBIT8_ENV, then ./.ENV, then ../.ENV.
    Keys live OUTSIDE the repo — never in config files, never committed."""
    candidates = []
    if os.getenv("ORBIT8_ENV"):
        candidates.append(Path(os.environ["ORBIT8_ENV"]))
    candidates += [Path(".ENV"), Path(".env"), Path("../.ENV")]
    for candidate in candidates:
        try:
            if not candidate.exists():
                continue
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key and value and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


class Provider(Protocol):
    name: str
    model: str
    tokens_spent: float

    def complete(self, system: str, user: str, *,
                 temperature: float = 0.3, max_tokens: int = 2000) -> str: ...


def model_fingerprint(provider: Provider, system_prompt: str) -> str:
    """model id + prompt hash — the reproducibility anchor for agent output."""
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    return f"{provider.name}/{provider.model}#{digest}"


# A request that never returns must not hang a batch job forever: an
# API can accept the socket and then go silent. Timeouts and bounded
# retries turn "hung overnight" into "one failed batch, reported".
DEFAULT_TIMEOUT = float(os.getenv("ORBIT8_LLM_TIMEOUT", "120"))
DEFAULT_RETRIES = int(os.getenv("ORBIT8_LLM_RETRIES", "3"))
RETRY_BACKOFF = 2.0                   # seconds: 2, 4, 8 …

# The httpx timeout above is per-SOCKET-OPERATION, not per request: it
# resets on every byte received. A server that dribbles output, or holds
# the connection while generating, can therefore run indefinitely without
# ever tripping it — observed in the wild as a single call stalling ~50
# minutes against a 120s timeout, with the whole batch job frozen behind
# it. DEADLINE is the wall-clock ceiling that socket behaviour cannot
# reset. Keep it a comfortable multiple of the socket timeout: it is the
# backstop for a stuck call, not the normal path for a slow one.
DEADLINE_FACTOR = float(os.getenv("ORBIT8_LLM_DEADLINE_FACTOR", "3.0"))

# Retry only what a retry can fix: timeouts, dropped connections, 429s
# and 5xx. A 400/401 (bad request, bad key) fails fast — retrying it just
# burns the clock.
_TRANSIENT_NAMES = ("APITimeoutError", "APIConnectionError",
                    "RateLimitError", "InternalServerError",
                    "APIStatusError", "Timeout", "ConnectionError",
                    "ReadTimeout")


def _is_transient(err: Exception) -> bool:
    status = getattr(err, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return type(err).__name__ in _TRANSIENT_NAMES


class _ResilientProvider:
    """The parts of "call a model" that are not vendor-specific.

    Subclasses implement ``_call`` (one request, no retries) and inherit
    the two things that took a production incident each to get right: the
    transient-only retry loop and the wall-clock deadline below. Sharing
    them is the point — a second provider that quietly reimplemented
    ``complete`` would reopen the exact hangs those comments describe, and
    the omission would look like nothing at all until a batch stalled
    overnight.
    """
    name: str
    model: str

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_RETRIES,
                 on_retry: Optional[Callable[[int, str], None]] = None):
        self.tokens_spent: float = 0.0
        self.timeout = timeout
        self.max_retries = max_retries
        self.on_retry = on_retry

    # ------------------------------------------------------------ vendor

    def _call(self, system: str, user: str, *, temperature: float,
              max_tokens: int) -> str:
        """ONE request. Raise on failure; the retry loop owns recovery.

        Implementations must add whatever they consumed to
        ``self.tokens_spent`` — the Stage-4 token budget reads it.
        """
        raise NotImplementedError

    # ------------------------------------------------------------ shared

    def _resolve_key(self, api_key: Optional[str], key_env: str) -> str:
        key = api_key or os.getenv(key_env)
        if not key:
            autoload_env()
            key = os.getenv(key_env)
        if not key:
            raise RuntimeError(f"no API key: pass api_key, set ${key_env}, or "
                               f"point $ORBIT8_ENV at your .ENV file")
        return key

    def _with_deadline(self, call):
        """Run ``call`` under a wall-clock ceiling the socket cannot reset.

        The worker is a daemon thread: if the deadline expires we abandon
        it rather than block on a call that has already proven it will
        not return. The caller sees a timeout error, so the existing
        retry policy handles it like any other timeout.
        """
        import threading

        deadline = self.timeout * DEADLINE_FACTOR
        box: dict = {}

        def run():
            try:
                box["value"] = call()
            except BaseException as err:        # re-raised on the caller
                box["error"] = err

        # A RAW DAEMON THREAD, not a ThreadPoolExecutor.
        #
        # ThreadPoolExecutor registers an atexit hook that JOINS every
        # worker it ever created, and `shutdown(wait=False)` does not
        # exempt them. A call stuck in _ssl__SSLSocket_read therefore
        # survives the deadline — the retry proceeds correctly — and then
        # blocks interpreter EXIT indefinitely, with no timeout of its
        # own. Observed on a real audit: the cascade finished, the report
        # was never written, and the process sat for over three hours at
        # ~0 CPU with two abandoned SSL reads pinned open.
        #
        # A daemon thread is abandoned for real: the interpreter does not
        # wait for it, so a stalled request costs one leaked thread and a
        # socket the OS reclaims at exit, instead of the whole run.
        worker = threading.Thread(target=run, daemon=True,
                                  name="orbit8-llm")
        worker.start()
        worker.join(timeout=deadline)
        if worker.is_alive():
            self._raise_timeout(
                f"{self.name}/{self.model}: no response within "
                f"{deadline:.0f}s wall-clock deadline")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _raise_timeout(self, message: str) -> None:
        """Raise the error a blown deadline reports.

        Vendor-specific so that `_is_transient` classifies it as the SDK's
        own timeout would, and so a caller catching that SDK's error type
        still catches this.
        """
        from openai import APITimeoutError
        raise APITimeoutError(request=None) from TimeoutError(message)

    def complete(self, system: str, user: str, *,
                 temperature: float = 0.3, max_tokens: int = 2000) -> str:
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._call(system, user, temperature=temperature,
                                  max_tokens=max_tokens)
            except Exception as err:
                if not _is_transient(err) or attempt == self.max_retries:
                    raise
                last_error = err
                delay = RETRY_BACKOFF ** attempt
                if self.on_retry:
                    self.on_retry(attempt, f"{type(err).__name__}: "
                                           f"{str(err)[:150]}")
                time.sleep(delay)
        raise RuntimeError(                       # unreachable in practice
            f"{self.name}/{self.model}: exhausted {self.max_retries} "
            f"attempts: {last_error}")


class OpenAICompatProvider(_ResilientProvider):
    def __init__(self, name: str = "deepseek", model: Optional[str] = None,
                 api_key: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_RETRIES,
                 on_retry: Optional[Callable[[int, str], None]] = None):
        if name not in PROVIDER_PRESETS:
            raise ValueError(
                f"unknown provider {name!r}; choose from {sorted(PROVIDER_PRESETS)}")
        super().__init__(timeout=timeout, max_retries=max_retries,
                         on_retry=on_retry)
        preset = PROVIDER_PRESETS[name]
        self.name = name
        self.model = model or preset.default_model
        self.preset = preset
        key = self._resolve_key(api_key, preset.api_key_env)
        from openai import OpenAI
        # max_retries=0: we own the retry policy (the SDK's would retry
        # silently, hiding stalls from the caller and the trace).
        self.client = OpenAI(api_key=key, timeout=timeout, max_retries=0,
                             **({"base_url": preset.base_url}
                                if preset.base_url else {}))

    def _call(self, system: str, user: str, *, temperature: float,
              max_tokens: int) -> str:
        budget = max_tokens
        if self.preset.headroom:
            # Reasoning tokens are billed against max_tokens but never
            # reach the caller, so the visible budget needs room on top.
            budget = max(2048, max_tokens) + self.preset.headroom
        extra = self.preset.body()
        response = self._with_deadline(
            lambda: self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=temperature, max_tokens=budget, **extra))
        if response.usage:
            self.tokens_spent += response.usage.total_tokens
        return (response.choices[0].message.content or "").strip()


# Anthropic's API is not OpenAI-compatible and Anthropic's own guidance is
# not to reach for a compatibility shim, so it gets a real client. Four
# shape differences, all handled here so nothing above this module sees
# them: the system prompt is a top-level parameter rather than a message,
# usage splits into input/output instead of a total, `content` is a list
# of typed blocks rather than a string, and thinking is configured with
# `thinking`/`effort` rather than `reasoning_effort`.
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider(_ResilientProvider):
    """Claude via the official `anthropic` SDK (an optional dependency).

    Install with ``uv sync --extra anthropic``. The import is lazy, exactly
    as `openai` is above, so the package stays installable — and every
    other provider stays usable — without it.
    """

    def __init__(self, model: Optional[str] = None,
                 api_key: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_RETRIES,
                 on_retry: Optional[Callable[[int, str], None]] = None,
                 effort: Optional[str] = None):
        super().__init__(timeout=timeout, max_retries=max_retries,
                         on_retry=on_retry)
        self.name = "anthropic"
        self.model = model or ANTHROPIC_DEFAULT_MODEL
        # Thinking is on by default on current models; `effort` trades
        # depth for spend. Left unset the model's own default applies.
        self.effort = effort
        key = self._resolve_key(api_key, ANTHROPIC_KEY_ENV)
        try:
            from anthropic import Anthropic
        except ImportError as err:               # optional dependency
            raise RuntimeError(
                "the anthropic provider needs the `anthropic` package: "
                "uv sync --extra anthropic") from err
        # max_retries=0 for the same reason as the OpenAI client: the
        # retry policy is ours, and a silent SDK retry hides a stall.
        self.client = Anthropic(api_key=key, timeout=timeout, max_retries=0)

    def _raise_timeout(self, message: str) -> None:
        from anthropic import APITimeoutError
        raise APITimeoutError(request=None) from TimeoutError(message)

    def _call(self, system: str, user: str, *, temperature: float,
              max_tokens: int) -> str:
        extra: dict = {}
        if self.effort:
            extra["output_config"] = {"effort": self.effort}
        # NOTE: temperature is deliberately dropped. Current Claude models
        # reject sampling parameters alongside thinking (400), and the
        # callers here use temperature only to ask for determinism —
        # which structured output plus `complete_json`'s validation
        # already enforce far more strictly than temperature=0 ever did.
        response = self._with_deadline(
            lambda: self.client.messages.create(
                model=self.model, max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                **extra))
        usage = getattr(response, "usage", None)
        if usage:
            self.tokens_spent += (getattr(usage, "input_tokens", 0)
                                  + getattr(usage, "output_tokens", 0))
        # `content` is a list of typed blocks; thinking blocks come back
        # alongside text and must not be concatenated into the JSON the
        # caller is about to parse.
        parts = [block.text for block in (response.content or [])
                 if getattr(block, "type", None) == "text"]
        return "".join(parts).strip()


# Every provider name the CLI accepts. `anthropic` is not in
# PROVIDER_PRESETS because that table describes ONE wire protocol it does
# not speak; keeping it out of the table and in this list is what lets
# `build_provider` stay a lookup rather than a special case at 13 call
# sites.
PROVIDER_NAMES = sorted(PROVIDER_PRESETS) + ["anthropic"]


def build_provider(name: str = "deepseek", model: Optional[str] = None,
                   api_key: Optional[str] = None, **kwargs) -> Provider:
    """Construct the provider called ``name``.

    The single place that knows which vendors need which client. Callers
    pass the operator's `--provider` through and get something satisfying
    `Provider` back.
    """
    if name == "anthropic":
        return AnthropicProvider(model=model, api_key=api_key, **kwargs)
    return OpenAICompatProvider(name, model=model, api_key=api_key, **kwargs)


class EchoProvider:
    """Dry-run provider: `[lang]source` stubs, zero API calls. Stubs are
    recognizable downstream so they never enter the TM (memory.py guard)."""
    name = "echo"
    model = "dry-run"

    def __init__(self, target_lang: str):
        self.target_lang = target_lang
        self.tokens_spent: float = 0.0

    def complete(self, system: str, user: str, *,
                 temperature: float = 0.3, max_tokens: int = 2000) -> str:
        blocks = re.findall(r"^### (\S+)\n(.*?)(?=\n### |\Z)", user,
                            flags=re.M | re.S)
        items = [{"key": k, "target_text": f"[{self.target_lang}]{v.strip()}",
                  "term_decisions": {}, "notes": None}
                 for k, v in blocks if k != "END"]
        return json.dumps({"items": items}, ensure_ascii=False)


def extract_json(text: str) -> str:
    """Defensively pull the first JSON object/array out of a completion."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0),
                default=-1)
    if start < 0:
        raise ValueError("no JSON object found in completion")
    opener, closer = text[start], {"{": "}", "[": "]"}[text[start]]
    depth, in_str, escape = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    raise ValueError("unbalanced JSON in completion")


def complete_json(provider: Provider, system: str, user: str,
                  model_cls: Type[M], *, temperature: float = 0.3,
                  max_tokens: int = 2000) -> M:
    """Completion validated against ``model_cls``; ONE repair re-prompt,
    then hard fail (docs/agents/README.md rule 1)."""
    raw = provider.complete(system, user, temperature=temperature,
                            max_tokens=max_tokens)
    budget = max_tokens
    for attempt in (1, 2):
        try:
            return model_cls.model_validate_json(extract_json(raw))
        except (ValueError, ValidationError) as err:
            if attempt == 2:
                # The completion itself is the evidence — without it the
                # caller cannot tell a truncation from a refusal from a
                # prose reply.
                tail = (raw or "").strip()
                detail = (f"empty completion (no content returned)"
                          if not tail else
                          f"completion did not parse; last 300 chars: "
                          f"…{tail[-300:]!r}")
                raise RuntimeError(
                    f"{provider.name}/{provider.model}: output failed "
                    f"{model_cls.__name__} validation twice: {err} "
                    f"[{detail}]") from err
            # A missing/unbalanced JSON object is usually TRUNCATION, so
            # the repair attempt gets more room, not the same budget.
            if "no JSON object found" in str(err) or "unbalanced" in str(err):
                budget = min(max_tokens * 2, 16000)
            repair_user = (
                f"{user}\n\nYour previous response was invalid:\n{err}\n\n"
                f"Respond again with ONLY the corrected JSON object, no prose.")
            raw = provider.complete(system, repair_user,
                                    temperature=0.0, max_tokens=budget)
