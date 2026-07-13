# tbay documentation

Execution safety for AI agent tool calls, as a library you install, not a
service you depend on.

| Guide | What it covers |
|---|---|
| [Quickstart](quickstart.md) | Install, first guarded tool, the dev container, running the demo |
| [Policies](policies.md) | The four built-in risk tiers, the YAML file, every policy field |
| [Caching and idempotency](caching.md) | Idempotency keys, TTL caching, singleflight, semantic caching, volatile calls |
| [Approvals](approvals.md) | The pause/approve flow, webhooks, bypass thresholds, signed approvals, rejection reasons |
| [Observability](observability.md) | The audit log, reasoning traces, agent identity, the CLI, the toolbay monitor dashboard |
| [Storage backends](backends.md) | SQLite, Postgres, and Redis: guarantees, schema, migrations, clearing data |
| [API reference](api.md) | Every public function and class, with examples |

New here? Read [Quickstart](quickstart.md), then skim
[Policies](policies.md). Everything else can wait until you need it.
