"""LangGraph agent loop: model -> tools -> model ... until the model stops calling tools.

The tools node is where control lives: schema validation, role policy, and turning every
write into an approval proposal instead of executing it.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from opspilot_agent.credits import CreditExhausted
from opspilot_agent.erp_client import ErpError
from opspilot_agent.policy import Decision, authorize_tool
from opspilot_agent.tools import TOOLS, ToolContext, anthropic_tool_schemas
from opspilot_core.auth import User
from opspilot_core.llm import LLMClient, LLMError

SYSTEM_PROMPT = """You are an operations assistant for a retail company with several branches.
You help staff check orders and stock and prepare changes in the ERP/POS system.

How to work:
- Look things up with the read tools before proposing any change. Check the SOPs with
  search_knowledge when a change has rules (transfers, cancellations, adjustments).
- Write tools (create_stock_transfer, update_order_status, adjust_inventory) do not run
  immediately: they create an approval request for a manager. Say so plainly in your answer
  and include the approval id the tool returns.
- If a tool returns a permission error, tell the user what they are not allowed to do; do not
  try to reach the same result through another tool.
- Only use facts returned by tools. If information is missing, say what is missing.
- Reply in the user's language, briefly: what you found, what you proposed, what happens next."""

ProposalSink = Callable[[str, dict[str, Any], str], Awaitable[str]]


@dataclass
class AgentDeps:
    llm: LLMClient
    tools: ToolContext
    user: User
    propose: ProposalSink  # (tool, args, summary) -> approval_id
    max_steps: int = 8


class AgentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    pending_calls: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    approvals: list[str]
    answer: str
    iterations: int
    stopped_reason: str


def _result(call_id: str, payload: Any, is_error: bool = False) -> dict[str, Any]:
    block = {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": json.dumps(payload, ensure_ascii=False, default=str),
    }
    if is_error:
        block["is_error"] = True
    return block


def build_agent_graph(deps: AgentDeps):
    schemas = anthropic_tool_schemas()

    async def call_model(state: AgentState) -> AgentState:
        iterations = state.get("iterations", 0) + 1
        try:
            turn = await deps.llm.respond(
                system=SYSTEM_PROMPT, messages=state["messages"], tools=schemas
            )
        except LLMError as exc:
            exhausted = isinstance(exc, CreditExhausted)
            answer = (
                "หยุดก่อนทำครบทุกขั้น เพราะถึงเพดานเครดิตของข้อความนี้"
                if exhausted
                else "ระบบ AI ไม่พร้อมใช้งานชั่วคราว กรุณาลองใหม่"
            )
            return {
                "pending_calls": [],
                "iterations": iterations,
                "answer": answer,
                "stopped_reason": "credit_limit" if exhausted else f"llm_error: {exc}",
                "steps": state.get("steps", [])
                + [{"type": "llm_error", "error": str(exc), "usage": exc.usage}],
            }
        messages = state["messages"]
        if turn.content:
            messages = messages + [{"role": "assistant", "content": turn.content}]
        step = {
            "type": "llm",
            "text": turn.text,
            "tool_calls": [{"name": c.name, "input": c.input} for c in turn.tool_calls],
            "stop_reason": turn.stop_reason,
            "usage": turn.usage,
        }
        return {
            "messages": messages,
            "pending_calls": [asdict(c) for c in turn.tool_calls],
            "answer": turn.text,
            "iterations": iterations,
            "steps": state.get("steps", []) + [step],
        }

    async def run_tools(state: AgentState) -> AgentState:
        results: list[dict[str, Any]] = []
        steps = list(state.get("steps", []))
        approvals = list(state.get("approvals", []))

        for call in state.get("pending_calls", []):
            name, raw_args, call_id = call["name"], call["input"], call["id"]
            step: dict[str, Any] = {"type": "tool", "tool": name, "args": raw_args}
            spec = TOOLS.get(name)
            if spec is None:
                step |= {"status": "error", "error": "unknown tool"}
                results.append(_result(call_id, {"error": f"unknown tool {name}"}, True))
                steps.append(step)
                continue
            try:
                args = spec.args_model.model_validate(raw_args)
            except ValidationError as exc:
                errors = exc.errors(include_url=False, include_context=False)
                step |= {"status": "invalid_args", "error": errors}
                results.append(
                    _result(call_id, {"error": "invalid arguments", "detail": errors}, True)
                )
                steps.append(step)
                continue

            policy = authorize_tool(deps.user, spec)
            step["decision"] = policy.decision.value
            if policy.decision is Decision.DENY:
                step |= {"status": "denied", "error": policy.reason}
                results.append(
                    _result(call_id, {"error": f"permission denied: {policy.reason}"}, True)
                )
            elif policy.decision is Decision.NEEDS_APPROVAL:
                summary = spec.summarize(args) if spec.summarize else name
                approval_id = await deps.propose(name, args.model_dump(), summary)
                approvals.append(approval_id)
                step |= {"status": "pending_approval", "approval_id": approval_id}
                results.append(
                    _result(
                        call_id,
                        {
                            "status": "pending_approval",
                            "approval_id": approval_id,
                            "summary": summary,
                        },
                    )
                )
            else:
                try:
                    data = await spec.read(deps.tools, args, deps.user)  # type: ignore[misc]
                    step |= {"status": "ok"}
                    results.append(_result(call_id, data))
                except ErpError as exc:
                    step |= {"status": "error", "error": str(exc)}
                    results.append(
                        _result(call_id, {"error": exc.detail, "status": exc.status}, True)
                    )
            steps.append(step)

        return {
            "messages": state["messages"] + [{"role": "user", "content": results}],
            "pending_calls": [],
            "steps": steps,
            "approvals": approvals,
        }

    def route(state: AgentState) -> str:
        if not state.get("pending_calls"):
            return END
        if state.get("iterations", 0) >= deps.max_steps:
            return "max_steps"
        return "tools"

    async def max_steps(state: AgentState) -> AgentState:
        return {
            "pending_calls": [],
            "stopped_reason": "max_steps",
            "answer": (state.get("answer") or "") + "\n(หยุดเพราะใช้จำนวนขั้นตอนครบกำหนด)",
        }

    g = StateGraph(AgentState)
    g.add_node("agent", call_model)
    g.add_node("tools", run_tools)
    g.add_node("max_steps", max_steps)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route, {"tools": "tools", "max_steps": "max_steps", END: END})
    g.add_edge("tools", "agent")
    g.add_edge("max_steps", END)
    return g.compile()


async def run_agent(deps: AgentDeps, message: str) -> AgentState:
    graph = build_agent_graph(deps)
    return await graph.ainvoke(
        {"messages": [{"role": "user", "content": message}], "steps": [], "approvals": []},
        {"recursion_limit": deps.max_steps * 2 + 4},
    )
