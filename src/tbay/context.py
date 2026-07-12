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
