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


def resolve_approval(source: str, execution_id: str, approved: bool) -> dict:
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
    client.backend.resolve_approval(execution_id, approved=approved, resolver="dashboard", signature=signature)
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
    --bg: #04060d; --ink: #e8edf7; --dim: #7d8aa5; --faint: #4a5670;
    --line: rgba(148,163,205,.14); --glass: rgba(20,27,45,.62);
    --cyan: #22d3ee; --violet: #a78bfa; --amber: #fbbf24; --green: #34d399; --red: #fb7185;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; margin: 0; }
  html { color-scheme: dark; }
  body {
    background: var(--bg); color: var(--ink); min-height: 100vh;
    font: 14px/1.55 -apple-system, "Segoe UI", Inter, sans-serif;
    padding: 26px clamp(14px, 3vw, 44px) 70px; overflow-x: hidden; position: relative;
  }

  /* -- aurora backdrop -- */
  .aurora { position: fixed; inset: 0; z-index: -2; overflow: hidden; }
  .aurora i { position: absolute; border-radius: 50%; filter: blur(110px); opacity: .32; }
  .aurora i:nth-child(1) { width: 620px; height: 620px; left: -180px; top: -220px;
    background: radial-gradient(circle, #0e7490, transparent 70%); animation: drift1 26s ease-in-out infinite alternate; }
  .aurora i:nth-child(2) { width: 560px; height: 560px; right: -160px; top: 8%;
    background: radial-gradient(circle, #6d28d9, transparent 70%); animation: drift2 32s ease-in-out infinite alternate; }
  .aurora i:nth-child(3) { width: 480px; height: 480px; left: 34%; bottom: -260px;
    background: radial-gradient(circle, #0f766e, transparent 70%); animation: drift1 38s ease-in-out infinite alternate-reverse; }
  @keyframes drift1 { to { transform: translate(90px, 60px) scale(1.15); } }
  @keyframes drift2 { to { transform: translate(-70px, 90px) scale(.9); } }
  .gridlines { position: fixed; inset: 0; z-index: -1; pointer-events: none; opacity: .05;
    background-image: linear-gradient(var(--ink) 1px, transparent 1px), linear-gradient(90deg, var(--ink) 1px, transparent 1px);
    background-size: 44px 44px;
    -webkit-mask-image: radial-gradient(ellipse 90% 70% at 50% 0%, black 30%, transparent 75%);
            mask-image: radial-gradient(ellipse 90% 70% at 50% 0%, black 30%, transparent 75%); }

  /* -- header -- */
  header { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  .logo { width: 42px; height: 42px; border-radius: 12px; display: grid; place-items: center;
    font-size: 22px; background: linear-gradient(135deg, rgba(34,211,238,.22), rgba(167,139,250,.22));
    border: 1px solid rgba(34,211,238,.35); box-shadow: 0 0 26px rgba(34,211,238,.28), inset 0 0 14px rgba(34,211,238,.12); }
  .brand { font-size: 24px; font-weight: 800; letter-spacing: -.02em;
    background: linear-gradient(92deg, #67e8f9 8%, #a78bfa 55%, #f0abfc 95%);
    -webkit-background-clip: text; background-clip: text; color: transparent; }
  .brand small { font-weight: 400; letter-spacing: .28em; text-transform: uppercase;
    font-size: 10px; color: var(--dim); -webkit-text-fill-color: var(--dim); margin-left: 10px; }
  .head-right { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .chip { font: 11px var(--mono); background: var(--glass); border: 1px solid var(--line);
    border-radius: 999px; padding: 5px 12px 5px 9px; color: var(--dim);
    display: inline-flex; align-items: center; gap: 7px; backdrop-filter: blur(12px); }
  .chip b { width: 7px; height: 7px; border-radius: 50%; background: var(--green);
    box-shadow: 0 0 8px var(--green); }
  .chip.err { border-color: rgba(251,113,133,.5); color: var(--red); }
  .chip.err b { background: var(--red); box-shadow: 0 0 8px var(--red); }

  /* -- glass panels -- */
  .panel { background: var(--glass); border: 1px solid var(--line); border-radius: 16px;
    backdrop-filter: blur(18px); box-shadow: 0 12px 40px rgba(0,0,0,.35); }

  /* -- stat cards -- */
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 14px; margin-top: 26px; }
  .card { padding: 18px 20px 16px; position: relative; overflow: hidden; }
  .card::after { content: ""; position: absolute; inset: auto 0 0 0; height: 2px;
    background: linear-gradient(90deg, transparent, var(--ac), transparent); opacity: .8; }
  .card .l { font-size: 10px; letter-spacing: .22em; text-transform: uppercase; color: var(--dim); }
  .card .n { font: 800 38px/1.2 var(--mono); font-variant-numeric: tabular-nums; color: var(--ac);
    text-shadow: 0 0 24px color-mix(in srgb, var(--ac) 55%, transparent); transition: transform .18s; }
  .card .n.pop { transform: scale(1.14); }
  .card .s { font-size: 11px; color: var(--faint); margin-top: 2px; }
  .card.running   { --ac: var(--cyan); }
  .card.waiting   { --ac: var(--amber); }
  .card.succeeded { --ac: var(--green); }
  .card.failed    { --ac: var(--red); }
  .card .ring { position: absolute; right: 16px; top: 16px; width: 26px; height: 26px; border-radius: 50%;
    border: 2px solid color-mix(in srgb, var(--ac) 30%, transparent); border-top-color: var(--ac);
    animation: spin 1.1s linear infinite; opacity: 0; transition: opacity .3s; }
  .card.live .ring { opacity: 1; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* -- sparkline -- */
  .spark-wrap { margin-top: 14px; padding: 14px 18px 10px; }
  .spark-head { display: flex; justify-content: space-between; align-items: baseline; }
  .spark-head .t { font-size: 10px; letter-spacing: .22em; text-transform: uppercase; color: var(--dim); }
  .spark-head .legend { font: 10px var(--mono); color: var(--faint); }
  .legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin: 0 4px 0 12px; vertical-align: -1px; }
  canvas#spark { width: 100%; height: 74px; display: block; margin-top: 6px; }

  /* -- section titles -- */
  h2 { margin: 30px 0 12px; font-size: 11px; letter-spacing: .26em; text-transform: uppercase;
    color: var(--dim); display: flex; align-items: center; gap: 12px; }
  h2::after { content: ""; flex: 1; height: 1px; background: linear-gradient(90deg, var(--line), transparent); }

  /* -- in flight -- */
  .flight { display: flex; flex-direction: column; gap: 12px; }
  .fcard { padding: 15px 18px; display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    position: relative; overflow: hidden; border-left: 3px solid var(--ac); --ac: var(--cyan); }
  .fcard.waiting { --ac: var(--amber); }
  .fcard::before { content: ""; position: absolute; inset: 0; pointer-events: none;
    background: linear-gradient(100deg, transparent 30%, color-mix(in srgb, var(--ac) 7%, transparent) 50%, transparent 70%);
    transform: translateX(-100%); animation: sweep 2.6s ease-in-out infinite; }
  @keyframes sweep { 60%, 100% { transform: translateX(100%); } }
  .orb { width: 11px; height: 11px; border-radius: 50%; background: var(--ac); flex: none;
    box-shadow: 0 0 12px var(--ac); animation: throb 1.3s ease-in-out infinite; }
  @keyframes throb { 50% { opacity: .35; transform: scale(.72); } }
  .fcard.waiting .orb { animation: none; }
  .fcard .tool { font: 600 14px var(--mono); }
  .tag { font: 700 9px var(--mono); letter-spacing: .18em; padding: 3px 9px; border-radius: 5px;
    color: var(--ac); background: color-mix(in srgb, var(--ac) 13%, transparent);
    border: 1px solid color-mix(in srgb, var(--ac) 35%, transparent); }
  .elapsed { font: 12px var(--mono); color: var(--dim); }
  .agent { font: 11px var(--mono); color: var(--violet); background: rgba(167,139,250,.1);
    border: 1px solid rgba(167,139,250,.3); border-radius: 999px; padding: 2px 10px; }
  .fcard code, td code { font: 11.5px var(--mono); color: var(--dim);
    background: rgba(3,6,14,.55); border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px; }
  .fcard code { flex: 1 1 240px; overflow-wrap: anywhere; }
  .empty { padding: 22px; text-align: center; color: var(--faint); }
  .empty .glyph { font-size: 26px; display: block; margin-bottom: 6px; opacity: .7; }

  /* -- buttons -- */
  button { font: 600 12px/1 var(--mono); color: var(--ink); cursor: pointer; border-radius: 9px;
    padding: 9px 16px; border: 1px solid var(--line); background: rgba(255,255,255,.04);
    transition: transform .12s, box-shadow .2s, border-color .2s; }
  button:hover { transform: translateY(-1px); border-color: var(--dim); }
  button:active { transform: translateY(0) scale(.97); }
  button.approve { color: #052014; border: none;
    background: linear-gradient(135deg, #34d399, #10b981); box-shadow: 0 0 18px rgba(52,211,153,.35); }
  button.approve:hover { box-shadow: 0 0 30px rgba(52,211,153,.6); }
  button.reject { color: var(--red); border-color: rgba(251,113,133,.45); background: rgba(251,113,133,.08); }
  button.reject:hover { box-shadow: 0 0 22px rgba(251,113,133,.35); border-color: var(--red); }

  /* -- filters -- */
  .filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
  .search { position: relative; }
  .search input { background: var(--glass); border: 1px solid var(--line); color: var(--ink);
    border-radius: 10px; padding: 9px 14px 9px 34px; font-size: 13px; width: 240px; outline: none;
    backdrop-filter: blur(12px); transition: border-color .2s, box-shadow .2s; }
  .search input:focus { border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(34,211,238,.15); }
  .search::before { content: "\2315"; position: absolute; left: 11px; top: 50%; translate: 0 -54%;
    color: var(--faint); font-size: 15px; }
  .seg { display: flex; background: var(--glass); border: 1px solid var(--line); border-radius: 10px;
    padding: 3px; gap: 2px; backdrop-filter: blur(12px); flex-wrap: wrap; }
  .seg button { border: none; background: transparent; padding: 7px 13px; border-radius: 8px;
    color: var(--dim); font-size: 11px; }
  .seg button:hover { transform: none; color: var(--ink); }
  .seg button.on { color: #04121c; background: linear-gradient(135deg, #67e8f9, #22d3ee);
    box-shadow: 0 0 14px rgba(34,211,238,.4); }
  .updated { font: 11px var(--mono); color: var(--faint); margin-left: auto; }

  /* -- table -- */
  .table-wrap { overflow-x: auto; border-radius: 16px; }
  table { border-collapse: collapse; width: 100%; min-width: 1020px; }
  th { text-align: left; font-size: 10px; letter-spacing: .18em; text-transform: uppercase;
    color: var(--dim); padding: 12px 14px; border-bottom: 1px solid var(--line);
    position: sticky; top: 0; background: rgba(10,15,28,.9); backdrop-filter: blur(8px); z-index: 1; }
  td { padding: 11px 14px; border-bottom: 1px solid rgba(148,163,205,.07); vertical-align: middle;
    font-size: 13px; white-space: nowrap; }
  tr.detail > td { white-space: normal; }
  tr.row { cursor: pointer; transition: background .15s; }
  tr.row:hover td { background: rgba(103,232,249,.045); }
  tr.row.active-row td { background: rgba(103,232,249,.07); }
  td.mono { font-family: var(--mono); font-size: 12px; }
  td .clip { display: inline-block; max-width: 220px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; vertical-align: bottom; }
  .pill { font: 700 10px var(--mono); letter-spacing: .08em; border-radius: 999px; padding: 4px 11px 4px 8px;
    white-space: nowrap; display: inline-flex; align-items: center; gap: 6px;
    color: var(--pc); background: color-mix(in srgb, var(--pc) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--pc) 30%, transparent); }
  .pill i { width: 6px; height: 6px; border-radius: 50%; background: var(--pc); box-shadow: 0 0 7px var(--pc); }
  .pill.RUNNING { --pc: var(--cyan); } .pill.RUNNING i { animation: throb 1.3s infinite; }
  .pill.WAITING_APPROVAL { --pc: var(--amber); } .pill.WAITING_APPROVAL i { animation: throb 1.8s infinite; }
  .pill.SUCCEEDED { --pc: var(--green); }
  .pill.FAILED { --pc: var(--red); }
  .dim { color: var(--dim); } .faint { color: var(--faint); }

  /* -- expanded detail -- */
  tr.detail > td { background: rgba(3,6,14,.5); padding: 18px 22px; }
  .meta { font: 11px var(--mono); color: var(--dim); display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 12px; }
  .meta b { color: var(--ink); font-weight: 600; }
  .k { font-size: 9px; letter-spacing: .24em; text-transform: uppercase; color: var(--faint);
    display: flex; align-items: center; gap: 8px; margin: 12px 0 5px; }
  .k button { padding: 3px 9px; font-size: 10px; border-radius: 6px; }
  pre.code { font: 12px/1.6 var(--mono); background: rgba(2,4,10,.75); border: 1px solid var(--line);
    border-radius: 10px; padding: 12px 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .jk { color: #67e8f9; } .js { color: #bef264; } .jn { color: #f0abfc; } .jb { color: #fbbf24; }
  .why { border-left: 2px solid var(--violet); padding: 8px 14px; color: var(--ink);
    background: rgba(167,139,250,.07); border-radius: 0 8px 8px 0; font-style: italic; }
  .detail-actions { margin-top: 14px; display: flex; gap: 10px; }

  /* -- toasts -- */
  .toasts { position: fixed; right: 20px; bottom: 20px; display: flex; flex-direction: column;
    gap: 10px; z-index: 50; }
  .toast { background: var(--glass); border: 1px solid var(--line); border-left: 3px solid var(--tc, var(--cyan));
    backdrop-filter: blur(16px); border-radius: 12px; padding: 12px 18px; font: 12px var(--mono);
    box-shadow: 0 10px 34px rgba(0,0,0,.45); animation: slidein .25s ease-out; max-width: 380px; }
  .toast.ok { --tc: var(--green); } .toast.bad { --tc: var(--red); }
  .toast.out { opacity: 0; translate: 12px 0; transition: all .3s; }
  @keyframes slidein { from { opacity: 0; translate: 16px 0; } }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: .01ms !important; animation-iteration-count: 1 !important; }
  }
</style>
</head>
<body>
<div class="aurora"><i></i><i></i><i></i></div>
<div class="gridlines"></div>

<header>
  <div class="logo">&#9875;</div>
  <div class="brand">toolbay<small>monitor</small></div>
  <div class="head-right">
    <span id="sources"></span>
    <button id="pause" data-action="pause"
      title="Freezes this page's auto-refresh so rows stop moving while you read. Executions themselves are not affected.">
      &#10074;&#10074; pause refresh</button>
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

<div class="spark-wrap panel">
  <div class="spark-head">
    <span class="t">activity &#183; last 10 minutes</span>
    <span class="legend">ok<i style="background:var(--green)"></i>failed<i style="background:var(--red)"></i>active<i style="background:var(--cyan)"></i></span>
  </div>
  <canvas id="spark"></canvas>
</div>

<h2>in flight</h2>
<div class="flight" id="flight"></div>

<h2>executions</h2>
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

<div class="toasts" id="toasts"></div>

<script>
"use strict";
const S = { paused: false, tool: "", status: "", open: new Set(), skew: 0, hash: "", data: null };

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const nowS = () => Date.now() / 1000 - S.skew;

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

/* JSON syntax highlighting: tokenize the raw pretty-printed text and escape
   each piece as it's emitted, so markup and content never mix. */
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

/* -- rendering (only when the data actually changed, so buttons never
      vanish out from under a click) -- */

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

function actionButtons(e, extraClass) {
  return '<button class="approve ' + (extraClass || "") + '" data-action="approve" data-source="' + esc(e.source) +
    '" data-id="' + esc(e.id) + '">&#10003; approve</button>' +
    '<button class="reject" data-action="reject" data-source="' + esc(e.source) +
    '" data-id="' + esc(e.id) + '">&#10005; reject</button>';
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
      (e.agent_id ? '<span class="agent">&#129302; ' + esc(e.agent_id) + "</span>" : "") +
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
    '<td>' + (e.agent_id ? '<span class="agent">' + esc(e.agent_id) + "</span>" : '<span class="faint">&#8212;</span>') + "</td>" +
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

function drawSpark(execs) {
  const c = $("spark"), dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (!w) return;
  c.width = w * dpr; c.height = h * dpr;
  const x = c.getContext("2d"); x.scale(dpr, dpr); x.clearRect(0, 0, w, h);
  const BUCKETS = 40, SPAN = 600, now = nowS();
  const ok = new Array(BUCKETS).fill(0), bad = new Array(BUCKETS).fill(0), act = new Array(BUCKETS).fill(0);
  for (const e of execs) {
    const age = now - e.created_at;
    if (age < 0 || age > SPAN) continue;
    const b = BUCKETS - 1 - Math.floor(age / (SPAN / BUCKETS));
    if (e.status === "FAILED") bad[b]++; else if (e.status === "SUCCEEDED") ok[b]++; else act[b]++;
  }
  const peak = Math.max(1, ...ok.map((v, i) => v + bad[i] + act[i]));
  const bw = w / BUCKETS;
  const css = getComputedStyle(document.documentElement);
  const colors = { ok: css.getPropertyValue("--green"), bad: css.getPropertyValue("--red"), act: css.getPropertyValue("--cyan") };
  for (let i = 0; i < BUCKETS; i++) {
    let y = h - 2;
    for (const [series, color] of [[ok, colors.ok], [bad, colors.bad], [act, colors.act]]) {
      if (!series[i]) continue;
      const bh = (series[i] / peak) * (h - 10);
      x.fillStyle = color; x.globalAlpha = .85;
      x.shadowColor = color; x.shadowBlur = 6;
      x.beginPath();
      x.roundRect(i * bw + 1.5, y - bh, Math.max(2, bw - 3), bh, 2);
      x.fill();
      y -= bh;
    }
  }
  x.shadowBlur = 0; x.globalAlpha = 1;
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

/* one delegated listener survives every re-render */
document.addEventListener("click", async ev => {
  const t = ev.target.closest("[data-action]");
  if (!t) return;
  const a = t.dataset.action;

  if (a === "approve" || a === "reject") {
    ev.stopPropagation();
    t.disabled = true;
    try {
      const res = await fetch("/api/" + a, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source: t.dataset.source, execution_id: t.dataset.id }),
      });
      const body = await res.json();
      if (body.ok) toast((a === "approve" ? "✓ approved " : "✕ rejected ") + t.dataset.id.slice(0, 8), a === "approve" ? "ok" : "bad");
      else toast("⚠ " + (body.error || a + " failed"), "bad");
    } catch (e) { toast("⚠ network error: " + e.message, "bad"); }
    refresh(true);
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
    t.innerHTML = S.paused ? "&#9654; resume refresh" : "&#10074;&#10074; pause refresh";
    if (!S.paused) refresh(true);
  } else if (a === "copy") {
    try { await navigator.clipboard.writeText(t.dataset.copy); toast("copied to clipboard", "ok"); }
    catch (e) { toast("copy failed: " + e.message, "bad"); }
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
