"""Reasoning-trace context: record WHY the agent made a tool call, right next
to the call itself in the audit log.

Wrap the calls (or the whole agent step) in `with reasoning("...")` and every
`@guarded` execution started inside the block stores that text in its
`reasoning` column. `tbay log` then shows not just what ran, but the agent's
stated justification for running it, which is usually the first thing a human
approver or a post-incident review wants to know.

Uses a contextvar, so concurrent async tasks and threads each see their own
reasoning text without interfering with each other.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator, Optional

_reasoning: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tbay_reasoning", default=None)
_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("tbay_agent", default=None)


@contextmanager
def reasoning(text: str) -> Iterator[None]:
    """Attach a justification to every guarded call made inside this block.

        with reasoning("customer 42 reported item damaged in transit"):
            refund_customer("cust_42", 30.0)

    Blocks nest; the innermost text wins for calls made inside it and the
    outer text is restored when the inner block exits."""
    token = _reasoning.set(text)
    try:
        yield
    finally:
        _reasoning.reset(token)


def current_reasoning() -> Optional[str]:
    """The reasoning text for the current context, or None outside any block."""
    return _reasoning.get()


@contextmanager
def agent(agent_id: str) -> Iterator[None]:
    """Attach an agent identity to every guarded call made inside this block,
    so the audit log (and anyone approving a paused call) can see WHICH agent
    asked for it, not just which tool ran:

        with agent("billing-agent-7"):
            refund_customer("cust_42", 30.0)

    In a multi-agent system, wrap each agent's turn in its own block. Blocks
    nest like reasoning() blocks, and concurrent async tasks each see their
    own agent id. For a whole process that IS one agent, setting
    TbayClient(agent_id=...) or the TBAY_AGENT_ID environment variable is
    simpler; a surrounding agent() block overrides both."""
    token = _agent.set(agent_id)
    try:
        yield
    finally:
        _agent.reset(token)


def current_agent() -> Optional[str]:
    """The agent id for the current context, or None outside any block."""
    return _agent.get()
