from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from opspilot_agent.api.deps import ServicesDep
from opspilot_agent.api.schemas import AgentRunIn, AgentRunOut, ApprovalOut
from opspilot_agent.graph import AgentDeps, run_agent
from opspilot_agent.models import AgentRun, ApprovalRequest, RunSandbox
from opspilot_core.api import UserDep
from opspilot_core.audit import record_audit

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/runs", response_model=AgentRunOut)
async def create_run(body: AgentRunIn, user: UserDep, svc: ServicesDep) -> AgentRunOut:
    async with svc.db.sessionmaker() as s:
        run = AgentRun(user_id=user.id, department=user.department, message=body.message)
        s.add(run)
        await s.flush()
        if svc.settings.erp_isolate_runs:
            s.add(RunSandbox(run_id=run.id))

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
        with svc.tools.erp.sandbox(run.id if svc.settings.erp_isolate_runs else None):
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
