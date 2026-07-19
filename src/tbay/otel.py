"""Optional OpenTelemetry bridge: one span per guarded tool call.

tbay itself depends on nothing here; this module imports OpenTelemetry only
when you call instrument(), so `pip install tbay[otel]` (or any project that
already ships opentelemetry-api) is all it takes:

    from tbay.otel import instrument

    client = TbayClient(db_url)
    instrument(client)

Every guarded call then produces a span named "tbay.<tool_name>" carrying
the policy, tenant, agent id, execution id, and outcome as attributes.
Executions this process runs span their real duration (including any wait
for human approval); decisions that never run anything (cache hits, kill
switch blocks, rate/budget refusals, followed singleflights) become
zero-ish-duration spans so they still show up in your traces, because "the
call didn't happen and here's why" is exactly what you want to see next to
the LLM spans your agent framework already emits.

Spans parent to whatever span is current in the calling context, so tbay's
spans nest under your agent framework's traces automatically.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from . import events as ev
from .events import Event, Handler


def instrument(client: Any, tracer: Optional[Any] = None) -> Handler:
    """Subscribe an event handler on `client` that mirrors tbay's lifecycle
    events into OpenTelemetry spans. Returns the handler; pass it to
    client.off() to uninstrument. Raises ImportError with install advice if
    OpenTelemetry isn't available."""
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise ImportError(
            "tbay.otel needs the opentelemetry-api package; install it with `pip install tbay[otel]` "
            "(and an SDK + exporter of your choice, e.g. opentelemetry-sdk)"
        ) from exc

    tracer = tracer or trace.get_tracer("tbay")

    # Spans for executions this process owns, opened at CALL_STARTED and
    # closed at CALL_SUCCEEDED / CALL_FAILED / APPROVAL_REJECTED.
    open_spans: Dict[str, Any] = {}

    def _attributes(event: Event) -> Dict[str, Any]:
        attrs = {
            "tbay.event": event.type,
            "tbay.tool": event.tool_name or "",
            "tbay.tenant": event.tenant or "",
        }
        if event.policy:
            attrs["tbay.policy"] = event.policy
        if event.execution_id:
            attrs["tbay.execution_id"] = event.execution_id
        if event.agent_id:
            attrs["tbay.agent_id"] = event.agent_id
        for key, value in event.data.items():
            if isinstance(value, (str, bool, int, float)):
                attrs[f"tbay.{key}"] = value
        return attrs

    terminal = frozenset({ev.CALL_SUCCEEDED, ev.CALL_FAILED, ev.APPROVAL_REJECTED})

    def handler(event: Event) -> None:
        name = f"tbay.{event.tool_name}"

        if event.type == ev.CALL_STARTED and event.execution_id:
            span = tracer.start_span(name, attributes=_attributes(event))
            open_spans[event.execution_id] = span
            return

        span = open_spans.get(event.execution_id) if event.execution_id else None
        if span is not None:
            if event.type not in terminal:
                # mid-flight milestones (approval requested/approved) become
                # span events on the call's own span
                span.add_event(event.type)
                return
            del open_spans[event.execution_id]
            for key, value in _attributes(event).items():
                span.set_attribute(key, value)
            if event.type in (ev.CALL_FAILED, ev.APPROVAL_REJECTED):
                span.set_status(Status(StatusCode.ERROR, str(event.data.get("error") or event.type)))
            else:
                span.set_status(Status(StatusCode.OK))
            span.end()
            return

        # Decisions with no owned execution in this process: record them as
        # their own short spans so cache hits and refusals are visible too.
        with tracer.start_as_current_span(name, attributes=_attributes(event)) as short:
            if event.type in (
                ev.CALL_FAILED,
                ev.RATE_LIMITED,
                ev.BUDGET_EXCEEDED,
                ev.CONCURRENCY_BLOCKED,
                ev.KILL_SWITCH_BLOCKED,
            ):
                short.set_status(Status(StatusCode.ERROR, event.type))

    return client.on(handler)
