"""Deterministic rule-based stand-in for Claude.

Speaks the same message/tool-block shapes as the Messages API so the whole loop
(graph, policy, approvals, workflow) runs offline in tests and demos without an API key.
It understands a handful of Thai/English phrasings around orders, stock and transfers.
"""

import itertools
import json
import re
from typing import Any

from aiw.core.llm import LLMTurn, ToolCall

_ids = itertools.count(1)
BRANCHES = ("silom", "bangna", "ladprao")


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            return m["content"]
    return ""


def _history(messages: list[dict[str, Any]]) -> tuple[list[dict], dict[str, dict]]:
    calls: list[dict] = []
    results: dict[str, dict] = {}
    for m in messages:
        if isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    calls.append(b)
                elif b.get("type") == "tool_result":
                    results[b["tool_use_id"]] = {
                        "data": json.loads(b["content"]),
                        "is_error": b.get("is_error", False),
                    }
    return calls, results


class FakeLLM:
    async def respond(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        text = _first_user_text(messages)
        low = text.lower()
        calls, results = _history(messages)
        called = {c["name"] for c in calls}

        def last(name: str) -> list[dict]:
            return [
                results[c["id"]] | {"input": c["input"]}
                for c in calls
                if c["name"] == name and c["id"] in results
            ]

        order_m = re.search(r"ORD-\d+", text, re.I)
        order_id = order_m.group(0).upper() if order_m else None
        wants_transfer = bool(re.search(r"โอน|ย้าย|transfer", low))
        wants_cancel = bool(re.search(r"ยกเลิก|cancel", low))
        wants_adjust = bool(re.search(r"ปรับสต็อก|adjust", low))
        source = next((b for b in BRANCHES if b in low and f"ไป {b}" not in low), None)

        if (wants_transfer or wants_cancel or wants_adjust) and "search_sop" not in called:
            topic = "stock transfer approval" if wants_transfer else "cancel inventory adjustments"
            return self._call("search_sop", {"query": topic})

        if wants_adjust and "adjust_inventory" not in called:
            sku = (re.search(r"SKU-[A-Z]+", text, re.I) or ["SKU-COFFEE"])[0].upper()
            branch = source or "silom"
            delta_m = re.search(r"([+-]?\d+)", text.replace(order_id or "", ""))
            delta = int(delta_m.group(1)) if delta_m else 1
            return self._call(
                "adjust_inventory",
                {"branch": branch, "sku": sku, "delta": delta, "reason": "requested via assistant"},
            )

        if order_id and "get_order" not in called:
            return self._call("get_order", {"order_id": order_id})

        orders = last("get_order")
        order = orders[0]["data"] if orders and not orders[0]["is_error"] else None

        if order and "check_stock" not in called and not wants_cancel:
            return self._multi(
                [
                    ("check_stock", {"sku": i["sku"], "branch": order["branch"]})
                    for i in order["items"]
                ]
            )

        shortages = []
        if order:
            on_hand = {
                r["input"]["sku"]: sum(s["on_hand"] for s in r["data"]["stock"])
                for r in last("check_stock")
                if not r["is_error"]
            }
            shortages = [
                (i["sku"], i["qty"] - on_hand.get(i["sku"], 0))
                for i in order["items"]
                if i["qty"] > on_hand.get(i["sku"], 0)
            ]

        if order and wants_transfer and shortages and "create_stock_transfer" not in called:
            src = source if source and source != order["branch"] else "bangna"
            return self._multi(
                [
                    (
                        "create_stock_transfer",
                        {
                            "sku": sku,
                            "qty": short,
                            "from_branch": src,
                            "to_branch": order["branch"],
                            "order_id": order["id"],
                        },
                    )
                    for sku, short in shortages
                ]
            )

        if order and wants_cancel and "update_order_status" not in called:
            return self._call(
                "update_order_status",
                {"order_id": order["id"], "status": "cancelled", "note": "ลูกค้าขอยกเลิก"},
            )

        return self._final(order_id, order, shortages, calls, results)

    def _call(self, name: str, args: dict[str, Any]) -> LLMTurn:
        return self._multi([(name, args)])

    def _multi(self, items: list[tuple[str, dict[str, Any]]]) -> LLMTurn:
        tcs = [ToolCall(id=f"toolu_fake_{next(_ids)}", name=n, input=a) for n, a in items]
        content = [{"type": "tool_use", "id": t.id, "name": t.name, "input": t.input} for t in tcs]
        return LLMTurn(content=content, tool_calls=tcs, stop_reason="tool_use")

    def _final(self, order_id, order, shortages, calls, results) -> LLMTurn:
        lines: list[str] = []
        if order_id and order is None:
            lines.append(f"ไม่พบข้อมูล order {order_id}")
        if order:
            lines.append(
                f"Order {order['id']} ({order['customer']}) สาขา {order['branch']} สถานะ {order['status']}"
            )
            if shortages:
                lines.append("ของไม่พอ: " + ", ".join(f"{s} ขาด {q}" for s, q in shortages))
            elif any(c["name"] == "check_stock" for c in calls):
                lines.append("สต็อกพอสำหรับทุกรายการ")
        for c in calls:
            r = results.get(c["id"], {})
            data = r.get("data", {})
            if r.get("is_error") and "permission denied" in str(data.get("error", "")):
                lines.append(f"ไม่มีสิทธิ์ใช้ {c['name']}: {data['error']}")
            elif data.get("status") == "pending_approval":
                lines.append(
                    f"ส่งคำขออนุมัติแล้ว ({data['approval_id']}): {data['summary']} — รอ manager อนุมัติ"
                )
        text = "\n".join(lines) or "ระบุเลข order (เช่น ORD-1001) หรือสิ่งที่ต้องการให้ตรวจสอบได้เลยครับ"
        return LLMTurn(content=[{"type": "text", "text": text}], text=text, stop_reason="end_turn")
