"""Tutorial 11: OpenTelemetry — tbay decisions inside your existing traces.

If your agent stack already emits OTel traces (most do, or will), two
lines put every tbay decision into them:

    from tbay.otel import instrument
    instrument(client)                # pip install "tbay[otel]" + an SDK

Every guarded call becomes a span named "tbay.<tool>" that parents to
whatever span is current — so tbay's spans nest right under your
framework's "agent step" spans. Executions span their real duration
(approval waits included, with approval milestones as span events);
decisions that never ran anything (cache hits, refusals, kill-switch
blocks) become short spans of their own, because "the call didn't happen
and here's why" belongs in the trace too.

This tutorial uses the in-memory SDK exporter so you can SEE the spans
it produces. It skips gracefully if the SDK isn't installed.

Run it:  pip install "tbay[otel]" opentelemetry-sdk
         python examples/tutorial/11_observability_otel.py
"""
import sys

from _tutorial_helpers import banner, fresh_client, step

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:
    print("This tutorial needs the OpenTelemetry SDK:")
    print("    pip install 'tbay[otel]' opentelemetry-sdk")
    sys.exit(0)

import tbay
from tbay import guarded
from tbay.otel import instrument

banner("11: OpenTelemetry")
client = fresh_client()

# ---------------------------------------------------------------------------
# Step 1: instrument. In production you'd omit `tracer=` and let it use
# your globally-configured provider; here we pass one wired to an
# in-memory exporter so the script can print what got recorded.
# ---------------------------------------------------------------------------
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
handler = instrument(client, tracer=provider.get_tracer("tutorial"))
print("    instrument(client) subscribed an event handler; client.off(handler) undoes it")


@guarded(client, policy="readonly")
def lookup(q: str) -> dict:
    return {"answer": q}


@guarded(client, policy="mutating")
def boom() -> dict:
    raise RuntimeError("kaput")


# ---------------------------------------------------------------------------
# Step 2: run tools, then inspect the spans.
# ---------------------------------------------------------------------------
step("2. One execution + one cache hit + one failure -> three spans")
with tbay.agent("traced-agent", model="gpt-5"):
    lookup("hello")     # real execution: span covers the actual duration
    lookup("hello")     # cache hit: short span, still visible in the trace
    try:
        boom()          # failure: span with ERROR status
    except RuntimeError:
        pass

for span in exporter.get_finished_spans():
    status = "OK " if span.status.is_ok else "ERR"
    attrs = {k: v for k, v in span.attributes.items() if k.startswith("tbay.")}
    print(f"    [{status}] {span.name:14s} event={attrs.get('tbay.event'):16s} "
          f"agent={attrs.get('tbay.agent_id')} policy={attrs.get('tbay.policy')}")

spans = exporter.get_finished_spans()
assert [s.name for s in spans] == ["tbay.lookup", "tbay.lookup", "tbay.boom"]
executed, cached, failed = spans
assert executed.status.is_ok and executed.attributes["tbay.event"] == "call.succeeded"
assert cached.attributes["tbay.event"] == "cache.hit"
assert not failed.status.is_ok and "kaput" in failed.status.description

# ---------------------------------------------------------------------------
# Step 3: attributes carried on every span. tbay.duration_s appears on
# executed calls; error text lands in the span status; agent identity
# from `with tbay.agent(...)` rides along automatically.
# ---------------------------------------------------------------------------
step("3. Everything a span knows about the executed call")
for key, value in sorted(executed.attributes.items()):
    print(f"    {key:22s} = {value}")
assert executed.attributes["tbay.agent_id"] == "traced-agent"

print("""
WHAT JUST HAPPENED
  - instrument(client) is a plain event subscriber (tutorial 09) that
    opens a span at call.started and closes it on the terminal event;
    non-executions become short spans so refusals stay visible.
  - No hard dependency: tbay imports OpenTelemetry only inside
    instrument(), so the core package stays stdlib+yaml+click.
  - In production: configure your provider/exporter as usual (OTLP,
    Jaeger, honeycomb, ...), call instrument(client), done.

NEXT: 12_cli_tour.py — the same visibility from a terminal.
""")
