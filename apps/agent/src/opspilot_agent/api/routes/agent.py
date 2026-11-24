import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from opspilot_agent.api.deps import Services, ServicesDep
from opspilot_agent.api.schemas import AgentRunIn, AgentRunOut, ApprovalOut, RunCredits
from opspilot_agent.credits import CreditDenied, Hold, MeteredLLM, to_credits
from opspilot_agent.graph import SYSTEM_PROMPT, AgentDeps, AgentState, run_agent
from opspilot_agent.models import AgentRun, ApprovalRequest, RunSandbox
from opspilot_agent.tools import anthropic_tool_schemas
from opspilot_core.api import UserDep
from opspilot_core.audit import record_audit
from opspilot_core.auth import User
from opspilot_core.db import new_id
from opspilot_core.llm import LLMClient

router = APIRouter(prefix="/agent", tags=["agent"])
log = logging.getLogger(__name__)


@router.post("/runs", response_model=AgentRunOut)
async def create_run(body: AgentRunIn, user: UserDep, svc: ServicesDep) -> AgentRunOut:
    run_id = new_id()
    if svc.credits is None:
        return await _run(svc, user, run_id, body.message, svc.llm)

    credits = svc.credits
    minimum = credits.first_call_cost(SYSTEM_PROMPT, body.message, anthropic_tool_schemas())
    try:
        hold = await credits.reserve(user.id, run_id, minimum)
    except CreditDenied as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    metered = MeteredLLM(svc.llm, credits, hold, svc.settings.ollama_max_tokens)
    try:
        out = await _run(svc, user, run_id, body.message, metered)
    finally:
        # If even this fails, the reservation is settled on the account's next message.
        charged = await _settle(svc, hold, metered)
    balance = await credits.balance(user.id)
    out.credits = RunCredits(charged=to_credits(charged), balance=to_credits(balance or 0))
    return out


async def _settle(svc: Services, hold: Hold, metered: MeteredLLM) -> int:
    return await svc.credits.settle(
        hold,
        {
            "model": svc.settings.ollama_model,
            "calls": metered.calls,
            "unmetered_calls": metered.unmetered_calls,
        },
    )


async def _run(svc: Services, user: User, run_id: str, message: str, llm: LLMClient) -> AgentRunOut:
    # The run row is committed first and no transaction stays open while the model works.
    async with svc.db.sessionmaker() as s:
        s.add(AgentRun(id=run_id, user_id=user.id, department=user.department, message=message))
        await s.flush()
        if svc.settings.erp_isolate_runs:
            s.add(RunSandbox(run_id=run_id))
        await s.commit()

    proposals: list[ApprovalRequest] = []

    async def propose(tool: str, args: dict[str, Any], summary: str) -> str:
        # Saved together with the answer below: a run that dies leaves no half-made requests.
        approval = ApprovalRequest(
            id=new_id(),
            run_id=run_id,
            tool=tool,
            args=args,
            summary=summary,
            requested_by=user.id,
            department=user.department,
        )
        proposals.append(approval)
        return approval.id

    deps = AgentDeps(
        llm=llm,
        tools=svc.tools,
        user=user,
        propose=propose,
        max_steps=svc.settings.agent_max_steps,
    )
    try:
        async with asyncio.timeout(svc.settings.agent_run_timeout_s):
            with svc.tools.erp.sandbox(run_id if svc.settings.erp_isolate_runs else None):
                state: AgentState = await run_agent(deps, message)
    except TimeoutError as exc:
        log.warning("agent run %s timed out", run_id)
        raise HTTPException(504, "Agent ใช้เวลานานเกินกำหนด กรุณาลองใหม่") from exc

    async with svc.db.sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        run.answer = state.get("answer", "")
        run.steps = state.get("steps", [])
        for approval in proposals:
            s.add(approval)
            await s.flush()
            await record_audit(
                s,
                actor=user.id,
                action="approval.requested",
                status="pending",
                target=approval.id,
                detail={"tool": approval.tool, "args": approval.args},
            )
        for step in run.steps:
            if step["type"] == "tool":
                await record_audit(
                    s,
                    actor=user.id,
                    action=f"tool.{step['tool']}",
                    status=step.get("status", "?"),
                    target=run_id,
                    detail={"args": step["args"], "error": step.get("error")},
                )
        await s.commit()

    return AgentRunOut(
        run_id=run_id,
        answer=run.answer or "",
        approvals=[ApprovalOut.model_validate(a) for a in proposals],
        steps=run.steps,
        stopped_reason=state.get("stopped_reason"),
    )
