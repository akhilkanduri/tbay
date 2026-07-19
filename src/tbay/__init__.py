from .client import TbayClient
from .context import agent, current_agent, current_agent_meta, current_reasoning, reasoning
from .decorator import guard_tools, guarded
from .embedders import HashingEmbedder, cosine_similarity
from .events import Event
from .exceptions import (
    ApprovalRejected,
    ApprovalTimeout,
    BudgetExceeded,
    ConcurrencyLimitExceeded,
    ExecutionFailed,
    ExecutionTimeout,
    RateLimitExceeded,
    TbayError,
    ToolPaused,
)
from .redaction import redact_structure
from .security import sign_approval, sign_webhook, verify_approval, verify_webhook

__version__ = "0.3.0"

__all__ = [
    "TbayClient",
    "guarded",
    "guard_tools",
    "reasoning",
    "current_reasoning",
    "agent",
    "current_agent",
    "current_agent_meta",
    "sign_approval",
    "verify_approval",
    "sign_webhook",
    "verify_webhook",
    "HashingEmbedder",
    "cosine_similarity",
    "Event",
    "redact_structure",
    "TbayError",
    "ApprovalRejected",
    "ApprovalTimeout",
    "BudgetExceeded",
    "ExecutionFailed",
    "ExecutionTimeout",
    "RateLimitExceeded",
    "ConcurrencyLimitExceeded",
    "ToolPaused",
]
