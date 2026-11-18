from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from opspilot_agent import approvals
from opspilot_agent.tools import ToolContext
from opspilot_core.db import Database


class ApprovalActivities:
    def __init__(self, db: Database, tools: ToolContext) -> None:
        self.db = db
        self.tools = tools

    @activity.defn(name="approval.execute")
    async def execute(self, approval_id: str) -> dict[str, Any]:
        try:
            return await approvals.execute_approved(self.db, self.tools, approval_id)
        except approvals.PermanentActionError as exc:
            raise ApplicationError(
                str(exc), type="PermanentActionError", non_retryable=True
            ) from exc

    @activity.defn(name="approval.mark_failed")
    async def mark_failed(self, approval_id: str, error: str) -> None:
        await approvals.mark_failed(self.db, approval_id, error)

    @activity.defn(name="approval.expire")
    async def expire(self, approval_id: str) -> None:
        await approvals.expire(self.db, approval_id)
