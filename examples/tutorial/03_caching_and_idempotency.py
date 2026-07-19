"""Tutorial 03: caching, idempotency keys, singleflight, retries, semantic cache.

This is the deep dive on tbay's dedup machinery — the five distinct
behaviors that all hang off the idempotency key:

  1. TTL caching        a stored result answers later identical calls, for a while
  2. custom keys        YOU define what "the same call" means (key_fn)
  3. singleflight       N concurrent identical calls -> 1 execution, N-1 followers
  4. failure replay     a failed key replays its error instead of re-running
                        (unless the policy grants retries)
  5. semantic caching   "similar enough" arguments can share an answer too

Run it:  python examples/tutorial/03_caching_and_idempotency.py
"""
import threading
import time

from _tutorial_helpers import banner, fresh_client, step

from tbay import ExecutionFailed, guarded
from tbay.policy import Policy

banner("03: caching and idempotency")
client = fresh_client()

# ---------------------------------------------------------------------------
# 1. TTL caching. cache_ttl bounds how long a stored result satisfies new
# calls. After expiry the next caller RECLAIMS the row (atomically — one
# winner) and re-runs for real.
# ---------------------------------------------------------------------------
client.policies["short_cache"] = Policy(name="short_cache", cache_ttl=0.3)
runs = []


@guarded(client, policy="short_cache")
def fetch_price(symbol: str) -> dict:
    runs.append(symbol)
    return {"symbol": symbol, "price": 101.5}


step("1. TTL caching: hit inside the TTL, re-run after it expires")
fetch_price("ACME"); fetch_price("ACME")
print(f"    within TTL: ran {len(runs)} time(s)")
time.sleep(0.35)
fetch_price("ACME")
print(f"    after TTL:  ran {len(runs)} time(s)")
assert len(runs) == 2

# ---------------------------------------------------------------------------
# 2. Custom idempotency keys. By default the key is hash(all args). Often
# "the same call" is really keyed by ONE argument: charging an order is
# the same charge whatever the retry metadata says. key_fn takes the
# call's args and returns the identity that matters.
# ---------------------------------------------------------------------------
charges = []


@guarded(client, policy="mutating", key_fn=lambda order_id, **_: order_id)
def charge(order_id: str, attempt: int = 0, note: str = "") -> dict:
    charges.append((order_id, attempt))
    return {"charged": order_id}


step("2. key_fn: same order_id = same call, even with different extras")
charge("order_1", attempt=1, note="first try")
charge("order_1", attempt=2, note="retry with different metadata")   # deduped!
charge("order_2", attempt=1)
print(f"    real executions: {charges}")
assert charges == [("order_1", 1), ("order_2", 1)]

# ---------------------------------------------------------------------------
# 3. Singleflight. Ten threads fire the same expensive call at the same
# instant. One thread wins the database claim and executes; the other
# nine see a RUNNING row and just... wait for its result. This works
# across PROCESSES too — the coordination is the database row itself.
# ---------------------------------------------------------------------------
slow_runs = []


@guarded(client, policy="readonly")
def slow_report(day: str) -> dict:
    slow_runs.append(day)
    time.sleep(0.3)                       # imagine an expensive API call
    return {"day": day, "rows": 12345}


step("3. singleflight: 10 concurrent identical calls")
results = [None] * 10
threads = [threading.Thread(target=lambda i=i: results.__setitem__(i, slow_report("2026-07-19")))
           for i in range(10)]
started = time.time()
for t in threads: t.start()
for t in threads: t.join()
print(f"    10 threads finished in {time.time() - started:.2f}s; "
      f"function ran {len(slow_runs)} time(s); all results equal: {all(r == results[0] for r in results)}")
assert len(slow_runs) == 1 and all(r == results[0] for r in results)

# ---------------------------------------------------------------------------
# 4. Failure replay and retries. Under "mutating" (max_retries=0) a
# failed key REPLAYS its stored error: tbay refuses to guess whether
# re-running a half-finished mutation is safe. Grant retries explicitly
# where re-running IS safe.
# ---------------------------------------------------------------------------
attempts = []


@guarded(client, policy="mutating")
def flaky_no_retry(job: str) -> dict:
    attempts.append(job)
    raise RuntimeError("upstream 500")


step("4a. no retries: the stored failure is replayed, not re-run")
try:
    flaky_no_retry("job_1")
except RuntimeError as exc:
    print(f"    first call raised the real error: {exc}")
try:
    flaky_no_retry("job_1")
except ExecutionFailed as exc:
    print(f"    second call raised ExecutionFailed (replayed, body did not run): {exc}")
assert len(attempts) == 1

client.policies["retryable"] = Policy(name="retryable", max_retries=2, retry_backoff=0.05)
retry_attempts = []


@guarded(client, policy="retryable")
def flaky_retryable(job: str) -> dict:
    retry_attempts.append(job)
    if len(retry_attempts) < 3:
        raise RuntimeError("upstream 500")
    return {"done": job}


step("4b. max_retries=2: later calls may re-attempt (after retry_backoff)")
for _ in range(3):
    try:
        result = flaky_retryable("job_2")
        break
    except (RuntimeError, ExecutionFailed):
        time.sleep(0.06)
print(f"    attempts made: {len(retry_attempts)}, final result: {result}")
assert result == {"done": "job_2"} and len(retry_attempts) == 3

# ---------------------------------------------------------------------------
# 5. Semantic caching. With semantic_cache: true, a call can be answered
# by a stored result whose arguments are merely SIMILAR (cosine
# similarity of embeddings >= semantic_threshold), not byte-identical.
# ONLY for read-only tools — "close enough" is wrong for a refund.
#
# The default HashingEmbedder is zero-dependency and catches same-words-
# different-order. For true paraphrases ("berlin forecast"), plug a real
# model in: TbayClient(embedder=<anything with .embed(text)->list[float]>).
# ---------------------------------------------------------------------------
client.policies["sem"] = Policy(name="sem", cache_ttl=60.0, semantic_cache=True,
                                semantic_threshold=0.9)
searches = []


@guarded(client, policy="sem")
def web_search(query: str) -> dict:
    searches.append(query)
    return {"answer": f"results for {query!r}"}


step("5. semantic cache: reworded query, same answer, no second execution")
a = web_search(query="weather berlin today")
b = web_search(query="today weather berlin")      # same tokens, different order
print(f"    executions: {searches}")
print(f"    both calls returned: {a == b} -> {b}")
assert searches == ["weather berlin today"] and a == b

print("""
WHAT JUST HAPPENED
  - One mechanism (the claimed idempotency key) gives you caching,
    dedup, singleflight, and failure replay; policies pick the mix.
  - key_fn is the lever when identity != "all the arguments".
  - Deeper detail: docs/caching.md.

NEXT: 04_approvals.py — the destructive tier, where a human stands
between the agent and the tool.
""")
