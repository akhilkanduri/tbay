from __future__ import annotations

import os

import click

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
@click.pass_context
def reject(ctx, execution_id, resolver):
    """Reject a paused (WAITING_APPROVAL) execution. The tool call never runs."""
    secret = os.environ.get("TBAY_APPROVAL_SECRET")
    signature = sign_approval(secret, execution_id, False) if secret else None
    ctx.obj["client"].backend.resolve_approval(
        execution_id, approved=False, resolver=resolver, signature=signature
    )
    click.echo(f"rejected {execution_id}" + (" (signed)" if signature else ""))


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
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def clear(ctx, yes):
    """Delete EVERY execution and approval from the connected database.

    Cached results, idempotency keys, pending approvals, the audit log:
    all gone, with no undo. Useful for resetting a demo or dev database.
    Works the same over SQLite, Postgres, and Redis (on Redis it deletes
    only tbay's own keys, never the whole database).
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
