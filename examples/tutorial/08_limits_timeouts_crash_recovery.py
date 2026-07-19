"""Tutorial 08: rate limits, concurrency caps, timeouts, crash recovery.

Four guardrails against the failure modes of an agent loop hitting real
infrastructure:

  rate_limit          "no more than N calls per window" (any arguments)
  max_concurrent      "no more than N in flight at once" (atomic everywhere)
  execution_timeout   "give up on a hung call after N seconds"
  lease_timeout       "if the process running a call DIED, let the next
                       caller take the call over instead of waiting forever"

Run it:  python examples/tutorial/08_limits_timeouts_crash_recovery.py
"""
import threading
import time

from _tutorial_helpers import banner, fresh_client, step

from tbay import ExecutionTimeout, RateLimitExceeded, guarded
from tbay.policy import Policy

banner("08: limits, timeouts, crash recovery")
client = fresh_client()

# ---------------------------------------------------------------------------
# 1. Rate limiting: counts CALLS to the tool (any args) in a rolling
# window. The N+1th raises RateLimitExceeded; the refusal is audit-logged.
# ---------------------------------------------------------------------------
client.policies["limited"] = Policy(
    name="limited", idempotent=False, singleflight=False,
    rate_limit_max_calls=3, rate_limit_window=60.0,     # YAML: rate_limit: {max_calls: 3, per: 1m}
)


@guarded(client, policy="limited")
def paid_api(query: str) -> dict:
    return {"result": query}


step("1. rate_limit 3/min: calls 1-3 pass, call 4 is refused")
for i in range(1, 4):
    paid_api(f"query {i}")
    print(f"    call {i}: ok")
try:
    paid_api("query 4")
except RateLimitExceeded as exc:
    print(f"    call 4: RateLimitExceeded: {exc}")

# ---------------------------------------------------------------------------
# 2. Concurrency cap: at most N calls IN FLIGHT at once, enforced in the
# same atomic step as the key claim, so two callers can never both sneak
# past the cap even from different processes. Excess callers WAIT for a
# slot (up to concurrency_wait_timeout) instead of erroring immediately.
# ---------------------------------------------------------------------------
client.policies["capped"] = Policy(
    name="capped", idempotent=False, singleflight=False,
    max_concurrent=2, concurrency_wait_timeout=10.0,
)
in_flight, peak = [0], [0]
lock = threading.Lock()


@guarded(client, policy="capped")
def heavy(job: int) -> dict:
    with lock:
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
    time.sleep(0.15)
    with lock:
        in_flight[0] -= 1
    return {"job": job}


step("2. max_concurrent=2: six threads, but never more than 2 inside at once")
threads = [threading.Thread(target=heavy, args=(i,)) for i in range(6)]
for t in threads: t.start()
for t in threads: t.join()
print(f"    peak concurrency observed inside the function: {peak[0]}")
assert peak[0] <= 2

# ---------------------------------------------------------------------------
# 3. Execution timeout: a hung call is abandoned ON TIME and marked
# FAILED. (Best effort: Python can't force-kill a thread, so the hung
# body may finish in the background — the timeout bounds YOUR wait and
# the record's state, not the side effect.)
# ---------------------------------------------------------------------------
client.policies["quick"] = Policy(
    name="quick", idempotent=False, singleflight=False, execution_timeout=0.3,
)


@guarded(client, policy="quick")
def hangs() -> dict:
    time.sleep(3.0)          # imagine a stuck HTTP request
    return {"never": True}


step("3. execution_timeout=0.3s on a 3s hang: we get our thread back fast")
started = time.time()
try:
    hangs()
except ExecutionTimeout as exc:
    elapsed = time.time() - started
    print(f"    ExecutionTimeout after {elapsed:.2f}s (not 3s!): {exc}")
assert elapsed < 1.0

# ---------------------------------------------------------------------------
# 4. Crash recovery. Simulate a worker that claimed an execution and then
# DIED: we insert the RUNNING row directly and never complete it. Without
# lease_timeout, every later caller would wait on that ghost. With it,
# once the row is older than the lease, the next caller atomically
# reclaims it (a created_at compare-and-swap: exactly one winner) and
# runs the call itself.
# ---------------------------------------------------------------------------
client.policies["leased"] = Policy(name="leased", lease_timeout=0.2)

step("4. A worker crashed mid-call; lease_timeout lets the next caller recover")
client.backend.acquire_or_get(          # <- what the dead worker did before dying
    execution_id="ghost-owner", tool_name="fetch_report", idempotency_key="report-1",
    tenant="", policy_name="leased", args_hash="h", args_json="{}",
    max_retries=0, retry_backoff=0.0,
)
print("    [a worker claimed 'report-1' as RUNNING and then died]")
time.sleep(0.25)                        # let the lease expire

recovered = []


@guarded(client, policy="leased", key_fn=lambda: "report-1", tool_name="fetch_report")
def fetch_report() -> dict:
    recovered.append(True)
    return {"rows": 99}


print(f"    next caller reclaims and runs it: {fetch_report()}")
assert recovered == [True]

# Why lease_timeout is OFF by default for mutating/destructive: reclaiming
# a call that is actually still running would DOUBLE-RUN it. Turn it on
# where a rare double-run is acceptable (reads: the built-in readonly
# tier ships with lease_timeout=10m) and keep it longer than your slowest
# legitimate call.

print("""
WHAT JUST HAPPENED
  - rate_limit counts calls; max_concurrent bounds in-flight calls
    atomically; execution_timeout bounds your wait on a hang;
    lease_timeout un-wedges keys owned by dead processes.
  - Every refusal/timeout is recorded in the audit log and emitted as an
    event (limit.rate / limit.concurrency / call.failed).
  - The full failure-mode -> control table: docs/controls.md.

NEXT: 09_events.py — watching every one of these decisions live.
""")
