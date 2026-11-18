from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opspilot.core.auth import Role
from opspilot.core.db import ApprovalStatus


class DevTokenIn(BaseModel):
    user_id: str
    role: Role
    department: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AgentRunIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    run_id: str
    tool: str
    args: dict[str, Any]
    summary: str
    requested_by: str
    department: str
    status: ApprovalStatus
    decided_by: str | None
    decision_reason: str | None
    result: dict[str, Any] | None
    error: str | None
    workflow_id: str | None
    created_at: datetime
    updated_at: datetime


class AgentRunOut(BaseModel):
    run_id: str
    answer: str
    approvals: list[ApprovalOut]
    steps: list[dict[str, Any]]
    stopped_reason: str | None = None


class DecisionIn(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=1000)


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor: str
    action: str
    target: str | None
    status: str
    detail: dict[str, Any]
    created_at: datetime
