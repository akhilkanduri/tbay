from __future__ import annotations


class TbayError(Exception):
    """Base class for every error tbay raises. Catch this if you just want
    to know whether tbay stopped your call for some safety reason, without
    caring which one."""


class ApprovalRejected(TbayError):
    """A human rejected this call with `tbay reject`. The tool never ran."""


class ApprovalTimeout(TbayError):
    """Nobody approved or rejected the call before the policy's approval_timeout
    ran out. The tool never ran; call it again if you still want it to happen."""


class ExecutionFailed(TbayError):
    """A previous call with the same idempotency key already failed, and the
    policy's retry budget is exhausted (or retries are off), so tbay replays
    that stored failure instead of trying again."""


class ExecutionTimeout(TbayError):
    """Waited too long for another caller's in-flight (or in-approval) execution
    to finish. This usually means that other call is stuck, not that anything
    about your call was wrong."""


class RateLimitExceeded(TbayError):
    """This tool has already been called too many times within the window set
    by its policy's rate_limit. Slow down, or raise the limit in your policy
    file if the limit is too strict for your use case."""


class ConcurrencyLimitExceeded(TbayError):
    """Waited for a free execution slot under this tool's max_concurrent cap
    and one never opened up. Either raise the cap or reduce how many callers
    hit this tool at the same time."""


class ToolPaused(TbayError):
    """The kill switch is on: someone ran `tbay pause` (globally or just for
    this tool), so tbay refused to start the call at all. `tbay resume`
    turns things back on. The pause reason, if one was given, is included
    in the message."""


class BudgetExceeded(TbayError):
    """Running this call would push the total of the tool's metered argument
    (the policy's budget.arg, e.g. `amount`) past budget.max within the
    budget window. The tool never ran. Also raised when the metered argument
    is missing or not numeric, since a call that can't be measured can't be
    safely counted against a spend cap."""
