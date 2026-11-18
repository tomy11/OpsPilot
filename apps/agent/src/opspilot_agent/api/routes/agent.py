import logging
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from opspilot_agent.api.deps import ServicesDep
from opspilot_agent.api.schemas import AgentRunIn, AgentRunOut, ApprovalOut
from opspilot_agent.approvals import ApprovalConflict, transition
from opspilot_agent.graph import AgentDeps, run_agent
from opspilot_agent.models import AgentRun, ApprovalRequest, ApprovalStatus
from opspilot_core.api import UserDep
from opspilot_core.audit import record_audit

router = APIRouter(prefix="/agent", tags=["agent"])
log = logging.getLogger(__name__)


@router.post("/runs", response_model=AgentRunOut)
async def create_run(body: AgentRunIn, user: UserDep, svc: ServicesDep) -> AgentRunOut:
    async with svc.db.sessionmaker() as s:
        run = AgentRun(user_id=user.id, department=user.department, message=body.message)
        s.add(run)
        await s.flush()

        async def propose(tool: str, args: dict[str, Any], summary: str) -> str:
            approval = ApprovalRequest(
                run_id=run.id,
                tool=tool,
                args=args,
                summary=summary,
                requested_by=user.id,
                department=user.department,
            )
            s.add(approval)
            await s.flush()
            await record_audit(
                s,
                actor=user.id,
                action="approval.requested",
                status="pending",
                target=approval.id,
                detail={"tool": tool, "args": args},
            )
            return approval.id

        deps = AgentDeps(
            llm=svc.llm,
            tools=svc.tools,
            user=user,
            propose=propose,
            max_steps=svc.settings.agent_max_steps,
        )
        state = await run_agent(deps, body.message)

        run.answer = state.get("answer", "")
        run.steps = state.get("steps", [])
        for step in run.steps:
            if step["type"] == "tool":
                await record_audit(
                    s,
                    actor=user.id,
                    action=f"tool.{step['tool']}",
                    status=step.get("status", "?"),
                    target=run.id,
                    detail={"args": step["args"], "error": step.get("error")},
                )
        await s.commit()
        approval_ids = state.get("approvals", [])

    # Start one durable workflow per approval, after the rows are committed.
    async with svc.db.sessionmaker() as s:
        for approval_id in approval_ids:
            try:
                workflow_id = await svc.orchestrator.start(approval_id)
                if workflow_id:
                    approval = await s.get(ApprovalRequest, approval_id)
                    approval.workflow_id = workflow_id
            except Exception as exc:  # noqa: BLE001 - surface any orchestrator outage on the row
                log.exception("failed to start approval workflow %s", approval_id)
                try:
                    await transition(
                        s,
                        approval_id,
                        expected=ApprovalStatus.PENDING,
                        new=ApprovalStatus.FAILED,
                        error=f"workflow start failed: {exc}",
                    )
                except ApprovalConflict:
                    pass
        await s.commit()
        rows = (
            await s.scalars(select(ApprovalRequest).where(ApprovalRequest.id.in_(approval_ids)))
        ).all()

    return AgentRunOut(
        run_id=run.id,
        answer=run.answer or "",
        approvals=[ApprovalOut.model_validate(r) for r in rows],
        steps=run.steps,
        stopped_reason=state.get("stopped_reason"),
    )
