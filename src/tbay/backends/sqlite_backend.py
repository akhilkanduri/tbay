from __future__ import annotations

import os
import sqlite3
import time
from typing import List, Optional

from .base import (
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
    FAILED,
    RUNNING,
    SUCCEEDED,
    WAITING_APPROVAL,
    AcquireResult,
    ExecutionRecord,
    StorageBackend,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    tenant TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_json TEXT,
    result_json TEXT,
    error TEXT,
    policy_name TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    cache_expires_at REAL,
    created_at REAL NOT NULL,
    finished_at REAL,
    embedding_json TEXT,
    reasoning TEXT,
    agent_id TEXT,
    agent_meta TEXT,
    budget_value REAL,
    UNIQUE (tool_name, idempotency_key, tenant)
);
CREATE TABLE IF NOT EXISTS approvals (
    execution_id TEXT PRIMARY KEY REFERENCES executions(id),
    status TEXT NOT NULL,
    requested_at REAL NOT NULL,
    resolved_at REAL,
    resolver TEXT,
    signature TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS controls (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _resolve_sqlite_path(db_url_or_path: str) -> str:
    """Turn a 'sqlite:///...' URL (or a bare filesystem path) into a real path.

    Supports the two forms people actually write:
      sqlite:///~/.tbay/db.sqlite   -> ~/.tbay/db.sqlite, then expanded
      sqlite:////abs/path/db.sqlite   -> /abs/path/db.sqlite
    """
    raw = db_url_or_path
    if raw.startswith("sqlite://"):
        raw = raw[len("sqlite://"):]
        while raw.startswith("//"):  # sqlite:////abs/path -> collapse the extra slash
            raw = raw[1:]
        if raw.startswith("/~"):  # sqlite:///~/path -> drop the slash so expanduser fires
            raw = raw[1:]
    if not raw:
        raw = "~/.tbay/db.sqlite"
    return os.path.expanduser(raw)


class SQLiteBackend(StorageBackend):
    """Storage backend for local, single-machine use (or anywhere a shared
    filesystem makes a SQLite file visible to every caller). Zero setup:
    the file and its parent directory are created automatically."""

    def __init__(self, db_url_or_path: str):
        self._path = _resolve_sqlite_path(db_url_or_path)
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        # A short-lived connection per call keeps this simple and safe across
        # threads; WAL mode + a generous busy_timeout let concurrent writers
        # queue instead of failing with "database is locked".
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            # Databases created by tbay < 0.2.0 predate these columns; add
            # them in place so an upgrade never requires dropping the table.
            for column in (
                "embedding_json TEXT",
                "reasoning TEXT",
                "agent_id TEXT",
                "agent_meta TEXT",
                "budget_value REAL",
            ):
                try:
                    conn.execute(f"ALTER TABLE executions ADD COLUMN {column}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            for column in ("signature TEXT", "note TEXT"):
                try:
                    conn.execute(f"ALTER TABLE approvals ADD COLUMN {column}")
                except sqlite3.OperationalError:
                    pass  # column already exists
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            id=row["id"],
            tool_name=row["tool_name"],
            idempotency_key=row["idempotency_key"],
            tenant=row["tenant"],
            status=row["status"],
            args_hash=row["args_hash"],
            args_json=row["args_json"],
            result_json=row["result_json"],
            error=row["error"],
            policy_name=row["policy_name"],
            retry_count=row["retry_count"],
            cache_expires_at=row["cache_expires_at"],
            created_at=row["created_at"],
            finished_at=row["finished_at"],
            embedding_json=row["embedding_json"],
            reasoning=row["reasoning"],
            agent_id=row["agent_id"],
            agent_meta=row["agent_meta"],
            budget_value=row["budget_value"],
        )

    def _fetch_by_key(self, conn, tool_name, idempotency_key, tenant) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM executions WHERE tool_name=? AND idempotency_key=? AND tenant=?",
            (tool_name, idempotency_key, tenant),
        ).fetchone()

    def acquire_or_get(
        self,
        *,
        execution_id,
        tool_name,
        idempotency_key,
        tenant,
        policy_name,
        args_hash,
        args_json,
        max_retries,
        retry_backoff,
        max_concurrent=None,
        embedding_json=None,
        reasoning=None,
        agent_id=None,
        agent_meta=None,
        budget_value=None,
        lease_timeout=None,
    ) -> AcquireResult:
        conn = self._connect()
        try:
            # BEGIN IMMEDIATE takes SQLite's write lock for the whole
            # transaction, so the concurrency check below and the INSERT
            # that follows it happen atomically: no other caller can sneak
            # in between "count RUNNING" and "insert a new RUNNING row".
            conn.execute("BEGIN IMMEDIATE")

            if max_concurrent:
                running = conn.execute(
                    "SELECT COUNT(*) AS n FROM executions WHERE tool_name=? AND tenant=? AND status=?",
                    (tool_name, tenant, RUNNING),
                ).fetchone()["n"]
                if running >= max_concurrent:
                    conn.rollback()
                    return AcquireResult(record=None, wait_for_slot=True)

            # Try to be the first one in: if nobody holds this (tool, key, tenant)
            # yet, this INSERT succeeds and we become the owner. If someone beat
            # us to it, ON CONFLICT DO NOTHING makes this a no-op and we fall
            # through to look at whatever they're doing.
            cur = conn.execute(
                """
                INSERT INTO executions
                    (id, tool_name, idempotency_key, tenant, status, args_hash, args_json, policy_name,
                     created_at, embedding_json, reasoning, agent_id, agent_meta, budget_value)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tool_name, idempotency_key, tenant) DO NOTHING
                """,
                (
                    execution_id,
                    tool_name,
                    idempotency_key,
                    tenant,
                    RUNNING,
                    args_hash,
                    args_json,
                    policy_name,
                    time.time(),
                    embedding_json,
                    reasoning,
                    agent_id,
                    agent_meta,
                    budget_value,
                ),
            )
            if cur.rowcount == 1:
                conn.commit()
                row = self._fetch_by_key(conn, tool_name, idempotency_key, tenant)
                return AcquireResult(record=self._row_to_record(row), owner=True)

            row = self._fetch_by_key(conn, tool_name, idempotency_key, tenant)
            record = self._row_to_record(row)

            if record.status == SUCCEEDED:
                if record.cache_expires_at is None or record.cache_expires_at > time.time():
                    conn.commit()
                    return AcquireResult(record=record, use_cached=True)
                # The cached result went stale. Try to reclaim the row so we
                # become the new owner; if another caller reclaims it first,
                # this UPDATE just matches zero rows and we fall back to
                # following along as if it were still RUNNING.
                cur2 = conn.execute(
                    "UPDATE executions SET status=?, result_json=NULL, error=NULL, created_at=?, finished_at=NULL, "
                    "cache_expires_at=NULL WHERE id=? AND status=?",
                    (RUNNING, time.time(), record.id, SUCCEEDED),
                )
                if cur2.rowcount == 1:
                    conn.commit()
                    record.status = RUNNING
                    return AcquireResult(record=record, owner=True)
                conn.rollback()
                return AcquireResult(record=record, follow_running=True)

            if record.status == FAILED:
                backoff_elapsed = time.time() >= (record.finished_at or 0.0) + retry_backoff
                if record.retry_count < max_retries and backoff_elapsed:
                    cur2 = conn.execute(
                        "UPDATE executions SET status=?, result_json=NULL, error=NULL, created_at=?, "
                        "finished_at=NULL, retry_count=retry_count+1 WHERE id=? AND status=?",
                        (RUNNING, time.time(), record.id, FAILED),
                    )
                    if cur2.rowcount == 1:
                        conn.commit()
                        record.status = RUNNING
                        return AcquireResult(record=record, owner=True)
                    conn.rollback()
                    return AcquireResult(record=record, follow_running=True)
                # Out of retries (or still within the backoff window): hand back
                # the failure that's already on record instead of trying again.
                conn.commit()
                return AcquireResult(record=record, raise_stored_error=True)

            if record.status == WAITING_APPROVAL:
                conn.commit()
                return AcquireResult(record=record, follow_approval=True)

            # RUNNING: someone else holds the lease right now. If the row is
            # old enough that its owner is presumed crashed (lease_timeout),
            # try to reclaim it; the created_at guard makes this a CAS, so
            # exactly one contender wins and the rest keep following.
            if lease_timeout is not None and record.created_at + lease_timeout <= time.time():
                cur2 = conn.execute(
                    "UPDATE executions SET created_at=?, result_json=NULL, error=NULL, finished_at=NULL "
                    "WHERE id=? AND status=? AND created_at=?",
                    (time.time(), record.id, RUNNING, record.created_at),
                )
                if cur2.rowcount == 1:
                    conn.commit()
                    return AcquireResult(record=record, owner=True)
            conn.commit()
            return AcquireResult(record=record, follow_running=True)
        finally:
            conn.close()

    def mark_waiting_approval(self, execution_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE executions SET status=? WHERE id=?", (WAITING_APPROVAL, execution_id))
            conn.execute(
                """
                INSERT INTO approvals (execution_id, status, requested_at) VALUES (?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET status=excluded.status, requested_at=excluded.requested_at,
                    resolved_at=NULL, resolver=NULL
                """,
                (execution_id, APPROVAL_PENDING, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def complete(self, execution_id: str, result_json: str, cache_ttl_seconds: Optional[float]) -> None:
        conn = self._connect()
        try:
            expires_at = time.time() + cache_ttl_seconds if cache_ttl_seconds else None
            conn.execute(
                "UPDATE executions SET status=?, result_json=?, finished_at=?, cache_expires_at=? WHERE id=?",
                (SUCCEEDED, result_json, time.time(), expires_at, execution_id),
            )
            conn.commit()
        finally:
            conn.close()

    def fail(self, execution_id: str, error: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE executions SET status=?, error=?, finished_at=? WHERE id=?",
                (FAILED, error, time.time(), execution_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get(self, execution_id: str) -> Optional[ExecutionRecord]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM executions WHERE id=?", (execution_id,)).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def get_approval_status(self, execution_id: str) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT status FROM approvals WHERE execution_id=?", (execution_id,)).fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def resolve_approval(self, execution_id: str, approved: bool, resolver: str = "", signature=None,
                         note=None) -> None:
        conn = self._connect()
        try:
            status = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
            conn.execute(
                "UPDATE approvals SET status=?, resolved_at=?, resolver=?, signature=?, note=? "
                "WHERE execution_id=?",
                (status, time.time(), resolver, signature, note, execution_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_approval(self, execution_id: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM approvals WHERE execution_id=?", (execution_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_executions(
        self, *, tool_name=None, status=None, tenant=None, limit=50
    ) -> List[ExecutionRecord]:
        conn = self._connect()
        try:
            clauses, params = [], []
            if tool_name:
                clauses.append("tool_name=?")
                params.append(tool_name)
            if status:
                clauses.append("status=?")
                params.append(status)
            if tenant is not None:
                clauses.append("tenant=?")
                params.append(tenant)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            rows = conn.execute(
                f"SELECT * FROM executions {where} ORDER BY created_at DESC LIMIT ?", params
            ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def count_since(self, tool_name: str, tenant: str, since: float) -> int:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM executions WHERE tool_name=? AND tenant=? AND created_at>=?",
                (tool_name, tenant, since),
            ).fetchone()
            return row["n"]
        finally:
            conn.close()

    def sum_budget_since(self, tool_name: str, tenant: str, since: float) -> float:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(budget_value), 0) AS total FROM executions "
                "WHERE tool_name=? AND tenant=? AND created_at>=? AND budget_value IS NOT NULL",
                (tool_name, tenant, since),
            ).fetchone()
            return float(row["total"])
        finally:
            conn.close()

    def set_control(self, key: str, value: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO controls (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, time.time()),
            )
            conn.commit()
        finally:
            conn.close()

    def get_control(self, key: str):
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM controls WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def delete_control(self, key: str) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM controls WHERE key=?", (key,))
            conn.commit()
        finally:
            conn.close()

    def list_controls(self):
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key, value FROM controls").fetchall()
            return {row["key"]: row["value"] for row in rows}
        finally:
            conn.close()

    def list_semantic_candidates(self, tool_name: str, tenant: str, limit: int = 200) -> List[ExecutionRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM executions WHERE tool_name=? AND tenant=? AND status=? "
                "AND embedding_json IS NOT NULL AND (cache_expires_at IS NULL OR cache_expires_at > ?) "
                "ORDER BY created_at DESC LIMIT ?",
                (tool_name, tenant, SUCCEEDED, time.time(), limit),
            ).fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def clear(self) -> int:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM approvals")  # references executions, so it goes first
            removed = conn.execute("DELETE FROM executions").rowcount
            conn.commit()
            return removed
        finally:
            conn.close()
