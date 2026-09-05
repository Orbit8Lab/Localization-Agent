"""Timeout + bounded retry policy on the provider layer."""
import pytest

from orbit8 import llm
from orbit8.llm import OpenAICompatProvider, _is_transient


class _Timeout(Exception):
    pass


_Timeout.__name__ = "APITimeoutError"


class _Status(Exception):
    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_is_transient_classification():
    assert _is_transient(_Timeout("read timed out"))
    assert _is_transient(_Status(429))          # rate limited
    assert _is_transient(_Status(503))          # server error
    assert not _is_transient(_Status(400))      # bad request — fail fast
    assert not _is_transient(_Status(401))      # bad key — fail fast
    assert not _is_transient(ValueError("nope"))


class _FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeResponse:
    class _Msg:
        content = '{"ok": true}'

    class _Choice:
        message = _FakeResponse._Msg() if False else None

    def __init__(self):
        choice = type("C", (), {"message": type("M", (), {
            "content": '{"ok": true}'})()})()
        self.choices = [choice]
        self.usage = type("U", (), {"total_tokens": 42})()


def _provider(monkeypatch, script, **kwargs) -> OpenAICompatProvider:
    monkeypatch.setenv("DEEPSEEK_API", "test-key")
    monkeypatch.setattr(llm.time, "sleep", lambda _s: None)   # no waiting
    provider = OpenAICompatProvider("deepseek", **kwargs)
    fake = _FakeCompletions(script)
    provider.client = type("C", (), {"chat": type("Ch", (), {
        "completions": fake})()})()
    provider._fake = fake
    return provider


def test_retries_transient_then_succeeds(monkeypatch):
    provider = _provider(monkeypatch,
                         [_Timeout("t1"), _Timeout("t2"), _FakeResponse()])
    seen = []
    provider.on_retry = lambda attempt, msg: seen.append(attempt)
    assert provider.complete("sys", "user") == '{"ok": true}'
    assert provider._fake.calls == 3
    assert seen == [1, 2]                    # two retries reported
    assert provider.tokens_spent == 42


def test_gives_up_after_max_retries(monkeypatch):
    provider = _provider(monkeypatch,
                         [_Timeout("t"), _Timeout("t"), _Timeout("t")],
                         max_retries=3)
    with pytest.raises(Exception) as excinfo:
        provider.complete("sys", "user")
    assert "APITimeoutError" in type(excinfo.value).__name__ or \
        "t" in str(excinfo.value)
    assert provider._fake.calls == 3         # bounded, never infinite


def test_non_transient_fails_immediately(monkeypatch):
    provider = _provider(monkeypatch, [_Status(400), _FakeResponse()])
    with pytest.raises(_Status):
        provider.complete("sys", "user")
    assert provider._fake.calls == 1         # no pointless retry


def test_timeout_is_configured_and_overridable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API", "test-key")
    default = OpenAICompatProvider("deepseek")
    assert default.timeout == llm.DEFAULT_TIMEOUT == 120
    custom = OpenAICompatProvider("deepseek", timeout=30, max_retries=1)
    assert custom.timeout == 30 and custom.max_retries == 1


def test_a_stalled_call_does_not_block_interpreter_exit():
    """`_with_deadline` used a ThreadPoolExecutor, which registers an
    atexit hook that JOINS every worker it created — and
    `shutdown(wait=False)` does not exempt them. A call stuck in
    `_ssl__SSLSocket_read` therefore survived the deadline (the retry
    proceeded correctly) and then blocked interpreter EXIT with no
    timeout of its own.

    Observed on a real audit: the cascade finished, the report was never
    written, and the process sat over three hours at ~0 CPU with two
    abandoned SSL reads pinned open. The deadline worked; the exit did
    not.

    Run in a SUBPROCESS because that is the only place the atexit
    behaviour is observable.
    """
    import subprocess
    import sys
    import textwrap
    import time

    code = textwrap.dedent("""
        import time
        from orbit8.llm import OpenAICompatProvider
        provider = OpenAICompatProvider.__new__(OpenAICompatProvider)
        provider.name, provider.model, provider.timeout = "t", "m", 1.0
        try:
            provider._with_deadline(lambda: time.sleep(60))
        except Exception:
            pass
        print("done")
    """)
    started = time.time()
    result = subprocess.run([sys.executable, "-c", code],
                            capture_output=True, text=True, timeout=45)
    elapsed = time.time() - started
    assert "done" in result.stdout
    # The abandoned worker sleeps 60s; exiting must not wait for it.
    assert elapsed < 20, f"interpreter waited {elapsed:.0f}s on a dead call"
