# Changelog

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
