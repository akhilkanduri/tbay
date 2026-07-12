# Changelog

## 0.2.0

- Redis storage backend (`pip install tbay[redis]`, `redis://` URLs):
  shared, low-latency coordination for multi-process deployments, with the
  idempotency-key claim and the `max_concurrent` check made atomic through
  server-side Lua scripts.
- Semantic caching: `semantic_cache: true` on a policy serves a stored
  result when a new call's arguments are similar by embedding cosine
  similarity (`semantic_threshold`, default 0.92), not just byte-identical.
  Ships a zero-dependency token-hashing embedder; any object with an
  `embed(text) -> list[float]` method plugs in via
  `TbayClient(embedder=...)` for real paraphrase matching.
- Reasoning-linked audit log: `with tbay.reasoning("why")` records the
  agent's stated justification on every execution started inside the block,
  shown by `tbay log` as `reason=...`. Context-local, so concurrent async
  tasks and threads never mix their reasoning up.
- Dev container (`.devcontainer/`) with Python 3.12 + uv, Postgres, and
  Redis preconfigured, so the examples and the full test suite (including
  the Postgres- and Redis-gated tests) run with zero local setup.
- Existing SQLite/Postgres databases from 0.1.0 are migrated in place (two
  new nullable columns); no manual steps.

## 0.1.0

Initial release.

- `@guarded` decorator for sync and async tool functions: idempotency, TTL
  caching, singleflight dedup, and approval gating, all in-process, no
  daemon or network hop.
- Pluggable storage: SQLite (stdlib, zero extra deps) and Postgres
  (`pip install tbay[postgres]`).
- Policies: `readonly`, `mutating`, `destructive`, and `volatile` (for
  non-idempotent calls like an LLM decision or a random value), plus a
  YAML policy file format for overriding or adding your own.
- Bounded retries with backoff, rate limiting, concurrency capping (atomic
  under both backends), and best-effort per-call execution timeouts.
- Human approval workflow: `tbay approve`/`tbay reject`, an optional
  best-effort webhook notification, and an approval bypass threshold for
  low-risk calls (e.g. auto-run refunds under $50).
- Audit log with each call's (optionally redacted) arguments, queryable via
  `tbay log` or SQL directly.
- Examples for plain Python, LangChain, and the OpenAI Agents SDK.
