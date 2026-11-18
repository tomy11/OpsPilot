"""Authorization rules. The LLM only proposes; these checks decide."""

import enum
from dataclasses import dataclass

from opspilot_agent.tools import ToolKind, ToolSpec
from opspilot_core.auth import Role, User, approval_denied_reason

_RANK = {Role.STAFF: 0, Role.MANAGER: 1, Role.ADMIN: 2}


class Decision(enum.StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str


def authorize_tool(user: User, spec: ToolSpec) -> PolicyResult:
    if _RANK[user.role] < _RANK[spec.min_role]:
        return PolicyResult(
            Decision.DENY, f"role '{user.role}' cannot use {spec.name} (needs {spec.min_role})"
        )
    if spec.kind is ToolKind.READ:
        return PolicyResult(Decision.ALLOW, "read tool")
    return PolicyResult(Decision.NEEDS_APPROVAL, "write tool requires human approval")


def can_decide(approver: User, *, requested_by: str, department: str) -> PolicyResult:
    reason = approval_denied_reason(approver, requested_by=requested_by, department=department)
    return PolicyResult(Decision.DENY, reason) if reason else PolicyResult(Decision.ALLOW, "ok")
