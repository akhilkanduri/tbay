from __future__ import annotations

import functools
import inspect
from typing import Callable, Optional

from .client import TbayClient


def guarded(
    client: TbayClient,
    *,
    policy: str = "mutating",
    key_fn: Optional[Callable] = None,
    tenant: str = "",
    tool_name: Optional[str] = None,
):
    """Wrap a tool function with the idempotency, caching, singleflight, and
    approval behavior defined by `policy`.

    Works on both sync and async functions, and never inspects the caller's
    framework: it only ever sees a plain callable, so it drops straight into
    LangChain tools, OpenAI Agents SDK function tools, CrewAI tools, or bare
    functions, with no adapter code needed.

    `key_fn`, if given, computes the idempotency key from the call's args
    (e.g. `key_fn=lambda customer_id, **_: customer_id`). It's ignored for
    policies with `idempotent=False` (see the "volatile" policy), since
    those always run fresh and never need a dedup key at all.
    """

    def decorator(fn: Callable) -> Callable:
        name = tool_name or fn.__name__

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                return await client.run_async(
                    fn, policy=policy, args=args, kwargs=kwargs, tenant=tenant, key_fn=key_fn, tool_name=name
                )

            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            return client.run(
                fn, policy=policy, args=args, kwargs=kwargs, tenant=tenant, key_fn=key_fn, tool_name=name
            )

        return sync_wrapper

    return decorator
