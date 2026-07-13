"""toolbay monitor: a small, standalone observability web app for tbay.

Shows every tool call your agents made (inputs, outputs, errors, reasoning),
what's in flight right now (a RUNNING call may be a container or long API job
that's still working), and what's paused waiting for a human. Approve or
reject paused calls straight from the page.

This is NOT part of the tbay Python package. It's a single file that reads
the same database(s) your app writes to, through tbay's own backends, so it
works identically over SQLite, Postgres, and Redis, in any combination:

    python dashboard/app.py --db postgresql://postgres:tbay@localhost:5432/tbay \
                            --db redis://localhost:6379/0

Then open http://localhost:8787. See dashboard/README.md for details.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from tbay import TbayClient
from tbay.security import sign_approval

# The dashboard never writes executions, it only reads them (and resolves
# approvals). One TbayClient per --db URL; the backend does all the work.
SOURCES: dict[str, TbayClient] = {}

ACTIVE_STATUSES = ("RUNNING", "WAITING_APPROVAL")


def _source_label(db_url: str) -> str:
    """A short, safe display name for a backend: scheme + host, never
    credentials. 'postgresql://user:pass@db:5432/tbay' -> 'postgres @ db'."""
    parsed = urlparse(db_url)
    scheme = parsed.scheme or "sqlite"
    if scheme.startswith("postgres"):
        scheme = "postgres"
    host = parsed.hostname or os.path.basename(parsed.path or "") or "local"
    return f"{scheme} @ {host}"


def _record_to_dict(record, source: str) -> dict:
    return {
        "id": record.id,
        "source": source,
        "tool_name": record.tool_name,
        "status": record.status,
        "policy_name": record.policy_name,
        "tenant": record.tenant,
        "args_json": record.args_json,
        "result_json": record.result_json,
        "error": record.error,
        "reasoning": record.reasoning,
        "agent_id": record.agent_id,
        "agent_meta": record.agent_meta,
        "retry_count": record.retry_count,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
    }


def fetch_executions(tool: str = "", status: str = "", limit: int = 200) -> dict:
    """Merge recent executions from every connected backend, newest first,
    plus status counts over that window. Backends that error (a Redis that
    went away, say) are reported per source instead of failing the page."""
    executions, errors = [], {}
    for source, client in SOURCES.items():
        try:
            records = client.backend.list_executions(
                tool_name=tool or None, status=status or None, limit=limit
            )
            executions.extend(_record_to_dict(r, source) for r in records)
        except Exception as exc:
            errors[source] = str(exc)
    executions.sort(key=lambda e: e["created_at"], reverse=True)
    executions = executions[:limit]
    counts = {"RUNNING": 0, "WAITING_APPROVAL": 0, "SUCCEEDED": 0, "FAILED": 0}
    for e in executions:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return {
        "now": time.time(),
        "sources": {s: errors.get(s) for s in SOURCES},
        "counts": counts,
        "executions": executions,
    }


def resolve_approval(source: str, execution_id: str, approved: bool, note: str = "") -> dict:
    client = SOURCES.get(source)
    if client is None:
        return {"ok": False, "error": f"unknown source {source!r}"}
    # Verify there's actually a pending approval before writing: a blind
    # UPDATE that matches zero rows would report success while doing
    # nothing, which reads as "the button doesn't work" in the UI.
    current = client.backend.get_approval_status(execution_id)
    if current is None:
        return {"ok": False, "error": f"execution {execution_id[:8]} has no approval request"}
    if current != "pending":
        return {"ok": False, "error": f"execution {execution_id[:8]} was already {current}"}
    # With TBAY_APPROVAL_SECRET set, sign the decision; executing clients
    # configured with the same secret verify it before running, so database
    # credentials alone can't approve (see src/tbay/security.py).
    secret = os.environ.get("TBAY_APPROVAL_SECRET")
    signature = sign_approval(secret, execution_id, approved) if secret else None
    client.backend.resolve_approval(
        execution_id, approved=approved, resolver="dashboard", signature=signature, note=note or None
    )
    return {"ok": True, "signed": bool(signature)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the terminal quiet; errors still surface
        pass

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        self._send(json.dumps(payload).encode(), "application/json", code)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
            return
        if url.path == "/api/executions":
            q = parse_qs(url.query)
            self._send_json(
                fetch_executions(
                    tool=q.get("tool", [""])[0],
                    status=q.get("status", [""])[0],
                    limit=min(int(q.get("limit", ["200"])[0]), 1000),
                )
            )
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        if url.path not in ("/api/approve", "/api/reject"):
            self._send_json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            result = resolve_approval(
                source=body.get("source", ""),
                execution_id=body.get("execution_id", ""),
                approved=(url.path == "/api/approve"),
                note=str(body.get("note", "") or "")[:500],
            )
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        self._send_json(result, 200 if result.get("ok") else 400)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>toolbay monitor</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>&#9875;</text></svg>">
<style>
  :root {
    --ink: #f6efe3; --dim: rgba(246,239,227,.6); --faint: rgba(246,239,227,.38);
    --line: rgba(255,255,255,.16); --line-soft: rgba(255,255,255,.09);
    --glass: linear-gradient(160deg, rgba(48,37,25,.72), rgba(28,21,14,.6));
    --glass-deep: linear-gradient(160deg, rgba(24,18,12,.88), rgba(14,10,6,.82));
    --gold: #e8a33d; --gold-2: #f2bd6a;
    --blue: #7cc4e8; --green: #7fd49a; --red: #ee7b66;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    --radius: 20px;
  }
  * { box-sizing: border-box; margin: 0; }
  html { color-scheme: dark; }
  body {
    min-height: 100vh; color: var(--ink); overflow-x: hidden; position: relative;
    font: 14px/1.55 -apple-system, "Segoe UI", Inter, sans-serif;
    padding: 26px clamp(14px, 3vw, 44px) 70px clamp(96px, 9vw, 130px);
    background: linear-gradient(135deg, #c9b294 0%, #a98e6c 38%, #7d6549 72%, #55422e 100%);
  }

  /* -- warm blurred "photo" backdrop -- */
  .bg { position: fixed; inset: 0; z-index: -1; overflow: hidden; }
  .bg i { position: absolute; border-radius: 50%; filter: blur(80px); }
  .bg i:nth-child(1) { width: 55vw; height: 55vw; left: -12vw; top: -18vh; background: #e6cfa8; opacity: .55;
    animation: drift1 40s ease-in-out infinite alternate; }
  .bg i:nth-child(2) { width: 45vw; height: 45vw; right: -10vw; top: 10vh; background: #6e5233; opacity: .5;
    animation: drift2 48s ease-in-out infinite alternate; }
  .bg i:nth-child(3) { width: 40vw; height: 40vw; left: 28vw; bottom: -22vh; background: #caa25e; opacity: .38;
    animation: drift1 56s ease-in-out infinite alternate-reverse; }
  .bg i:nth-child(4) { width: 26vw; height: 26vw; left: 8vw; bottom: 6vh; background: #3c2c1c; opacity: .45;
    animation: drift2 44s ease-in-out infinite alternate-reverse; }
  @keyframes drift1 { to { transform: translate(6vw, 4vh) scale(1.12); } }
  @keyframes drift2 { to { transform: translate(-5vw, 5vh) scale(.92); } }

  /* -- glass primitives -- */
  .panel { background: var(--glass); border: 1px solid var(--line); border-radius: var(--radius);
    backdrop-filter: blur(24px) saturate(1.15); -webkit-backdrop-filter: blur(24px) saturate(1.15);
    box-shadow: 0 18px 44px rgba(20,12,4,.35), inset 0 1px 0 rgba(255,255,255,.12); }

  /* -- left rail -- */
  .rail { position: fixed; left: 18px; top: 26px; bottom: 26px; width: 62px; z-index: 5;
    display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 12px 0; }
  .rail .tile { width: 40px; height: 40px; border-radius: 13px; display: grid; place-items: center;
    font-size: 18px; color: var(--dim); cursor: pointer; text-decoration: none;
    transition: background .2s, color .2s, transform .12s; }
  .rail .tile:hover { background: rgba(255,255,255,.08); color: var(--ink); transform: translateY(-1px); }
  .rail .tile.logo { background: linear-gradient(135deg, var(--gold-2), var(--gold)); color: #241708;
    box-shadow: 0 6px 18px rgba(232,163,61,.45); font-size: 20px; }
  .rail .spacer { flex: 1; }
  @media (max-width: 760px) { .rail { display: none; } body { padding-left: clamp(14px, 3vw, 44px); } }

  /* -- header -- */
  header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  .brand { font-size: 23px; font-weight: 800; letter-spacing: -.02em; color: #2c1f10;
    text-shadow: 0 1px 0 rgba(255,255,255,.25); }
  .brand small { font-weight: 700; letter-spacing: .3em; text-transform: uppercase; font-size: 10px;
    color: rgba(44,31,16,.7); margin-left: 10px; }
  .brand .sub { display: block; font-size: 11px; font-weight: 500; color: rgba(44,31,16,.65);
    letter-spacing: 0; text-shadow: none; }
  .head-right { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chip { font: 11px var(--mono); background: var(--glass); border: 1px solid var(--line);
    border-radius: 999px; padding: 6px 13px 6px 10px; color: var(--dim);
    display: inline-flex; align-items: center; gap: 7px; backdrop-filter: blur(14px); }
  .chip b { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); }
  .chip.err { border-color: rgba(238,123,102,.55); color: var(--red); }
  .chip.err b { background: var(--red); box-shadow: 0 0 8px var(--red); }

  /* -- stat cards -- */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-top: 26px; }
  .card { padding: 18px 22px; position: relative; }
  .card .l { font-size: 12px; color: var(--dim); display: flex; align-items: center; gap: 8px; }
  .card .l::before { content: ""; width: 8px; height: 8px; border-radius: 3px; background: var(--ac); }
  .card .n { font-size: 34px; font-weight: 800; letter-spacing: -.02em; margin-top: 4px;
    font-variant-numeric: tabular-nums; transition: transform .18s; }
  .card .n.pop { transform: scale(1.12); }
  .card .s { font-size: 11px; color: var(--faint); }
  .card.running { --ac: var(--blue); } .card.waiting { --ac: var(--gold); }
  .card.succeeded { --ac: var(--green); } .card.failed { --ac: var(--red); }
  .card .ring { position: absolute; right: 18px; top: 18px; width: 24px; height: 24px; border-radius: 50%;
    border: 2.5px solid rgba(255,255,255,.14); border-top-color: var(--ac);
    animation: spin 1.1s linear infinite; opacity: 0; transition: opacity .3s; }
  .card.live .ring { opacity: 1; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* -- activity chart -- */
  .spark-wrap { margin-top: 16px; padding: 18px 22px 12px; }
  .spark-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
  .spark-head .t { font-size: 15px; font-weight: 700; }
  .spark-head .t small { display: block; font-size: 10px; font-weight: 400; color: var(--faint);
    letter-spacing: .18em; text-transform: uppercase; }
  .legend { font: 10.5px var(--mono); color: var(--dim); display: flex; gap: 14px; }
  .legend i { display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; }
  canvas#spark { width: 100%; height: 120px; display: block; margin-top: 10px; }

  h2 { margin: 30px 0 12px; font-size: 11px; letter-spacing: .28em; text-transform: uppercase;
    color: var(--dim); display: flex; align-items: center; gap: 12px; }
  h2::after { content: ""; flex: 1; height: 1px; background: linear-gradient(90deg, var(--line), transparent); }

  /* -- in flight -- */
  .flight { display: flex; flex-direction: column; gap: 12px; }
  .fcard { padding: 15px 20px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    position: relative; overflow: hidden; --ac: var(--blue); }
  .fcard.waiting { --ac: var(--gold); }
  .fcard::before { content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
    border-radius: 3px; background: var(--ac); box-shadow: 0 0 14px var(--ac); }
  .fcard::after { content: ""; position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(100deg, transparent 32%, rgba(255,255,255,.05) 50%, transparent 68%);
    transform: translateX(-100%); animation: sweep 2.8s ease-in-out infinite; }
  @keyframes sweep { 60%, 100% { transform: translateX(100%); } }
  .orb { width: 10px; height: 10px; border-radius: 50%; background: var(--ac); flex: none;
    box-shadow: 0 0 10px var(--ac); animation: throb 1.4s ease-in-out infinite; }
  .fcard.waiting .orb { animation: none; }
  @keyframes throb { 50% { opacity: .35; transform: scale(.7); } }
  .fcard .tool { font: 600 14px var(--mono); }
  .tag { font: 700 9.5px var(--mono); letter-spacing: .16em; padding: 4px 11px; border-radius: 999px;
    color: #241708; background: linear-gradient(135deg, color-mix(in srgb, var(--ac) 90%, white), var(--ac)); }
  .fcard.waiting .tag { color: #241708; }
  .agent { font: 11px var(--mono); color: var(--gold-2); background: rgba(232,163,61,.14);
    border: 1px solid rgba(232,163,61,.35); border-radius: 999px; padding: 3px 11px; }
  .elapsed { font: 12px var(--mono); color: var(--dim); }
  .fcard code, td code { font: 11.5px var(--mono); color: var(--dim);
    background: rgba(12,8,4,.5); border: 1px solid var(--line-soft); border-radius: 8px; padding: 3px 9px; }
  .fcard code { flex: 1 1 240px; overflow-wrap: anywhere; }
  .empty { padding: 24px; text-align: center; color: var(--dim); }
  .empty .glyph { font-size: 24px; display: block; margin-bottom: 6px; opacity: .75; }

  /* -- buttons -- */
  button { font: 600 12.5px var(--mono); color: var(--ink); cursor: pointer; border-radius: 999px;
    padding: 9px 18px; border: 1px solid var(--line); background: rgba(255,255,255,.06);
    transition: transform .12s, box-shadow .2s, border-color .2s, background .2s; }
  button:hover { transform: translateY(-1px); border-color: rgba(255,255,255,.35); }
  button:active { transform: translateY(0) scale(.97); }
  button.approve { color: #142012; border: none;
    background: linear-gradient(135deg, #9fe0b2, var(--green)); box-shadow: 0 6px 18px rgba(127,212,154,.35); }
  button.approve:hover { box-shadow: 0 8px 26px rgba(127,212,154,.55); }
  button.reject { color: var(--red); border-color: rgba(238,123,102,.5); background: rgba(238,123,102,.1); }
  button.reject:hover { box-shadow: 0 6px 18px rgba(238,123,102,.3); border-color: var(--red); }
  button.gold { color: #241708; border: none;
    background: linear-gradient(135deg, var(--gold-2), var(--gold)); box-shadow: 0 6px 18px rgba(232,163,61,.4); }

  /* -- filters -- */
  .filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  .search { position: relative; }
  .search input { background: var(--glass); border: 1px solid var(--line); color: var(--ink);
    border-radius: 999px; padding: 10px 16px 10px 36px; font-size: 13px; width: 250px; outline: none;
    backdrop-filter: blur(14px); transition: border-color .2s, box-shadow .2s; }
  .search input::placeholder { color: var(--faint); }
  .search input:focus { border-color: var(--gold); box-shadow: 0 0 0 3px rgba(232,163,61,.2); }
  .search::before { content: "\2315"; position: absolute; left: 13px; top: 50%; translate: 0 -54%;
    color: var(--faint); font-size: 15px; }
  .seg { display: flex; background: var(--glass); border: 1px solid var(--line); border-radius: 999px;
    padding: 4px; gap: 2px; backdrop-filter: blur(14px); flex-wrap: wrap; }
  .seg button { border: none; background: transparent; padding: 7px 15px; color: var(--dim); font-size: 12px; }
  .seg button:hover { transform: none; color: var(--ink); }
  .seg button.on { color: #241708; background: linear-gradient(135deg, var(--gold-2), var(--gold));
    box-shadow: 0 4px 14px rgba(232,163,61,.4); }
  .updated { font: 11px var(--mono); color: var(--faint); margin-left: auto; }

  /* -- table -- */
  .table-wrap { overflow-x: auto; border-radius: var(--radius); }
  table { border-collapse: collapse; width: 100%; min-width: 980px; }
  th { text-align: left; font-size: 10px; letter-spacing: .18em; text-transform: uppercase;
    color: var(--dim); padding: 13px 15px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: rgba(30,22,14,.92); backdrop-filter: blur(10px); z-index: 1; }
  td { padding: 12px 15px; border-bottom: 1px solid var(--line-soft); vertical-align: middle;
    font-size: 13px; white-space: nowrap; }
  tr.detail > td { white-space: normal; }
  tr.row { cursor: pointer; transition: background .15s; }
  tr.row:hover td { background: rgba(255,255,255,.045); }
  tr.row.active-row td { background: rgba(232,163,61,.08); }
  td.mono { font-family: var(--mono); font-size: 12px; }
  td .clip { display: inline-block; max-width: 185px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; vertical-align: bottom; }
  .pill { font: 700 10px var(--mono); letter-spacing: .07em; border-radius: 999px; padding: 5px 12px 5px 9px;
    white-space: nowrap; display: inline-flex; align-items: center; gap: 6px;
    color: var(--pc); background: color-mix(in srgb, var(--pc) 14%, transparent);
    border: 1px solid color-mix(in srgb, var(--pc) 32%, transparent); }
  .pill i { width: 6px; height: 6px; border-radius: 50%; background: var(--pc); box-shadow: 0 0 7px var(--pc); }
  .pill.RUNNING { --pc: var(--blue); } .pill.RUNNING i { animation: throb 1.3s infinite; }
  .pill.WAITING_APPROVAL { --pc: var(--gold); } .pill.WAITING_APPROVAL i { animation: throb 1.8s infinite; }
  .pill.SUCCEEDED { --pc: var(--green); }
  .pill.FAILED { --pc: var(--red); }
  .dim { color: var(--dim); } .faint { color: var(--faint); }

  /* -- expanded detail -- */
  tr.detail > td { background: rgba(14,10,6,.45); padding: 20px 24px; }
  .meta { font: 11px var(--mono); color: var(--dim); display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 12px; }
  .meta b { color: var(--ink); font-weight: 600; }
  .k { font-size: 9px; letter-spacing: .24em; text-transform: uppercase; color: var(--faint);
    display: flex; align-items: center; gap: 10px; margin: 14px 0 6px; }
  .k button { padding: 3px 11px; font-size: 10px; }
  pre.code { font: 12px/1.6 var(--mono); background: rgba(10,7,4,.6); border: 1px solid var(--line-soft);
    border-radius: 12px; padding: 13px 15px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .jk { color: var(--gold-2); } .js { color: #b8dc9a; } .jn { color: #e8b6d8; } .jb { color: var(--blue); }
  .why { border-left: 2px solid var(--gold); padding: 9px 15px; color: var(--ink);
    background: rgba(232,163,61,.09); border-radius: 0 12px 12px 0; font-style: italic; }
  .detail-actions { margin-top: 15px; display: flex; gap: 10px; }

  /* -- reject modal -- */
  .overlay { position: fixed; inset: 0; z-index: 40; display: none; place-items: center;
    background: rgba(22,15,8,.45); backdrop-filter: blur(10px) saturate(1.1);
    -webkit-backdrop-filter: blur(10px) saturate(1.1); }
  .overlay.show { display: grid; }
  .modal { width: min(460px, calc(100vw - 40px)); padding: 26px 26px 22px; border-radius: 24px;
    background: var(--glass-deep); border: 1px solid var(--line);
    box-shadow: 0 30px 80px rgba(10,6,2,.6), inset 0 1px 0 rgba(255,255,255,.14);
    animation: rise .22s ease-out; }
  @keyframes rise { from { opacity: 0; transform: translateY(14px) scale(.97); } }
  .modal h3 { font-size: 17px; font-weight: 800; display: flex; align-items: center; gap: 10px; }
  .modal h3 .badge-x { width: 30px; height: 30px; border-radius: 10px; display: grid; place-items: center;
    background: rgba(238,123,102,.16); border: 1px solid rgba(238,123,102,.4); color: var(--red); font-size: 14px; }
  .modal .sub { color: var(--dim); font-size: 12.5px; margin: 8px 0 4px; }
  .modal .sub code { font: 11px var(--mono); color: var(--gold-2); }
  .modal textarea { width: 100%; min-height: 92px; resize: vertical; margin-top: 12px;
    background: rgba(255,255,255,.05); border: 1px solid var(--line); border-radius: 14px;
    color: var(--ink); padding: 12px 14px; font: 13px/1.5 inherit; outline: none;
    transition: border-color .2s, box-shadow .2s; }
  .modal textarea::placeholder { color: var(--faint); }
  .modal textarea:focus { border-color: var(--gold); box-shadow: 0 0 0 3px rgba(232,163,61,.18); }
  .modal .hint { font-size: 11px; color: var(--faint); margin-top: 6px; }
  .modal .actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
  .modal button.danger { color: #2a0f08; border: none;
    background: linear-gradient(135deg, #f39a85, var(--red)); box-shadow: 0 6px 18px rgba(238,123,102,.4); }
  .modal button.danger:hover { box-shadow: 0 8px 26px rgba(238,123,102,.6); }

  /* -- toasts -- */
  .toasts { position: fixed; right: 20px; bottom: 20px; display: flex; flex-direction: column; gap: 10px; z-index: 50; }
  .toast { background: var(--glass-deep); border: 1px solid var(--line); border-left: 3px solid var(--tc, var(--gold));
    backdrop-filter: blur(18px); border-radius: 14px; padding: 12px 18px; font: 12px var(--mono);
    box-shadow: 0 12px 36px rgba(10,6,2,.5); animation: slidein .25s ease-out; max-width: 380px; color: var(--ink); }
  .toast.ok { --tc: var(--green); } .toast.bad { --tc: var(--red); }
  .toast.out { opacity: 0; translate: 12px 0; transition: all .3s; }
  @keyframes slidein { from { opacity: 0; translate: 16px 0; } }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
  }
</style>
</head>
<body>
<div class="bg"><i></i><i></i><i></i><i></i></div>

<nav class="rail panel">
  <a class="tile logo" href="#" title="toolbay">&#9875;</a>
  <a class="tile" href="#top" title="overview">&#9638;</a>
  <a class="tile" href="#activity" title="activity">&#8767;</a>
  <a class="tile" href="#flight-h" title="in flight">&#9096;</a>
  <a class="tile" href="#table-h" title="executions">&#9776;</a>
  <span class="spacer"></span>
  <button class="tile" style="border:none" data-action="pause" id="pause"
    title="Freezes this page's auto-refresh so rows stop moving while you read. Executions themselves are not affected.">&#10074;&#10074;</button>
</nav>

<header id="top">
  <div class="brand">toolbay<small>monitor</small>
    <span class="sub" id="today"></span>
  </div>
  <div class="head-right">
    <span id="sources"></span>
  </div>
</header>

<div class="cards">
  <div class="card panel running" id="card-RUNNING"><div class="ring"></div>
    <div class="l">running</div><div class="n" id="c-RUNNING">0</div><div class="s">executing right now</div></div>
  <div class="card panel waiting" id="card-WAITING_APPROVAL">
    <div class="l">waiting approval</div><div class="n" id="c-WAITING_APPROVAL">0</div><div class="s">needs a human</div></div>
  <div class="card panel succeeded" id="card-SUCCEEDED">
    <div class="l">succeeded</div><div class="n" id="c-SUCCEEDED">0</div><div class="s">completed and stored</div></div>
  <div class="card panel failed" id="card-FAILED">
    <div class="l">failed</div><div class="n" id="c-FAILED">0</div><div class="s">errored or timed out</div></div>
</div>

<div class="spark-wrap panel" id="activity">
  <div class="spark-head">
    <span class="t">Activity<small>last 10 minutes</small></span>
    <span class="legend">
      <span><i style="background:var(--gold)"></i>calls</span>
      <span><i style="background:var(--red)"></i>failed</span>
    </span>
  </div>
  <canvas id="spark"></canvas>
</div>

<h2 id="flight-h">in flight</h2>
<div class="flight" id="flight"></div>

<h2 id="table-h">executions</h2>
<div class="filters">
  <span class="search"><input id="f-tool" placeholder="filter by tool name" autocomplete="off"></span>
  <div class="seg" id="seg">
    <button class="on" data-action="seg" data-status="">all</button>
    <button data-action="seg" data-status="RUNNING">running</button>
    <button data-action="seg" data-status="WAITING_APPROVAL">waiting</button>
    <button data-action="seg" data-status="SUCCEEDED">succeeded</button>
    <button data-action="seg" data-status="FAILED">failed</button>
  </div>
  <span class="updated" id="updated"></span>
</div>
<div class="table-wrap panel">
  <table>
    <thead><tr>
      <th>when</th><th>tool</th><th>agent</th><th>status</th><th>duration</th>
      <th>input</th><th>output</th><th>why</th><th>source</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</div>

<div class="overlay" id="reject-overlay">
  <div class="modal">
    <h3><span class="badge-x">&#10005;</span>Reject this call</h3>
    <p class="sub">The blocked caller will receive <code>ApprovalRejected</code> and the tool
      will never run. Tell the agent why:</p>
    <p class="sub" id="reject-target"></p>
    <textarea id="reject-note" placeholder="e.g. amount exceeds the daily refund budget, open a ticket instead"></textarea>
    <div class="hint">Optional, but the reason travels into the caller's exception and the audit log.</div>
    <div class="actions">
      <button data-action="reject-cancel">Cancel</button>
      <button class="danger" data-action="reject-confirm">&#10005; Reject call</button>
    </div>
  </div>
</div>

<div class="toasts" id="toasts"></div>

<script>
"use strict";
const S = { paused: false, tool: "", status: "", open: new Set(), skew: 0, hash: "", data: null,
            reject: null };

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nowS = () => Date.now() / 1000 - S.skew;

$("today").textContent = new Date().toLocaleDateString(undefined,
  { weekday: "long", year: "numeric", month: "short", day: "numeric" });

function fmtAgo(t) {
  const d = Math.max(0, nowS() - t);
  if (d < 60) return d.toFixed(0) + "s ago";
  if (d < 3600) return (d / 60).toFixed(0) + "m ago";
  if (d < 86400) return (d / 3600).toFixed(1) + "h ago";
  return (d / 86400).toFixed(1) + "d ago";
}
function fmtElapsed(t) {
  const d = Math.max(0, nowS() - t);
  return d < 60 ? d.toFixed(0) + "s" : Math.floor(d / 60) + "m " + Math.floor(d % 60) + "s";
}
function fmtDur(e) {
  if (!e.finished_at) return null;
  const d = e.finished_at - e.created_at;
  return d < 1 ? (d * 1000).toFixed(0) + "ms" : d.toFixed(2) + "s";
}

/* JSON syntax highlighting: tokenize raw pretty text, escaping as we emit. */
function hljson(raw) {
  let t;
  try { t = JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { return esc(raw); }
  const re = /("(?:[^"\\]|\\.)*")(\s*:)?|\b(true|false|null)\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g;
  let out = "", last = 0, m;
  while ((m = re.exec(t))) {
    out += esc(t.slice(last, m.index));
    if (m[1] !== undefined) out += '<span class="' + (m[2] ? "jk" : "js") + '">' + esc(m[1]) + "</span>" + (m[2] || "");
    else if (m[3]) out += '<span class="jb">' + m[3] + "</span>";
    else out += '<span class="jn">' + esc(m[0]) + "</span>";
    last = m.index + m[0].length;
  }
  return out + esc(t.slice(last));
}

function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast " + (kind || "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 350); }, 3200);
}

function renderSources(sources) {
  $("sources").innerHTML = Object.entries(sources).map(([name, err]) =>
    '<span class="chip' + (err ? " err" : "") + '"><b></b>' + esc(name) + (err ? " &#183; " + esc(err) : "") + "</span>"
  ).join(" ");
}

function renderCounts(counts) {
  for (const k of ["RUNNING", "WAITING_APPROVAL", "SUCCEEDED", "FAILED"]) {
    const el = $("c-" + k), v = String(counts[k] || 0);
    if (el.textContent !== v) {
      el.textContent = v;
      el.classList.remove("pop"); void el.offsetWidth; el.classList.add("pop");
    }
  }
  $("card-RUNNING").classList.toggle("live", (counts.RUNNING || 0) > 0);
}

function actionButtons(e) {
  return '<button class="approve" data-action="approve" data-source="' + esc(e.source) +
    '" data-id="' + esc(e.id) + '">&#10003; approve</button>' +
    '<button class="reject" data-action="reject" data-source="' + esc(e.source) +
    '" data-id="' + esc(e.id) + '" data-tool="' + esc(e.tool_name) + '">&#10005; reject</button>';
}

function renderFlight(execs) {
  const active = execs.filter(e => e.status === "RUNNING" || e.status === "WAITING_APPROVAL");
  if (!active.length) {
    $("flight").innerHTML = '<div class="empty panel"><span class="glyph">&#9096;</span>calm seas: nothing in flight right now</div>';
    return;
  }
  $("flight").innerHTML = active.map(e => {
    const waiting = e.status === "WAITING_APPROVAL";
    return '<div class="fcard panel' + (waiting ? " waiting" : "") + '">' +
      '<span class="orb"></span>' +
      '<span class="tool">' + esc(e.tool_name) + "</span>" +
      (e.agent_id ? '<span class="agent" title="' + esc(e.agent_meta || "") + '">&#129302; ' + esc(e.agent_id) + "</span>" : "") +
      '<span class="tag">' + (waiting ? "awaiting human" : "executing") + "</span>" +
      '<span class="elapsed" data-since="' + e.created_at + '">' + fmtElapsed(e.created_at) + "</span>" +
      "<code>" + esc(e.args_json || "") + "</code>" +
      (waiting ? actionButtons(e) : "") +
      "</div>";
  }).join("");
}

function rowHtml(e) {
  const key = e.source + ":" + e.id;
  const out = e.status === "FAILED" ? (e.error || "") : (e.result_json || "");
  const openNow = S.open.has(key);
  let html =
    '<tr class="row' + (openNow ? " active-row" : "") + '" data-action="toggle" data-key="' + esc(key) + '">' +
    '<td class="mono dim" title="' + new Date(e.created_at * 1000).toISOString() + '">' + fmtAgo(e.created_at) + "</td>" +
    '<td class="mono">' + esc(e.tool_name) + "</td>" +
    '<td>' + (e.agent_id ? '<span class="agent" title="' + esc(e.agent_meta || "") + '">' + esc(e.agent_id) + "</span>" : '<span class="faint">&#8212;</span>') + "</td>" +
    '<td><span class="pill ' + e.status + '"><i></i>' + e.status.replace("_", " ") + "</span></td>" +
    '<td class="mono dim">' + (fmtDur(e) || '<span class="elapsed" data-since="' + e.created_at + '">' + fmtElapsed(e.created_at) + "</span>&#8230;") + "</td>" +
    "<td><code class=\"clip\">" + esc(e.args_json || "") + "</code></td>" +
    "<td><code class=\"clip\">" + esc(out) + "</code></td>" +
    '<td class="dim"><span class="clip">' + esc(e.reasoning || "") + "</span></td>" +
    '<td class="mono faint">' + esc(e.source) + "</td></tr>";
  if (openNow) {
    html += '<tr class="detail"><td colspan="9">' +
      '<div class="meta"><span>id <b>' + esc(e.id) + "</b></span><span>policy <b>" + esc(e.policy_name) + "</b></span>" +
      "<span>agent <b>" + esc(e.agent_id || "(none)") + "</b></span>" +
      "<span>tenant <b>" + esc(e.tenant || "(none)") + "</b></span><span>retries <b>" + e.retry_count + "</b></span></div>" +
      '<div class="k">input args <button data-action="copy" data-copy="' + esc(e.args_json || "") + '">copy</button></div>' +
      '<pre class="code">' + hljson(e.args_json || "(not recorded)") + "</pre>" +
      '<div class="k">' + (e.status === "FAILED" ? "error" : "output") +
      ' <button data-action="copy" data-copy="' + esc(out) + '">copy</button></div>' +
      '<pre class="code">' + hljson(out || "(none yet)") + "</pre>" +
      (e.agent_meta ? '<div class="k">agent metadata</div><pre class="code">' + hljson(e.agent_meta) + "</pre>" : "") +
      (e.reasoning ? '<div class="k">agent reasoning</div><div class="why">&#8220;' + esc(e.reasoning) + "&#8221;</div>" : "") +
      (e.status === "WAITING_APPROVAL" ? '<div class="detail-actions">' + actionButtons(e) + "</div>" : "") +
      "</td></tr>";
  }
  return html;
}

function renderTable(execs) {
  $("rows").innerHTML = execs.length
    ? execs.map(rowHtml).join("")
    : '<tr><td colspan="9" class="empty">no executions match</td></tr>';
}

/* Smooth warm area chart, in the style of the reference dashboard. */
function drawSpark(execs) {
  const c = $("spark"), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w) return;
  c.width = w * dpr; c.height = h * dpr;
  const x = c.getContext("2d"); x.scale(dpr, dpr); x.clearRect(0, 0, w, h);

  const BUCKETS = 30, SPAN = 600, now = nowS();
  const total = new Array(BUCKETS).fill(0), bad = new Array(BUCKETS).fill(0);
  for (const e of execs) {
    const age = now - e.created_at;
    if (age < 0 || age > SPAN) continue;
    const b = BUCKETS - 1 - Math.floor(age / (SPAN / BUCKETS));
    total[b]++;
    if (e.status === "FAILED") bad[b]++;
  }
  const peak = Math.max(2, ...total);
  const px = i => (i / (BUCKETS - 1)) * (w - 8) + 4;
  const py = v => h - 6 - (v / peak) * (h - 22);

  // dashed vertical gridlines, like the reference
  x.strokeStyle = "rgba(255,255,255,.14)"; x.lineWidth = 1; x.setLineDash([3, 5]);
  for (let i = 5; i < BUCKETS; i += 6) {
    x.beginPath(); x.moveTo(px(i), 8); x.lineTo(px(i), h - 6); x.stroke();
  }
  x.setLineDash([]);

  const css = getComputedStyle(document.documentElement);
  const gold = css.getPropertyValue("--gold").trim() || "#e8a33d";
  const red = css.getPropertyValue("--red").trim() || "#ee7b66";

  function smoothPath(series) {
    // Catmull-Rom through the bucket points, converted to cubic beziers
    const pts = series.map((v, i) => [px(i), py(v)]);
    x.beginPath();
    x.moveTo(pts[0][0], pts[0][1]);
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1],
            p3 = pts[Math.min(pts.length - 1, i + 2)];
      x.bezierCurveTo(
        p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
        p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6,
        p2[0], p2[1]);
    }
  }

  // filled gold area
  smoothPath(total);
  x.lineTo(px(BUCKETS - 1), h - 6); x.lineTo(px(0), h - 6); x.closePath();
  const grad = x.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(232,163,61,.55)");
  grad.addColorStop(1, "rgba(232,163,61,.03)");
  x.fillStyle = grad; x.fill();

  // gold line on top
  smoothPath(total);
  x.strokeStyle = gold; x.lineWidth = 2.5;
  x.shadowColor = gold; x.shadowBlur = 10;
  x.stroke(); x.shadowBlur = 0;

  // failed line, only when something failed
  if (bad.some(v => v)) {
    smoothPath(bad);
    x.strokeStyle = red; x.lineWidth = 1.8;
    x.shadowColor = red; x.shadowBlur = 6;
    x.stroke(); x.shadowBlur = 0;
  }
}

async function refresh(force) {
  let data;
  try {
    const res = await fetch("/api/executions?tool=" + encodeURIComponent(S.tool) +
      "&status=" + encodeURIComponent(S.status));
    data = await res.json();
  } catch (e) { return; }
  S.skew = Date.now() / 1000 - data.now;
  S.data = data;
  renderSources(data.sources);
  renderCounts(data.counts);
  drawSpark(data.executions);
  const hash = JSON.stringify([S.tool, S.status, [...S.open],
    data.executions.map(e => [e.id, e.status, e.finished_at, e.retry_count])]);
  if (force || hash !== S.hash) {
    S.hash = hash;
    renderFlight(data.executions);
    renderTable(data.executions);
  }
  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

/* elapsed timers tick in place: no re-render, so buttons stay put */
setInterval(() => {
  document.querySelectorAll(".elapsed[data-since]").forEach(el => {
    el.textContent = fmtElapsed(parseFloat(el.dataset.since));
  });
}, 1000);

async function decide(kind, source, id, note) {
  try {
    const res = await fetch("/api/" + kind, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: source, execution_id: id, note: note || "" }),
    });
    const body = await res.json();
    if (body.ok) toast((kind === "approve" ? "✓ approved " : "✕ rejected ") + id.slice(0, 8), kind === "approve" ? "ok" : "bad");
    else toast("⚠ " + (body.error || kind + " failed"), "bad");
  } catch (e) { toast("⚠ network error: " + e.message, "bad"); }
  refresh(true);
}

function openReject(source, id, tool) {
  S.reject = { source, id };
  $("reject-target").innerHTML = "Rejecting <code>" + esc(tool || "") + "</code> &#183; <code>" + esc(id.slice(0, 8)) + "&#8230;</code>";
  $("reject-note").value = "";
  $("reject-overlay").classList.add("show");
  setTimeout(() => $("reject-note").focus(), 50);
}
function closeReject() {
  S.reject = null;
  $("reject-overlay").classList.remove("show");
}

/* one delegated listener survives every re-render */
document.addEventListener("click", async ev => {
  const t = ev.target.closest("[data-action]");
  if (!t) {
    if (ev.target === $("reject-overlay")) closeReject();  // click outside the modal
    return;
  }
  const a = t.dataset.action;

  if (a === "approve") {
    ev.stopPropagation();
    t.disabled = true;
    await decide("approve", t.dataset.source, t.dataset.id, "");
  } else if (a === "reject") {
    ev.stopPropagation();
    openReject(t.dataset.source, t.dataset.id, t.dataset.tool);
  } else if (a === "reject-confirm") {
    const r = S.reject;
    if (r) { closeReject(); await decide("reject", r.source, r.id, $("reject-note").value.trim()); }
  } else if (a === "reject-cancel") {
    closeReject();
  } else if (a === "toggle") {
    const key = t.dataset.key;
    S.open.has(key) ? S.open.delete(key) : S.open.add(key);
    if (S.data) { renderTable(S.data.executions); S.hash = ""; }
  } else if (a === "seg") {
    S.status = t.dataset.status;
    document.querySelectorAll("#seg button").forEach(b => b.classList.toggle("on", b === t));
    refresh(true);
  } else if (a === "pause") {
    S.paused = !S.paused;
    t.innerHTML = S.paused ? "&#9654;" : "&#10074;&#10074;";
    if (!S.paused) refresh(true);
  } else if (a === "copy") {
    try { await navigator.clipboard.writeText(t.dataset.copy); toast("copied to clipboard", "ok"); }
    catch (e) { toast("copy failed: " + e.message, "bad"); }
  }
});

document.addEventListener("keydown", ev => {
  if (ev.key === "Escape" && S.reject) closeReject();
  if (ev.key === "Enter" && (ev.metaKey || ev.ctrlKey) && S.reject) {
    const r = S.reject;
    closeReject();
    decide("reject", r.source, r.id, $("reject-note").value.trim());
  }
});

$("f-tool").addEventListener("input", ev => { S.tool = ev.target.value.trim(); refresh(true); });
setInterval(() => { if (!S.paused) refresh(false); }, 2000);
refresh(true);
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="toolbay monitor: observability dashboard for tbay executions")
    parser.add_argument(
        "--db",
        action="append",
        default=None,
        metavar="URL",
        help="database to watch; repeat for several (sqlite:///path, postgresql://..., redis://...). "
        "Defaults to $TBAY_DASHBOARD_DBS (comma separated), then $TBAY_DB_URL, then tbay's default SQLite file.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    args = parser.parse_args()

    urls = args.db
    if not urls and os.environ.get("TBAY_DASHBOARD_DBS"):
        urls = [u.strip() for u in os.environ["TBAY_DASHBOARD_DBS"].split(",") if u.strip()]
    if not urls:
        urls = [os.environ.get("TBAY_DB_URL", "sqlite:///~/.tbay/db.sqlite")]

    for url in urls:
        label = _source_label(url)
        if label in SOURCES:  # two URLs with the same scheme+host: keep both, disambiguated
            label = f"{label} #{len(SOURCES) + 1}"
        SOURCES[label] = TbayClient(url)
        print(f"  watching {label}  ({url.split('@')[-1] if '@' in url else url})")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\ntoolbay monitor running at http://{args.host}:{args.port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
