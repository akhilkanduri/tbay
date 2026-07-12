# toolbay monitor

A small, standalone web dashboard for watching what your agents' tool calls
are doing. It is intentionally NOT part of the `tbay` Python package: it's
one file (`app.py`, stdlib HTTP server, no web framework) that reads the
same database your app writes to.

What it shows:

- **In flight**: every call that is RUNNING right now, with a live elapsed
  timer. A tool call that kicked off a container or a long API job shows up
  here until it actually returns.
- **Waiting approval**: paused destructive calls, with Approve / Reject
  buttons that act directly on the database (same effect as `tbay approve`).
- **Every execution**: tool name, status, duration, input arguments
  (redacted per policy), output or error, and the agent's recorded
  reasoning. Click any row for the full pretty-printed detail.
- **Status counts** and per-backend connection health, refreshed every 2
  seconds.

## Run it

From the repo root (this installs tbay plus the Postgres and Redis drivers):

```
uv sync --extra dev
uv run python dashboard/app.py --db postgresql://postgres:tbay@localhost:5432/tbay \
                               --db redis://localhost:6379/0
```

Then open http://localhost:8787.

`--db` takes any URL tbay itself accepts and can be repeated, so one
dashboard can watch Postgres and Redis (and SQLite) at the same time; each
execution row shows which backend it came from:

```
--db sqlite:///~/.tbay/db.sqlite
--db postgresql://user:pass@host:5432/dbname
--db redis://host:6379/0
```

With no `--db` at all it falls back to `$TBAY_DASHBOARD_DBS` (comma
separated), then `$TBAY_DB_URL`, then tbay's default SQLite file, so the
zero-argument `uv run python dashboard/app.py` already works for a local
demo.

Outside this repo, all it needs is tbay installed with the right extras:

```
pip install "tbay[postgres,redis]"
python app.py --db postgresql://... --db redis://...
```

## In the dev container

The bundled Postgres and Redis share the dev container's network
namespace, so `localhost:5432` / `localhost:6379` work in the container
terminal, and devcontainer.json forwards 5432, 6379, and 8787 to your
machine through VS Code, so the same localhost URLs also work in a normal
terminal outside while the devcontainer is open (check the Ports tab; if a
port is taken locally, VS Code maps it to a nearby free one and shows it
there).

Inside the container it's even shorter, because `TBAY_DASHBOARD_DBS` is
already set to the bundled Postgres and Redis (the container never uses
SQLite; everything in it runs on Postgres):

```
uv run python dashboard/app.py
```

After changing anything under `.devcontainer/`, run "Dev Containers:
Rebuild Container" for it to take effect.

Run an example in a second terminal (`uv run python
examples/demo.py`) and watch its calls appear live, including
the large refund pausing in WAITING_APPROVAL, which you can approve from
the page instead of the CLI.

## Options

```
--db URL      database to watch, repeatable (default: env vars as above)
--host HOST   bind address (default 127.0.0.1)
--port PORT   port (default 8787)
```

There is no authentication: the dashboard can read every recorded call and
approve paused ones, so bind it to localhost (the default) or put it behind
something that does authentication before exposing it more widely.
