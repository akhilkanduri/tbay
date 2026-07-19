from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..exceptions import ApprovalTimeout, ExecutionTimeout

RUNNING = "RUNNING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
WAITING_APPROVAL = "WAITING_APPROVAL"

APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


@dataclass
class ExecutionRecord:
    """One row of the audit log: everything tbay knows about a single
    tool call, keyed by (tool_name, idempotency_key, tenant)."""

    id: str
    tool_name: str
    idempotency_key: str
    tenant: str
    status: str
    args_hash: str
    args_json: Optional[str]
    result_json: Optional[str]
    error: Optional[str]
    policy_name: str
    retry_count: int
    cache_expires_at: Optional[float]
    created_at: float
    finished_at: Optional[float]
    embedding_json: Optional[str] = None  # args embedding, present only when the policy has semantic_cache on
    reasoning: Optional[str] = None  # why the agent made this call, captured from `with tbay.reasoning(...)`
    agent_id: Optional[str] = None  # which agent asked for this call (with tbay.agent(...) / TBAY_AGENT_ID)
    agent_meta: Optional[str] = None  # JSON metadata about that agent (model, team, version, ...)
    budget_value: Optional[float] = None  # this call's metered amount, when the policy has a budget cap


@dataclass
class AcquireResult:
    """What acquire_or_get() decided the caller should do next. Exactly one
    of the flags below matters:

    - owner:              caller must run the real function.
    - use_cached:          caller should return record.result_json right away.
    - follow_running:      caller should block on wait_for_result().
    - follow_approval:     caller should block on wait_for_approval(), then wait_for_result().
    - raise_stored_error:  caller should raise the stored error right away (no retry left).
    - wait_for_slot:       max_concurrent is full; caller should pause briefly and call
                           acquire_or_get() again (record is None in this case, since
                           nothing was reserved).
    """

    record: Optional[ExecutionRecord]
    owner: bool = False
    use_cached: bool = False
    follow_running: bool = False
    wait_for_slot: bool = False
    follow_approval: bool = False
    raise_stored_error: bool = False


class StorageBackend:
    """The interface SQLiteBackend and PostgresBackend both implement.

    Coordinating multiple callers, possibly in different processes or on
    different machines, is entirely the database's job here: acquire_or_get
    must claim a lease atomically (INSERT ... ON CONFLICT DO NOTHING,
    followed by a compare-and-swap UPDATE when reclaiming an expired or
    failed row). There's no shared in-memory state between callers, only
    whatever the database itself guarantees.
    """

    def init_schema(self) -> None:
        """Create the executions/approvals tables if they don't already exist."""
        raise NotImplementedError

    def acquire_or_get(
        self,
        *,
        execution_id: str,
        tool_name: str,
        idempotency_key: str,
        tenant: str,
        policy_name: str,
        args_hash: str,
        args_json: Optional[str],
        max_retries: int,
        retry_backoff: float,
        max_concurrent: Optional[int] = None,
        embedding_json: Optional[str] = None,
        reasoning: Optional[str] = None,
        agent_id: Optional[str] = None,
        agent_meta: Optional[str] = None,
        budget_value: Optional[float] = None,
        lease_timeout: Optional[float] = None,
    ) -> AcquireResult:
        """Try to become the owner of this (tool_name, idempotency_key, tenant).
        If someone already owns it, report back what the caller should do instead.

        If max_concurrent is set, the RUNNING count check and the insert
        happen in the same transaction, so this is atomic, not a separate
        check-then-act: two simultaneous new callers can't both slip through
        past the cap the way a check performed before this call would allow.

        If lease_timeout is set and the existing owner's RUNNING row is older
        than that many seconds, the owner is presumed crashed and the row is
        reclaimed with a compare-and-swap keyed on the row's observed
        created_at, so exactly one contender wins the reclaim and everyone
        else keeps following.
        """
        raise NotImplementedError

    def mark_waiting_approval(self, execution_id: str) -> None:
        raise NotImplementedError

    def complete(self, execution_id: str, result_json: str, cache_ttl_seconds: Optional[float]) -> None:
        raise NotImplementedError

    def fail(self, execution_id: str, error: str) -> None:
        raise NotImplementedError

    def get(self, execution_id: str) -> Optional[ExecutionRecord]:
        raise NotImplementedError

    def get_approval_status(self, execution_id: str) -> Optional[str]:
        raise NotImplementedError

    def resolve_approval(
        self, execution_id: str, approved: bool, resolver: str = "", signature: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        """Record a human decision. `signature` is the HMAC from
        tbay.security.sign_approval; when the executing client is configured
        with an approval secret, it verifies this before running (see
        src/tbay/security.py for the whole trust model)."""
        raise NotImplementedError

    def get_approval(self, execution_id: str) -> Optional[dict]:
        """The full approval row (status/resolver/signature/timestamps) or
        None. get_approval_status stays for cheap polling; this exists so the
        client can verify the signature on the final decision."""
        raise NotImplementedError

    def list_executions(
        self,
        *,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        tenant: Optional[str] = None,
        limit: int = 50,
    ) -> List[ExecutionRecord]:
        raise NotImplementedError

    def count_since(self, tool_name: str, tenant: str, since: float) -> int:
        """How many calls to this tool (for this tenant) were created at or
        after `since` (a time.time() timestamp). Backs the rate_limit guardrail."""
        raise NotImplementedError

    def sum_budget_since(self, tool_name: str, tenant: str, since: float) -> float:
        """The total of budget_value across every execution of this tool (for
        this tenant) created at or after `since`. Backs the budget guardrail.
        Counts every recorded execution regardless of status: a call that
        failed mid-flight may still have had its side effect, so the
        conservative reading (never under-count spend) is the safe one."""
        raise NotImplementedError

    # -- controls: small named switches, shared by every process on this
    # database. Today they back the kill switch (`pause:*` / `pause:tool:X`);
    # the namespace is generic so future controls don't need schema changes.

    def set_control(self, key: str, value: str) -> None:
        raise NotImplementedError

    def get_control(self, key: str) -> Optional[str]:
        raise NotImplementedError

    def delete_control(self, key: str) -> None:
        raise NotImplementedError

    def list_controls(self) -> Dict[str, str]:
        raise NotImplementedError

    def list_semantic_candidates(self, tool_name: str, tenant: str, limit: int = 200) -> List[ExecutionRecord]:
        """Recent SUCCEEDED executions for this tool/tenant that carry an args
        embedding and whose cached result hasn't expired. The client compares
        these against a new call's embedding to find a semantic cache hit;
        the cosine math happens in the client, so backends only filter."""
        raise NotImplementedError

    def clear(self) -> int:
        """Delete every execution and approval this backend stores. Returns
        how many executions were removed. Backs `tbay clear`; there is no
        undo, so the CLI asks for confirmation before calling this."""
        raise NotImplementedError

    # -- generic polling helpers, shared by every backend --

    def wait_for_result(self, execution_id: str, timeout: float, poll_interval: float) -> ExecutionRecord:
        deadline = time.time() + timeout
        while True:
            record = self.get(execution_id)
            if record and record.status in (SUCCEEDED, FAILED):
                return record
            if time.time() > deadline:
                raise ExecutionTimeout(f"timed out waiting for execution {execution_id} to finish")
            time.sleep(poll_interval)

    def wait_for_approval(self, execution_id: str, timeout: float, poll_interval: float) -> str:
        deadline = time.time() + timeout
        while True:
            status = self.get_approval_status(execution_id)
            if status and status != APPROVAL_PENDING:
                return status
            if time.time() > deadline:
                raise ApprovalTimeout(f"nobody approved or rejected execution {execution_id} in time")
            time.sleep(poll_interval)
