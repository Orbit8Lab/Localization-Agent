"""The job assistant: the CLI orchestrator, over HTTP.

This is deliberately NOT a second agent. `ChatOrchestrator` already has
21 tools, a step loop, a circuit breaker, and evidence tiering; building
a parallel one for the browser would guarantee the two drift. The web
endpoint constructs the same orchestrator and calls `turn()`.

Three things the browser adds that a terminal does not need:

1. **Confirmation on consequential tools.** Full parity with the CLI
   including `approve`, but a browser reader may be a producer who does
   not know what a glossary lock is. Consequential tools return a
   description and a token; the action runs when the human confirms.
   See `consent.py` — the model cannot mint a token.
2. **A per-turn spend ceiling.** The CLI operator watches a trace and
   can Ctrl-C. Nobody is watching a browser tab, and the per-finding
   verifier is ~80% of LLM calls.
3. **Session continuity across stateless requests.** The orchestrator
   keeps history in memory; HTTP does not. Sessions are held per job so
   a follow-up ("yes, do that") lands in the same conversation.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..controller import Job
from ..llm import build_provider
from ..orchestrator import ChatOrchestrator
from . import explain
from .consent import CONFIRMS, ConsentLedger

MAX_CHARS = 2000
SESSION_TTL_SECONDS = 3600
# Per turn, not per session: a runaway loop is bounded even if the user
# keeps talking. Roughly a few cents at current prices.
DEFAULT_TURN_TOKEN_CAP = 120_000


class ChatIn(BaseModel):
    message: str
    #: Returned by a previous reply; carries the human's assent for one
    #: specific action. Absent on an ordinary message.
    confirm_token: Optional[str] = None


class ActionOut(BaseModel):
    """A tool the assistant actually ran, surfaced so the UI can show
    what changed rather than only the prose about it."""
    tool: str
    summary: str


class PendingOut(BaseModel):
    token: str
    tool: str
    description: str


class ChatOut(BaseModel):
    reply: str
    model: Optional[str] = None
    actions: List[ActionOut] = Field(default_factory=list)
    #: Set when the assistant wants to do something consequential. The UI
    #: renders a confirm button; nothing has happened yet.
    pending: Optional[PendingOut] = None
    tokens_spent: Optional[float] = None
    #: True when the job's stage may have moved, so the page should refetch.
    job_changed: bool = False


class OpeningOut(BaseModel):
    message: str
    pending_gate: Optional[str] = None
    suggestions: List[str] = Field(default_factory=list)


class _Session:
    """One orchestrator plus its consent ledger, kept between requests."""

    def __init__(self, orchestrator: ChatOrchestrator) -> None:
        self.orchestrator = orchestrator
        self.ledger = ConsentLedger()
        self.touched = time.time()
        self.lock = threading.Lock()


class SessionStore:
    """Per-job orchestrator sessions.

    Not persisted: conversational context is cheap to rebuild (the
    orchestrator reads the job on every tool call) and a stale session
    surviving a restart would answer from a conversation the user no
    longer remembers having.
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, _Session] = {}
        self._lock = threading.Lock()

    def get(self, job: Job, factory) -> _Session:
        key = job.job_id
        with self._lock:
            self._prune()
            session = self._sessions.get(key)
            if session is None:
                session = _Session(factory())
                self._sessions[key] = session
            session.touched = time.time()
            return session

    def drop(self, job_id: str) -> None:
        with self._lock:
            self._sessions.pop(job_id, None)

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, s in self._sessions.items()
                    if now - s.touched > SESSION_TTL_SECONDS]:
            self._sessions.pop(key, None)


def _suggestions(context: Dict) -> List[str]:
    gate = context.get("gate_explainer")
    if gate:
        out = [f"What does {gate['id']} mean?",
               f"What happens if I approve {gate['id']}?"]
        if context.get("glossary_health"):
            out.append("What did the glossary check find?")
        return out
    return ["What is this job doing right now?",
            "What happens next?",
            "What files has it produced?"]


def register(app, get_job, require_user) -> None:
    sessions = SessionStore()
    app.state.chat_sessions = sessions

    def _build(job: Job) -> ChatOrchestrator:
        provider_name = (os.environ.get("ORBIT8_CHAT_PROVIDER")
                         or os.environ.get("ORBIT8_PROVIDER", "deepseek"))
        model = os.environ.get("ORBIT8_CHAT_MODEL") or None
        provider = build_provider(provider_name, model=model)
        return ChatOrchestrator(
            job, provider, operator="console",
            provider_factory=lambda locale: build_provider(
                os.environ.get("ORBIT8_PROVIDER", "deepseek"),
                model=os.environ.get("ORBIT8_MODEL") or None))

    @app.get("/jobs/{job_id}/chat/opening", response_model=OpeningOut,
             dependencies=[Depends(require_user)])
    def opening(job: Job = Depends(get_job)) -> OpeningOut:
        """Server-rendered, no model call: the panel speaks the moment it
        mounts. A spinner before the first word defeats the point for a
        reader who does not know what they are looking at."""
        context = explain.collect(job)
        return OpeningOut(message=explain.opening_message(context),
                          pending_gate=context.get("pending_gate"),
                          suggestions=_suggestions(context))

    @app.post("/jobs/{job_id}/chat", response_model=ChatOut,
              dependencies=[Depends(require_user)])
    def chat(body: ChatIn, job: Job = Depends(get_job),
             operator: Optional[str] = Depends(require_user)) -> ChatOut:
        message = body.message.strip()
        if not message:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty message")
        if len(message) > MAX_CHARS:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"message over {MAX_CHARS} characters")

        try:
            session = sessions.get(job, lambda: _build(job))
        except Exception as err:                        # noqa: BLE001
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                f"chat is not configured on this deployment: {err}")

        if session.lock.locked():
            # One turn at a time per job: two concurrent turns would
            # interleave tool calls against one artifact tree.
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "the assistant is still working on the "
                                "previous message")

        with session.lock:
            agent = session.orchestrator
            # Attribute gate signatures to the authenticated user, not to
            # a generic console identity: an approval carries a name.
            agent.operator = operator or "console"

            before = job.derive()
            actions: List[ActionOut] = []
            pending: Optional[PendingOut] = None

            original_tools = agent._tools
            from .consent import wrap_tools

            def tools_with_consent():
                return wrap_tools(original_tools(), job_id=job.job_id,
                                  ledger=session.ledger,
                                  approved_token=body.confirm_token)

            def on_action(tool: str, observation: str) -> None:
                nonlocal pending
                if observation.startswith("CONFIRMATION_REQUIRED"):
                    token = observation.split("[token=")[-1].rstrip("]")
                    record = session.ledger.peek(token)
                    if record:
                        pending = PendingOut(token=token, tool=record.tool,
                                             description=record.description)
                    return
                actions.append(ActionOut(tool=tool,
                                         summary=observation[:280]))

            agent._tools = tools_with_consent            # type: ignore[method-assign]
            agent.on_action = on_action
            start_tokens = getattr(agent.provider, "tokens_spent", 0.0) or 0.0
            cap = int(os.environ.get("ORBIT8_CHAT_TURN_TOKEN_CAP",
                                     DEFAULT_TURN_TOKEN_CAP))
            try:
                reply = agent.turn(message)
            except Exception as err:                    # noqa: BLE001
                raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                    f"the assistant could not answer: {err}")
            finally:
                agent._tools = original_tools            # type: ignore[method-assign]

            spent = (getattr(agent.provider, "tokens_spent", 0.0) or 0.0)
            turn_tokens = spent - start_tokens
            if turn_tokens > cap:
                # Reported, not silently absorbed: a turn that blew the
                # ceiling is the signal that something looped.
                reply += (f"\n\n_(This turn used {turn_tokens:,.0f} tokens, "
                          f"over the {cap:,} ceiling. Later turns are "
                          f"unaffected, but check what it was doing.)_")

            after = job.derive()
            changed = (before.phase, before.action, before.gate) != (
                after.phase, after.action, after.gate)

            return ChatOut(reply=reply,
                           model=f"{agent.provider.name}/{agent.provider.model}",
                           actions=actions, pending=pending,
                           tokens_spent=round(turn_tokens, 1),
                           job_changed=changed)
