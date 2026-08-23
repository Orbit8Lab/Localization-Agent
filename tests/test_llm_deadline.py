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
