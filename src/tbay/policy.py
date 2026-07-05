from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml

_TTL_RE = re.compile(r"^(\d+)\s*(s|m|h|d)?$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_ttl(value) -> Optional[float]:
    """Turn a duration into seconds. Accepts '5m', '30s', '1h', '2d', a plain
    number of seconds (300), or None. 0 and None both mean "no expiry"."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    match = _TTL_RE.match(str(value).strip())
    if not match:
        raise ValueError(f"invalid duration {value!r}, try something like '5m', '30s', or '1h'")
    amount, unit = match.groups()
    return float(int(amount) * _UNITS[unit]) or None


@dataclass
class Policy:
    """One named risk tier. Attach it to a tool with @guarded(client, policy="name").

    Every field has a sensible default, so a policy file only needs to spell
    out the fields it wants to change from DEFAULT_POLICIES.
    """

    name: str

    # caching and idempotency
    cache_ttl: Optional[float] = None  # how long a stored result satisfies later calls; None means forever
    idempotent: bool = True  # once a call with this key succeeds, never silently re-run it.
    # Set this to False for tools that should run fresh every time even with identical
    # arguments: an LLM call used to make a decision, "roll a die", "get the current time".
    # When False, tbay ignores key_fn/caching/singleflight entirely and always executes.
    singleflight: bool = True  # let concurrent identical calls share one execution instead of racing

    # retrying a failed call
    max_retries: int = 0  # how many times a new call may re-attempt a previously failed key
    retry_backoff: float = 0.0  # seconds to wait after a failure before a retry is allowed

    # human approval for risky calls
    approval_required: bool = False
    approval_webhook: Optional[str] = None  # fired, best effort, when a call starts waiting for approval
    approval_timeout: float = 3600.0  # give up waiting for a human after this many seconds
    approval_bypass_arg: Optional[str] = None  # e.g. "amount", skip approval when this arg is small enough
    approval_bypass_max: Optional[float] = None  # the threshold that arg must stay at or under to bypass

    # throughput guardrails, so a runaway agent loop can't hammer a tool
    rate_limit_max_calls: Optional[int] = None  # max calls, any args, allowed per rate_limit_window
    rate_limit_window: Optional[float] = None  # the window those calls are counted over, in seconds
    max_concurrent: Optional[int] = None  # at most this many calls to this tool running at once
    concurrency_wait_timeout: float = 300.0  # how long a caller waits for a free slot before giving up

    # the actual function call
    execution_timeout: Optional[float] = None  # give up on a hung tool call after this many seconds

    # audit log
    redact_args: List[str] = field(default_factory=list)  # argument names to mask before they're stored


DEFAULT_POLICIES: Dict[str, Policy] = {
    "readonly": Policy(
        name="readonly",
        cache_ttl=300.0,
        singleflight=True,
        max_retries=2,
        retry_backoff=1.0,
    ),
    "mutating": Policy(
        name="mutating",
        idempotent=True,
        cache_ttl=None,
        max_retries=0,
    ),
    "destructive": Policy(
        name="destructive",
        idempotent=True,
        approval_required=True,
        max_retries=0,
    ),
    "volatile": Policy(
        name="volatile",
        idempotent=False,  # always run for real; never dedupe, cache, or singleflight
        cache_ttl=None,
        singleflight=False,
        max_retries=0,
    ),
}


def _build_policy(name: str, cfg: dict) -> Policy:
    rate_limit = cfg.get("rate_limit") or {}

    max_retries = cfg.get("max_retries")
    if max_retries is None:
        # shorthand from the original plan doc: `retry: true` means "allow one retry"
        max_retries = 1 if cfg.get("retry") else 0

    return Policy(
        name=name,
        cache_ttl=parse_ttl(cfg.get("cache_ttl")),
        idempotent=bool(cfg.get("idempotent", True)),
        singleflight=bool(cfg.get("singleflight", True)),
        max_retries=int(max_retries),
        retry_backoff=parse_ttl(cfg.get("retry_backoff")) or 0.0,
        approval_required=bool(cfg.get("approval_required", False)),
        approval_webhook=cfg.get("approval_webhook"),
        approval_timeout=float(cfg.get("approval_timeout", 3600.0)),
        approval_bypass_arg=cfg.get("approval_bypass_arg"),
        approval_bypass_max=(
            float(cfg["approval_bypass_max"]) if cfg.get("approval_bypass_max") is not None else None
        ),
        rate_limit_max_calls=(int(rate_limit["max_calls"]) if rate_limit.get("max_calls") else None),
        rate_limit_window=parse_ttl(rate_limit.get("per")),
        max_concurrent=(int(cfg["max_concurrent"]) if cfg.get("max_concurrent") else None),
        concurrency_wait_timeout=float(cfg.get("concurrency_wait_timeout", 300.0)),
        execution_timeout=parse_ttl(cfg.get("execution_timeout")),
        redact_args=list(cfg.get("redact_args") or []),
    )


def load_policies(path: Optional[str] = None) -> Dict[str, Policy]:
    """Start from DEFAULT_POLICIES, then let a YAML file override or add policies by name.

    Returns fresh Policy objects, deep-copied from the defaults, not shared
    references. Every TbayClient (and every test) mutates its own copy of
    `client.policies[...]`; none of them can accidentally corrupt the module-
    level DEFAULT_POLICIES or another client's policies by doing so.
    """
    policies = {name: copy.deepcopy(pol) for name, pol in DEFAULT_POLICIES.items()}
    if path is None:
        return policies
    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    for name, cfg in (data.get("policies") or {}).items():
        policies[name] = _build_policy(name, cfg or {})
    return policies
