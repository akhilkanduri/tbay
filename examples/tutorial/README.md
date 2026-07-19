# The tbay tutorial

A step-by-step tour of **every** tbay feature, as small runnable Python
scripts. Each script is self-contained, uses a throwaway SQLite database
(no Postgres, no Redis, nothing to set up), narrates what it is doing as
it runs, and ends by *proving* the behavior with assertions. Read the
code top to bottom, run it, tinker, re-run.

```
pip install tbay          # or: uv add tbay
python examples/tutorial/01_first_guarded_tool.py
```

Every script cleans up after itself. Run them in any order; the numbers
are a suggested learning path.

| # | Script | What you'll learn |
|---|---|---|
| 01 | [`01_first_guarded_tool.py`](01_first_guarded_tool.py) | The core loop: `TbayClient`, `@guarded`, why the second identical call doesn't run, and what landed in the audit log |
| 02 | [`02_policies.py`](02_policies.py) | The four built-in risk tiers, writing a policy YAML, overriding in code, and how typos in a policy file are caught |
| 03 | [`03_caching_and_idempotency.py`](03_caching_and_idempotency.py) | `cache_ttl`, custom idempotency keys (`key_fn`), singleflight under real threads, failure replay + retries, volatile tools, semantic caching |
| 04 | [`04_approvals.py`](04_approvals.py) | The pause-for-a-human flow, bypass thresholds, rejection reasons, and cryptographically **signed** approvals (including a tamper demo) |
| 05 | [`05_kill_switch.py`](05_kill_switch.py) | `pause()`/`resume()`: stopping every process sharing the database, per-tool scoping, `ToolPaused`, and why a pause survives `clear` |
| 06 | [`06_budgets.py`](06_budgets.py) | Spend caps: metering the SUM of an argument over a rolling window, the exact cap semantics, unmeterable calls, window rollover |
| 07 | [`07_redaction.py`](07_redaction.py) | Keeping secrets out of the audit log: names at any depth, dotted paths, regex patterns, and `redact_auto` |
| 08 | [`08_limits_timeouts_crash_recovery.py`](08_limits_timeouts_crash_recovery.py) | Rate limits, `max_concurrent` under threads, `execution_timeout`, and `lease_timeout` recovering from a crashed process |
| 09 | [`09_events.py`](09_events.py) | The lifecycle event system: subscribing, filtering, every event type, error isolation, and building a tiny metrics counter |
| 10 | [`10_agents_and_reasoning.py`](10_agents_and_reasoning.py) | `with agent(...)` and `with reasoning(...)`: who asked, why, context isolation across concurrent async agents |
| 11 | [`11_observability_otel.py`](11_observability_otel.py) | OpenTelemetry spans for every guarded call (`pip install tbay[otel]` + an SDK; skips gracefully without) |
| 12 | [`12_cli_tour.py`](12_cli_tour.py) | Driving the `tbay` CLI end to end: `log`, `pending`, `approve`, `show`, `stats`, `pause`/`resume`, `export`, `policies` |
| 13 | [`13_integrations.py`](13_integrations.py) | `guard_tools`, `client.run` without the decorator, async tools, tenants, and the decorator-ordering rule for LangChain/OpenAI SDK/CrewAI/MCP |

## How the scripts explain "why", not just "how"

Each script follows the same shape on purpose:

1. **The problem first.** The docstring states the concrete failure the
   feature exists to prevent (double-charged customer, runaway spend,
   wedged idempotency key) before any API appears.
2. **Observable proof.** Behaviors are demonstrated with counters and
   assertions — "the function ran once for three calls" is *shown*, not
   claimed. If you edit a script and break an invariant, it fails.
3. **The deliberate edge cases.** Where tbay chose a trade-off (budgets
   refuse unmeterable calls, lease recovery is off for mutating tools,
   handler crashes are swallowed), the script demonstrates the edge and
   says why that side of the trade was picked.
4. **A "WHAT JUST HAPPENED" recap** connecting the mechanics back to the
   design, with pointers into the reference docs.

The full reasoning behind every design decision — why a library and not
a service, why the database is the lock, why budgets exist next to rate
limits — lives in [docs/design.md](../../docs/design.md). Read it after
tutorial 05 or so; it will land much better with the mechanics fresh.

## Where to go next

- [`../demo.py`](../demo.py) — everything at once against Postgres, with
  the live dashboard.
- [docs/design.md](../../docs/design.md) — the "why" behind each
  mechanism, in one place.
- [`../../docs/`](../../docs/README.md) — the reference docs each script
  links to for deeper detail.
