# tbay

Execution safety for AI agent tool calls: idempotency, TTL and semantic
caching, singleflight deduplication, risk-tiered policy, human approval
gating, and a reasoning-linked audit log, as a library you install, not a
service you depend on.

```python
from tbay import TbayClient, guarded

client = TbayClient("sqlite:///~/.tbay/db.sqlite")
# or: TbayClient("postgresql://user:pass@host/dbname")
# or: TbayClient("redis://localhost:6379/0")

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
pip install tbay[redis]        # + Redis backend

# or, with uv:
uv add tbay
uv add "tbay[postgres]"
uv add "tbay[redis]"
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

Coordination works the same way over all three backends: SQLite (zero
setup, single machine), Postgres (shared, via `INSERT ... ON CONFLICT` and
an advisory lock), or Redis (shared, low latency, via Lua scripts that
Redis executes as one uninterruptible unit).

## Semantic caching

Exact-match caching misses when an agent rewords its own query:
`"weather in berlin today"` and `"today weather in berlin"` hash to
different idempotency keys even though any human would call them the same
question. With `semantic_cache: true` on a policy, tbay embeds each call's
arguments and serves a stored result whenever a previous call's embedding
is close enough (`semantic_threshold`, cosine similarity, default 0.92):

```yaml
policies:
  semantic_readonly:
    cache_ttl: 5m
    semantic_cache: true
    semantic_threshold: 0.92
```

Only enable this on read-only tools. A "close enough" answer is fine for a
search; it is not fine for a refund.

The built-in zero-dependency embedder (token hashing) matches queries that
reuse the same words in a different order or shape. For true paraphrase
matching ("weather in berlin" vs "berlin forecast"), plug in a real
embedding model; anything with an `embed(text) -> list[float]` method works:

```python
class OpenAIEmbedder:
    def __init__(self, client):
        self.client = client

    def embed(self, text):
        out = self.client.embeddings.create(model="text-embedding-3-small", input=text)
        return out.data[0].embedding

client = TbayClient(db_url, embedder=OpenAIEmbedder(openai_client))
```

## Reasoning-linked audit

The audit log records what ran. `with reasoning(...)` also records *why*,
straight from the agent's own justification, next to the call it explains:

```python
from tbay import reasoning

with reasoning("customer 42 reported item damaged in transit"):
    refund_customer("cust_42", 30.0)
```

`tbay log` then shows `reason='customer 42 reported item damaged in
transit'` on that execution, which is usually the first thing a human
approver or a post-incident review wants to know. Blocks nest, and
concurrent async tasks each see their own reasoning text.

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
tbay clear                                 # wipe ALL executions/approvals (asks first)
```

Point the CLI at the same database as your app with `--db-url` or the
`TBAY_DB_URL` environment variable. `tbay clear` resets a demo or dev
database: every execution, cached result, and approval is deleted (on
Redis it removes only tbay's own keys, never anything else in that
database). Add `--yes` to skip the confirmation prompt.

For a GUI instead, the dev container ships with the SQLTools extension
(preconfigured "tbay postgres" connection: browse and edit the
`executions` and `approvals` tables) and the Redis for VS Code extension
(connect to `localhost:6379` and browse the `tbay:` keys).

## Monitoring dashboard

`dashboard/` contains a small standalone web app (not part of the Python
package) for watching your agents' tool calls live: what's RUNNING right
now with an elapsed timer (a call that started a container or a long job
stays visible until it returns), what's paused in WAITING_APPROVAL with
Approve/Reject buttons, and every finished call with its input arguments,
output or error, duration, and recorded reasoning.

```
uv sync --extra dev
uv run python dashboard/app.py --db postgresql://postgres:tbay@localhost:5432/tbay \
                               --db redis://localhost:6379/0
```

Then open http://localhost:8787. One dashboard can watch several backends
at once (Postgres, Redis, SQLite, in any combination); see
`dashboard/README.md` for all options.

The dev container's Postgres and Redis listen on `localhost` inside the
container, and VS Code forwards 5432/6379/8787 to your machine, so the
command above works in either place while the devcontainer is open.
Inside the container you can also drop the `--db` flags entirely, since
`TBAY_DASHBOARD_DBS` is pre-wired:

```
uv run python dashboard/app.py
```

## Examples

There is one example: `examples/demo.py`. It walks every feature in
order, in a single run: readonly caching, semantic caching on a reworded
query, mutating idempotency, volatile LLM-style calls, the reasoning
audit, the Redis backend, stacking under LangChain / OpenAI Agents SDK
tool decorators (when those packages are installed), and finally the full
approval flow: a small refund bypassing approval, a large one pausing, the
webhook firing against a bundled stand-in server that prints the payload,
and you approving or rejecting it.

### Running it in the dev container (recommended)

The dev container pre-sets `TBAY_DB_URL` to its bundled Postgres and
`TBAY_TEST_REDIS_URL` to its bundled Redis, and the demo reads both, so
there is nothing to configure:

1. Open the repo in VS Code and run "Dev Containers: Reopen in Container"
   (first build takes a few minutes).
2. Terminal 1: `uv run python dashboard/app.py`, then open the forwarded
   port 8787 (Ports tab). Empty at first.
3. Terminal 2: `uv run python examples/demo.py`. Every step appears on
   the dashboard as it happens.
4. The demo ends blocked on a $500 refund in WAITING_APPROVAL. Approve or
   reject it from the dashboard row, or in a third terminal with the
   `tbay approve <execution_id>` command the demo prints. The blocked run
   then finishes.

### Running it outside the container

With no environment variables set, the demo falls back to a local SQLite
file (`sqlite:///~/.tbay/demo.sqlite`) and skips the Redis step; that
works with nothing but `uv sync --extra dev`. To run it against your own
servers instead, set the same two variables the container sets:

```
export TBAY_DB_URL="postgresql://user:pass@host:5432/dbname"
export TBAY_TEST_REDIS_URL="redis://host:6379/0"   # optional
uv run python examples/demo.py
```

Point the dashboard at the same URLs to watch it:
`uv run python dashboard/app.py --db "$TBAY_DB_URL"`.

## Development

The repo ships a [dev container](https://containers.dev/): open it in
VS Code ("Dev Containers: Reopen in Container") or GitHub Codespaces and
you get Python 3.12 with uv, plus real Postgres and Redis services already
wired to the right environment variables. Inside it, everything just runs:

```
uv run python examples/demo.py
uv run pytest        # includes the Postgres- and Redis-gated tests
```

Working without the container uses [uv](https://docs.astral.sh/uv/)
directly:

```
uv sync --extra dev
uv run pytest
```

Postgres- and Redis-backed tests are skipped unless `TBAY_TEST_PG_DSN` and
`TBAY_TEST_REDIS_URL` point at running servers (CI and the dev container
provide both automatically).

## License

MIT
