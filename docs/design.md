# Design: why tbay is built this way

Every notable decision in tbay, with the reasoning. Read this when you
want to know not *what* a feature does (the other docs cover that) but
*why it works the way it does* — and what the deliberate trade-offs are.

## Why a library, not a service

The obvious architecture for "execution safety" is a proxy service that
tool calls flow through. tbay rejects it on purpose:

- **A service is a dependency with its own uptime.** If the safety proxy
  is down, either tools stop working (an outage you caused) or they
  bypass it (a guardrail that vanishes exactly when things are chaotic).
- **A service sees your arguments.** Card numbers, customer data, and
  API keys would transit and be stored by someone else's process. As a
  library, arguments never leave your process except into *your own*
  database, post-redaction.
- **A library is adoptable in one line.** `@guarded` on one function is
  a complete deployment. There is no infrastructure step, so the safe
  path is also the lazy path — which is the only way safety tooling
  actually gets adopted.

The cost: tbay's guarantees are only as strong as your database and
process security. That's stated honestly in the [trust
model](approvals.md#signed-approvals-separating-storage-access-from-approval-authority)
rather than papered over.

## Why the database is the coordination point

Two workers on different hosts must not both run `refund(order_42)`.
Something has to arbitrate, and there are only three candidates:
in-process memory (fails across processes), a coordination service
(see above), or a database you already run. tbay claims an execution by
INSERTing a row with a uniqueness constraint on `(tool_name,
idempotency_key, tenant)` — the database's own atomicity is the lock.
First caller wins; everyone else *observes the winner's row* and follows
it. Every other guarantee (singleflight, `max_concurrent`, budgets,
stale-lease reclaim) is built from the same primitive: an atomic
claim-or-observe against shared storage, implemented as `BEGIN
IMMEDIATE` on SQLite, `ON CONFLICT` + advisory locks on Postgres, and
uninterruptible Lua scripts on Redis.

## Why behavior lives on policies, not on tools

A tool author knows what a function *does*; an operator knows how risky
it is *in this deployment*. Those are different people on different
timelines. Policies split the concern: the function stays a plain
callable, and a YAML file owns caching, approvals, budgets, and limits —
reviewable in a PR, changeable without touching tool code, listable with
`tbay policies`.

This is also why **policy files fail loud**. An unknown key, half a
`rate_limit`, or `approval_bypass_arg` without its threshold is a
load-time error, not a warning. A safety setting that is silently
ignored is worse than a crash: the author *believes* the guardrail
exists. (`aproval_required: true` would otherwise be an approval gate
that simply isn't there.)

## Why "volatile" exists

Idempotency machinery assumes two identical calls are the same call.
For an LLM sampling step, a dice roll, or "get the current time", that
assumption is *wrong* — caching them corrupts behavior rather than
protecting it. Rather than making tools opt out ad hoc, non-idempotency
is a first-class tier (`idempotent: false`) that disables caching,
dedup, and `key_fn` coherently while keeping limits, budgets, the audit
log, and the kill switch.

## Why approval pauses *before* execution, and why signing exists

The approval gate's entire value is its ordering: tbay parks the
execution while it still has control, so "waiting" provably means "has
not run". The webhook is deliberately *not* part of that guarantee — it
is best-effort notification only, because a dead Slack endpoint must
never be able to strand or (worse) release a decision.

Signing exists because of an awkward default: an approval is a database
row, so **database credentials are approval authority**. For teams where
those must differ, an HMAC over `(execution_id, decision)` moves
authority to whoever holds the approval secret; the executing client
verifies before running. The honest limits are documented: someone with
full DB access can still delete history, and someone who can edit the
executing process is past any in-process guardrail. Signing narrows one
specific, common gap; it is not a cryptosystem against root.

## Why budgets exist next to rate limits

A rate limit bounds *frequency*; nothing about frequency bounds
*consequence*. Ten well-paced calls each refunding $9,999 pass any rate
limit ever configured. Budgets meter the thing that actually matters —
the sum of a numeric argument over a rolling window — because for
money-moving, message-sending, resource-provisioning tools, "how much"
is the risk, not "how often". The conservative edge cases are chosen
deliberately: unmeterable calls (missing/non-numeric arg) are refused,
and failed executions still count as spend, because a call that died
mid-flight may still have had its effect. tbay never under-counts.

## Why the kill switch is a database row

In an incident you want one command that stops every worker on every
host *without a deploy, restart, or service discovery*. The only channel
every tbay process already shares is the database, so the pause is a
control row checked at the very top of every guarded call — before
acquiring, before waiting, before anything. Corollaries that fall out of
this design: it takes effect at the next call everywhere at once; it
survives `tbay clear` (resetting demo data must not release an emergency
brake); and it is an *operational* brake, not a security boundary —
anyone with DB write access can lift it, like every other control.

## Why stale-lease recovery is opt-in (and on for `readonly`)

A crashed owner leaves a RUNNING row that blocks its key forever. The
tempting fix — reclaim aggressively everywhere — trades a visible hang
for a silent **double-run**, and for a refund the double-run is the
disaster tbay exists to prevent. So `lease_timeout` is a policy choice:
on by default only for `readonly` (re-running a read is harmless), off
for `mutating`/`destructive`, where a wedged key that a human
investigates is the safer failure. The reclaim itself is a created_at
compare-and-swap so racing contenders can't all "win" and re-run it N
times. `WAITING_APPROVAL` rows are never reclaimed — a pending human
decision has no crash to recover from.

## Why redaction happens at write time, by key name

Arguments are stored once and then fan out everywhere: `tbay log`,
approver screens, webhooks, exports, the dashboard. Masking at the
single write point means no downstream surface can leak what a policy
masked. Matching is by key *name* only — values are never inspected —
because value-sniffing (regexes for card numbers etc.) is
false-positive-prone and gives a false sense of coverage. The whole
value is replaced, so length and shape don't leak either. `redact_auto`
is off by default for the same reason policy typos are errors: behavior
should never change silently.

## Why events are synchronous and in-process

The event system could have been a queue, a log file, or an OTLP
exporter. It is deliberately none of those: just callables invoked
inline, because tbay must not acquire infrastructure dependencies to be
observable. Two consequences are load-bearing: handler exceptions are
swallowed (observability must never take the workload down — the
guarded call always wins), and handlers run on the calling thread (so a
slow handler slows the call it observes; hand off to a queue if you do
real I/O). Everything else — the OTel bridge, metrics, alerting, even a
self-pausing circuit breaker — is a subscriber, which keeps the core
dependency-free while making integrations ~5 lines.

## Why the CLI talks to the database, not to your app

`tbay approve`, `tbay pause`, and `tbay log` work from any machine that
can reach the database, with no port open on your agent workers and no
admin endpoint in your app. The operator surface is the storage layer
itself — which is also why least-privilege database roles are the real
production hardening step.

## Where to see each decision in action

The [tutorial series](../examples/tutorial/README.md) demonstrates every
one of these behaviors as runnable code, in the same order features were
introduced above.
