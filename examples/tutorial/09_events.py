"""Tutorial 09: lifecycle events — every decision, live, in-process.

The audit log tells you what happened after the fact. Events tell you AS
IT HAPPENS: every decision tbay makes fires a structured Event to
handlers you subscribe with client.on(). No dependencies, no exporter,
no agent — just callables. This is the integration point for metrics,
alerting, custom circuit breakers, and the OpenTelemetry bridge
(tutorial 11 is built entirely on this).

The event types (constants in tbay.events):

  call.started              this process won ownership; the function will run
  call.succeeded            it returned (data: duration_s)
  call.failed               it raised, or a stored failure replayed (data: error, replayed)
  cache.hit                 an unexpired stored result was served
  cache.semantic_hit        a similar-enough previous answer was served
  singleflight.coalesced    another caller owns the key; we followed its result
  approval.requested/.approved/.rejected
  limit.rate / limit.budget / limit.concurrency
  killswitch.blocked

Run it:  python examples/tutorial/09_events.py
"""
from _tutorial_helpers import banner, fresh_client, step

from tbay import ToolPaused, guarded
from tbay.events import CALL_FAILED, KILL_SWITCH_BLOCKED
from tbay.policy import Policy

banner("09: lifecycle events")
client = fresh_client()

# ---------------------------------------------------------------------------
# Step 1: subscribe. Three equivalent forms — pick whichever reads best.
# ---------------------------------------------------------------------------
timeline = []


@client.on                                   # form 1: decorator, all events
def record_everything(event):
    timeline.append(event)


@client.on(events=[CALL_FAILED])             # form 2: decorator, filtered
def only_failures(event):
    print(f"    [failure handler] {event.tool_name} failed: {event.data['error']}")


def audit_print(event):                      # form 3: plain call
    print(f"    [event] {event.type:24s} tool={event.tool_name} data={event.data}")


client.on(audit_print)

# ---------------------------------------------------------------------------
# Step 2: run a tool and watch the events flow.
# ---------------------------------------------------------------------------
@guarded(client, policy="readonly")
def lookup(q: str) -> dict:
    return {"answer": q}


step("2. A run and a cache hit, narrated by events")
lookup("hello")     # -> call.started, call.succeeded
lookup("hello")     # -> cache.hit

types = [e.type for e in timeline]
assert types == ["call.started", "call.succeeded", "cache.hit"], types

# ---------------------------------------------------------------------------
# Step 3: what an Event carries. Note agent_id/reasoning ride along
# automatically when the call is inside `with agent(...)`/`reasoning(...)`
# blocks (tutorial 10) — your handlers get attribution for free.
# ---------------------------------------------------------------------------
step("3. Anatomy of one event")
done = timeline[1]   # the call.succeeded
print(f"    type={done.type} tool={done.tool_name} execution_id={done.execution_id[:8]}")
print(f"    policy={done.policy} tenant={done.tenant!r} agent={done.agent_id} ts={done.ts:.0f}")
print(f"    data={done.data}   <- duration_s lives here for call.succeeded")
assert done.data["duration_s"] >= 0

# ---------------------------------------------------------------------------
# Step 4: failures are events too — including REPLAYED ones, flagged so
# your alerting can tell "it broke now" from "it broke earlier".
# ---------------------------------------------------------------------------
@guarded(client, policy="mutating")
def boom() -> dict:
    raise RuntimeError("kaput")


step("4. A real failure, then its replay (data.replayed distinguishes them)")
for _ in range(2):
    try:
        boom()
    except Exception:
        pass
failures = [e for e in timeline if e.type == CALL_FAILED]
print(f"    replayed flags: {[e.data['replayed'] for e in failures]}")
assert [e.data["replayed"] for e in failures] == [False, True]

# ---------------------------------------------------------------------------
# Step 5: a handler that CRASHES cannot break the guarded call.
# Observability must never take the workload down.
# ---------------------------------------------------------------------------
@client.on
def bad_handler(event):
    raise ValueError("I am a buggy handler")


step("5. A crashing handler is logged and swallowed; the call still works")
print(f"    {lookup('still fine')}")   # works despite bad_handler raising on every event

# ---------------------------------------------------------------------------
# Step 6: build something real in 10 lines — a metrics counter and an
# automatic circuit breaker that pauses a tool wired to safety events.
# ---------------------------------------------------------------------------
step("6. A tiny metrics counter + an automatic circuit breaker")
from collections import Counter

metrics = Counter()
client.on(lambda e: metrics.update([e.type]))

consecutive_failures = Counter()


@client.on(events=[CALL_FAILED])
def circuit_breaker(event):
    consecutive_failures[event.tool_name] += 1
    if consecutive_failures[event.tool_name] >= 3:
        client.pause(event.tool_name, reason="circuit breaker: 3 straight failures", by="auto")
        print(f"    [breaker] paused {event.tool_name}!")


client.policies["flaky"] = Policy(name="flaky", idempotent=False, singleflight=False)


@guarded(client, policy="flaky")
def flaky() -> dict:
    raise RuntimeError("down")


for _ in range(3):
    try:
        flaky()
    except RuntimeError:
        pass
try:
    flaky()
except ToolPaused as exc:
    print(f"    4th call blocked by our own breaker: {exc}")

print(f"\n    metrics so far: {dict(metrics)}")
assert metrics[KILL_SWITCH_BLOCKED] >= 1

# Unsubscribing: client.off(handler). BUDGET_EXCEEDED etc. work the same
# way — subscribe to the safety events and you have an alerting feed.
client.off(bad_handler)

print("""
WHAT JUST HAPPENED
  - client.on() (bare / decorator / filtered) taps every decision;
    handler errors are isolated; client.off() unsubscribes.
  - Events + pause() compose into self-defending behavior (the breaker).
  - Full reference: docs/observability.md#lifecycle-events.

NEXT: 10_agents_and_reasoning.py — attribution: which agent asked, and why.
""")
