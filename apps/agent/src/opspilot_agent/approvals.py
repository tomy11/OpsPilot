"""Approval state transitions, shared by the API (decisions) and the worker (execution).

Every transition is a conditional UPDATE on the current status, so a double click, a duplicate
signal or a retried activity cannot apply the same change twice.
"""

from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_agent.erp_client import ErpError
from opspilot_agent.models import ApprovalRequest, ApprovalStatus
from opspilot_agent.tools import ToolContext, execute_write
from opspilot_core.audit import record_audit
from opspilot_core.db import Database


class ApprovalConflict(Exception):
    """The approval is not in the state this transition needs."""


class PermanentActionError(Exception):
    """The ERP rejected the action for a reason a retry will not fix."""


async def transition(
    session: AsyncSession,
    approval_id: str,
    *,
    expected: ApprovalStatus,
    new: ApprovalStatus,
    **fields: Any,
) -> None:
    stmt = (
        update(ApprovalRequest)
        .where(ApprovalRequest.id == approval_id, ApprovalRequest.status == expected)
        .values(status=new, **fields)
    )
    result = await session.execute(stmt)
    if result.rowcount != 1:
        raise ApprovalConflict(f"approval {approval_id} is not {expected.value}")


async def execute_approved(db: Database, ctx: ToolContext, approval_id: str) -> dict[str, Any]:
    async with db.sessionmaker() as s:
        approval = await s.get(ApprovalRequest, approval_id)
        if approval is None:
            raise PermanentActionError(f"approval {approval_id} not found")
        if approval.status is ApprovalStatus.EXECUTED:
            return approval.result or {}
        if approval.status is not ApprovalStatus.APPROVED:
            raise PermanentActionError(f"approval {approval_id} is {approval.status.value}")

        try:
            # The approval id doubles as the ERP idempotency key.
            result = await execute_write(ctx, approval.tool, approval.args, approval.id)
        except ErpError as exc:
            if exc.retryable:
                raise
            await mark_failed(db, approval_id, str(exc))
            raise PermanentActionError(str(exc)) from exc

        await transition(
            s,
            approval_id,
            expected=ApprovalStatus.APPROVED,
            new=ApprovalStatus.EXECUTED,
            result=result,
        )
        await record_audit(
            s,
            actor="system",
            action="approval.executed",
            status="ok",
            target=approval_id,
            detail={"tool": approval.tool, "args": approval.args, "result": result},
        )
        await s.commit()
        return result


async def mark_failed(db: Database, approval_id: str, error: str) -> None:
    async with db.sessionmaker() as s:
        try:
            await transition(
                s,
                approval_id,
                expected=ApprovalStatus.APPROVED,
                new=ApprovalStatus.FAILED,
                error=error,
            )
        except ApprovalConflict:
            return
        await record_audit(
            s,
            actor="system",
            action="approval.failed",
            status="error",
            target=approval_id,
            detail={"error": error},
        )
        await s.commit()


async def expire(db: Database, approval_id: str) -> None:
    async with db.sessionmaker() as s:
        try:
            await transition(
                s, approval_id, expected=ApprovalStatus.PENDING, new=ApprovalStatus.EXPIRED
            )
        except ApprovalConflict:
            return
        await record_audit(
            s, actor="system", action="approval.expired", status="ok", target=approval_id
        )
        await s.commit()
