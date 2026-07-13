"""Signed approvals: separate "can write the database" from "may authorize
a destructive action".

The problem: an approval is a database row, so by default anyone holding the
database password can flip a pending approval to "approved". Storage access
and approval authority are the same credential, which is wrong for anything
sensitive.

The fix: configure an approval secret (TBAY_APPROVAL_SECRET, or
TbayClient(approval_secret=...)). Approvers sign their decision with an
HMAC over (execution_id, decision), and the executing client recomputes and
verifies that signature BEFORE running the function. A decision written
straight into the database without the secret fails verification and is
treated as a rejection, so database credentials alone can no longer approve
anything. Give the secret only to your approval surface (the tbay CLI on an
operator's machine, the dashboard process, your Slack bot); give agents and
services only the database credentials.

Honest limits, so you can reason about them:
- Someone with FULL database access can still delete rows, mark executions
  FAILED, or wipe results. Signing protects the approve decision, not the
  storage itself; least-privilege database roles protect the rest.
- The verification runs in the waiting client's process. Someone who can
  edit that process's code or environment is already past any guardrail.
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Optional

APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"


def sign_approval(secret: str, execution_id: str, approved: bool) -> str:
    """The signature an approver attaches to a decision: HMAC-SHA256 over
    the execution id and the decision, keyed by the shared approval secret."""
    decision = APPROVAL_APPROVED if approved else APPROVAL_REJECTED
    return hmac.new(secret.encode(), f"{execution_id}:{decision}".encode(), hashlib.sha256).hexdigest()


def verify_approval(secret: str, execution_id: str, approved: bool, signature: Optional[str]) -> bool:
    """Constant-time check that a stored decision was made by someone holding
    the approval secret. A missing signature never verifies."""
    if not signature:
        return False
    expected = sign_approval(secret, execution_id, approved)
    return hmac.compare_digest(expected, signature)
