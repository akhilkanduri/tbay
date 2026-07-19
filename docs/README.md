# tbay documentation

Execution safety for AI agent tool calls, as a library you install, not a
service you depend on.

| Guide | What it covers |
|---|---|
| [Quickstart](quickstart.md) | Install, first guarded tool, the dev container, running the demo |
| [Tutorial](../examples/tutorial/README.md) | 13 runnable, narrated scripts covering every feature step by step |
| [Design rationale](design.md) | WHY everything works the way it does: the trade-offs behind each decision |
| [Policies](policies.md) | The four built-in risk tiers, the YAML file, every policy field |
| [Caching and idempotency](caching.md) | Idempotency keys, TTL caching, singleflight, semantic caching, volatile calls |
| [Approvals](approvals.md) | The pause/approve flow, signed webhooks, bypass thresholds, signed approvals, rejection reasons |
| [Runtime controls](controls.md) | The kill switch (`tbay pause`), spend budgets, stale-lease crash recovery |
| [Observability](observability.md) | The audit log, lifecycle events, OpenTelemetry, reasoning traces, agent identity, the CLI, the dashboard |
| [Integrations](integrations.md) | LangChain, OpenAI Agents SDK, CrewAI, MCP servers, `guard_tools`, wiring events |
| [Storage backends](backends.md) | SQLite, Postgres, and Redis: guarantees, schema, migrations, clearing data |
| [API reference](api.md) | Every public function and class, with examples |

New here? Read [Quickstart](quickstart.md), then run the
[tutorial scripts](../examples/tutorial/README.md) in order — each one
demonstrates a feature live and explains why it behaves that way.
Everything else can wait until you need it.
