# tbay

Execution safety for AI agent tool calls: idempotency, TTL caching,
singleflight deduplication, risk-tiered policy, and human approval gating,
as a library you install, not a service you depend on.

```python
from tbay import TbayClient, guarded

client = TbayClient("sqlite:///~/.tbay/db.sqlite")
# or: TbayClient("postgresql://user:pass@host/dbname")

@guarded(client, policy="readonly")
def github_search(query: str) -> dict:
    return real_github_api_call(query)

@guarded(client, policy="destructive")
def refund_customer(customer_id: str, amount: float) -> dict:
    return stripe_refund(customer_id, amount)
```

`@guarded` only ever wraps a plain callable, so it drops in under
LangChain's `@tool`, the OpenAI Agents SDK's `@function_tool`, CrewAI tools,
or bare functions, with zero framework-specific code. See `examples/`.

## Why

Agent frameworks solve planning and orchestration. None of them solve
*execution safety*: once a tool is selected, nothing stops it from being
called twice, cached when it shouldn't be, called too often, or fired on a
destructive action without a human in the loop. tbay sits underneath any
framework and handles that, durably, across processes, in whatever database
you already run.

**This is not a hosted service.** You `pip install` it; state (idempotency
keys, cached results, the audit log) lives entirely in a database you own.
Nothing calls home.

## Install

```
pip install tbay               # SQLite backend, stdlib only
pip install tbay[postgres]     # + Postgres backend

# or, with uv:
uv add tbay
uv add "tbay[postgres]"
```

## How it works

Every `@guarded` call computes an idempotency key (tool name + normalized
args + tenant), then atomically claims a row in your database:

- **First caller** for a given key becomes the owner and runs the real
  function.
- **Concurrent callers** with the identical key block and receive the same
  result once the owner finishes (singleflight). No in-memory daemon is
  required; coordination happens through the database's own atomicity
  (`INSERT ... ON CONFLICT`, plus an advisory lock for the Postgres backend
  when `max_concurrent` is set).
- **Later callers**, after the owner finishes, get the stored result
  instead of re-running: permanently for `mutating`/`destructive` policies
  (true idempotency), or until the policy's `cache_ttl` expires for
  `readonly` policies.
- **`destructive`-policy calls** pause in `WAITING_APPROVAL` until someone
  runs `tbay approve <execution_id>` (optionally after a webhook fires).
- **Not every tool call is idempotent.** An LLM call used to decide
  something, "roll a die", "get the current time": calling these twice with
  the same arguments should *not* return the same cached answer. Use the
  `volatile` policy (`idempotent: false`) for these; tbay then ignores
  caching, dedup, and `key_fn` entirely and runs the call fresh every time.

## Policies

```yaml
policies:
  # Safe to call repeatedly; safe to serve a slightly stale answer.
  readonly:
    cache_ttl: 5m
    singleflight: true
    max_retries: 2
    retry_backoff: 1s

  # Has a real effect, but must never double-run for the same input.
  mutating:
    idempotent: true
    cache_ttl: 0          # 0/omitted means "keep the result forever"
    max_retries: 0

  # Has a real-world consequence a human should sign off on.
  destructive:
    idempotent: true
    approval_required: true
    approval_timeout: 1h
    approval_bypass_arg: amount     # optional: skip approval for small values
    approval_bypass_max: 50
    redact_args: [card_number]       # optional: mask these args in the audit log

  # Runs fresh every time, even with identical arguments: an LLM call used
  # to decide something, a random number, "get the current time".
  volatile:
    idempotent: false
    max_retries: 1
    retry_backoff: 0.5
```

Pass a policy file via `TbayClient(db_url, policy_file="policy.yaml")`, or
override policies in code (`client.policies["readonly"].cache_ttl = 60`).
See `policy.example.yaml` for every field, including the `rate_limit` and
`max_concurrent` throughput guardrails.

### Approval webhooks

Setting `approval_webhook` on a policy makes tbay fire an HTTP POST when a
call enters `WAITING_APPROVAL`, so a human finds out without polling
`tbay log` themselves. The body is
`{"execution_id": "...", "tool_name": "..."}`; look the execution up with
`tbay log --tool <tool_name>` to see its (possibly redacted) arguments
before approving or rejecting it.

The webhook is best-effort: if the URL is unreachable or returns an error,
tbay ignores it silently and the call still waits normally. `tbay
approve`/`tbay reject` always work, whether or not the webhook fired, so a
flaky webhook endpoint can never leave a call stuck.

### Approval bypass and other guardrails

`approval_bypass_arg`/`approval_bypass_max` let small, low-risk calls
through automatically while still pausing anything larger for a human:
a refund of $50 or less runs immediately, a refund of $500 waits for
`tbay approve`. `rate_limit` and `max_concurrent` protect a tool (and
whatever paid or rate-limited API it calls) from a runaway agent loop by
capping how often it can be called and how many calls can be in flight at
once. `execution_timeout` gives up on a hung call after a set time, though
this is best-effort: Python can't force-kill a thread, so the call may keep
running in the background even after tbay marks it failed.

## CLI

```
tbay log                                   # the audit log
tbay log --tool refund_customer --status WAITING_APPROVAL
tbay approve <execution_id>
tbay reject <execution_id>
```

Point the CLI at the same database as your app with `--db-url` or the
`TBAY_DB_URL` environment variable.

## Examples

- `examples/plain_python_demo.py`: no framework, just `@guarded` functions,
  covering readonly caching, mutating idempotency, a volatile LLM call, and
  an approval bypass threshold.
- `examples/langchain_demo.py`: stacks under LangChain's `@tool`.
- `examples/openai_agents_demo.py`: stacks under the OpenAI Agents SDK's
  `@function_tool`.

## Development

Uses [uv](https://docs.astral.sh/uv/) for dependency management:

```
uv sync --extra dev
uv run pytest
```

Postgres-backed tests are skipped unless `TBAY_TEST_PG_DSN` is set to a
running Postgres instance (CI provides one automatically).

## License

MIT
