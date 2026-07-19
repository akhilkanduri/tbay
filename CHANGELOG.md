# Changelog

## Unreleased

- Repo health: `.github/SECURITY.md` (reporting process + the honest
  trust-model scope), a pull-request template with a safety-behavior
  checklist, and structured issue forms for bugs and feature requests.
- Coverage pipeline: a workflow running the full suite (all three
  backends) under `pytest-cov` and publishing a self-hosted coverage
  badge to the `badges` branch — no external coverage service — now shown
  on the README.
- README rewritten around the **toolbay** framing: badge row, the
  failure-mode table ("what it stops"), the plans/decides/executes
  diagram, and direct paths into the tutorial and design docs.
- `scripts/create_roadmap_issues.sh`: seeds the maintenance roadmap
  (pooling, push-based waits, retention, `tbay-mcp`, per-agent budgets,
  and more) as labeled GitHub issues via `gh`.

## 0.3.0

Safety, observability, and operability for agent fleets. Fully backward
compatible: existing databases are migrated in place at client startup,
and no existing API changed shape.

- **Kill switch**: `tbay pause [--tool NAME] [--reason ...]` /
  `tbay resume` (or `client.pause()` / `client.resume()`) stops guarded
  calls immediately, across every process and host sharing the database.
  Blocked calls raise the new `ToolPaused` with the pause reason. Pauses
  survive `tbay clear`.
- **Spend budgets**: `budget: {arg: amount, max: 1000, per: 1d}` on a
  policy caps the SUM of a numeric argument over a rolling window, per
  tool per tenant, atomically across processes; rate limits count calls,
  budgets meter magnitude. Past the cap (or when the metered argument is
  missing/non-numeric) the new `BudgetExceeded` is raised and the tool
  never runs.
- **Lifecycle events**: `client.on(...)` subscribes handlers (optionally
  filtered by type) to structured events for every decision tbay makes:
  call started/succeeded/failed, cache and semantic-cache hits,
  singleflight coalescing, approval requested/approved/rejected, rate/
  budget/concurrency refusals, kill-switch blocks. Handler exceptions are
  isolated and can never break a guarded call.
- **OpenTelemetry bridge** (`pip install tbay[otel]`):
  `tbay.otel.instrument(client)` turns every guarded call into a span
  (nesting under your agent framework's traces) with policy, tenant,
  agent, and outcome attributes; refusals and cache hits become short
  spans of their own, and failures get error status.
- **Stale-lease crash recovery**: a policy-level `lease_timeout` lets the
  next caller atomically reclaim a RUNNING execution abandoned by a
  crashed process (created_at CAS; exactly one contender wins, on every
  backend). On by default only for `readonly` (10m), where a re-run is
  harmless.
- **Deep redaction**: `redact_args` now masks at any depth (including
  inside lists) and supports dotted paths ("card.number"); new
  `redact_patterns` (regexes against key names) and `redact_auto` (mask
  well-known secret-ish names like password/token/api_key). Redaction now
  also applies to webhook payloads. Exported as `tbay.redact_structure`.
- **Hardened webhooks**: approval webhooks now carry the full decision
  context (tool, tenant, policy, redacted args, agent, reasoning), fire
  only for http(s) URLs (a policy file can no longer point tbay at
  file:// or similar), and are HMAC-signed (X-Tbay-Signature) when an
  approval secret is configured; `verify_webhook` checks them
  receiver-side.
- **Execution timeout fix**: the sync `execution_timeout` path no longer
  blocks until the hung call eventually finishes (the ThreadPoolExecutor
  context manager joined the worker thread); it now returns as soon as
  the timeout fires.
- **Policy files fail loud**: unknown keys are rejected at load time
  with the list of valid keys; a `rate_limit` must have both `max_calls`
  and `per` (half a rate limit previously crashed at call time with a
  bare TypeError); a `budget` must have `arg`/`max`/`per`; and
  `approval_bypass_arg`/`approval_bypass_max` must be set together (one
  without the other previously meant the bypass silently never fired).
- **Tutorial series** (`examples/tutorial/`): 13 runnable, narrated,
  self-verifying scripts covering every feature step by step against
  throwaway SQLite databases, plus `docs/design.md` explaining the
  reasoning behind each design decision.
- **CLI**: new `pending` (everything awaiting a human, oldest first, with
  args/agent/reasoning), `show <id>` (full record + approval row),
  `stats` (counts by status/tool + active pauses), `export` (audit log as
  JSON Lines), `pause`/`resume`, and `policies` (every effective policy
  and its key settings).
- **`guard_tools()`**: wrap a list or dict of tool functions under one
  policy in a single call; new docs/integrations.md with LangChain,
  OpenAI Agents SDK, CrewAI, and MCP-server recipes.
- **Typing and tooling**: ships `py.typed` (PEP 561), CI now tests 3.9,
  3.11, and 3.13 and runs ruff, and the package declares explicit
  3.9-3.13 classifiers.

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
- Agent identity with metadata: `with tbay.agent("billing-agent-7",
  model="gpt-5", team="payments")`, TbayClient(agent_id=..., agent_meta=...),
  or TBAY_AGENT_ID records WHICH agent asked for every call, shown in
  `tbay log` and the dashboard (chip, column, and metadata JSON in detail).
- Rejection reasons: `tbay reject <id> --reason "..."` (the dashboard
  prompts for one); the blocked caller's ApprovalRejected error and the
  audit log both carry it.
- Documentation split into docs/ (quickstart, policies, caching, approvals,
  observability, backends, API reference); the demo is Postgres-only.
- Signed approvals: with an approval secret configured (TBAY_APPROVAL_SECRET
  or TbayClient(approval_secret=...)), approvals carry an HMAC signature the
  executing client verifies before running, so raw database credentials
  alone can no longer approve anything.
- `tbay clear` CLI command to wipe all executions/approvals (Redis variant
  deletes only tbay's own keys).
- Monitoring dashboard (`dashboard/`, standalone, not part of the package):
  a single-file web app showing live in-flight calls with elapsed timers,
  paused approvals with Approve/Reject buttons, and every execution's
  input, output/error, duration, and reasoning, across multiple backends
  (Postgres, Redis, SQLite) at once.
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
