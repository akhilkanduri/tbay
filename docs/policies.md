# Policies

A policy is a named risk tier. You attach one to a tool with
`@guarded(client, policy="name")`, and it decides everything about how
calls to that tool behave: caching, dedup, retries, approvals, rate
limits, timeouts, redaction.

## The four built-in tiers

```python
@guarded(client, policy="readonly")     # safe to repeat, safe to serve stale
def weather(city: str) -> dict: ...

@guarded(client, policy="mutating")     # has an effect; must never double-run
def create_ticket(title: str) -> dict: ...

@guarded(client, policy="destructive")  # a human signs off before it runs
def refund_customer(customer_id: str, amount: float) -> dict: ...

@guarded(client, policy="volatile")     # always runs fresh, never cached
def ask_llm(prompt: str) -> dict: ...
```

| Tier | Caching | Dedup | Retries | Approval |
|---|---|---|---|---|
| `readonly` | 5 minutes | yes | 2, 1s backoff | no |
| `mutating` | result kept forever | yes | none | no |
| `destructive` | result kept forever | yes | none | required |
| `volatile` | never | never | none | no |

`volatile` exists because not every tool call is idempotent: an LLM call
used to decide something, "roll a die", "get the current time". Calling
those twice with the same arguments should NOT return the same cached
answer, so `volatile` (`idempotent: false`) turns off caching, dedup, and
`key_fn` entirely.

## The policy file

Override the built-ins, or add your own tiers, in YAML:

```yaml
policies:
  readonly:
    cache_ttl: 5m
    singleflight: true
    max_retries: 2
    retry_backoff: 1s

  destructive:
    idempotent: true
    approval_required: true
    approval_timeout: 1h
    approval_bypass_arg: amount      # skip approval for small values...
    approval_bypass_max: 50          # ...at or under this threshold
    redact_args: [card_number]       # mask these args in the audit log

  rate_limited_api:                  # your own tier for a paid API
    rate_limit:
      max_calls: 30
      per: 1m
    max_concurrent: 3
    concurrency_wait_timeout: 30s
    execution_timeout: 10s

  refunds:                           # cap total spend, not just call counts
    approval_required: true
    approval_bypass_arg: amount
    approval_bypass_max: 50
    budget:
      arg: amount                    # SUM of this argument...
      max: 1000                      # ...may not pass this...
      per: 1d                        # ...in this rolling window
```

```python
client = TbayClient(db_url, policy_file="policy.yaml")
```

Any policy you don't mention keeps its built-in defaults. You can also
override in code: `client.policies["readonly"].cache_ttl = 60`. See
`policy.example.yaml` in the repo root for a fully commented example.

Policy files **fail loud** at load time rather than degrade silently,
because a safety setting the author believes exists but doesn't is the
worst kind of bug:

- An unknown key (a typo like `aproval_required`) is rejected with the
  list of valid keys.
- A `rate_limit` needs both `max_calls` and `per`; half a rate limit
  would either crash at call time or silently not limit.
- A `budget` needs all of `arg`, `max`, and `per`.
- `approval_bypass_arg` and `approval_bypass_max` must be set together;
  one without the other would mean the bypass never fires, silently.

The reasoning behind this (and every other design choice) is collected
in [Design rationale](design.md).

## Every policy field

Durations (`cache_ttl`, `retry_backoff`, `per`, `concurrency_wait_timeout`,
`execution_timeout`) accept `30s`, `5m`, `1h`, `2d`, or a plain number of
seconds.

| Field | Default | Meaning |
|---|---|---|
| `cache_ttl` | none | How long a stored result satisfies later calls. `0`/omitted means keep forever. |
| `idempotent` | `true` | Once a call with a given key succeeds, never silently re-run it. `false` = volatile behavior. |
| `singleflight` | `true` | Concurrent identical calls share one execution instead of racing. |
| `semantic_cache` | `false` | Serve a stored result for *similar* (not byte-identical) args. Read-only tools only. See [Caching](caching.md#semantic-caching). |
| `semantic_threshold` | `0.92` | Cosine similarity a candidate must reach to count as a semantic hit. |
| `max_retries` | `0` | How many times a later call may re-attempt a previously failed key. |
| `retry_backoff` | `0` | Seconds after a failure before a retry is allowed. |
| `approval_required` | `false` | Pause in WAITING_APPROVAL until a human decides. See [Approvals](approvals.md). |
| `approval_webhook` | none | HTTP POST fired (best effort) when a call starts waiting. |
| `approval_timeout` | `3600` | Give up waiting for a human after this many seconds. |
| `approval_bypass_arg` | none | Argument name checked for the bypass threshold. |
| `approval_bypass_max` | none | Bypass approval when that argument is at or under this value. |
| `rate_limit.max_calls` | none | Max calls (any args) per window. |
| `rate_limit.per` | none | The window those calls are counted over. |
| `budget.arg` | none | Numeric argument metered against a spend cap. See [Runtime controls](controls.md#budgets-capping-magnitude-not-just-call-counts). |
| `budget.max` | none | Ceiling for that argument's rolling total; past it, `BudgetExceeded`. |
| `budget.per` | none | The rolling window the total is summed over. All three `budget` keys are required together. |
| `max_concurrent` | none | At most this many calls to the tool running at once (atomic on every backend). |
| `concurrency_wait_timeout` | `300` | How long a caller waits for a free slot before `ConcurrencyLimitExceeded`. |
| `lease_timeout` | none (`10m` for `readonly`) | Reclaim a RUNNING execution abandoned by a crashed process after this long. See [Runtime controls](controls.md#stale-lease-recovery-surviving-crashed-owners). |
| `execution_timeout` | none | Best-effort per-call timeout. Python can't force-kill a thread, so a hung call may keep running after being marked FAILED. |
| `redact_args` | `[]` | Argument names (any depth) or dotted paths masked as `***REDACTED***` in the audit log. |
| `redact_patterns` | `[]` | Regexes matched against argument key names at any depth; matches are masked. |
| `redact_auto` | `false` | Also mask well-known secret-ish key names (`password`, `token`, `api_key`, `authorization`, ...). |
