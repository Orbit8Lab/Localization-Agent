"""The wall-clock deadline that bounds a stuck provider call.

httpx timeouts are per socket operation and reset on every byte, so a
server that holds the connection while generating never trips them. A
a live LQA run stalled ~50 minutes on one call against a 120s
timeout, freezing the whole batch job. These tests pin the backstop.
"""
from __future__ import annotations

import time

import pytest
from openai import APITimeoutError

from orbit8.llm import OpenAICompatProvider


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API", "test-key-not-used")
    return OpenAICompatProvider(timeout=1, max_retries=1)   # deadline 3s


def test_deadline_interrupts_a_hung_call(provider):
    """A call that never returns must raise, not hang the run."""
    start = time.time()
    with pytest.raises(APITimeoutError):
        provider._with_deadline(lambda: time.sleep(30))
    assert time.time() - start < 10, "deadline did not fire promptly"


def test_deadline_reports_the_wall_clock_limit(provider):
    """The cause chain must name the deadline, so a stalled batch is
    diagnosable from the log alone."""
    with pytest.raises(APITimeoutError) as caught:
        provider._with_deadline(lambda: time.sleep(30))
    assert "wall-clock deadline" in str(caught.value.__cause__)


def test_normal_call_passes_through(provider):
    """The guard must be invisible to a call that returns in time."""
    assert provider._with_deadline(lambda: "result") == "result"


def test_slow_but_finishing_call_is_not_cut_off(provider):
    """Real T3 batches take 100s+. The deadline is a multiple of the
    socket timeout precisely so slow-but-healthy calls survive."""
    assert provider._with_deadline(lambda: (time.sleep(1.5), "ok")[1]) == "ok"


def test_deadline_scales_with_timeout(monkeypatch):
    """A caller raising the socket timeout raises the ceiling with it."""
    monkeypatch.setenv("DEEPSEEK_API", "test-key-not-used")
    slow = OpenAICompatProvider(timeout=10, max_retries=1)   # deadline 30s
    start = time.time()
    assert slow._with_deadline(lambda: "fast") == "fast"
    assert time.time() - start < 1


def test_exceptions_propagate_unchanged(provider):
    """A real API error must not be masked as a timeout."""
    def boom():
        raise ValueError("bad request")
    with pytest.raises(ValueError, match="bad request"):
        provider._with_deadline(boom)


def test_the_deadline_is_absolutely_capped(monkeypatch):
    """A multiple of the timeout scales with the thing it is supposed to
    bound. Raising a reasoning provider's timeout to 300s took the
    deadline to 900s, and at 3 retries that is 45 minutes of hang per
    call rather than a guard — a real scan then sat for 27 HOURS at 0%
    CPU with no open sockets, which is precisely the failure the deadline
    exists to prevent.
    """
    monkeypatch.setenv("DEEPSEEK_API", "test-key")
    from orbit8.llm import (DEADLINE_CEILING, DEADLINE_FACTOR,
                            OpenAICompatProvider)

    # A long reasoning timeout must NOT buy a proportionally long hang.
    slow = OpenAICompatProvider("deepseek", timeout=300)
    assert min(slow.timeout * DEADLINE_FACTOR,
               DEADLINE_CEILING) == DEADLINE_CEILING

    # A short timeout still gets the multiple — the cap is a ceiling, not
    # a floor, so a fast provider keeps failing fast.
    quick = OpenAICompatProvider("deepseek", timeout=10)
    assert min(quick.timeout * DEADLINE_FACTOR,
               DEADLINE_CEILING) == 10 * DEADLINE_FACTOR


def test_a_long_timeout_still_fires_the_deadline(monkeypatch):
    """The end-to-end property: a hung call under a 300s timeout must
    still be abandoned, and within the ceiling rather than 900s."""
    monkeypatch.setenv("DEEPSEEK_API", "test-key")
    monkeypatch.setattr("orbit8.llm.DEADLINE_CEILING", 2.0)
    from orbit8.llm import OpenAICompatProvider

    provider = OpenAICompatProvider("deepseek", timeout=300, max_retries=1)
    start = time.time()
    with pytest.raises(APITimeoutError):
        provider._with_deadline(lambda: time.sleep(60))
    assert time.time() - start < 10, "deadline ignored the ceiling"
