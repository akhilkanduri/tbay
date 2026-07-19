"""Tutorial 02: policies — where all behavior lives.

A policy is a named risk tier. The tool function stays dumb; the policy
attached to it decides caching, dedup, retries, approvals, limits,
budgets, redaction, timeouts. Changing safety behavior means editing a
YAML file, not the tool.

Four tiers ship built in:

  readonly     safe to repeat, safe to serve slightly stale  (5m cache, retries)
  mutating     has an effect; runs exactly once per unique input
  destructive  a HUMAN approves each call before it runs
  volatile     never cached, never deduped: always runs fresh

Run it:  python examples/tutorial/02_policies.py
"""
import tempfile
from pathlib import Path

from _tutorial_helpers import banner, fresh_client, step

from tbay import TbayClient, guarded

banner("02: policies")

# ---------------------------------------------------------------------------
# Step 1: the four built-in tiers, and what each defaults to.
# ---------------------------------------------------------------------------
client = fresh_client()

step("The built-in tiers (this is exactly what `tbay policies` prints)")
for name, pol in sorted(client.policies.items()):
    print(f"    {name:12s} idempotent={pol.idempotent!s:5s} cache_ttl={pol.cache_ttl} "
          f"retries={pol.max_retries} approval={pol.approval_required} lease={pol.lease_timeout}")

# Notice `readonly` ships with lease_timeout=600: a read abandoned by a
# crashed process gets re-run by the next caller (tutorial 08 shows this
# live). Mutating/destructive leave it off — a false reclaim would mean a
# double-run, and for those tiers a hang is safer than a double-run.

# ---------------------------------------------------------------------------
# Step 2: `volatile` vs `readonly`, side by side. Identical arguments;
# watch which one runs the function body again.
# ---------------------------------------------------------------------------
runs = {"lookup": 0, "roll": 0}


@guarded(client, policy="readonly")
def lookup(city: str) -> dict:
    runs["lookup"] += 1
    return {"weather": "sunny", "city": city}


@guarded(client, policy="volatile")   # idempotent=False: two same-args calls are NOT the same call
def roll(sides: int) -> dict:
    runs["roll"] += 1
    return {"sides": sides}


step("readonly: second identical call is a cache hit")
lookup("berlin"); lookup("berlin")
print(f"    lookup ran {runs['lookup']} time(s) for 2 calls")
assert runs["lookup"] == 1

step("volatile: second identical call runs again (an LLM call, a dice roll...)")
roll(6); roll(6)
print(f"    roll ran {runs['roll']} time(s) for 2 calls")
assert runs["roll"] == 2

# ---------------------------------------------------------------------------
# Step 3: a policy FILE. This is the normal way to configure tbay: named
# tiers in YAML, checked into your repo, loaded at client construction.
# Everything you don't mention keeps its built-in default.
# ---------------------------------------------------------------------------
policy_yaml = """
policies:
  readonly:
    cache_ttl: 10m                  # durations: 30s / 5m / 1h / 2d, or plain seconds
    max_retries: 2
    retry_backoff: 1s

  refunds:                          # your own tier, from scratch
    approval_required: true
    approval_timeout: 1h
    approval_bypass_arg: amount     # refunds <= 50 skip the human...
    approval_bypass_max: 50
    budget: {arg: amount, max: 1000, per: 1d}   # ...but never more than 1000/day TOTAL
    redact_args: [card_number]

  paid_api:
    rate_limit: {max_calls: 30, per: 1m}
    max_concurrent: 3
    execution_timeout: 10s
"""
policy_path = Path(tempfile.mkdtemp()) / "policy.yaml"
policy_path.write_text(policy_yaml)

db = Path(tempfile.mkdtemp()) / "t.sqlite"
configured = TbayClient(f"sqlite:///{db}", policy_file=str(policy_path), poll_interval=0.02)

step("Loaded from YAML: built-ins + your tiers, overrides applied")
print(f"    readonly.cache_ttl is now {configured.policies['readonly'].cache_ttl}s (was 300)")
print(f"    'refunds' exists: budget {configured.policies['refunds'].budget_arg} "
      f"<= {configured.policies['refunds'].budget_max} per {configured.policies['refunds'].budget_window}s")
assert configured.policies["readonly"].cache_ttl == 600.0
assert configured.policies["refunds"].budget_max == 1000.0

# ---------------------------------------------------------------------------
# Step 4: typos are REJECTED, not ignored. A silently-ignored safety
# setting is the worst kind of bug, so an unknown key refuses to load
# and tells you the valid ones.
# ---------------------------------------------------------------------------
step("A misspelled key ('aproval_required') fails at load time")
bad_path = Path(tempfile.mkdtemp()) / "bad.yaml"
bad_path.write_text("policies:\n  risky:\n    aproval_required: true\n")
try:
    TbayClient(f"sqlite:///{db}", policy_file=str(bad_path))
    raise AssertionError("should have refused to load")
except ValueError as exc:
    print(f"    ValueError: {str(exc)[:100]}...")

# ---------------------------------------------------------------------------
# Step 5: overriding in code. client.policies is a plain dict of Policy
# dataclasses, deep-copied per client, so tests and special cases can
# tweak freely without affecting other clients or the module defaults.
# ---------------------------------------------------------------------------
step("Tweak in code (each client owns its copy)")
configured.policies["readonly"].cache_ttl = 60.0
print(f"    configured.policies['readonly'].cache_ttl = {configured.policies['readonly'].cache_ttl}")
print(f"    ...but the first client still has {client.policies['readonly'].cache_ttl} (isolated copies)")
assert client.policies["readonly"].cache_ttl == 300.0

print("""
WHAT JUST HAPPENED
  - Behavior lives on named policies, not on tools; @guarded(policy="x")
    is the only coupling.
  - YAML file > code defaults; unknown keys are load-time errors.
  - Full field reference: docs/policies.md. The fully-commented example
    file: policy.example.yaml in the repo root.

NEXT: 03_caching_and_idempotency.py — everything the "readonly" and
"mutating" machinery can do, under real concurrency.
""")
