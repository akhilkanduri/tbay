# API reference

Everything importable from `tbay`, with examples. Import surface:

```python
from tbay import (
    TbayClient, guarded,
    agent, current_agent, current_agent_meta,
    reasoning, current_reasoning,
    sign_approval, verify_approval,
    HashingEmbedder, cosine_similarity,
    TbayError, ApprovalRejected, ApprovalTimeout, ExecutionFailed,
    ExecutionTimeout, RateLimitExceeded, ConcurrencyLimitExceeded,
)
```

---

## `TbayClient`

```python
TbayClient(
    db_url="sqlite:///~/.tbay/db.sqlite",
    policy_file=None,
    poll_interval=0.25,
    embedder=None,
    agent_id=None,
    agent_meta=None,
    approval_secret=None,
)
```

The embeddable client. Creating one connects to the database, creates or
migrates the schema, and loads policies. There is no daemon or network
hop; multiple processes pointing at the same `db_url` coordinate through
the database itself.

| Parameter | Meaning |
|---|---|
| `db_url` | `sqlite:///path`, `postgresql://user:pass@host/db`, or `redis://host:6379/0`. Chooses the backend. |
| `policy_file` | Path to a policy YAML file, merged over the built-in defaults. See [Policies](policies.md). |
| `poll_interval` | Seconds between polls while following another caller's execution or waiting for approval. |
| `embedder` | Object with `embed(text) -> list[float]`, used by `semantic_cache` policies. Defaults to `HashingEmbedder` on first use. |
| `agent_id` | Default agent identity recorded on every execution this client starts. Falls back to `$TBAY_AGENT_ID`. A surrounding `agent()` block overrides it. |
| `agent_meta` | Dict of metadata stored (as JSON) alongside `agent_id`. |
| `approval_secret` | Enables [signed approvals](approvals.md). Falls back to `$TBAY_APPROVAL_SECRET`. |

Attributes: `client.policies` (name to `Policy` dataclass, safely mutable
per client), `client.backend` (the `StorageBackend`).

```python
client = TbayClient(
    "postgresql://postgres:tbay@localhost:5432/tbay",
    policy_file="policy.yaml",
    agent_id="support-bot",
    approval_secret=os.environ["TBAY_APPROVAL_SECRET"],
)
client.policies["readonly"].cache_ttl = 60   # tweak in code
```

### `client.run(fn, *, policy, args, kwargs, tenant="", key_fn=None, tool_name=None)`

The engine underneath `@guarded`; call it directly when decorating isn't
convenient (for example, wrapping a function you don't own):

```python
result = client.run(some_api_call, policy="readonly", args=("query",), kwargs={})
```

`run_async(...)` is the identical coroutine form for async functions.

---

## `guarded(client, *, policy="mutating", key_fn=None, tenant="", tool_name=None)`

Decorator that routes a sync or async callable through the client. It
never inspects the caller's framework, so it stacks under LangChain's
`@tool`, the OpenAI Agents SDK's `@function_tool`, or nothing at all.

| Parameter | Meaning |
|---|---|
| `policy` | Which named policy governs this tool. |
| `key_fn` | `key_fn(*args, **kwargs) -> str`: custom idempotency key. Ignored for `idempotent: false` policies. |
| `tenant` | Namespace for the idempotency key, cache, and rate limits. |
| `tool_name` | Override the recorded name (defaults to `fn.__name__`). |

```python
@guarded(client, policy="mutating", key_fn=lambda order_id, **_: order_id)
def ship_order(order_id: str, carrier: str) -> dict:
    ...

@guarded(client, policy="readonly")
async def fetch_profile(user_id: str) -> dict:   # async works identically
    ...
```

Raises, depending on what happens: `ExecutionFailed` (a stored failure was
replayed), `ApprovalRejected`, `ApprovalTimeout`, `ExecutionTimeout`,
`RateLimitExceeded`, `ConcurrencyLimitExceeded`.

---

## Context managers

### `agent(agent_id, **metadata)`

Attach WHO is calling (and any metadata worth showing a human) to every
guarded call in the block. Context-local: blocks nest, concurrent async
tasks are isolated, spawned threads start clean.

```python
with agent("billing-agent-7", model="gpt-5", team="payments"):
    refund_customer("cust_42", 30.0)
```

`current_agent() -> str | None` and `current_agent_meta() -> dict | None`
read the active block, mostly useful in your own middleware.

### `reasoning(text)`

Attach WHY the agent is calling. Same context semantics as `agent()`.

```python
with reasoning("user asked to escalate the outage"):
    create_ticket("prod outage")
```

`current_reasoning() -> str | None` reads the active block.

---

## Signed approvals

### `sign_approval(secret, execution_id, approved) -> str`

The HMAC-SHA256 signature an approver attaches to a decision. Use it to
build your own approval surface (a Slack bot, an internal tool):

```python
from tbay import sign_approval

sig = sign_approval(secret, execution_id, approved=True)
client.backend.resolve_approval(execution_id, approved=True,
                                resolver="slack:@akhil", signature=sig)
```

### `verify_approval(secret, execution_id, approved, signature) -> bool`

Constant-time check of a stored decision. The executing client calls this
automatically when configured with `approval_secret`; it's exported for
symmetry and your own tooling.

---

## Embedders

### `HashingEmbedder(dims=256)`

The zero-dependency default for semantic caching: token hashing into a
normalized term-frequency vector. Catches reworded-same-words queries,
not true paraphrases; see [Caching](caching.md#semantic-caching) for
plugging in a real model.

### `cosine_similarity(a, b) -> float`

Plain cosine similarity, safe for unnormalized vectors.

---

## Exceptions

All inherit `TbayError`, so `except TbayError` catches everything tbay
raises deliberately.

| Exception | Raised when |
|---|---|
| `ExecutionFailed` | The key's stored failure was replayed instead of re-running (retries exhausted or disallowed). |
| `ApprovalRejected` | A human rejected the call, or (with a secret) an approval failed signature verification. The message carries the rejection reason when one was given. |
| `ApprovalTimeout` | Nobody decided within `approval_timeout`. |
| `ExecutionTimeout` | The call outlived `execution_timeout`, or a follower outlived its wait. |
| `RateLimitExceeded` | The policy's `rate_limit` window is full. |
| `ConcurrencyLimitExceeded` | No `max_concurrent` slot freed up within `concurrency_wait_timeout`. |

---

## Storage layer (advanced)

`client.backend` implements `StorageBackend`
(`src/tbay/backends/base.py`). The methods you might call directly:

| Method | Purpose |
|---|---|
| `list_executions(tool_name=None, status=None, tenant=None, limit=50)` | Query the audit log; returns `ExecutionRecord` dataclasses. |
| `get(execution_id)` | One record by id. |
| `resolve_approval(execution_id, approved, resolver="", signature=None, note=None)` | Record a human decision; `note` is the rejection reason. |
| `get_approval(execution_id)` | The full approval row as a dict. |
| `count_since(tool_name, tenant, since)` | Calls created since a timestamp (backs rate limiting). |
| `clear()` | Delete everything tbay stores; returns the number of executions removed. |

`ExecutionRecord` fields: `id`, `tool_name`, `idempotency_key`, `tenant`,
`status` (`RUNNING` / `SUCCEEDED` / `FAILED` / `WAITING_APPROVAL`),
`args_hash`, `args_json`, `result_json`, `error`, `policy_name`,
`retry_count`, `cache_expires_at`, `created_at`, `finished_at`,
`embedding_json`, `reasoning`, `agent_id`, `agent_meta`.

Writing a new backend means implementing `StorageBackend` behind a new URL
scheme; the atomicity contract is documented on `acquire_or_get` in
`base.py`.
