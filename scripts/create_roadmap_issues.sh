#!/usr/bin/env bash
# Creates the tbay roadmap as GitHub issues, with labels, via the gh CLI.
#
#   ./scripts/create_roadmap_issues.sh              # against akhilkanduri/tbay
#   REPO=you/fork ./scripts/create_roadmap_issues.sh
#
# Requires: gh (authenticated: `gh auth login`). Safe to re-run pieces by
# hand; it does NOT dedupe, so run it once.
set -euo pipefail

REPO="${REPO:-akhilkanduri/tbay}"

echo "Creating labels on $REPO (existing ones are left untouched)..."
gh label create performance   -R "$REPO" --color 1D76DB --description "Latency, throughput, resource usage" 2>/dev/null || true
gh label create safety        -R "$REPO" --color B60205 --description "Guardrail semantics and coverage"    2>/dev/null || true
gh label create observability -R "$REPO" --color 0E8A16 --description "Events, metrics, tracing, dashboard" 2>/dev/null || true
gh label create ecosystem     -R "$REPO" --color 5319E7 --description "Framework/MCP/tooling integrations"  2>/dev/null || true
gh label create ops           -R "$REPO" --color FBCA04 --description "Running tbay in production"          2>/dev/null || true

new_issue() {  # new_issue <title> <labels-csv>  (body on stdin)
  local title="$1" labels="$2" body
  body="$(cat)"
  gh issue create -R "$REPO" --title "$title" --label "$labels" --body "$body" >/dev/null \
    && echo "  created: $title"
}

# ---------------------------------------------------------------- tier 1: production hardness

new_issue "Connection pooling for the Postgres backend" "performance" <<'EOF'
**Problem.** `PostgresBackend` opens a fresh connection per operation
(`_connect()` in `src/tbay/backends/postgres_backend.py`), and one guarded
call performs several operations (kill-switch check → acquire → limit
checks → complete). Under load that's connection-storm territory and adds
tens of ms per call.

**Proposal.** A small connection pool (psycopg2 `ThreadedConnectionPool`,
or a seam that also allows psycopg3). Pool size configurable via
`TbayClient(...)`; connections validated/reopened on failure so a database
restart doesn't strand the client. SQLite keeps its short-lived
connections (that's what makes it thread-safe today).

**Acceptance.**
- No behavior change to any atomicity guarantee (the advisory-lock and
  ON CONFLICT paths must still hold under the pool).
- Benchmark in the PR: guarded-call overhead before/after at 1/8/32
  concurrent callers.
- Full suite green; a pool-exhaustion path that fails loud rather than
  deadlocking.
EOF

new_issue "Push-based waits: Postgres LISTEN/NOTIFY and Redis pub/sub instead of polling" "performance" <<'EOF'
**Problem.** Approval waits and singleflight followers poll every
`poll_interval` (default 250ms): approvals feel laggy and idle polling
loads the database (`wait_for_approval` / `wait_for_result` in
`src/tbay/backends/base.py`).

**Proposal.** Backends optionally push: `NOTIFY tbay_events` on
complete/fail/resolve_approval in Postgres; `PUBLISH` on the same in
Redis; waiters block on the notification with the current polling loop as
the fallback (and the SQLite implementation unchanged). Keep the public
`StorageBackend` contract; add an optional `wait_hint()` capability so the
client uses push when available.

**Acceptance.** Approval latency drops from O(poll_interval) to ~ms on
Postgres/Redis; polling fallback still exercised in tests; no new
required dependencies.
EOF

new_issue "Audit log retention: `tbay prune` and policy-driven TTL" "ops" <<'EOF'
**Problem.** The audit log grows forever. A year of agent traffic makes
`list_executions`, `stats`, and semantic-candidate scans slow, and
unbounded PII retention is a compliance problem in itself.

**Proposal.**
- `tbay prune --older-than 30d [--tool X] [--dry-run]`: delete finished
  executions (SUCCEEDED/FAILED) older than the cutoff. Never touch
  RUNNING or WAITING_APPROVAL rows; never delete rows still inside their
  `cache_ttl` (pruning must not cause silent re-execution of an
  idempotent call).
- Optional client-side retention setting that prunes opportunistically.
- Works on all three backends (Redis: trim the zsets + delete hashes).

**Acceptance.** Prune of 1M rows completes batched without long locks;
tests prove cache/idempotency semantics survive; docs/backends.md updated.
EOF

new_issue "Strict result serialization mode (fail loud on non-JSON results)" "safety" <<'EOF'
**Problem.** Results are stored via `json.dumps(result, default=str)`.
A tool returning datetimes, tuples, or dataclasses gets silently mangled
— and only on the *cache-hit* path, so first caller and followers see
different shapes. Classic silent-behavior-change, which the rest of tbay
is explicitly designed against (see docs/design.md "fail loud").

**Proposal.** `TbayClient(strict_results=True)` (consider defaulting on
in 0.4): serialization failure marks the execution FAILED with a clear
"result not JSON-serializable: <path>" error instead of coercing.
Document the round-trip contract prominently in docs/caching.md either way.

**Acceptance.** Strict mode test per backend; error names the offending
key/path; changelog + docs updated.
EOF

# ---------------------------------------------------------------- tier 2: ecosystem

new_issue "tbay-mcp: wrap ANY MCP server in the safety layer without code changes" "ecosystem" <<'EOF'
**The flagship idea.** docs/integrations.md covers guarding MCP servers
you own. The bigger win is servers you *don't* own: a proxy that speaks
MCP on both sides and guards every tool call passing through:

    tbay-mcp --db postgresql://... --policy policy.yaml -- npx some-mcp-server

Every MCP client (Claude, Cursor, anything) connecting through the proxy
inherits idempotency, budgets, rate limits, approvals, and the kill
switch — for tools whose code you never touch. Policy mapping by tool
name (globs), default policy for unmatched tools.

**Sketch.** Separate package (`tbay-mcp`) depending on `tbay` + the MCP
SDK, so the core stays stdlib+yaml+click. Stdio transport first; tool
list passthrough; guarded dispatch; `WAITING_APPROVAL` surfaced as an
in-protocol progress/notification message where the client supports it.

**Acceptance.** Demo: wrap a reference MCP server, show a destructive
tool pausing for `tbay approve` while the MCP client waits; README badge
moment. This is the feature most likely to make the project widely known
— it deserves its own repo + announcement post.
EOF

new_issue "Slack approver reference implementation (signed webhook -> approve/reject buttons)" "ecosystem" <<'EOF'
**Problem.** Approvals live or die on the human side being effortless.
The signed webhook already carries everything needed (tool, redacted
args, agent, reasoning, HMAC signature) — there's just no reference
receiver.

**Proposal.** `examples/slack_approver/`: a small Flask/FastAPI app that
verifies `X-Tbay-Signature` (`tbay.verify_webhook`), posts a Slack Block
Kit message with Approve/Reject buttons, and writes the signed decision
via `resolve_approval` (+ `sign_approval`) with the Slack user as
resolver and rejection reason from a modal. Deployable on anything that
runs Python.

**Acceptance.** Runs against the dev container end to end; README GIF of
a refund approved from Slack; docs/approvals.md links it.
EOF

new_issue "Prometheus/OpenMetrics bridge built on lifecycle events" "observability" <<'EOF'
**Problem.** The OTel bridge covers tracing; fleets that alert on
Prometheus have nothing first-class.

**Proposal.** `tbay.prometheus.instrument(client, registry=None)` (extra:
`tbay[prometheus]`), an event subscriber exporting counters
(`tbay_calls_total{tool,policy,outcome}`, `tbay_blocked_total{reason}`),
a duration histogram, and gauges for pending approvals + active pauses.
~60 lines, same shape as `tbay/otel.py`.

**Acceptance.** Works with the default registry and a custom one; sample
Grafana panel JSON in docs; no hard dependency added to core.
EOF

# ---------------------------------------------------------------- tier 3: safety depth

new_issue "Per-agent budgets and rate limits" "safety" <<'EOF'
**Problem.** Budgets/limits scope to (tool, tenant). Agents are the
natural risk unit now: "agent-7 may move at most $500/day *across all
tools*" or "each agent gets 100 calls/hour" is inexpressible today, even
though `agent_id` is already recorded on every execution.

**Proposal.** Policy (or client-level) fields scoped by agent, e.g.
`agent_budget: {arg: amount, max: 500, per: 1d}` metering per
`agent_id`, and `agent_rate_limit`. Needs backend sums/counts keyed by
agent (a `sum_budget_since`/`count_since` variant); unattributed calls
(no agent_id) under an agent-scoped policy should fail loud rather than
slip the cap.

**Acceptance.** Works across processes like tool budgets; events carry
the agent scope; tutorial 06/10 extended; all three backends.
EOF

new_issue "Time-bounded pause: tbay pause --for 30m" "safety" <<'EOF'
**Problem.** Pauses last until someone remembers to resume. For "pause
while I look at this" the failure mode is a tool left off all weekend.

**Proposal.** `tbay pause --for 30m` (and `client.pause(..., ttl=...)`)
storing an `expires_at` in the control value; `_check_kill_switch`
treats an expired pause as lifted (and may lazily delete it). `tbay
stats` shows time remaining. No background process — expiry is evaluated
at read time, consistent with the no-daemon design.

**Acceptance.** Expired pause unblocks calls with no resume needed;
survives clear like other controls; docs/controls.md updated. Good first
issue for someone who has read tutorial 05.
EOF

new_issue "Approval secret rotation: accept multiple valid secrets" "safety" <<'EOF'
**Problem.** `approval_secret` is a single shared HMAC key. Rotating it
today means a flag-day: in-flight approvals signed with the old secret
fail verification the moment the executing clients restart with the new
one.

**Proposal.** Accept a list (TBAY_APPROVAL_SECRET may be
comma-separated; `TbayClient(approval_secret=[new, old])`). Signing
always uses the first; verification accepts any, constant-time per
candidate. Document the rotation runbook (add new -> roll approvers ->
drop old) in docs/approvals.md.

**Acceptance.** Verification tests for old+new during overlap; no timing
shortcut that leaks which secret matched.
EOF

# ---------------------------------------------------------------- tier 4: platform

new_issue "Dashboard: surface pauses, budget consumption, and the live event stream" "observability" <<'EOF'
**Problem.** The dashboard predates 0.3.0: it shows executions and
approvals, but not the new safety state — you can't see that everything
is paused, how much of a budget window is spent, or the live decision
stream.

**Proposal.**
- A prominent PAUSED banner (global/per-tool, with reason/by/age) and
  resume buttons.
- Per-tool budget meters (spent vs max over the window) computed via
  `sum_budget_since`.
- A live event feed panel fed by a small `client.on(...)` -> SSE relay.

**Acceptance.** Works across the same multi-backend setup the dashboard
already supports; pause/resume from the UI records who clicked.
EOF

new_issue "Multiprocess concurrency stress test in CI" "safety" <<'EOF'
**Problem.** The atomicity claims (single execution per key,
max_concurrent never exceeded, exactly-one stale-lease reclaim, budget
cap under parallel spend) are tested with threads in one process. The
real deployment story is N processes on M hosts; nothing hammers that in
CI.

**Proposal.** A `multiprocessing`-based stress test (marked `slow`, run
in a dedicated CI job): 8-16 processes x hundreds of calls against
Postgres and Redis, asserting invariants from the audit log afterwards
(exactly one SUCCEEDED execution per key; RUNNING concurrency never
exceeded — sampled via a sentinel table; budget totals never past cap).

**Acceptance.** Runs in <2 min in CI; failures print the violating
execution rows; flake-free across 50 consecutive runs.
EOF

new_issue "Async-native backends (asyncpg / redis.asyncio)" "performance" <<'EOF'
**Problem.** `run_async` is correct but wraps every blocking backend call
in `asyncio.to_thread`, so a high-concurrency async agent pays thread
pool overhead per operation and holds threads while polling.

**Proposal.** An `AsyncStorageBackend` counterpart (asyncpg,
redis.asyncio) selected automatically for `run_async` when the driver is
installed, falling back to to_thread otherwise. Combines naturally with
push-based waits (LISTEN/NOTIFY) — consider sequencing after that issue.

**Acceptance.** No public API change; async suite passes on both native
and fallback paths; benchmark showing the win at 100+ concurrent guarded
coroutines.
EOF

new_issue "Record the 60-second demo GIF for the README" "ecosystem" <<'EOF'
**Problem.** The README's hero moment (dashboard shows a blocked $500
refund; `tbay pause` freezes an agent mid-loop; approve resumes it) is
described in words. Nothing sells execution safety like watching it.

**Proposal.** Script the dev-container demo (`examples/demo.py` +
dashboard), record ~60s (vhs/asciinema+agg for terminal, or a screen
capture including the dashboard), commit under `docs/assets/`, embed at
the top of the README.

**Acceptance.** <5 MB, readable at README width, shows: guarded call
dedup -> blocked refund -> dashboard approve -> kill switch. Good first
issue.
EOF

echo
echo "Done. View them: gh issue list -R $REPO"
