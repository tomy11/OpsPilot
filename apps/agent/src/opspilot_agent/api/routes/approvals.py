from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from opspilot_agent.api.deps import ServicesDep
from opspilot_agent.api.schemas import ApprovalOut, DecisionIn
from opspilot_agent.approvals import ApprovalConflict, transition
from opspilot_agent.conversations import erp_scope
from opspilot_agent.erp_client import ErpError
from opspilot_agent.models import ApprovalDelivery, ApprovalRequest, ApprovalStatus
from opspilot_agent.policy import Decision, can_decide
from opspilot_agent.worker.workflows import DecisionSignal
from opspilot_core.api import SessionDep, UserDep
from opspilot_core.audit import record_audit
from opspilot_core.auth import Role, User

router = APIRouter(prefix="/approvals", tags=["approvals"])


def _visible(user: User, a: ApprovalRequest) -> bool:
    if user.role is Role.ADMIN:
        return True
    if user.role is Role.MANAGER:
        return a.department == user.department
    return a.requested_by == user.id


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    user: UserDep,
    s: SessionDep,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    limit: int = 50,
) -> list[ApprovalOut]:
    q = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(min(limit, 200))
    if status_filter:
        q = q.where(ApprovalRequest.status == status_filter)
    if user.role is Role.MANAGER:
        q = q.where(ApprovalRequest.department == user.department)
    elif user.role is Role.STAFF:
        q = q.where(ApprovalRequest.requested_by == user.id)
    return [ApprovalOut.model_validate(a) for a in (await s.scalars(q)).all()]


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: str, user: UserDep, s: SessionDep) -> ApprovalOut:
    a = await s.get(ApprovalRequest, approval_id)
    if a is None or not _visible(user, a):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    return ApprovalOut.model_validate(a)


@router.get("/{approval_id}/inventory")
async def approval_inventory(approval_id: str, user: UserDep, s: SessionDep, svc: ServicesDep):
    a = await s.get(ApprovalRequest, approval_id)
    if a is None or not _visible(user, a) or a.tool != "create_stock_transfer":
        raise HTTPException(404, "stock transfer not found")
    scope = await erp_scope(s, a.run_id)
    try:
        with svc.tools.erp.sandbox(scope):
            result = await svc.tools.erp.get_inventory(a.args["sku"])
    except ErpError as exc:
        raise HTTPException(503, "ERP inventory temporarily unavailable") from exc
    branches = {a.args["from_branch"], a.args["to_branch"]}
    return {
        "sku": result["sku"],
        "stock": [row for row in result["stock"] if row["branch"] in branches],
    }


@router.post("/{approval_id}/decision", response_model=ApprovalOut)
async def decide(
    approval_id: str, body: DecisionIn, user: UserDep, svc: ServicesDep
) -> ApprovalOut:
    async with svc.db.sessionmaker() as s:
        a = await s.get(ApprovalRequest, approval_id)
        if a is None or not _visible(user, a):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
        policy = can_decide(user, requested_by=a.requested_by, department=a.department)
        if policy.decision is Decision.DENY:
            await record_audit(
                s,
                actor=user.id,
                action="approval.decide",
                status="denied",
                target=approval_id,
                detail={"reason": policy.reason},
            )
            await s.commit()
            raise HTTPException(status.HTTP_403_FORBIDDEN, policy.reason)

        if (
            svc.settings.durable_approvals
            and a.status is ApprovalStatus.PENDING
            and not a.workflow_id
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "approval workflow is not ready")

        new = ApprovalStatus.APPROVED if body.approve else ApprovalStatus.REJECTED
        try:
            await transition(
                s,
                approval_id,
                expected=ApprovalStatus.PENDING,
                new=new,
                decided_by=user.id,
                decision_reason=body.reason,
            )
        except ApprovalConflict as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        await record_audit(
            s,
            actor=user.id,
            action=f"approval.{new.value}",
            status="ok",
            target=approval_id,
            detail={"reason": body.reason},
        )
        if svc.settings.durable_approvals:
            s.add(ApprovalDelivery(approval_id=approval_id))
        await s.commit()

    if not svc.settings.durable_approvals:
        await svc.orchestrator.decide(
            approval_id,
            DecisionSignal(approve=body.approve, approver_id=user.id, reason=body.reason),
        )

    async with svc.db.sessionmaker() as s:
        return ApprovalOut.model_validate(await s.get(ApprovalRequest, approval_id))
