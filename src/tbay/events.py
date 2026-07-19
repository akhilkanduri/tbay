"""Structured lifecycle events: every decision tbay makes, as it happens.

The audit log answers "what happened" after the fact; events answer it live,
in-process, so you can wire tbay into whatever observability stack you
already run without tbay depending on any of it:

    client = TbayClient(db_url)

    @client.on
    def print_everything(event):
        print(event.type, event.tool_name, event.data)

    @client.on(events=[CALL_FAILED, BUDGET_EXCEEDED, KILL_SWITCH_BLOCKED])
    def page_someone(event):
        alerting.notify(f"tbay: {event.type} on {event.tool_name}")

Handlers run synchronously in the calling process, in subscription order.
A handler that raises never breaks the guarded call: the exception is
logged to the `tbay` logger and swallowed, because observability must never
take the workload down with it. Keep handlers fast (hand off to a queue or
thread if they do I/O); a slow handler slows the tool call it observes.

For OpenTelemetry spans built on these events, see `tbay.otel.instrument`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("tbay")

# -- event types --

CALL_STARTED = "call.started"  # this process became the owner and will run the function
CALL_SUCCEEDED = "call.succeeded"  # the function returned; data: duration_s
CALL_FAILED = "call.failed"  # the function raised, or a stored failure was replayed; data: error, replayed
CACHE_HIT = "cache.hit"  # an unexpired stored result was returned instead of running
SEMANTIC_CACHE_HIT = "cache.semantic_hit"  # a similar-enough previous call's result was returned
SINGLEFLIGHT_COALESCED = "singleflight.coalesced"  # another caller owns this key; we followed its result
APPROVAL_REQUESTED = "approval.requested"  # the call paused, waiting for a human
APPROVAL_APPROVED = "approval.approved"  # a human approved; the call proceeds
APPROVAL_REJECTED = "approval.rejected"  # a human rejected (or the signature failed); data: note
RATE_LIMITED = "limit.rate"  # the policy's rate_limit refused the call
BUDGET_EXCEEDED = "limit.budget"  # the policy's budget cap refused the call; data: spent, budget_max
CONCURRENCY_BLOCKED = "limit.concurrency"  # no max_concurrent slot freed up in time
KILL_SWITCH_BLOCKED = "killswitch.blocked"  # a pause control refused the call; data: scope, reason

ALL_EVENTS = frozenset(
    {
        CALL_STARTED,
        CALL_SUCCEEDED,
        CALL_FAILED,
        CACHE_HIT,
        SEMANTIC_CACHE_HIT,
        SINGLEFLIGHT_COALESCED,
        APPROVAL_REQUESTED,
        APPROVAL_APPROVED,
        APPROVAL_REJECTED,
        RATE_LIMITED,
        BUDGET_EXCEEDED,
        CONCURRENCY_BLOCKED,
        KILL_SWITCH_BLOCKED,
    }
)


@dataclass
class Event:
    """One thing tbay decided or observed about one tool call."""

    type: str
    tool_name: Optional[str] = None
    execution_id: Optional[str] = None
    tenant: str = ""
    policy: Optional[str] = None
    agent_id: Optional[str] = None  # which agent asked (from `with tbay.agent(...)` or the client default)
    reasoning: Optional[str] = None  # the agent's stated justification, if inside `with tbay.reasoning(...)`
    data: Dict[str, Any] = field(default_factory=dict)  # event-specific extras, documented per type above
    ts: float = field(default_factory=time.time)


Handler = Callable[[Event], None]


class EventBus:
    """Synchronous fan-out of Events to subscribed handlers, with handler
    failures isolated so observability can never break execution."""

    def __init__(self) -> None:
        self._subscribers: List[Tuple[Optional[frozenset], Handler]] = []

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subscribers)

    def subscribe(self, handler: Handler, events: Optional[Iterable[str]] = None) -> Handler:
        """Register `handler` for every event (default) or just the given
        event types. Returns the handler, so it works as a decorator."""
        wanted = None
        if events is not None:
            wanted = frozenset(events)
            unknown = wanted - ALL_EVENTS
            if unknown:
                raise ValueError(f"unknown event type(s) {sorted(unknown)}; known: {sorted(ALL_EVENTS)}")
        self._subscribers.append((wanted, handler))
        return handler

    def unsubscribe(self, handler: Handler) -> None:
        self._subscribers = [(wanted, h) for wanted, h in self._subscribers if h is not handler]

    def emit(self, event: Event) -> None:
        for wanted, handler in list(self._subscribers):
            if wanted is not None and event.type not in wanted:
                continue
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - a handler must never break the guarded call
                logger.exception("tbay event handler %r raised on %s; ignoring", handler, event.type)
