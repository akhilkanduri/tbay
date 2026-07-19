from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Callable, Dict, Iterable, Optional

from .backends.base import APPROVAL_APPROVED, FAILED, SUCCEEDED, AcquireResult, StorageBackend
from .context import current_agent, current_agent_meta, current_reasoning
from .embedders import cosine_similarity
from .events import (
    APPROVAL_APPROVED as EV_APPROVAL_APPROVED,
    APPROVAL_REJECTED as EV_APPROVAL_REJECTED,
    APPROVAL_REQUESTED as EV_APPROVAL_REQUESTED,
    BUDGET_EXCEEDED as EV_BUDGET_EXCEEDED,
    CACHE_HIT as EV_CACHE_HIT,
    CALL_FAILED as EV_CALL_FAILED,
    CALL_STARTED as EV_CALL_STARTED,
    CALL_SUCCEEDED as EV_CALL_SUCCEEDED,
    CONCURRENCY_BLOCKED as EV_CONCURRENCY_BLOCKED,
    KILL_SWITCH_BLOCKED as EV_KILL_SWITCH_BLOCKED,
    RATE_LIMITED as EV_RATE_LIMITED,
    SEMANTIC_CACHE_HIT as EV_SEMANTIC_CACHE_HIT,
    SINGLEFLIGHT_COALESCED as EV_SINGLEFLIGHT_COALESCED,
    Event,
    EventBus,
    Handler,
)
from .exceptions import (
    ApprovalRejected,
    ApprovalTimeout,
    BudgetExceeded,
    ConcurrencyLimitExceeded,
    ExecutionFailed,
    ExecutionTimeout,
    RateLimitExceeded,
    ToolPaused,
)
from .policy import Policy, load_policies
from .redaction import redact_structure
from .security import sign_webhook, verify_approval

logger = logging.getLogger("tbay")

# Control keys for the kill switch. "pause:*" stops every guarded call on
# this database; "pause:tool:<name>" stops just that tool.
PAUSE_ALL_KEY = "pause:*"


def pause_tool_key(tool_name: str) -> str:
    return f"pause:tool:{tool_name}"


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


def _redacted_args_json(normalized_args: Dict[str, Any], pol: Policy) -> str:
    """What gets written to the audit log for this call's arguments. Anyone
    approving a WAITING_APPROVAL execution reads this, so redact anything
    sensitive (card numbers, tokens, PII) through the policy's redact_args /
    redact_patterns / redact_auto settings (see src/tbay/redaction.py)."""
    if not (pol.redact_args or pol.redact_patterns or pol.redact_auto):
        return json.dumps(normalized_args, sort_keys=True, default=str)
    masked = redact_structure(
        normalized_args, fields=pol.redact_args, patterns=pol.redact_patterns, auto=pol.redact_auto
    )
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
        agent_id: Optional[str] = None,
        agent_meta: Optional[dict] = None,
        approval_secret: Optional[str] = None,
    ):
        self.backend = _make_backend(db_url)
        self.backend.init_schema()
        self.policies: Dict[str, Policy] = load_policies(policy_file)
        self.poll_interval = poll_interval
        # Used only by policies with semantic_cache: true. Anything with an
        # embed(text) -> list[float] method works; when left as None, the
        # zero-dependency HashingEmbedder is created on first use.
        self._embedder = embedder
        # Recorded on every execution this client starts, so the audit log
        # shows WHICH agent asked. A surrounding `with tbay.agent(...)` block
        # overrides this per call (see src/tbay/context.py).
        self.agent_id = agent_id or os.environ.get("TBAY_AGENT_ID")
        self.agent_meta = agent_meta
        # When set, an approval only counts if it carries a valid HMAC
        # signature made with this secret, so raw database credentials alone
        # can no longer approve anything (see src/tbay/security.py). The same
        # secret also signs outgoing approval webhooks (X-Tbay-Signature).
        self.approval_secret = approval_secret or os.environ.get("TBAY_APPROVAL_SECRET")
        # Structured lifecycle events (see src/tbay/events.py). Subscribe
        # with client.on(...); handlers can never break a guarded call.
        self.events = EventBus()

    def get_policy(self, name: str) -> Policy:
        if name not in self.policies:
            raise KeyError(f"unknown policy {name!r}; known policies: {sorted(self.policies)}")
        return self.policies[name]

    # -- observability: event subscription --

    def on(self, handler: Optional[Handler] = None, *, events: Optional[Iterable[str]] = None):
        """Subscribe a handler to this client's lifecycle events.

            @client.on
            def all_events(event): ...

            @client.on(events=[tbay.events.CALL_FAILED])
            def only_failures(event): ...

            client.on(my_handler)   # plain call works too

        Handlers run synchronously and their exceptions are logged and
        swallowed, never raised into the guarded call. Returns the handler
        (or a registering decorator), so `client.off(handler)` can remove it."""
        if handler is None:
            return lambda h: self.events.subscribe(h, events)
        return self.events.subscribe(handler, events)

    def off(self, handler: Handler) -> None:
        """Remove a previously subscribed event handler."""
        self.events.unsubscribe(handler)

    def _emit(self, type_: str, *, tool_name=None, execution_id=None, tenant="", policy=None, **data) -> None:
        if not self.events.has_subscribers:
            return
        self.events.emit(
            Event(
                type=type_,
                tool_name=tool_name,
                execution_id=execution_id,
                tenant=tenant,
                policy=policy,
                agent_id=current_agent() or self.agent_id,
                reasoning=current_reasoning(),
                data=data,
            )
        )

    # -- the kill switch --

    def pause(self, tool_name: Optional[str] = None, *, reason: str = "", by: str = "") -> None:
        """Stop every guarded call (or just `tool_name`) on this database,
        across every process and host that shares it, until resume() is
        called. Blocked calls raise ToolPaused immediately, before acquiring
        anything. This is the emergency brake for a misbehaving agent."""
        key = pause_tool_key(tool_name) if tool_name else PAUSE_ALL_KEY
        self.backend.set_control(key, json.dumps({"reason": reason, "by": by, "at": time.time()}))

    def resume(self, tool_name: Optional[str] = None) -> None:
        """Lift a pause() for `tool_name`, or the global pause when omitted."""
        key = pause_tool_key(tool_name) if tool_name else PAUSE_ALL_KEY
        self.backend.delete_control(key)

    def paused(self) -> Dict[str, dict]:
        """Active pauses, keyed by scope: "*" for the global pause, otherwise
        the tool name. Values are {"reason", "by", "at"} dicts."""
        out: Dict[str, dict] = {}
        for key, raw in self.backend.list_controls().items():
            if key == PAUSE_ALL_KEY:
                scope = "*"
            elif key.startswith("pause:tool:"):
                scope = key[len("pause:tool:"):]
            else:
                continue
            try:
                out[scope] = json.loads(raw)
            except (TypeError, ValueError):
                out[scope] = {"reason": raw}
        return out

    def _check_kill_switch(self, name: str, tenant: str, policy_name: str) -> None:
        for scope, key in (("*", PAUSE_ALL_KEY), (name, pause_tool_key(name))):
            raw = self.backend.get_control(key)
            if raw is None:
                continue
            try:
                info = json.loads(raw)
            except (TypeError, ValueError):
                info = {"reason": raw}
            reason = (info or {}).get("reason") or ""
            self._emit(
                EV_KILL_SWITCH_BLOCKED, tool_name=name, tenant=tenant, policy=policy_name,
                scope=scope, reason=reason,
            )
            where = "all tools" if scope == "*" else f"tool {name!r}"
            hint = "`tbay resume`" if scope == "*" else f"`tbay resume --tool {name}`"
            raise ToolPaused(
                f"{name!r} was not run: tbay is paused for {where}"
                + (f" ({reason})" if reason else "")
                + f"; {hint} lifts the pause"
            )

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

    def _budget_value(self, pol: Policy, name: str, normalized: Dict[str, Any]) -> Optional[float]:
        """The metered amount this call contributes to its policy's budget.
        A budgeted call whose metered argument is missing or non-numeric is
        refused outright: a call that can't be measured can't be safely
        counted against a spend cap."""
        if not pol.budget_arg:
            return None
        value = normalized.get(pol.budget_arg)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise BudgetExceeded(
                f"{name!r} was not run: its policy meters the {pol.budget_arg!r} argument against a "
                f"budget, but this call's value ({value!r}) is missing or not numeric, so it cannot "
                f"be counted"
            ) from None

    def _resolve_agent(self):
        """(agent_id, agent_meta_json) for the current call: a surrounding
        `with tbay.agent(...)` block wins over the client-level defaults."""
        context_id = current_agent()
        if context_id:
            meta = current_agent_meta()
        else:
            meta = self.agent_meta
        agent_id = context_id or self.agent_id
        meta_json = json.dumps(meta, sort_keys=True, default=str) if meta else None
        return agent_id, meta_json

    def _rejection_note(self, execution_id: str) -> str:
        """The human's stated reason for rejecting, if they gave one, so the
        caller's exception says WHY instead of a bare 'rejected'."""
        approval = self.backend.get_approval(execution_id) or {}
        note = approval.get("note")
        return f": {note}" if note else ""

    def _verify_approval_signature(self, execution_id: str) -> bool:
        """With an approval secret configured, an 'approved' row only counts
        if its signature verifies. Without one, any approved row counts
        (the pre-signing behavior)."""
        if not self.approval_secret:
            return True
        approval = self.backend.get_approval(execution_id) or {}
        return verify_approval(self.approval_secret, execution_id, True, approval.get("signature"))

    def _fire_webhook(
        self, url: str, execution_id: str, tool_name: str, *,
        tenant: str = "", policy: Optional[str] = None, args_json: Optional[str] = None,
        reasoning: Optional[str] = None, agent_id: Optional[str] = None,
    ) -> None:
        # Best effort only. If this fails (network blip, bad URL, whatever),
        # the execution still sits in WAITING_APPROVAL and `tbay approve`
        # still works. The webhook is just a convenience notification, not
        # something approval depends on to function.
        #
        # The payload carries everything an approval surface needs to render
        # a decision without a database round trip; args_json is the already-
        # redacted audit-log form, so nothing masked can leak here either.
        # With an approval secret configured, the body is HMAC-signed
        # (X-Tbay-Signature; verify with tbay.security.verify_webhook).
        try:
            scheme = urllib.parse.urlparse(url).scheme
            if scheme not in ("http", "https"):
                logger.warning("approval_webhook %r ignored: only http(s) URLs are allowed", url)
                return
            payload = {
                "event": "approval.requested",
                "execution_id": execution_id,
                "tool_name": tool_name,
                "tenant": tenant,
                "policy": policy,
                "args": args_json,
                "reasoning": reasoning,
                "agent_id": agent_id,
                "ts": time.time(),
            }
            data = json.dumps(payload, sort_keys=True).encode()
            headers = {"Content-Type": "application/json", "X-Tbay-Event": "approval.requested"}
            if self.approval_secret:
                headers["X-Tbay-Signature"] = sign_webhook(self.approval_secret, data)
            req = urllib.request.Request(url, data=data, headers=headers)
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            logger.warning("approval_webhook %r failed; approval still works via the CLI/dashboard", url)

    def _enforce_rate_limit(self, pol: Policy, name: str, tenant: str) -> None:
        if not pol.rate_limit_max_calls:
            return
        if pol.rate_limit_window is None:
            # Reachable only by setting the Policy in code (YAML validates
            # both keys together). Fail loud: a half-configured limit that
            # silently didn't limit would be a safety hole.
            raise ValueError(
                f"policy {pol.name!r} sets rate_limit_max_calls without rate_limit_window; "
                f"set both (in YAML: rate_limit: {{max_calls: N, per: '1m'}})"
            )
        # By the time this runs, acquire_or_get() has already inserted this
        # call's own row, so count_since() includes it. ">" (not ">=") is
        # what makes "rate_limit_max_calls: 2" actually allow 2 calls.
        since = time.time() - pol.rate_limit_window
        count = self.backend.count_since(name, tenant, since)
        if count > pol.rate_limit_max_calls:
            self._emit(EV_RATE_LIMITED, tool_name=name, tenant=tenant, policy=pol.name, count=count)
            raise RateLimitExceeded(
                f"{name!r} has already been called {count} times in the last "
                f"{pol.rate_limit_window:.0f}s (limit is {pol.rate_limit_max_calls})"
            )

    def _enforce_budget(self, pol: Policy, name: str, tenant: str) -> None:
        if pol.budget_max is None:
            return
        if pol.budget_window is None:
            # Reachable only by setting the Policy in code (YAML validates
            # arg/max/per together). Fail loud rather than mis-meter.
            raise ValueError(
                f"policy {pol.name!r} sets budget_max without budget_window; "
                f"set all of budget_arg/budget_max/budget_window (in YAML: "
                f"budget: {{arg: ..., max: ..., per: '1d'}})"
            )
        # Like the rate limit, this runs after acquire_or_get() inserted this
        # call's own row (with its budget_value), so the sum includes it and
        # ">" makes the cap inclusive: a call landing exactly on budget_max
        # still runs, the first one past it doesn't.
        since = time.time() - pol.budget_window
        spent = self.backend.sum_budget_since(name, tenant, since)
        if spent > pol.budget_max:
            self._emit(
                EV_BUDGET_EXCEEDED, tool_name=name, tenant=tenant, policy=pol.name,
                spent=spent, budget_max=pol.budget_max,
            )
            raise BudgetExceeded(
                f"{name!r} was not run: {pol.budget_arg!r} would total {spent:g} over the last "
                f"{(pol.budget_window or 0.0):.0f}s, past the budget of {pol.budget_max:g}"
            )

    def _enforce_limits(self, pol: Policy, name: str, tenant: str) -> None:
        self._enforce_rate_limit(pol, name, tenant)
        self._enforce_budget(pol, name, tenant)

    def _guard_limits(self, record, pol: Policy, name: str, tenant: str) -> None:
        try:
            self._enforce_limits(pol, name, tenant)
        except Exception as exc:
            self.backend.fail(record.id, str(exc))
            raise

    async def _guard_limits_async(self, record, pol: Policy, name: str, tenant: str) -> None:
        try:
            await asyncio.to_thread(self._enforce_limits, pol, name, tenant)
        except Exception as exc:
            await asyncio.to_thread(self.backend.fail, record.id, str(exc))
            raise

    def _acquire_gated(self, pol: Policy, tool_name: str, idempotency_key: str, tenant: str,
                        policy_name: str, args_hash: str, args_json: str,
                        embedding_json: Optional[str] = None, reasoning: Optional[str] = None,
                        agent_id: Optional[str] = None, agent_meta: Optional[str] = None,
                        budget_value: Optional[float] = None) -> AcquireResult:
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
                agent_id=agent_id,
                agent_meta=agent_meta,
                budget_value=budget_value,
                lease_timeout=pol.lease_timeout,
            )
            if not acq.wait_for_slot:
                return acq
            if time.time() > deadline:
                self._emit(EV_CONCURRENCY_BLOCKED, tool_name=tool_name, tenant=tenant, policy=policy_name)
                raise ConcurrencyLimitExceeded(
                    f"{tool_name!r} stayed at its concurrency limit ({pol.max_concurrent}) for over "
                    f"{pol.concurrency_wait_timeout:.0f}s"
                )
            time.sleep(self.poll_interval)

    async def _acquire_gated_async(self, pol: Policy, tool_name: str, idempotency_key: str, tenant: str,
                                    policy_name: str, args_hash: str, args_json: str,
                                    embedding_json: Optional[str] = None,
                                    reasoning: Optional[str] = None,
                                    agent_id: Optional[str] = None,
                                    agent_meta: Optional[str] = None,
                                    budget_value: Optional[float] = None) -> AcquireResult:
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
                agent_id=agent_id,
                agent_meta=agent_meta,
                budget_value=budget_value,
                lease_timeout=pol.lease_timeout,
            )
            if not acq.wait_for_slot:
                return acq
            if time.time() > deadline:
                self._emit(EV_CONCURRENCY_BLOCKED, tool_name=tool_name, tenant=tenant, policy=policy_name)
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
        #
        # shutdown(wait=False) matters here: a `with` block (or wait=True)
        # would join the worker thread, blocking exactly as long as the hung
        # call it was supposed to give up on.
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(fn, *args, **kwargs)
            try:
                return future.result(timeout=timeout)
            except FutureTimeoutError:
                raise ExecutionTimeout(f"{fn.__name__} did not finish within {timeout:.0f}s") from None
        finally:
            pool.shutdown(wait=False)

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
        self._check_kill_switch(name, tenant, policy)
        normalized = _bind_args(fn, args, kwargs)
        args_hash = _hash_args(normalized)
        args_json = _redacted_args_json(normalized, pol)
        budget_value = self._budget_value(pol, name, normalized)
        reasoning = current_reasoning()
        agent_id, agent_meta = self._resolve_agent()

        if not pol.idempotent:
            return self._run_volatile(fn, pol, args, kwargs, tenant, name, args_hash, args_json, normalized,
                                      reasoning=reasoning, agent_id=agent_id, agent_meta=agent_meta,
                                      budget_value=budget_value)

        embedding_json = None
        if pol.semantic_cache:
            embedding, hit = self._semantic_lookup(pol, name, tenant, normalized)
            if hit is not None:
                self._emit(EV_SEMANTIC_CACHE_HIT, tool_name=name, execution_id=hit.id, tenant=tenant,
                           policy=policy)
                return json.loads(hit.result_json)
            embedding_json = json.dumps(embedding)

        idem_key = key_fn(*args, **kwargs) if key_fn else _default_key(name, normalized, tenant)
        acq = self._acquire_gated(pol, name, idem_key, tenant, policy, args_hash, args_json,
                                  embedding_json=embedding_json, reasoning=reasoning, agent_id=agent_id,
                                  agent_meta=agent_meta, budget_value=budget_value)

        if acq.use_cached:
            self._emit(EV_CACHE_HIT, tool_name=name, execution_id=acq.record.id, tenant=tenant, policy=policy)
            return json.loads(acq.record.result_json)
        if acq.raise_stored_error:
            self._emit(EV_CALL_FAILED, tool_name=name, execution_id=acq.record.id, tenant=tenant,
                       policy=policy, error=acq.record.error, replayed=True)
            raise ExecutionFailed(acq.record.error)
        if acq.follow_approval:
            self._emit(EV_SINGLEFLIGHT_COALESCED, tool_name=name, execution_id=acq.record.id, tenant=tenant,
                       policy=policy)
            status = self.backend.wait_for_approval(acq.record.id, pol.approval_timeout, self.poll_interval)
            if status != APPROVAL_APPROVED:
                raise ApprovalRejected(f"execution {acq.record.id} was rejected")
            final = self.backend.wait_for_result(acq.record.id, pol.approval_timeout, self.poll_interval)
            return self._resolve(final)
        if acq.follow_running:
            self._emit(EV_SINGLEFLIGHT_COALESCED, tool_name=name, execution_id=acq.record.id, tenant=tenant,
                       policy=policy)
            final = self.backend.wait_for_result(acq.record.id, 3600.0, self.poll_interval)
            return self._resolve(final)

        # We own the lease: this is a genuine new execution, so the rate
        # limit and budget apply now. (Its own row already exists at this
        # point, so a rejection here has to mark it FAILED, or it would sit
        # RUNNING forever and hang any later caller with the same key.)
        self._guard_limits(acq.record, pol, name, tenant)
        return self._execute_as_owner(acq.record, fn, args, kwargs, pol, name, normalized)

    def _execute_as_owner(
        self, record, fn: Callable, args: tuple, kwargs: dict, pol: Policy, name: str, normalized: Dict[str, Any]
    ) -> Any:
        self._emit(EV_CALL_STARTED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                   policy=pol.name)
        if self._needs_approval(pol, normalized):
            self.backend.mark_waiting_approval(record.id)
            self._emit(EV_APPROVAL_REQUESTED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name)
            if pol.approval_webhook:
                self._fire_webhook(
                    pol.approval_webhook, record.id, name, tenant=record.tenant, policy=pol.name,
                    args_json=record.args_json, reasoning=record.reasoning, agent_id=record.agent_id,
                )
            status = self.backend.wait_for_approval(record.id, pol.approval_timeout, self.poll_interval)
            if status != APPROVAL_APPROVED:
                note = self._rejection_note(record.id)
                self.backend.fail(record.id, f"rejected by approval{note}")
                self._emit(EV_APPROVAL_REJECTED, tool_name=name, execution_id=record.id,
                           tenant=record.tenant, policy=pol.name, note=note.lstrip(": ") or None)
                raise ApprovalRejected(f"execution {record.id} was rejected{note}")
            if not self._verify_approval_signature(record.id):
                self.backend.fail(record.id, "approval signature missing or invalid")
                self._emit(EV_APPROVAL_REJECTED, tool_name=name, execution_id=record.id,
                           tenant=record.tenant, policy=pol.name, note="approval signature missing or invalid")
                raise ApprovalRejected(
                    f"execution {record.id} was marked approved WITHOUT a valid signature; "
                    "refusing to run (an approval secret is configured, so decisions written "
                    "straight into the database do not count)"
                )
            self._emit(EV_APPROVAL_APPROVED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name)

        started = time.time()
        try:
            if pol.execution_timeout:
                result = self._call_with_timeout(fn, args, kwargs, pol.execution_timeout)
            else:
                result = fn(*args, **kwargs)
            self.backend.complete(record.id, json.dumps(result, default=str), pol.cache_ttl)
            self._emit(EV_CALL_SUCCEEDED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name, duration_s=time.time() - started)
            return result
        except Exception as exc:
            self.backend.fail(record.id, str(exc))
            self._emit(EV_CALL_FAILED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name, error=str(exc), replayed=False)
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
        agent_id: Optional[str] = None,
        agent_meta: Optional[str] = None,
        budget_value: Optional[float] = None,
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
                                      reasoning=reasoning, agent_id=agent_id, agent_meta=agent_meta,
                                      budget_value=budget_value)
            self._guard_limits(acq.record, pol, name, tenant)
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
        await asyncio.to_thread(self._check_kill_switch, name, tenant, policy)
        normalized = _bind_args(fn, args, kwargs)
        args_hash = _hash_args(normalized)
        args_json = _redacted_args_json(normalized, pol)
        budget_value = self._budget_value(pol, name, normalized)
        reasoning = current_reasoning()
        agent_id, agent_meta = self._resolve_agent()

        if not pol.idempotent:
            return await self._run_volatile_async(
                fn, pol, args, kwargs, tenant, name, args_hash, args_json, normalized,
                reasoning=reasoning, agent_id=agent_id, agent_meta=agent_meta, budget_value=budget_value
            )

        embedding_json = None
        if pol.semantic_cache:
            embedding, hit = await asyncio.to_thread(self._semantic_lookup, pol, name, tenant, normalized)
            if hit is not None:
                self._emit(EV_SEMANTIC_CACHE_HIT, tool_name=name, execution_id=hit.id, tenant=tenant,
                           policy=policy)
                return json.loads(hit.result_json)
            embedding_json = json.dumps(embedding)

        idem_key = key_fn(*args, **kwargs) if key_fn else _default_key(name, normalized, tenant)
        acq = await self._acquire_gated_async(pol, name, idem_key, tenant, policy, args_hash, args_json,
                                              embedding_json=embedding_json, reasoning=reasoning,
                                              agent_id=agent_id, agent_meta=agent_meta,
                                              budget_value=budget_value)

        if acq.use_cached:
            self._emit(EV_CACHE_HIT, tool_name=name, execution_id=acq.record.id, tenant=tenant, policy=policy)
            return json.loads(acq.record.result_json)
        if acq.raise_stored_error:
            self._emit(EV_CALL_FAILED, tool_name=name, execution_id=acq.record.id, tenant=tenant,
                       policy=policy, error=acq.record.error, replayed=True)
            raise ExecutionFailed(acq.record.error)
        if acq.follow_approval:
            self._emit(EV_SINGLEFLIGHT_COALESCED, tool_name=name, execution_id=acq.record.id, tenant=tenant,
                       policy=policy)
            status = await self._await_approval(acq.record.id, pol.approval_timeout)
            if status != APPROVAL_APPROVED:
                raise ApprovalRejected(f"execution {acq.record.id} was rejected")
            final = await self._await_result(acq.record.id, pol.approval_timeout)
            return self._resolve(final)
        if acq.follow_running:
            self._emit(EV_SINGLEFLIGHT_COALESCED, tool_name=name, execution_id=acq.record.id, tenant=tenant,
                       policy=policy)
            final = await self._await_result(acq.record.id, 3600.0)
            return self._resolve(final)

        await self._guard_limits_async(acq.record, pol, name, tenant)
        return await self._execute_as_owner_async(acq.record, fn, args, kwargs, pol, name, normalized)

    async def _execute_as_owner_async(
        self, record, fn, args, kwargs, pol: Policy, name: str, normalized: Dict[str, Any]
    ) -> Any:
        self._emit(EV_CALL_STARTED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                   policy=pol.name)
        if self._needs_approval(pol, normalized):
            await asyncio.to_thread(self.backend.mark_waiting_approval, record.id)
            self._emit(EV_APPROVAL_REQUESTED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name)
            if pol.approval_webhook:
                await asyncio.to_thread(
                    self._fire_webhook, pol.approval_webhook, record.id, name,
                    tenant=record.tenant, policy=pol.name, args_json=record.args_json,
                    reasoning=record.reasoning, agent_id=record.agent_id,
                )
            status = await self._await_approval(record.id, pol.approval_timeout)
            if status != APPROVAL_APPROVED:
                note = await asyncio.to_thread(self._rejection_note, record.id)
                await asyncio.to_thread(self.backend.fail, record.id, f"rejected by approval{note}")
                self._emit(EV_APPROVAL_REJECTED, tool_name=name, execution_id=record.id,
                           tenant=record.tenant, policy=pol.name, note=note.lstrip(": ") or None)
                raise ApprovalRejected(f"execution {record.id} was rejected{note}")
            if not await asyncio.to_thread(self._verify_approval_signature, record.id):
                await asyncio.to_thread(self.backend.fail, record.id, "approval signature missing or invalid")
                self._emit(EV_APPROVAL_REJECTED, tool_name=name, execution_id=record.id,
                           tenant=record.tenant, policy=pol.name, note="approval signature missing or invalid")
                raise ApprovalRejected(
                    f"execution {record.id} was marked approved WITHOUT a valid signature; "
                    "refusing to run (an approval secret is configured, so decisions written "
                    "straight into the database do not count)"
                )
            self._emit(EV_APPROVAL_APPROVED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name)

        started = time.time()
        try:
            if pol.execution_timeout:
                result = await asyncio.wait_for(fn(*args, **kwargs), timeout=pol.execution_timeout)
            else:
                result = await fn(*args, **kwargs)
            await asyncio.to_thread(
                self.backend.complete, record.id, json.dumps(result, default=str), pol.cache_ttl
            )
            self._emit(EV_CALL_SUCCEEDED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name, duration_s=time.time() - started)
            return result
        except asyncio.TimeoutError:
            message = f"{fn.__name__} did not finish within {pol.execution_timeout:.0f}s"
            await asyncio.to_thread(self.backend.fail, record.id, message)
            self._emit(EV_CALL_FAILED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name, error=message, replayed=False)
            raise ExecutionTimeout(message) from None
        except Exception as exc:
            await asyncio.to_thread(self.backend.fail, record.id, str(exc))
            self._emit(EV_CALL_FAILED, tool_name=name, execution_id=record.id, tenant=record.tenant,
                       policy=pol.name, error=str(exc), replayed=False)
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
        agent_id: Optional[str] = None,
        agent_meta: Optional[str] = None,
        budget_value: Optional[float] = None,
    ) -> Any:
        attempt = 0
        while True:
            execution_id = str(uuid.uuid4())
            acq = await self._acquire_gated_async(pol, name, execution_id, tenant, pol.name, args_hash,
                                                  args_json, reasoning=reasoning, agent_id=agent_id,
                                                  agent_meta=agent_meta, budget_value=budget_value)
            await self._guard_limits_async(acq.record, pol, name, tenant)
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
