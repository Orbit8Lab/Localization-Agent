"""Provider layer — ported from localization-pipeline `locpipe/providers.py`.

One OpenAI-compatible client covers DeepSeek (default), OpenAI, Qwen, HF and
Gemini; `complete_json` validates against a Pydantic model and re-prompts
ONCE with the validation error before failing hard (never best-effort).

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
from pathlib import Path
from typing import Callable, Optional, Protocol, Type, TypeVar

from pydantic import BaseModel, ValidationError

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
REASONING_HEADROOM = 4096

PROVIDER_PRESETS = {
    # name: (base_url, default_model, api_key_env, is_reasoning)
    "deepseek": (DEEPSEEK_BASE_URL, "deepseek-v4-pro", "DEEPSEEK_API", True),
    "openai": (None, "gpt-4o-mini", "OPENAI_API_KEY", False),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",
             "DASHSCOPE_API_KEY", False),
    "huggingface": ("https://router.huggingface.co/v1",
                    "Qwen/Qwen2.5-72B-Instruct", "HF_API", False),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/",
               "gemini-3.5-flash", "GEMINI_API", True),
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


class OpenAICompatProvider:
    def __init__(self, name: str = "deepseek", model: Optional[str] = None,
                 api_key: Optional[str] = None, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_RETRIES,
                 on_retry: Optional[Callable[[int, str], None]] = None):
        if name not in PROVIDER_PRESETS:
            raise ValueError(
                f"unknown provider {name!r}; choose from {sorted(PROVIDER_PRESETS)}")
        base_url, default_model, key_env, is_reasoning = PROVIDER_PRESETS[name]
        self.name = name
        self.model = model or default_model
        self.is_reasoning = is_reasoning
        self.tokens_spent: float = 0.0
        self.timeout = timeout
        self.max_retries = max_retries
        self.on_retry = on_retry
        key = api_key or os.getenv(key_env)
        if not key:
            autoload_env()
            key = os.getenv(key_env)
        if not key:
            raise RuntimeError(f"no API key: pass api_key, set ${key_env}, or "
                               f"point $ORBIT8_ENV at your .ENV file")
        from openai import OpenAI
        # max_retries=0: we own the retry policy (the SDK's would retry
        # silently, hiding stalls from the caller and the trace).
        self.client = OpenAI(api_key=key, timeout=timeout, max_retries=0,
                             **({"base_url": base_url} if base_url else {}))

    def _with_deadline(self, call):
        """Run ``call`` under a wall-clock ceiling the socket cannot reset.

        The worker is a daemon thread: if the deadline expires we abandon
        it rather than block on a call that has already proven it will
        not return. The caller sees APITimeoutError, so the existing
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
            from openai import APITimeoutError
            raise APITimeoutError(
                request=None) from TimeoutError(
                    f"{self.name}/{self.model}: no response within "
                    f"{deadline:.0f}s wall-clock deadline")
        if "error" in box:
            raise box["error"]
        return box["value"]

    def complete(self, system: str, user: str, *,
                 temperature: float = 0.3, max_tokens: int = 2000) -> str:
        budget = max_tokens
        extra = {}
        if self.is_reasoning:
            budget = max(2048, max_tokens) + REASONING_HEADROOM
            extra["extra_body"] = {"reasoning_effort": "low"}
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._with_deadline(
                    lambda: self.client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                        temperature=temperature, max_tokens=budget, **extra))
            except Exception as err:
                if not _is_transient(err) or attempt == self.max_retries:
                    raise
                last_error = err
                delay = RETRY_BACKOFF ** attempt
                if self.on_retry:
                    self.on_retry(attempt, f"{type(err).__name__}: "
                                           f"{str(err)[:150]}")
                time.sleep(delay)
                continue
            if response.usage:
                self.tokens_spent += response.usage.total_tokens
            return (response.choices[0].message.content or "").strip()
        raise RuntimeError(                       # unreachable in practice
            f"{self.name}/{self.model}: exhausted {self.max_retries} "
            f"attempts: {last_error}")


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
