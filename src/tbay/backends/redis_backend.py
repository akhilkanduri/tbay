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


def _import_redis():
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - exercised only without the extra installed
        raise ImportError(
            "the redis backend needs the `redis` package; install it with `pip install tbay[redis]`"
        ) from exc
    return redis


# Everything acquire_or_get needs to decide atomically, in one server-side
# script (Redis runs Lua scripts without interleaving other commands, which
# gives us the same guarantee BEGIN IMMEDIATE gives the SQLite backend):
#
#   KEYS[1]  key mapping   (tool, idempotency_key, tenant) -> execution_id
#   KEYS[2]  running set   execution_ids currently RUNNING for this tool/tenant
#   KEYS[3]  calls zset    created_at per execution, backs count_since()
#   KEYS[4]  log zset      every execution ever, newest first, backs list_executions()
#   KEYS[5]  record hash   the ExecutionRecord fields for the new execution
#
#   ARGV[1]  max_concurrent (0 disables the check)
#   ARGV[2]  execution_id
#   ARGV[3]  created_at
#   ARGV[4+] field/value pairs for the record hash
#
# Returns {'EXISTS', id} if someone already holds the key, {'WAIT'} if
# max_concurrent is full, or {'OWNER'} if we claimed it and wrote the record.
_ACQUIRE_LUA = """
local existing = redis.call('GET', KEYS[1])
if existing then
    return {'EXISTS', existing}
end
local maxc = tonumber(ARGV[1])
if maxc > 0 and redis.call('SCARD', KEYS[2]) >= maxc then
    return {'WAIT'}
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[2])
redis.call('ZADD', KEYS[3], tonumber(ARGV[3]), ARGV[2])
redis.call('ZADD', KEYS[4], tonumber(ARGV[3]), ARGV[2])
for i = 4, #ARGV, 2 do
    redis.call('HSET', KEYS[5], ARGV[i], ARGV[i + 1])
end
return {'OWNER'}
"""

# Compare-and-swap a finished record back to RUNNING, the same way the SQL
# backends reclaim a stale cache entry or a retryable failure with
# "UPDATE ... WHERE id=? AND status=?". Returns 1 if we won the race.
#
#   KEYS[1] record hash    KEYS[2] running set    KEYS[3] calls zset
#   ARGV[1] expected current status
#   ARGV[2] new created_at
#   ARGV[3] '1' to increment retry_count (retry path), '0' to leave it
#   ARGV[4] execution_id
_RECLAIM_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then
    return 0
end
redis.call('HSET', KEYS[1], 'status', 'RUNNING', 'created_at', ARGV[2])
redis.call('HDEL', KEYS[1], 'result_json', 'error', 'finished_at', 'cache_expires_at')
if ARGV[3] == '1' then
    redis.call('HINCRBY', KEYS[1], 'retry_count', 1)
end
redis.call('SADD', KEYS[2], ARGV[4])
redis.call('ZADD', KEYS[3], tonumber(ARGV[2]), ARGV[4])
return 1
"""

_FLOAT_FIELDS = ("cache_expires_at", "created_at", "finished_at")


class RedisBackend(StorageBackend):
    """Storage backend for shared, low-latency use: point every process at
    the same Redis and they'll all dedupe against each other, the same way
    they would through Postgres. Atomicity comes from Lua scripts (Redis
    executes a script as one uninterruptible unit), so the idempotency-key
    claim and the max_concurrent check can't race across callers.

    Keys all live under the `tbay:` prefix. Records persist until you delete
    them, exactly like rows in the SQL backends, so the audit log survives
    restarts as long as Redis itself is persistent (AOF/RDB)."""

    def __init__(self, url: str, prefix: str = "tbay:"):
        redis = _import_redis()
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._p = prefix
        self._acquire_script = self._r.register_script(_ACQUIRE_LUA)
        self._reclaim_script = self._r.register_script(_RECLAIM_LUA)

    # -- key layout --

    def _key_map(self, tool_name, idempotency_key, tenant) -> str:
        return f"{self._p}key:{tool_name}:{tenant}:{idempotency_key}"

    def _running_key(self, tool_name, tenant) -> str:
        return f"{self._p}running:{tool_name}:{tenant}"

    def _calls_key(self, tool_name, tenant) -> str:
        return f"{self._p}calls:{tool_name}:{tenant}"

    def _log_key(self) -> str:
        return f"{self._p}log"

    def _exec_key(self, execution_id) -> str:
        return f"{self._p}exec:{execution_id}"

    def _approval_key(self, execution_id) -> str:
        return f"{self._p}approval:{execution_id}"

    # -- record (de)serialization: Redis hashes only store strings --

    @staticmethod
    def _record_to_fields(record: dict) -> list:
        fields = []
        for key, value in record.items():
            if value is None:
                continue
            fields.extend([key, str(value)])
        return fields

    def _hash_to_record(self, data: dict) -> Optional[ExecutionRecord]:
        if not data:
            return None
        return ExecutionRecord(
            id=data["id"],
            tool_name=data["tool_name"],
            idempotency_key=data["idempotency_key"],
            tenant=data.get("tenant", ""),
            status=data["status"],
            args_hash=data["args_hash"],
            args_json=data.get("args_json"),
            result_json=data.get("result_json"),
            error=data.get("error"),
            policy_name=data["policy_name"],
            retry_count=int(data.get("retry_count", 0)),
            cache_expires_at=float(data["cache_expires_at"]) if data.get("cache_expires_at") else None,
            created_at=float(data["created_at"]),
            finished_at=float(data["finished_at"]) if data.get("finished_at") else None,
            embedding_json=data.get("embedding_json"),
            reasoning=data.get("reasoning"),
            agent_id=data.get("agent_id"),
            agent_meta=data.get("agent_meta"),
        )

    def init_schema(self) -> None:
        # Nothing to create: Redis keys appear on first write. A ping here
        # surfaces a bad URL at client construction instead of first call.
        self._r.ping()

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
    ) -> AcquireResult:
        now = time.time()
        record_fields = self._record_to_fields(
            {
                "id": execution_id,
                "tool_name": tool_name,
                "idempotency_key": idempotency_key,
                "tenant": tenant,
                "status": RUNNING,
                "args_hash": args_hash,
                "args_json": args_json,
                "policy_name": policy_name,
                "retry_count": 0,
                "created_at": now,
                "embedding_json": embedding_json,
                "reasoning": reasoning,
                "agent_id": agent_id,
                "agent_meta": agent_meta,
            }
        )
        outcome = self._acquire_script(
            keys=[
                self._key_map(tool_name, idempotency_key, tenant),
                self._running_key(tool_name, tenant),
                self._calls_key(tool_name, tenant),
                self._log_key(),
                self._exec_key(execution_id),
            ],
            args=[max_concurrent or 0, execution_id, now] + record_fields,
        )

        if outcome[0] == "WAIT":
            return AcquireResult(record=None, wait_for_slot=True)
        if outcome[0] == "OWNER":
            return AcquireResult(record=self.get(execution_id), owner=True)

        # Someone else holds this key: same decision tree as the SQL backends,
        # with the reclaim script standing in for "UPDATE ... WHERE status=?".
        record = self.get(outcome[1])
        if record is None:
            # The mapping exists but the record hash isn't visible yet or was
            # deleted out from under us; treat it as in flight and follow.
            return AcquireResult(record=None, wait_for_slot=True)

        if record.status == SUCCEEDED:
            if record.cache_expires_at is None or record.cache_expires_at > time.time():
                return AcquireResult(record=record, use_cached=True)
            if self._reclaim(record, expected=SUCCEEDED, bump_retry=False):
                record.status = RUNNING
                return AcquireResult(record=record, owner=True)
            return AcquireResult(record=record, follow_running=True)

        if record.status == FAILED:
            backoff_elapsed = time.time() >= (record.finished_at or 0.0) + retry_backoff
            if record.retry_count < max_retries and backoff_elapsed:
                if self._reclaim(record, expected=FAILED, bump_retry=True):
                    record.status = RUNNING
                    return AcquireResult(record=record, owner=True)
                return AcquireResult(record=record, follow_running=True)
            return AcquireResult(record=record, raise_stored_error=True)

        if record.status == WAITING_APPROVAL:
            return AcquireResult(record=record, follow_approval=True)

        return AcquireResult(record=record, follow_running=True)

    def _reclaim(self, record: ExecutionRecord, *, expected: str, bump_retry: bool) -> bool:
        won = self._reclaim_script(
            keys=[
                self._exec_key(record.id),
                self._running_key(record.tool_name, record.tenant),
                self._calls_key(record.tool_name, record.tenant),
            ],
            args=[expected, time.time(), "1" if bump_retry else "0", record.id],
        )
        return won == 1

    def _finish(self, execution_id: str, updates: dict) -> None:
        """Write terminal fields and drop the execution from its running set
        (looked up from the record itself, so callers only need the id)."""
        exec_key = self._exec_key(execution_id)
        data = self._r.hgetall(exec_key)
        pipe = self._r.pipeline()
        cleared = [k for k, v in updates.items() if v is None]
        kept = {k: str(v) for k, v in updates.items() if v is not None}
        if kept:
            pipe.hset(exec_key, mapping=kept)
        if cleared:
            pipe.hdel(exec_key, *cleared)
        if data:
            pipe.srem(self._running_key(data.get("tool_name", ""), data.get("tenant", "")), execution_id)
        pipe.execute()

    def mark_waiting_approval(self, execution_id: str) -> None:
        self._finish(execution_id, {"status": WAITING_APPROVAL})
        self._r.hset(
            self._approval_key(execution_id),
            mapping={"status": APPROVAL_PENDING, "requested_at": str(time.time())},
        )
        self._r.hdel(self._approval_key(execution_id), "resolved_at", "resolver")

    def complete(self, execution_id: str, result_json: str, cache_ttl_seconds: Optional[float]) -> None:
        expires_at = time.time() + cache_ttl_seconds if cache_ttl_seconds else None
        self._finish(
            execution_id,
            {
                "status": SUCCEEDED,
                "result_json": result_json,
                "finished_at": time.time(),
                "cache_expires_at": expires_at,
                "error": None,
            },
        )

    def fail(self, execution_id: str, error: str) -> None:
        self._finish(execution_id, {"status": FAILED, "error": error, "finished_at": time.time()})

    def get(self, execution_id: str) -> Optional[ExecutionRecord]:
        return self._hash_to_record(self._r.hgetall(self._exec_key(execution_id)))

    def get_approval_status(self, execution_id: str) -> Optional[str]:
        return self._r.hget(self._approval_key(execution_id), "status")

    def resolve_approval(self, execution_id: str, approved: bool, resolver: str = "", signature=None,
                         note=None) -> None:
        status = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
        mapping = {"status": status, "resolved_at": str(time.time()), "resolver": resolver}
        if signature:
            mapping["signature"] = signature
        if note:
            mapping["note"] = note
        self._r.hset(self._approval_key(execution_id), mapping=mapping)

    def get_approval(self, execution_id: str):
        data = self._r.hgetall(self._approval_key(execution_id))
        return data or None

    def list_executions(
        self, *, tool_name=None, status=None, tenant=None, limit=50
    ) -> List[ExecutionRecord]:
        results: List[ExecutionRecord] = []
        # Newest first from the log zset, filtered in Python. Scanning is
        # capped so one query can't pull an unbounded history into memory.
        for execution_id in self._r.zrevrange(self._log_key(), 0, max(limit * 20, 1000)):
            record = self.get(execution_id)
            if record is None:
                continue
            if tool_name and record.tool_name != tool_name:
                continue
            if status and record.status != status:
                continue
            if tenant is not None and record.tenant != tenant:
                continue
            results.append(record)
            if len(results) >= limit:
                break
        return results

    def count_since(self, tool_name: str, tenant: str, since: float) -> int:
        return self._r.zcount(self._calls_key(tool_name, tenant), since, "+inf")

    def list_semantic_candidates(self, tool_name: str, tenant: str, limit: int = 200) -> List[ExecutionRecord]:
        now = time.time()
        results: List[ExecutionRecord] = []
        for execution_id in self._r.zrevrange(self._calls_key(tool_name, tenant), 0, max(limit * 5, 500)):
            record = self.get(execution_id)
            if record is None or record.status != SUCCEEDED or not record.embedding_json:
                continue
            if record.cache_expires_at is not None and record.cache_expires_at <= now:
                continue
            results.append(record)
            if len(results) >= limit:
                break
        return results

    def clear(self) -> int:
        # Only tbay's own keys (everything under the prefix), never a blind
        # FLUSHDB: the Redis database may hold other applications' data.
        removed = self._r.zcard(self._log_key())
        batch = []
        for key in self._r.scan_iter(match=f"{self._p}*", count=500):
            batch.append(key)
            if len(batch) >= 500:
                self._r.delete(*batch)
                batch = []
        if batch:
            self._r.delete(*batch)
        return removed
