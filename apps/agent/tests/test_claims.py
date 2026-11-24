"""An answer must not tell the user about a request that does not exist."""

import pytest

from opspilot_agent.graph import (
    CLAIMS_REQUEST,
    CORRECTION,
    MENTIONS_REQUEST,
    NO_REQUEST_NOTICE,
    NO_REQUEST_NOTICE_EN,
    AgentDeps,
    run_agent,
)
from opspilot_agent.tools import ToolContext
from opspilot_core.auth import Role, User
from opspilot_core.llm import LLMTurn, ToolCall

# Mostly real GLM answers, shortened. The first one invented its approval ids.
CLAIMS = [
    "Proposed changes (both need manager approval before they run):\n- Stock transfer: 7 "
    "SKU-COFFEE. Approval id: TR-3319",
    "สิ่งที่เสนอไปแล้ว (รอ manager อนุมัติ):\n1. โอนสต็อก SKU-COFFEE x7",
    "ส่งคำขอยกเลิก ORD-1002 เรียบร้อยแล้ว (รหัสอนุมัติ: a38615d3...)",
    "Both changes are awaiting manager approval.",
    "ต้องตั้งออร์เดอร์เป็น on_hold ด้วย ผมจึงเสนอ 2 รายการ:\n1. โอนสต็อก SKU-COFFEE 7 ชิ้น",
    "I am submitting two approval requests for a manager:\n- Stock transfer: 7 units",
    "ฉันจึงยื่นคำขอเปลี่ยนสถานะเป็น cancelled พร้อมหมายเหตุว่า ลูกค้าสั่งซื้อผิดพลาด แล้ว",
    "I have submitted the transfer request.",
    "The transfer has been requested.",
]
# Real answers that only explain or offer something.
HONEST = [
    "ของพอครับ อยากให้ผมเปลี่ยนสถานะเป็น confirmed ไหมครับ (จะส่งคำขอให้ผู้จัดการอนุมัติก่อน)",
    "ระบบปฏิเสธคำขอ เพราะบทบาทของคุณเป็น staff",
    "โอนสต็อกทุกครั้งต้องได้รับการอนุมัติจากผู้จัดการ และผู้ขอไม่สามารถอนุมัติเองได้",
    "เมื่อเสนอการโอนแล้ว ตาม SOP ต้องเปลี่ยนสถานะออเดอร์เป็น on_hold",
    "บอกจำนวนที่ต้องการได้เลยครับ แล้วผมจะเสนอให้",
    "A stock transfer needs manager approval before it runs.",
    "ORD-1002 is pending. silom has 20 on hand, which is enough.",
]
TRANSFER = {
    "sku": "SKU-COFFEE",
    "qty": 7,
    "from_branch": "bangna",
    "to_branch": "silom",
    "order_id": "ORD-1001",
}


@pytest.mark.parametrize("text", CLAIMS)
def test_claims_are_recognised(text):
    assert MENTIONS_REQUEST.search(text) and CLAIMS_REQUEST.search(text)


@pytest.mark.parametrize("text", HONEST)
def test_honest_answers_are_left_alone(text):
    assert not CLAIMS_REQUEST.search(text)


class Scripted:
    def __init__(self, *turns) -> None:
        self.turns, self.seen = list(turns), []

    async def respond(self, *, system, messages, tools) -> LLMTurn:
        self.seen.append(messages)
        return self.turns.pop(0)


def say(text) -> LLMTurn:
    return LLMTurn(content=[{"type": "text", "text": text}], text=text, stop_reason="end_turn")


def propose() -> LLMTurn:
    block = {"type": "tool_use", "id": "t1", "name": "create_stock_transfer", "input": TRANSFER}
    return LLMTurn(content=[block], tool_calls=[ToolCall("t1", block["name"], TRANSFER)])


async def run(*turns, max_steps=8):
    llm, made = Scripted(*turns), []

    async def sink(tool, args, summary):
        made.append(tool)
        return "real-id"

    deps = AgentDeps(
        llm=llm,
        tools=ToolContext(erp=None),
        user=User(id="s", role=Role.STAFF, department="ops"),
        propose=sink,
        max_steps=max_steps,
    )
    return await run_agent(deps, "move 7 coffee"), made, llm


async def test_false_claim_is_sent_back_and_the_request_is_really_made():
    state, made, llm = await run(say(CLAIMS[0]), propose(), say("Proposed changes: one transfer."))
    assert made == ["create_stock_transfer"] and state["approvals"] == ["real-id"]
    assert llm.seen[1][-1] == {"role": "user", "content": CORRECTION}
    assert "note" not in state["answer"] and "stopped_reason" not in state
    assert [s["type"] for s in state["steps"]].count("correction") == 1


async def test_wording_nobody_listed_is_still_checked():
    odd = "The manager just needs to approve it and the coffee moves."
    assert not CLAIMS_REQUEST.search(odd)
    state, made, _ = await run(say(odd), propose(), say("Done: one request."))
    assert made == ["create_stock_transfer"]


async def test_honest_answer_about_approvals_survives_the_check():
    rule = HONEST[2]
    state, made, llm = await run(say(rule), say(rule))
    assert state["answer"] == rule and made == [] and len(llm.seen) == 2
    assert "stopped_reason" not in state


async def test_corrected_answer_replaces_the_false_one():
    state, made, _ = await run(say(CLAIMS[0]), say("silom is short by 7."))
    assert made == [] and state["answer"] == "silom is short by 7."


async def test_repeated_false_claim_gets_a_system_notice_not_a_loop():
    state, made, llm = await run(say(CLAIMS[0]), say(CLAIMS[3]))
    assert made == [] and len(llm.seen) == 2
    assert state["answer"].endswith(NO_REQUEST_NOTICE_EN)  # the answer is in English
    assert state["stopped_reason"] == "unconfirmed_claim"


async def test_no_extra_call_beyond_the_step_limit():
    state, _, llm = await run(say(CLAIMS[1]), max_steps=1)
    assert len(llm.seen) == 1 and state["answer"].endswith(NO_REQUEST_NOTICE)


async def test_true_claims_and_plain_answers_cost_no_extra_call():
    state, made, llm = await run(propose(), say(CLAIMS[0]))
    assert made and len(llm.seen) == 2 and "note" not in state["answer"]
    _, _, llm = await run(say(HONEST[-1]))
    assert len(llm.seen) == 1


async def test_demo_planner_is_not_affected(api):
    from opspilot_core.testing import token

    h = await token(api, "s1", "staff")
    r = await api.post(
        "/agent/runs", json={"message": "เช็ค ORD-1001 ถ้าของไม่พอโอนจาก bangna"}, headers=h
    )
    assert len(r.json()["approvals"]) == 1 and r.json()["stopped_reason"] is None
