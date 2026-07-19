from __future__ import annotations

import dataclasses
import json
import os
import time
from collections import Counter

import click

from .backends.base import FAILED, RUNNING, SUCCEEDED, WAITING_APPROVAL
from .client import TbayClient
from .security import sign_approval

DEFAULT_DB = os.environ.get("TBAY_DB_URL", "sqlite:///~/.tbay/db.sqlite")


@click.group()
@click.option(
    "--db-url", default=DEFAULT_DB, show_default=True,
    help="sqlite:///path or postgresql://... (or set the TBAY_DB_URL environment variable)",
)
@click.option("--policy-file", default=None, help="Path to a policy YAML file")
@click.pass_context
def main(ctx, db_url, policy_file):
    """tbay: execution safety for AI agent tool calls, installed as a library, not run as a service.

    Point this at the same database your app uses (--db-url or TBAY_DB_URL)
    so it can see and act on the same executions your code created.
    """
    ctx.ensure_object(dict)
    ctx.obj["client"] = TbayClient(db_url=db_url, policy_file=policy_file)
    ctx.obj["db_url"] = db_url


@main.command()
@click.argument("execution_id")
@click.option("--resolver", default="cli", help="Who's approving this, recorded in the audit log")
@click.pass_context
def approve(ctx, execution_id, resolver):
    """Approve a paused (WAITING_APPROVAL) execution so it can go ahead and run.

    With TBAY_APPROVAL_SECRET set, the decision is signed; executing clients
    configured with the same secret verify that signature before running, so
    database credentials alone can't approve (see src/tbay/security.py).
    """
    secret = os.environ.get("TBAY_APPROVAL_SECRET")
    signature = sign_approval(secret, execution_id, True) if secret else None
    ctx.obj["client"].backend.resolve_approval(
        execution_id, approved=True, resolver=resolver, signature=signature
    )
    click.echo(f"approved {execution_id}" + (" (signed)" if signature else ""))


@main.command()
@click.argument("execution_id")
@click.option("--resolver", default="cli", help="Who's rejecting this, recorded in the audit log")
@click.option("--reason", default=None, help="Why this was rejected; shown to the caller and in the audit log")
@click.pass_context
def reject(ctx, execution_id, resolver, reason):
    """Reject a paused (WAITING_APPROVAL) execution. The tool call never runs.

    Give a --reason: the blocked caller's ApprovalRejected error carries it,
    so the agent (and whoever reads the log) learns WHY, not just that it
    was refused.
    """
    secret = os.environ.get("TBAY_APPROVAL_SECRET")
    signature = sign_approval(secret, execution_id, False) if secret else None
    ctx.obj["client"].backend.resolve_approval(
        execution_id, approved=False, resolver=resolver, signature=signature, note=reason
    )
    click.echo(f"rejected {execution_id}" + (" (signed)" if signature else ""))


@main.command()
@click.pass_context
def pending(ctx):
    """Show every execution waiting for a human decision, oldest first.

    Everything needed to decide is on one line per call: the tool, its
    (redacted) arguments, which agent asked, its stated reasoning, and how
    long it has been waiting. Then `tbay approve <id>` or `tbay reject <id>`.
    """
    records = ctx.obj["client"].backend.list_executions(status=WAITING_APPROVAL, limit=200)
    if not records:
        click.echo("nothing is waiting for approval")
        return
    now = time.time()
    for r in sorted(records, key=lambda r: r.created_at):
        waited = now - r.created_at
        line = f"{r.id}  {r.tool_name:24s} waiting {waited:7.0f}s"
        if r.agent_id:
            line += f"  agent={r.agent_id}"
        if r.args_json:
            line += f"  args={r.args_json}"
        if r.reasoning:
            line += f"  reason={r.reasoning!r}"
        click.echo(line)


@main.command()
@click.argument("execution_id")
@click.pass_context
def show(ctx, execution_id):
    """Show everything stored about one execution, including its approval row."""
    client = ctx.obj["client"]
    record = client.backend.get(execution_id)
    if record is None:
        raise click.ClickException(f"no execution {execution_id!r}")
    for key, value in dataclasses.asdict(record).items():
        if key == "embedding_json" and value:
            value = f"<{len(value)} bytes>"
        click.echo(f"{key:18s} {value}")
    approval = client.backend.get_approval(execution_id)
    if approval:
        click.echo("approval:")
        for key, value in approval.items():
            click.echo(f"  {key:16s} {value}")


@main.command(name="log")
@click.option("--tool", "tool_name", default=None, help="Only show calls to this tool")
@click.option("--status", default=None, help="Only show this status (RUNNING/SUCCEEDED/FAILED/WAITING_APPROVAL)")
@click.option("--tenant", default=None, help="Only show this tenant")
@click.option("--limit", default=20, show_default=True, help="Max rows to show")
@click.option("--args/--no-args", "show_args", default=True, help="Show each call's (possibly redacted) arguments")
@click.pass_context
def log_cmd(ctx, tool_name, status, tenant, limit, show_args):
    """Show the audit log: what tbay has seen, in progress or finished.

    This is what you'd check before running `tbay approve` on something,
    to see exactly what you're about to greenlight.
    """
    records = ctx.obj["client"].backend.list_executions(
        tool_name=tool_name, status=status, tenant=tenant, limit=limit
    )
    if not records:
        click.echo("no executions found")
        return
    for r in records:
        line = f"{r.id}  {r.status:16s} {r.tool_name:24s} policy={r.policy_name}"
        if r.agent_id:
            line += f"  agent={r.agent_id}"
        if show_args and r.args_json:
            line += f"  args={r.args_json}"
        if r.reasoning:
            line += f"  reason={r.reasoning!r}"
        click.echo(line)


@main.command()
@click.option("--limit", default=1000, show_default=True, help="How many recent executions to aggregate")
@click.pass_context
def stats(ctx, limit):
    """Summarize recent activity: counts by status and by tool, plus any active pauses."""
    client = ctx.obj["client"]
    records = client.backend.list_executions(limit=limit)
    if not records:
        click.echo("no executions found")
    else:
        by_status = Counter(r.status for r in records)
        by_tool = Counter(r.tool_name for r in records)
        click.echo(f"last {len(records)} executions:")
        for status in (RUNNING, WAITING_APPROVAL, SUCCEEDED, FAILED):
            if by_status.get(status):
                click.echo(f"  {status:18s} {by_status[status]}")
        click.echo("by tool:")
        for tool, count in by_tool.most_common(20):
            click.echo(f"  {tool:26s} {count}")
    paused = client.paused()
    if paused:
        click.echo("PAUSED:")
        for scope, info in paused.items():
            label = "all tools" if scope == "*" else scope
            reason = info.get("reason") or ""
            click.echo(f"  {label}" + (f"  ({reason})" if reason else ""))


@main.command()
@click.option("--tool", "tool_name", default=None, help="Pause only this tool (default: everything)")
@click.option("--reason", default="", help="Why, shown to blocked callers and in `tbay stats`")
@click.option("--by", default="cli", help="Who's pausing, recorded with the pause")
@click.pass_context
def pause(ctx, tool_name, reason, by):
    """Kill switch: stop guarded calls NOW, across every process on this database.

    Blocked calls raise ToolPaused immediately instead of running. Scope it
    to one tool with --tool, or pause everything. `tbay resume` lifts it.
    This is the first thing to reach for when an agent misbehaves.
    """
    ctx.obj["client"].pause(tool_name, reason=reason, by=by)
    click.echo(f"paused {tool_name or 'ALL tools'}" + (f" ({reason})" if reason else ""))


@main.command()
@click.option("--tool", "tool_name", default=None, help="Resume only this tool (default: lift the global pause)")
@click.pass_context
def resume(ctx, tool_name):
    """Lift a pause set by `tbay pause` (per-tool with --tool, otherwise the global one)."""
    ctx.obj["client"].resume(tool_name)
    click.echo(f"resumed {tool_name or 'ALL tools'}")


@main.command()
@click.option("--limit", default=10000, show_default=True, help="Max executions to export, newest first")
@click.option("--output", type=click.File("w"), default="-", help="File to write to (default: stdout)")
@click.pass_context
def export(ctx, limit, output):
    """Export the audit log as JSON Lines, one execution per line.

    Feed it to jq, load it into a warehouse, or attach it to a compliance
    review. Arguments appear exactly as stored (post-redaction).
    """
    records = ctx.obj["client"].backend.list_executions(limit=limit)
    for r in records:
        output.write(json.dumps(dataclasses.asdict(r), sort_keys=True) + "\n")
    click.echo(f"exported {len(records)} executions", err=True)


@main.command(name="policies")
@click.pass_context
def policies_cmd(ctx):
    """List every effective policy (defaults + your --policy-file) and its key settings."""
    for name, pol in sorted(ctx.obj["client"].policies.items()):
        traits = []
        if not pol.idempotent:
            traits.append("volatile")
        if pol.cache_ttl:
            traits.append(f"cache_ttl={pol.cache_ttl:g}s")
        if pol.semantic_cache:
            traits.append(f"semantic>={pol.semantic_threshold}")
        if pol.max_retries:
            traits.append(f"retries={pol.max_retries}")
        if pol.approval_required:
            traits.append("approval")
        if pol.rate_limit_max_calls:
            traits.append(f"rate={pol.rate_limit_max_calls}/{pol.rate_limit_window or 0:g}s")
        if pol.budget_max is not None:
            traits.append(f"budget[{pol.budget_arg}]<={pol.budget_max:g}/{pol.budget_window or 0:g}s")
        if pol.max_concurrent:
            traits.append(f"concurrent<={pol.max_concurrent}")
        if pol.lease_timeout:
            traits.append(f"lease={pol.lease_timeout:g}s")
        if pol.execution_timeout:
            traits.append(f"timeout={pol.execution_timeout:g}s")
        if pol.redact_args or pol.redact_patterns or pol.redact_auto:
            traits.append("redaction")
        click.echo(f"{name:16s} {', '.join(traits) or 'defaults'}")


@main.command()
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def clear(ctx, yes):
    """Delete EVERY execution and approval from the connected database.

    Cached results, idempotency keys, pending approvals, the audit log:
    all gone, with no undo. Useful for resetting a demo or dev database.
    Works the same over SQLite, Postgres, and Redis (on Redis it deletes
    only tbay's own keys, never the whole database). Active pauses survive.
    """
    db_url = ctx.obj["db_url"]
    if not yes:
        click.confirm(f"Delete every execution and approval in {db_url}?", abort=True)
    removed = ctx.obj["client"].backend.clear()
    click.echo(f"cleared {removed} executions from {db_url}")


@main.command()
@click.pass_context
def init(ctx):
    """Create the executions/approvals tables if they don't exist yet.

    You don't usually need to run this by hand: TbayClient() does it on
    startup automatically. It's here for scripting a fresh environment.
    """
    click.echo("tbay storage initialized.")


if __name__ == "__main__":
    main()
