from .client import TbayClient
from .context import agent, current_agent, current_agent_meta, current_reasoning, reasoning
from .decorator import guarded
from .embedders import HashingEmbedder, cosine_similarity
from .security import sign_approval, verify_approval
from .exceptions import (
    ApprovalRejected,
    ApprovalTimeout,
    ConcurrencyLimitExceeded,
    ExecutionFailed,
    ExecutionTimeout,
    RateLimitExceeded,
    TbayError,
)

__version__ = "0.2.0"

__all__ = [
    "TbayClient",
    "guarded",
    "reasoning",
    "current_reasoning",
    "agent",
    "current_agent",
    "current_agent_meta",
    "sign_approval",
    "verify_approval",
    "HashingEmbedder",
    "cosine_similarity",
    "TbayError",
    "ApprovalRejected",
    "ApprovalTimeout",
    "ExecutionFailed",
    "ExecutionTimeout",
    "RateLimitExceeded",
    "ConcurrencyLimitExceeded",
]
