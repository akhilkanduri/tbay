from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Dict, Optional

from .backends.base import AcquireResult, APPROVAL_APPROVED, FAILED, StorageBackend, SUCCEEDED
from .context import current_reasoning
from .embedders import cosine_similarity
from .exceptions import (
    ApprovalRejected,
    ApprovalTimeout,
    ConcurrencyLimitExceeded,
    ExecutionFailed,
    ExecutionTimeout,
    RateLimitExceeded,
)
from .policy import Policy, load_policies


def _bind_args(fn: Callable, args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Normalize positional and keyword args together, so foo(1, 2) and
    foo(a=1, b=2) produce the same idempotency key instead of two different ones."""
    try:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return {"args": list(args), "kwargs": kwargs}


def _default_key(tool_name: str, normalized_args: Dict[str, Any], tenant: str) -> str:
    payload = json.dumps({"tool": tool_name, "args": normalized_args, "tenant": tenant}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _hash_args(normalized_args: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(normalized_args, sort_keys=True, default=str).encode()).hexdigest()


def _redacted_args_json(normalized_args: Dict[str, Any], redact_fields) -> str:
    """What gets written to the audit log for this call's arguments. Anyone
    approving a WAITING_APPROVAL execution reads this, so redact anything
    sensitive (card numbers, tokens, PII) through the policy's redact_args list."""
    if not redact_fields:
        return json.dumps(normalized_args, sort_keys=True, default=str)
    masked = dict(normalized_args)
    for field_name in redact_fields:
        if field_name in masked:
            masked[field_name] = "***REDACTED***"
    return json.dumps(masked, sort_keys=True, default=str)


def _make_backend(db_url: str) -> StorageBackend:
    if db_url.startswith("sqlite://") or db_url.startswith("/") or db_url.startswith("~"):
        from .backends.sqlite_backend import SQLiteBackend

        return SQLiteBackend(db_url)
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from .backends.postgres_backend import PostgresBackend

        return PostgresBackend(db_url)
    if db_url.startswith("redis://") or db_url.startswith("rediss://") or db_url.startswith("unix://"):
        from .backends.redis_backend import RedisBackend

        return RedisBackend(db_url)
    raise ValueError(
        f"unsupported db_url {db_url!r}; use 'sqlite:///path/to/db.sqlite', "
        f"'postgresql://user:pass@host/db', or 'redis://host:6379/0'"
    )


class TbayClient:
    """The embeddable client: no daemon, no network hop. State lives in
    whatever database you point it at, so multiple processes or hosts still
    dedupe against each other through the database's own atomicity guarantees."""

    def __init__(
        self,
        db_url: str = "sqlite:///~/.tbay/db.sqlite",
        policy_file: Optional[str] = None,
        poll_interval: float = 0.25,
        embedder=None,
    ):
        self.backend = _make_backend(db_url)
        self.backend.init_schema()
        self.policies: Dict[str, Policy] = load_policies(policy_file)
        self.poll_interval = poll_interval
        # Used only by policies with semantic_cache: true. Anything with an
        # embed(text) -> list[float] method works; when left as None, the
        # zero-dependency HashingEmbedder is created on first use.
        self._embedder = embedder

    def get_policy(self, name: str) -> Policy:
        if name not in self.policies:
            raise KeyError(f"unknown policy {name!r}; known policies: {sorted(self.policies)}")
        return self.policies[name]

    # -- small helpers shared by the sync and async code paths --

    def _resolve(self, record):
        if record.status == FAILED:
            raise ExecutionFailed(record.error)
        return json.loads(record.result_json)

    def _get_embedder(self):
        if self._embedder is None:
            from .embedders import HashingEmbedder

            self._embedder = HashingEmbedder()
        return self._embedder

    def _semantic_lookup(self, pol: Policy, name: str, tenant: str, normalized: Dict[str, Any]):
        """Embed this call's args, then look for an already-answered call
        whose args are close enough (cosine >= semantic_threshold). Returns
        (embedding, best_match_or_None); the embedding is stored with the new
        execution on a miss so future similar calls can find it."""
        text = json.dumps(normalized, sort_keys=True, default=str)
        embedding = self._get_embedder().embed(text)
        best, best_score = None, 0.0
        for candidate in self.backend.list_semantic_candidates(name, tenant):
            try:
                vector = json.loads(candidate.embedding_json)
            except (TypeError, ValueError):
                continue
            score = cosine_similarity(embedding, vector)
            if score >= pol.semantic_threshold and score > best_score:
                best, best_score = candidate, score
        return embedding, best

    def _needs_approval(self, pol: Policy, normalized: Dict[str, Any]) -> bool:
        """Whether *this specific call* needs a human, taking approval_bypass_arg
        into account. A destructive policy with approval_bypass_arg="amount" and
        approval_bypass_max=50 lets small refunds through automatically while
        still stopping large ones for a human to look at."""
        if not pol.approval_required:
            return False
        if pol.approval_bypass_arg is None:
            return True
        value = normalized.get(pol.approval_bypass_arg)
        if value is None:
            return True  # can't check the bypass condition, so stay on the safe side
        try:
            return not (float(value) <= pol.approval_bypass_max)
        except (TypeError, ValueError):
            return True

    def _fire_webhook(self, url: str, execution_id: str, tool_name: str) -> None:
        # Best effort only. If this fails (network blip, bad URL, whatever),
        # the execution still sits in WAITING_APPROVAL and `tbay approve`
        # still works. The webhook is just a convenience notification, not
        # something approval depends on to function.
        try:
            import urllib.request

            data = json.dumps({"execution_id": execution_id, "tool_name": tool_name}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    def _enforce_rate_limit(self, pol: Policy, name: str, tenant: str) -> None:
        if not pol.rate_limit_max_calls:
            return
        # By the time this runs, acquire_or_get() has already inserted this
        # call's own row, so count_since() includes it. ">" (not ">=") is
        # what makes "rate_limit_max_calls: 2" actually allow 2 calls.
        since = time.time() - pol.rate_limit_window
        count = self.backend.count_since(name, tenant, since)
        if count > pol.rate_limit_max_calls:
            raise RateLimitExceeded(
                f"{name!r} has already been called {count} times in the last "
                f"{pol.rate_limit_window:.0f}s (limit is {pol.rate_limit_max_calls})"
            )

    def _guard_rate_limit(self, record, pol: Policy, name: str, tenant: str) -> None:
        try:
            self._enforce_rate_limit(pol, name, tenant)
        except Exception as exc:
            self.backend.fail(record.id, str(exc))
            raise

    async def _guard_rate_limit_async(self, record, pol: Policy, name: str, tenant: str) -> None:
        try:
            await asyncio.to_thread(self._enforce_rate_limit, pol, name, tenant)
        except Exception as exc:
            await asyncio.to_thread(self.backend.fail, record.id, str(exc))
            raise

    def _acquire_gated(self, pol: Policy, tool_name: str, idempotency_key: str, tenant: str,
                        policy_name: str, args_hash: str, args_json: str,
                        embedding_json: Optional[str] = None, reasoning: Optional[str] = None) -> AcquireResult:
        """Call acquire_or_get(), retrying while max_concurrent holds it at
        "wait_for_slot" (see StorageBackend.acquire_or_get: the RUNNING count
        and the insert happen in one atomic step there, so this loop can't
        let two callers both slip through the cap the way checking the count
        separately, beforehand, would)."""
        deadline = time.time() + pol.concurrency_wait_timeout
        while True:
            acq = self.backend.acquire_or_get(
                execution_id=str(uuid.uuid4()),
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                tenant=tenant,
                policy_name=policy_name,
                args_hash=args_hash,
                args_json=args_json,
                max_retries=pol.max_retries,
                retry_backoff=pol.retry_backoff,
                max_concurrent=pol.max_concurrent,
                embedding_json=embedding_json,
                reasoning=reasoning,
            )
            if not acq.wait_for_slot:
                return acq
            if time.time() > deadline:
                raise ConcurrencyLimitExceeded(
                    f"{tool_name!r} stayed at its concurrency limit ({pol.max_concurrent}) for over "
                    f"{pol.concurrency_wait_timeout:.0f}s"
                )
            time.sleep(self.poll_interval)

    async def _acquire_gated_async(self, pol: Policy, tool_name: str, idempotency_key: str, tenant: str,
                                    policy_name: str, args_hash: str, args_json: str,
                                    embedding_json: Optional[str] = None,
                                    reasoning: Optional[str] = None) -> AcquireResult:
        deadline = time.time() + pol.concurrency_wait_timeout
        while True:
            acq = await asyncio.to_thread(
                self.backend.acquire_or_get,
                execution_id=str(uuid.uuid4()),
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                tenant=tenant,
                policy_name=policy_name,
                args_hash=args_hash,
                args_json=args_json,
                max_retries=pol.max_retries,
                retry_backoff=pol.retry_backoff,
                max_concurrent=pol.max_concurrent,
                embedding_json=embedding_json,
                reasoning=reasoning,
            )
            if not acq.wait_for_slot:
                return acq
            if time.time() > deadline:
                raise ConcurrencyLimitExceeded(
                    f"{tool_name!r} stayed at its concurrency limit ({pol.max_concurrent}) for over "
                    f"{pol.concurrency_wait_timeout:.0f}s"
                )
            await asyncio.sleep(self.poll_interval)

    def _call_with_timeout(self, fn: Callable, args: tuple, kwargs: dict, timeout: float) -> Any:
        # Best effort: Python can't force-kill a thread, so a genuinely hung
        # call keeps running in the background even after we give up on it
        # and mark the execution FAILED. This catches ordinary hangs (a
        # stuck HTTP request); it does not guarantee the side effect never
        # happens after the timeout fires.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(fn, *args, **kwargs)
            try:
                return future.result(timeout=timeout)
            except FutureTimeoutError:
                raise ExecutionTimeout(f"{fn.__name__} did not finish within {timeout:.0f}s") from None

    # -- sync path --

    def run(
        self,
        fn: Callable,
        *,
        policy: str,
        args: tuple,
        kwargs: dict,
        tenant: str = "",
        key_fn: Optional[Callable] = None,
        tool_name: Optional[str] = None,
    ) -> Any:
        pol = self.get_policy(policy)
        name = tool_name or fn.__name__
        normalized = _bind_args(fn, args, kwargs)
        args_hash = _hash_args(normalized)
        args_json = _redacted_args_json(normalized, pol.redact_args)
        reasoning = current_reasoning()

        if not pol.idempotent:
            return self._run_volatile(fn, pol, args, kwargs, tenant, name, args_hash, args_json, normalized,
                                      reasoning=reasoning)

        embedding_json = None
        if pol.semantic_cache:
            embedding, hit = self._semantic_lookup(pol, name, tenant, normalized)
            if hit is not None:
                return json.loads(hit.result_json)
            embedding_json = json.dumps(embedding)

        idem_key = key_fn(*args, **kwargs) if key_fn else _default_key(name, normalized, tenant)
        acq = self._acquire_gated(pol, name, idem_key, tenant, policy, args_hash, args_json,
                                  embedding_json=embedding_json, reasoning=reasoning)

        if acq.use_cached:
            return json.loads(acq.record.result_json)
        if acq.raise_stored_error:
            raise ExecutionFailed(acq.record.error)
        if acq.follow_approval:
            status = self.backend.wait_for_approval(acq.record.id, pol.approval_timeout, self.poll_interval)
            if status != APPROVAL_APPROVED:
                raise ApprovalRejected(f"execution {acq.record.id} was rejected")
            final = self.backend.wait_for_result(acq.record.id, pol.approval_timeout, self.poll_interval)
            return self._resolve(final)
        if acq.follow_running:
            final = self.backend.wait_for_result(acq.record.id, 3600.0, self.poll_interval)
            return self._resolve(final)

        # We own the lease: this is a genuine new execution, so the rate
        # limit applies now. (Its own row already exists at this point, so
        # a rejection here has to mark it FAILED, or it would sit RUNNING
        # forever and hang any later caller with the same idempotency key.)
        self._guard_rate_limit(acq.record, pol, name, tenant)
        return self._execute_as_owner(acq.record, fn, args, kwargs, pol, name, normalized)

    def _execute_as_owner(
        self, record, fn: Callable, args: tuple, kwargs: dict, pol: Policy, name: str, normalized: Dict[str, Any]
    ) -> Any:
        if self._needs_approval(pol, normalized):
            self.backend.mark_waiting_approval(record.id)
            if pol.approval_webhook:
                self._fire_webhook(pol.approval_webhook, record.id, name)
            status = self.backend.wait_for_approval(record.id, pol.approval_timeout, self.poll_interval)
            if status != APPROVAL_APPROVED:
                self.backend.fail(record.id, "rejected by approval")
                raise ApprovalRejected(f"execution {record.id} was rejected")

        try:
            if pol.execution_timeout:
                result = self._call_with_timeout(fn, args, kwargs, pol.execution_timeout)
            else:
                result = fn(*args, **kwargs)
            self.backend.complete(record.id, json.dumps(result, default=str), pol.cache_ttl)
            return result
        except Exception as exc:
            self.backend.fail(record.id, str(exc))
            raise

    def _run_volatile(
        self,
        fn: Callable,
        pol: Policy,
        args: tuple,
        kwargs: dict,
        tenant: str,
        name: str,
        args_hash: str,
        args_json: str,
        normalized: Dict[str, Any],
        reasoning: Optional[str] = None,
    ) -> Any:
        """Path for idempotent=False policies: an LLM call used to decide
        something, "roll a die", "get the current time". Every call gets its
        own execution, always runs for real, and is never cached or deduped
        against another call's arguments. If max_retries is set, a failure
        just tries again from scratch rather than replaying a stored error,
        since there's no shared identity across attempts to replay from."""
        attempt = 0
        while True:
            execution_id = str(uuid.uuid4())
            acq = self._acquire_gated(pol, name, execution_id, tenant, pol.name, args_hash, args_json,
                                      reasoning=reasoning)
            self._guard_rate_limit(acq.record, pol, name, tenant)
            try:
                return self._execute_as_owner(acq.record, fn, args, kwargs, pol, name, normalized)
            except Exception:
                if attempt >= pol.max_retries:
                    raise
                attempt += 1
                if pol.retry_backoff:
                    time.sleep(pol.retry_backoff)

    # -- async path (mirrors run(), with every blocking call offloaded or awaited) --

    async def run_async(
        self,
        fn: Callable,
        *,
        policy: str,
        args: tuple,
        kwargs: dict,
        tenant: str = "",
        key_fn: Optional[Callable] = None,
        tool_name: Optional[str] = None,
    ) -> Any:
        pol = self.get_policy(policy)
        name = tool_name or fn.__name__
        normalized = _bind_args(fn, args, kwargs)
        args_hash = _hash_args(normalized)
        args_json = _redacted_args_json(normalized, pol.redact_args)
        reasoning = current_reasoning()

        if not pol.idempotent:
            return await self._run_volatile_async(
                fn, pol, args, kwargs, tenant, name, args_hash, args_json, normalized, reasoning=reasoning
            )

        embedding_json = None
        if pol.semantic_cache:
            embedding, hit = await asyncio.to_thread(self._semantic_lookup, pol, name, tenant, normalized)
            if hit is not None:
                return json.loads(hit.result_json)
            embedding_json = json.dumps(embedding)

        idem_key = key_fn(*args, **kwargs) if key_fn else _default_key(name, normalized, tenant)
        acq = await self._acquire_gated_async(pol, name, idem_key, tenant, policy, args_hash, args_json,
                                              embedding_json=embedding_json, reasoning=reasoning)

        if acq.use_cached:
            return json.loads(acq.record.result_json)
        if acq.raise_stored_error:
            raise ExecutionFailed(acq.record.error)
        if acq.follow_approval:
            status = await self._await_approval(acq.record.id, pol.approval_timeout)
            if status != APPROVAL_APPROVED:
                raise ApprovalRejected(f"execution {acq.record.id} was rejected")
            final = await self._await_result(acq.record.id, pol.approval_timeout)
            return self._resolve(final)
        if acq.follow_running:
            final = await self._await_result(acq.record.id, 3600.0)
            return self._resolve(final)

        await self._guard_rate_limit_async(acq.record, pol, name, tenant)
        return await self._execute_as_owner_async(acq.record, fn, args, kwargs, pol, name, normalized)

    async def _execute_as_owner_async(
        self, record, fn, args, kwargs, pol: Policy, name: str, normalized: Dict[str, Any]
    ) -> Any:
        if self._needs_approval(pol, normalized):
            await asyncio.to_thread(self.backend.mark_waiting_approval, record.id)
            if pol.approval_webhook:
                await asyncio.to_thread(self._fire_webhook, pol.approval_webhook, record.id, name)
            status = await self._await_approval(record.id, pol.approval_timeout)
            if status != APPROVAL_APPROVED:
                await asyncio.to_thread(self.backend.fail, record.id, "rejected by approval")
                raise ApprovalRejected(f"execution {record.id} was rejected")

        try:
            if pol.execution_timeout:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=pol.execution_timeout)
            else:
                result = await fn(*args, **kwargs)
            await asyncio.to_thread(
                self.backend.complete, record.id, json.dumps(result, default=str), pol.cache_ttl
            )
            return result
        except asyncio.TimeoutError:
            message = f"{fn.__name__} did not finish within {pol.execution_timeout:.0f}s"
            await asyncio.to_thread(self.backend.fail, record.id, message)
            raise ExecutionTimeout(message) from None
        except Exception as exc:
            await asyncio.to_thread(self.backend.fail, record.id, str(exc))
            raise

    async def _run_volatile_async(
        self,
        fn,
        pol: Policy,
        args: tuple,
        kwargs: dict,
        tenant: str,
        name: str,
        args_hash: str,
        args_json: str,
        normalized: Dict[str, Any],
        reasoning: Optional[str] = None,
    ) -> Any:
        attempt = 0
        while True:
            execution_id = str(uuid.uuid4())
            acq = await self._acquire_gated_async(pol, name, execution_id, tenant, pol.name, args_hash,
                                                  args_json, reasoning=reasoning)
            await self._guard_rate_limit_async(acq.record, pol, name, tenant)
            try:
                return await self._execute_as_owner_async(acq.record, fn, args, kwargs, pol, name, normalized)
            except Exception:
                if attempt >= pol.max_retries:
                    raise
                attempt += 1
                if pol.retry_backoff:
                    await asyncio.sleep(pol.retry_backoff)

    async def _await_approval(self, execution_id: str, timeout: float) -> str:
        deadline = time.time() + timeout
        while True:
            status = await asyncio.to_thread(self.backend.get_approval_status, execution_id)
            if status and status != "pending":
                return status
            if time.time() > deadline:
                raise ApprovalTimeout(f"nobody approved or rejected execution {execution_id} in time")
            await asyncio.sleep(self.poll_interval)

    async def _await_result(self, execution_id: str, timeout: float):
        deadline = time.time() + timeout
        while True:
            record = await asyncio.to_thread(self.backend.get, execution_id)
            if record and record.status in (SUCCEEDED, FAILED):
                return record
            if time.time() > deadline:
                raise ExecutionTimeout(f"timed out waiting for execution {execution_id} to finish")
            await asyncio.sleep(self.poll_interval)
