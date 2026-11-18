"""Authorization rules. The LLM only proposes; these checks decide."""

import enum
from dataclasses import dataclass

from opspilot.agent.tools import ToolKind, ToolSpec
from opspilot.core.auth import Role, User

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
    if approver.role not in (Role.MANAGER, Role.ADMIN):
        return PolicyResult(Decision.DENY, "only managers or admins can approve")
    if approver.id == requested_by:
        return PolicyResult(Decision.DENY, "requester cannot approve their own request")
    if approver.role is Role.MANAGER and approver.department != department:
        return PolicyResult(Decision.DENY, "manager must belong to the requesting department")
    return PolicyResult(Decision.ALLOW, "ok")
