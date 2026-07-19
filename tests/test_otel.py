"""The optional OpenTelemetry bridge: one span per guarded call, error status
on failures, short spans for decisions that never ran anything."""
import pytest

otel_sdk = pytest.importorskip("opentelemetry.sdk.trace")

from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from tbay import guarded  # noqa: E402
from tbay.otel import instrument  # noqa: E402


@pytest.fixture
def spans_and_client(client):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrument(client, tracer=provider.get_tracer("tbay-test"))
    return exporter, client


def test_success_and_cache_hit_spans(spans_and_client):
    exporter, client = spans_and_client

    @guarded(client, policy="readonly")
    def lookup(q: str) -> dict:
        return {"answer": q}

    lookup("x")
    lookup("x")
    spans = exporter.get_finished_spans()
    assert [s.name for s in spans] == ["tbay.lookup", "tbay.lookup"]
    executed, cached = spans
    assert executed.attributes["tbay.policy"] == "readonly"
    assert executed.attributes["tbay.event"] == "call.succeeded"  # the terminal event wins
    assert executed.status.is_ok
    assert cached.attributes["tbay.event"] == "cache.hit"


def test_failure_span_has_error_status(spans_and_client):
    exporter, client = spans_and_client

    @guarded(client, policy="mutating")
    def boom() -> dict:
        raise RuntimeError("kaput")

    with pytest.raises(RuntimeError):
        boom()
    (span,) = exporter.get_finished_spans()
    assert not span.status.is_ok
    assert "kaput" in span.status.description


def test_uninstrument_via_off(spans_and_client):
    exporter, client = spans_and_client
    handler = client.events._subscribers[0][1]
    client.off(handler)

    @guarded(client, policy="readonly")
    def lookup(q: str) -> dict:
        return {"answer": q}

    lookup("x")
    assert exporter.get_finished_spans() == ()
