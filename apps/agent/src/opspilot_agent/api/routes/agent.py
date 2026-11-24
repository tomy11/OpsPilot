import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_agent import conversations
from opspilot_agent.api.deps import Services, ServicesDep
from opspilot_agent.api.schemas import (
    AgentRunIn,
    AgentRunOut,
    ApprovalOut,
    ConversationOut,
    RunCredits,
    TranscriptOut,
    TurnOut,
)
from opspilot_agent.conversations import Memory
from opspilot_agent.credits import CreditDenied, Hold, MeteredLLM, to_credits
from opspilot_agent.graph import SYSTEM_PROMPT, AgentDeps, AgentState, run_agent
from opspilot_agent.models import (
    AgentRun,
    ApprovalRequest,
    ApprovalStatus,
    Conversation,
    RunSandbox,
)
from opspilot_agent.tools import anthropic_tool_schemas
from opspilot_core.api import SessionDep, UserDep
from opspilot_core.audit import record_audit
from opspilot_core.auth import User
from opspilot_core.db import new_id
from opspilot_core.llm import LLMClient

router = APIRouter(prefix="/agent", tags=["agent"])
log = logging.getLogger(__name__)


@dataclass
class Chat:
    id: str
    new: bool
    memory: Memory = field(default_factory=Memory)


@router.post("/runs", response_model=AgentRunOut)
async def create_run(body: AgentRunIn, user: UserDep, svc: ServicesDep) -> AgentRunOut:
    run_id = new_id()
    chat = Chat(body.conversation_id or new_id(), new=body.conversation_id is None)
    if not chat.new:
        async with svc.db.sessionmaker() as s:
            if await conversations.owned(s, user, chat.id) is None:
                raise HTTPException(404, "ไม่พบแชตนี้")
            chat.memory = await conversations.remember(s, chat.id, svc.settings)
    if svc.credits is None:
        return await _run(svc, user, run_id, body.message, chat, svc.llm)

    credits = svc.credits
    minimum = credits.first_call_cost(
        SYSTEM_PROMPT + chat.memory.context,
        [*chat.memory.messages, {"role": "user", "content": body.message}],
        anthropic_tool_schemas(),
    )
    try:
        hold = await credits.reserve(user.id, run_id, minimum)
    except CreditDenied as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    metered = MeteredLLM(svc.llm, credits, hold, svc.settings.ollama_max_tokens)
    try:
        out = await _run(svc, user, run_id, body.message, chat, metered)
    finally:
        # If even this fails, the reservation is settled on the account's next message.
        charged = await _settle(svc, hold, metered)
    balance = await credits.balance(user.id)
    out.credits = RunCredits(charged=to_credits(charged), balance=to_credits(balance or 0))
    return out


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(user: UserDep, s: SessionDep):
    return await conversations.recent(s, user)


async def _owned(s: AsyncSession, user: User, conversation_id: str) -> Conversation:
    c = await conversations.owned(s, user, conversation_id)
    if c is None:
        raise HTTPException(404, "ไม่พบแชตนี้")
    return c


@router.get("/conversations/{conversation_id}", response_model=TranscriptOut)
async def get_conversation(conversation_id: str, user: UserDep, s: SessionDep) -> TranscriptOut:
    c = await _owned(s, user, conversation_id)
    return TranscriptOut(
        id=c.id,
        title=c.title,
        turns=[
            TurnOut(
                prompt=run.message,
                run=AgentRunOut(
                    run_id=run.id,
                    conversation_id=c.id,
                    answer=run.answer or "",
                    approvals=[ApprovalOut.model_validate(a) for a in requests],
                    steps=run.steps,
                ),
            )
            for run, requests in await conversations.transcript(s, c.id)
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, user: UserDep, s: SessionDep) -> None:
    c = await _owned(s, user, conversation_id)
    await conversations.delete(s, c.id)
    await record_audit(s, actor=user.id, action="conversation.deleted", status="ok", target=c.id)
    await s.commit()


async def _settle(svc: Services, hold: Hold, metered: MeteredLLM) -> int:
    return await svc.credits.settle(
        hold,
        {
            "model": svc.settings.ollama_model,
            "calls": metered.calls,
            "unmetered_calls": metered.unmetered_calls,
        },
    )


async def _run(
    svc: Services, user: User, run_id: str, message: str, chat: Chat, llm: LLMClient
) -> AgentRunOut:
    # The run row is committed first and no transaction stays open while the model works.
    async with svc.db.sessionmaker() as s:
        s.add(AgentRun(id=run_id, user_id=user.id, department=user.department, message=message))
        await s.flush()
        if svc.settings.erp_isolate_runs:
            s.add(RunSandbox(run_id=run_id))
        await conversations.add_run(s, user, chat.id, run_id, message, new=chat.new)
        await s.commit()

    proposals: list[ApprovalRequest] = []
    repeated: list[ApprovalRequest] = []

    async def propose(tool: str, args: dict[str, Any], summary: str) -> str:
        # The same change again ("yes, do it") is the request that is already waiting.
        for earlier in [*proposals, *chat.memory.requests]:
            waiting = earlier in proposals or earlier.status is ApprovalStatus.PENDING
            if waiting and earlier.tool == tool and earlier.args == args:
                if earlier not in proposals and earlier not in repeated:
                    repeated.append(earlier)
                return earlier.id
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
        history=chat.memory.messages,
        context=chat.memory.context,
    )
    try:
        async with asyncio.timeout(svc.settings.agent_run_timeout_s):
            with svc.tools.erp.sandbox(chat.id if svc.settings.erp_isolate_runs else None):
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
        conversation_id=chat.id,
        answer=run.answer or "",
        approvals=[ApprovalOut.model_validate(a) for a in [*proposals, *repeated]],
        steps=run.steps,
        stopped_reason=state.get("stopped_reason"),
    )
