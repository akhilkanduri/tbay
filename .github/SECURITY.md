# Security Policy

tbay is an execution-safety layer: people adopt it precisely because they
expect its guarantees to hold. Security reports are treated accordingly.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Preferred: use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). If that is unavailable to you,
email **kanduriakhilteja108@gmail.com** with `[tbay security]` in the
subject.

Include what you can: affected version, backend (SQLite/Postgres/Redis), a
minimal reproduction, and the impact as you understand it. You can expect
an acknowledgment within **72 hours** and a status update at least every
**7 days** until resolution. Fixes are released as patch versions with
credit to the reporter (unless you prefer otherwise).

## What counts as a vulnerability here

Anything that breaks a documented guarantee, for example:

- Executing a tool call that should have been blocked: bypassing
  `approval_required`, the kill switch (`pause`), budgets, rate limits, or
  concurrency caps — including through races between processes.
- **Approval signature bypass**: getting a call to run from an approval
  row that was not signed with the configured secret, or forging/replaying
  signatures (`src/tbay/security.py`).
- **Redaction leaks**: values a policy masks (`redact_args`,
  `redact_patterns`, `redact_auto`) reaching the audit log, webhook
  payloads, exports, or events in cleartext.
- Double-execution of an idempotent call that the design says runs once
  (e.g. via the stale-lease reclaim CAS).
- Injection via untrusted inputs tbay parses: policy YAML, tool arguments
  flowing into SQL/Lua, webhook URLs (scheme allowlist bypass → SSRF).

## What is out of scope (the documented trust model)

tbay's guarantees are explicitly bounded by its
[trust model](../docs/design.md): an attacker with **full database write
access** can delete rows, lift pauses, or corrupt state — signing protects
the *approve decision*, not the storage; and an attacker who can modify
the executing process's code or environment is past any in-process
guardrail. Reports that reduce to "database credentials grant database
access" or "root on the box wins" are appreciated but will be closed as
working-as-documented — unless they cross a boundary the docs claim holds
(e.g. DB access alone yielding a *verified signed* approval).

## Supported versions

| Version | Supported |
|---|---|
| 0.3.x | yes |
| < 0.3 | no — please upgrade; 0.3.0 migrates databases in place |

## Hardening checklist for deployments

Least-privilege database roles (agents get INSERT/UPDATE on tbay tables,
not DELETE/DDL); set `TBAY_APPROVAL_SECRET` and give it only to approval
surfaces; keep `redact_auto: true` on policies handling third-party data;
bind the dashboard to localhost or put real auth in front of it.
