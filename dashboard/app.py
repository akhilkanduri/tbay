"""tbay monitor: a small, standalone observability web app for tbay.

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
    client.backend.resolve_approval(execution_id, approved=approved, resolver="dashboard")
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # keep the terminal quiet; errors still surface
        pass

    def _send(self, body: bytes, content_type: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
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
<title>tbay monitor</title>
<style>
  :root {
    --bg: #0d1117; --panel: #161b22; --border: #2d333b; --text: #e6edf3;
    --muted: #8b949e; --blue: #58a6ff; --amber: #d29922; --green: #3fb950;
    --red: #f85149; --mono: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--text); font: 14px/1.5 -apple-system, "Segoe UI", sans-serif; padding: 20px; }
  h1 { font-size: 18px; display: flex; align-items: center; gap: 10px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green); }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: var(--muted); margin: 26px 0 10px; }
  .sources { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  .chip { font: 12px var(--mono); background: var(--panel); border: 1px solid var(--border); border-radius: 999px; padding: 3px 12px; color: var(--muted); }
  .chip.err { border-color: var(--red); color: var(--red); }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 18px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .card .n { font-size: 28px; font-weight: 700; font-family: var(--mono); }
  .card .l { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .06em; }
  .card.running .n { color: var(--blue); } .card.waiting .n { color: var(--amber); }
  .card.succeeded .n { color: var(--green); } .card.failed .n { color: var(--red); }
  .flight { display: flex; flex-direction: column; gap: 10px; }
  .flight-item { background: var(--panel); border: 1px solid var(--border); border-left: 3px solid var(--blue); border-radius: 8px; padding: 12px 16px; display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .flight-item.waiting { border-left-color: var(--amber); }
  .pulse { width: 9px; height: 9px; border-radius: 50%; background: var(--blue); animation: pulse 1.2s ease-in-out infinite; flex: none; }
  .flight-item.waiting .pulse { background: var(--amber); animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; transform: scale(1);} 50% { opacity: .35; transform: scale(.75);} }
  .flight-item .tool { font-family: var(--mono); font-weight: 600; }
  .flight-item .elapsed { font-family: var(--mono); color: var(--muted); }
  .flight-item .args { font: 12px var(--mono); color: var(--muted); overflow-wrap: anywhere; flex: 1 1 260px; }
  .empty { color: var(--muted); background: var(--panel); border: 1px dashed var(--border); border-radius: 8px; padding: 14px 16px; }
  button { background: transparent; color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 4px 12px; cursor: pointer; font-size: 13px; }
  button:hover { border-color: var(--muted); }
  button.approve { border-color: var(--green); color: var(--green); }
  button.reject { border-color: var(--red); color: var(--red); }
  .filters { display: flex; gap: 8px; margin: 0 0 10px; flex-wrap: wrap; align-items: center; }
  input, select { background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 5px 10px; font-size: 13px; }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; min-width: 900px; }
  th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); padding: 10px 12px; border-bottom: 1px solid var(--border); background: var(--panel); position: sticky; top: 0; }
  td { padding: 9px 12px; border-bottom: 1px solid var(--border); vertical-align: top; font-size: 13px; }
  tr.row { cursor: pointer; } tr.row:hover td { background: rgba(255,255,255,.02); }
  td.mono, .detail pre { font-family: var(--mono); font-size: 12px; }
  td .clip { display: inline-block; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: bottom; color: var(--muted); }
  .badge { font: 600 11px var(--mono); border-radius: 999px; padding: 2px 9px; white-space: nowrap; }
  .badge.RUNNING { color: var(--blue); background: rgba(88,166,255,.12); }
  .badge.WAITING_APPROVAL { color: var(--amber); background: rgba(210,153,34,.12); }
  .badge.SUCCEEDED { color: var(--green); background: rgba(63,185,80,.12); }
  .badge.FAILED { color: var(--red); background: rgba(248,81,73,.12); }
  tr.detail td { background: #10151c; }
  .detail pre { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin: 6px 0 10px; }
  .detail .k { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .06em; }
  .muted { color: var(--muted); }
</style>
</head>
<body>
  <h1><span class="dot"></span>tbay monitor <span id="paused" class="muted" style="font-size:12px"></span></h1>
  <div class="sources" id="sources"></div>

  <div class="cards">
    <div class="card running"><div class="n" id="c-running">0</div><div class="l">running</div></div>
    <div class="card waiting"><div class="n" id="c-waiting">0</div><div class="l">waiting approval</div></div>
    <div class="card succeeded"><div class="n" id="c-succeeded">0</div><div class="l">succeeded</div></div>
    <div class="card failed"><div class="n" id="c-failed">0</div><div class="l">failed</div></div>
  </div>

  <h2>In flight</h2>
  <div class="flight" id="flight"><div class="empty">Nothing running right now.</div></div>

  <h2>Executions</h2>
  <div class="filters">
    <input id="f-tool" placeholder="filter by tool name">
    <select id="f-status">
      <option value="">all statuses</option>
      <option>RUNNING</option><option>WAITING_APPROVAL</option>
      <option>SUCCEEDED</option><option>FAILED</option>
    </select>
    <button id="pause">Pause</button>
    <span class="muted" id="updated"></span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr>
        <th>When</th><th>Tool</th><th>Status</th><th>Duration</th>
        <th>Input</th><th>Output</th><th>Why</th><th>Source</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

<script>
let paused = false, skew = 0, open = new Set();

const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const pretty = s => { try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s; } };
const ago = t => {
  const d = Math.max(0, (Date.now()/1000 - skew) - t);
  if (d < 60) return d.toFixed(0) + "s ago";
  if (d < 3600) return (d/60).toFixed(0) + "m ago";
  return (d/3600).toFixed(1) + "h ago";
};
const elapsed = t => {
  const d = Math.max(0, (Date.now()/1000 - skew) - t);
  return d < 60 ? d.toFixed(0) + "s" : Math.floor(d/60) + "m " + (d%60).toFixed(0) + "s";
};
const dur = e => {
  if (e.finished_at) { const d = e.finished_at - e.created_at; return d < 1 ? (d*1000).toFixed(0)+"ms" : d.toFixed(2)+"s"; }
  return elapsed(e.created_at) + "…";
};

async function act(kind, source, id) {
  await fetch("/api/" + kind, { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({source, execution_id: id}) });
  refresh();
}

function flightItem(e) {
  const waiting = e.status === "WAITING_APPROVAL";
  const note = waiting ? "waiting for a human" : "still working (the tool call has not returned yet)";
  const buttons = waiting
    ? `<button class="approve" onclick="act('approve','${esc(e.source)}','${esc(e.id)}')">Approve</button>
       <button class="reject" onclick="act('reject','${esc(e.source)}','${esc(e.id)}')">Reject</button>` : "";
  return `<div class="flight-item ${waiting ? "waiting" : ""}">
    <span class="pulse"></span>
    <span class="tool">${esc(e.tool_name)}</span>
    <span class="badge ${e.status}">${e.status}</span>
    <span class="elapsed">${elapsed(e.created_at)} · ${esc(note)}</span>
    <span class="args">${esc(e.args_json || "")}</span>
    ${buttons}</div>`;
}

function row(e) {
  const out = e.status === "FAILED" ? (e.error || "") : (e.result_json || "");
  const key = e.source + ":" + e.id;
  const main = `<tr class="row" onclick="toggle('${esc(key)}')">
    <td class="mono muted" title="${new Date(e.created_at*1000).toISOString()}">${ago(e.created_at)}</td>
    <td class="mono">${esc(e.tool_name)}</td>
    <td><span class="badge ${e.status}">${e.status}</span></td>
    <td class="mono muted">${dur(e)}</td>
    <td class="mono"><span class="clip">${esc(e.args_json || "")}</span></td>
    <td class="mono"><span class="clip">${esc(out)}</span></td>
    <td><span class="clip">${esc(e.reasoning || "")}</span></td>
    <td class="mono muted">${esc(e.source)}</td></tr>`;
  if (!open.has(key)) return main;
  return main + `<tr class="detail"><td colspan="8">
    <span class="k">execution</span><pre>${esc(e.id)}  policy=${esc(e.policy_name)}  tenant=${esc(e.tenant || "(none)")}  retries=${e.retry_count}</pre>
    <span class="k">input args</span><pre>${esc(pretty(e.args_json || "(not recorded)"))}</pre>
    <span class="k">${e.status === "FAILED" ? "error" : "output"}</span><pre>${esc(pretty(out || "(none yet)"))}</pre>
    ${e.reasoning ? `<span class="k">agent reasoning</span><pre>${esc(e.reasoning)}</pre>` : ""}</td></tr>`;
}

function toggle(key) { open.has(key) ? open.delete(key) : open.add(key); refresh(); }

async function refresh() {
  const tool = document.getElementById("f-tool").value.trim();
  const status = document.getElementById("f-status").value;
  const res = await fetch(`/api/executions?tool=${encodeURIComponent(tool)}&status=${encodeURIComponent(status)}`);
  const data = await res.json();
  skew = Date.now()/1000 - data.now;

  document.getElementById("sources").innerHTML = Object.entries(data.sources).map(([name, err]) =>
    `<span class="chip ${err ? "err" : ""}">${esc(name)}${err ? " · " + esc(err) : ""}</span>`).join("");
  document.getElementById("c-running").textContent = data.counts.RUNNING;
  document.getElementById("c-waiting").textContent = data.counts.WAITING_APPROVAL;
  document.getElementById("c-succeeded").textContent = data.counts.SUCCEEDED;
  document.getElementById("c-failed").textContent = data.counts.FAILED;

  const active = data.executions.filter(e => e.status === "RUNNING" || e.status === "WAITING_APPROVAL");
  document.getElementById("flight").innerHTML =
    active.length ? active.map(flightItem).join("") : '<div class="empty">Nothing running right now.</div>';
  document.getElementById("rows").innerHTML = data.executions.map(row).join("");
  document.getElementById("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

document.getElementById("pause").onclick = () => {
  paused = !paused;
  document.getElementById("pause").textContent = paused ? "Resume" : "Pause";
  document.getElementById("paused").textContent = paused ? "(paused)" : "";
};
document.getElementById("f-tool").oninput = refresh;
document.getElementById("f-status").onchange = refresh;
setInterval(() => { if (!paused) refresh(); }, 2000);
refresh();
</script>
</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="tbay monitor: observability dashboard for tbay executions")
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
    print(f"\ntbay monitor running at http://{args.host}:{args.port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
