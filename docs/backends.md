# Storage backends

The backend is chosen by URL scheme; nothing else in your code changes:

```python
TbayClient("sqlite:///~/.tbay/db.sqlite")                  # local, zero setup
TbayClient("postgresql://user:pass@host:5432/dbname")      # shared, durable
TbayClient("redis://host:6379/0")                          # shared, low latency
```

## What every backend guarantees

Coordination across processes and hosts happens entirely through the
database. Claiming an idempotency key is atomic (first caller wins,
everyone else observes their row), and the `max_concurrent` check is part
of the same atomic step as the claim, so two simultaneous callers can
never both slip past the cap:

| Backend | Atomicity mechanism | Best for |
|---|---|---|
| SQLite | `BEGIN IMMEDIATE` transaction | local dev, single machine, zero deps |
| Postgres | `INSERT ... ON CONFLICT` + `pg_advisory_xact_lock` | production, shared, durable |
| Redis | server-side Lua scripts (uninterruptible) | shared, lowest latency |

On Redis, records persist like SQL rows (everything lives under the
`tbay:` key prefix), so the audit log survives restarts as long as Redis
itself is persistent (AOF/RDB).

## Schema

Two tables (or their Redis-hash equivalents):

- `executions`: one row per tool call. Identity (`tool_name`,
  `idempotency_key`, `tenant`, unique together), state (`status`,
  `result_json`, `error`, `retry_count`, timestamps), audit fields
  (`args_json`, `reasoning`, `agent_id`, `agent_meta`), and
  `embedding_json` for semantic caching.
- `approvals`: one row per approval request: `status`
  (pending/approved/rejected), `resolver`, `signature` (see
  [signed approvals](approvals.md#signed-approvals-separating-storage-access-from-approval-authority)),
  `note` (the rejection reason), timestamps.

Databases created by older tbay versions are migrated in place at client
startup (columns are added, nothing is dropped); no manual steps.

## Multi-tenancy

Every guarded call takes a `tenant` (default `""`), which is part of the
idempotency key: `@guarded(client, policy="mutating", tenant="acme")`.
Two tenants calling the same tool with the same args get separate
executions, separate caches, and separate rate-limit buckets.

## Clearing data

```
tbay clear         # asks for confirmation
tbay clear --yes
```

Deletes every execution and approval from the connected database. On
Redis only tbay's own prefixed keys are removed, never other data in the
same database. Remember the trust model: anyone with database write access
can do this with plain SQL too, so protect production databases with
least-privilege roles, not hope.
