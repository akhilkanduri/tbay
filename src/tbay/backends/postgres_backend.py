from __future__ import annotations

import time
from typing import List, Optional

from .base import (
    AcquireResult,
    ExecutionRecord,
    FAILED,
    RUNNING,
    StorageBackend,
    SUCCEEDED,
    WAITING_APPROVAL,
    APPROVAL_APPROVED,
    APPROVAL_PENDING,
    APPROVAL_REJECTED,
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
    cache_expires_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL,
    finished_at DOUBLE PRECISION,
    embedding_json TEXT,
    reasoning TEXT,
    agent_id TEXT,
    CONSTRAINT uq_tbay_execution UNIQUE (tool_name, idempotency_key, tenant)
);
CREATE TABLE IF NOT EXISTS approvals (
    execution_id TEXT PRIMARY KEY REFERENCES executions(id),
    status TEXT NOT NULL,
    requested_at DOUBLE PRECISION NOT NULL,
    resolved_at DOUBLE PRECISION,
    resolver TEXT,
    signature TEXT
);
-- Databases created by older tbay versions predate these columns; add them
-- in place (after both CREATEs) so an upgrade never requires dropping the
-- tables and a fresh database never references a missing one.
ALTER TABLE executions ADD COLUMN IF NOT EXISTS embedding_json TEXT;
ALTER TABLE executions ADD COLUMN IF NOT EXISTS reasoning TEXT;
ALTER TABLE executions ADD COLUMN IF NOT EXISTS agent_id TEXT;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS signature TEXT;
"""


def _import_psycopg2():
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise ImportError(
            "the postgres backend needs psycopg2-binary; install it with `pip install tbay[postgres]`"
        ) from exc
    return psycopg2, psycopg2.extras


class PostgresBackend(StorageBackend):
    """Storage backend for shared/production use: point every process at the
    same Postgres database and they'll all dedupe against each other, the
    same way they would with SQLite on one machine."""

    def __init__(self, dsn: str):
        psycopg2, _ = _import_psycopg2()
        self._psycopg2 = psycopg2
        self._dsn = dsn

    def _connect(self):
        _, extras = _import_psycopg2()
        conn = self._psycopg2.connect(self._dsn)
        conn.cursor_factory = extras.RealDictCursor
        return conn

    def init_schema(self) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row) -> ExecutionRecord:
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
        )

    def _fetch_by_key(self, cur, tool_name, idempotency_key, tenant):
        cur.execute(
            "SELECT * FROM executions WHERE tool_name=%s AND idempotency_key=%s AND tenant=%s",
            (tool_name, idempotency_key, tenant),
        )
        return cur.fetchone()

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
    ) -> AcquireResult:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if max_concurrent:
                    # An advisory lock scoped to this (tool_name, tenant) and
                    # this transaction: it serializes concurrent acquire_or_get
                    # calls for the same tool just long enough to make the
                    # RUNNING count below and the INSERT after it atomic,
                    # without locking the whole executions table. Released
                    # automatically when the transaction commits or rolls back.
                    cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{tool_name}:{tenant}",))
                    cur.execute(
                        "SELECT COUNT(*) AS n FROM executions WHERE tool_name=%s AND tenant=%s AND status=%s",
                        (tool_name, tenant, RUNNING),
                    )
                    if cur.fetchone()["n"] >= max_concurrent:
                        conn.rollback()
                        return AcquireResult(record=None, wait_for_slot=True)

                # Same idea as the SQLite backend: try to insert first and
                # become the owner. If someone already has this key, ON
                # CONFLICT DO NOTHING makes this a no-op and we look at what
                # they're doing instead.
                cur.execute(
                    """
                    INSERT INTO executions
                        (id, tool_name, idempotency_key, tenant, status, args_hash, args_json, policy_name,
                         created_at, embedding_json, reasoning, agent_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tool_name, idempotency_key, tenant) DO NOTHING
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
                    ),
                )
                if cur.rowcount == 1:
                    conn.commit()
                    row = self._fetch_by_key(cur, tool_name, idempotency_key, tenant)
                    return AcquireResult(record=self._row_to_record(row), owner=True)

                row = self._fetch_by_key(cur, tool_name, idempotency_key, tenant)
                record = self._row_to_record(row)

                if record.status == SUCCEEDED:
                    if record.cache_expires_at is None or record.cache_expires_at > time.time():
                        conn.commit()
                        return AcquireResult(record=record, use_cached=True)
                    # Stale cache entry: try to reclaim it and become the new
                    # owner. If another caller reclaims it first, this UPDATE
                    # just matches zero rows.
                    cur.execute(
                        "UPDATE executions SET status=%s, result_json=NULL, error=NULL, created_at=%s, "
                        "finished_at=NULL, cache_expires_at=NULL WHERE id=%s AND status=%s",
                        (RUNNING, time.time(), record.id, SUCCEEDED),
                    )
                    if cur.rowcount == 1:
                        conn.commit()
                        record.status = RUNNING
                        return AcquireResult(record=record, owner=True)
                    conn.rollback()
                    return AcquireResult(record=record, follow_running=True)

                if record.status == FAILED:
                    backoff_elapsed = time.time() >= (record.finished_at or 0.0) + retry_backoff
                    if record.retry_count < max_retries and backoff_elapsed:
                        cur.execute(
                            "UPDATE executions SET status=%s, result_json=NULL, error=NULL, created_at=%s, "
                            "finished_at=NULL, retry_count=retry_count+1 WHERE id=%s AND status=%s",
                            (RUNNING, time.time(), record.id, FAILED),
                        )
                        if cur.rowcount == 1:
                            conn.commit()
                            record.status = RUNNING
                            return AcquireResult(record=record, owner=True)
                        conn.rollback()
                        return AcquireResult(record=record, follow_running=True)
                    # Out of retries (or still within the backoff window):
                    # hand back the failure that's already on record.
                    conn.commit()
                    return AcquireResult(record=record, raise_stored_error=True)

                if record.status == WAITING_APPROVAL:
                    conn.commit()
                    return AcquireResult(record=record, follow_approval=True)

                # RUNNING: someone else holds the lease right now.
                conn.commit()
                return AcquireResult(record=record, follow_running=True)
        finally:
            conn.close()

    def mark_waiting_approval(self, execution_id: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE executions SET status=%s WHERE id=%s", (WAITING_APPROVAL, execution_id))
                cur.execute(
                    """
                    INSERT INTO approvals (execution_id, status, requested_at) VALUES (%s, %s, %s)
                    ON CONFLICT (execution_id) DO UPDATE SET status=excluded.status,
                        requested_at=excluded.requested_at, resolved_at=NULL, resolver=NULL
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
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE executions SET status=%s, result_json=%s, finished_at=%s, cache_expires_at=%s "
                    "WHERE id=%s",
                    (SUCCEEDED, result_json, time.time(), expires_at, execution_id),
                )
            conn.commit()
        finally:
            conn.close()

    def fail(self, execution_id: str, error: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE executions SET status=%s, error=%s, finished_at=%s WHERE id=%s",
                    (FAILED, error, time.time(), execution_id),
                )
            conn.commit()
        finally:
            conn.close()

    def get(self, execution_id: str) -> Optional[ExecutionRecord]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM executions WHERE id=%s", (execution_id,))
                row = cur.fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def get_approval_status(self, execution_id: str) -> Optional[str]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT status FROM approvals WHERE execution_id=%s", (execution_id,))
                row = cur.fetchone()
            return row["status"] if row else None
        finally:
            conn.close()

    def resolve_approval(self, execution_id: str, approved: bool, resolver: str = "", signature=None) -> None:
        conn = self._connect()
        try:
            status = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE approvals SET status=%s, resolved_at=%s, resolver=%s, signature=%s "
                    "WHERE execution_id=%s",
                    (status, time.time(), resolver, signature, execution_id),
                )
            conn.commit()
        finally:
            conn.close()

    def get_approval(self, execution_id: str):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM approvals WHERE execution_id=%s", (execution_id,))
                row = cur.fetchone()
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
                clauses.append("tool_name=%s")
                params.append(tool_name)
            if status:
                clauses.append("status=%s")
                params.append(status)
            if tenant is not None:
                clauses.append("tenant=%s")
                params.append(tenant)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.append(limit)
            with conn.cursor() as cur:
                cur.execute(f"SELECT * FROM executions {where} ORDER BY created_at DESC LIMIT %s", params)
                rows = cur.fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def count_since(self, tool_name: str, tenant: str, since: float) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS n FROM executions WHERE tool_name=%s AND tenant=%s AND created_at>=%s",
                    (tool_name, tenant, since),
                )
                return cur.fetchone()["n"]
        finally:
            conn.close()

    def list_semantic_candidates(self, tool_name: str, tenant: str, limit: int = 200) -> List[ExecutionRecord]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM executions WHERE tool_name=%s AND tenant=%s AND status=%s "
                    "AND embedding_json IS NOT NULL AND (cache_expires_at IS NULL OR cache_expires_at > %s) "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tool_name, tenant, SUCCEEDED, time.time(), limit),
                )
                rows = cur.fetchall()
            return [self._row_to_record(r) for r in rows]
        finally:
            conn.close()

    def clear(self) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM approvals")  # references executions, so it goes first
                cur.execute("DELETE FROM executions")
                removed = cur.rowcount
            conn.commit()
            return removed
        finally:
            conn.close()
