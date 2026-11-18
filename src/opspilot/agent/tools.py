"""Tool registry.

READ tools run immediately. WRITE tools are never executed by the agent loop: they become
approval proposals, and only the approval workflow calls `execute_write` after a human approves.
"""

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opspilot.agent.erp_client import ErpClient
from opspilot.core.auth import Role, User
from opspilot.knowledge.service import KnowledgeService


class ToolKind(enum.StrEnum):
    READ = "read"
    WRITE = "write"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetOrderArgs(_Args):
    order_id: str = Field(description="Order id, e.g. ORD-1001")


class CheckStockArgs(_Args):
    sku: str = Field(description="SKU code, e.g. SKU-COFFEE")
    branch: str = Field(description="Branch code, or 'all' for every branch")


class SearchKnowledgeArgs(_Args):
    query: str = Field(description="What to look up in internal SOPs and policies")


class CreateStockTransferArgs(_Args):
    sku: str
    qty: int = Field(gt=0)
    from_branch: str
    to_branch: str
    order_id: str = Field(description="Order this transfer serves")


class UpdateOrderStatusArgs(_Args):
    order_id: str
    status: Literal["confirmed", "on_hold", "cancelled"]
    note: str = Field(description="Reason, visible to the customer")


class AdjustInventoryArgs(_Args):
    branch: str
    sku: str
    delta: int = Field(description="Positive to add, negative to remove")
    reason: str


@dataclass
class ToolContext:
    erp: ErpClient
    knowledge: KnowledgeService | None = None


ReadHandler = Callable[[ToolContext, Any, User], Awaitable[dict[str, Any]]]
WriteHandler = Callable[[ToolContext, Any, str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[_Args]
    kind: ToolKind
    min_role: Role
    read: ReadHandler | None = None
    write: WriteHandler | None = None
    summarize: Callable[[Any], str] | None = None

    def anthropic_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties", {}).keys())
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
            "strict": True,
        }


async def _get_order(ctx: ToolContext, a: GetOrderArgs, user: User) -> dict[str, Any]:
    return await ctx.erp.get_order(a.order_id)


async def _check_stock(ctx: ToolContext, a: CheckStockArgs, user: User) -> dict[str, Any]:
    return await ctx.erp.get_inventory(a.sku, None if a.branch == "all" else a.branch)


async def _search_knowledge(ctx: ToolContext, a: SearchKnowledgeArgs, user: User) -> dict[str, Any]:
    if ctx.knowledge is None:
        return {"results": [], "error": "knowledge base unavailable"}
    # Same access filter as /knowledge/search: the agent can only read what the user can read.
    hits = await ctx.knowledge.search_as(user, a.query, k=3)
    return {
        "results": [
            {"title": h.title, "section": h.heading, "source": h.source, "text": h.text}
            for h in hits
        ]
    }


async def _create_transfer(ctx: ToolContext, a: CreateStockTransferArgs, key: str) -> dict:
    return await ctx.erp.create_stock_transfer(a.model_dump(), key)


async def _update_order(ctx: ToolContext, a: UpdateOrderStatusArgs, key: str) -> dict:
    return await ctx.erp.update_order(a.order_id, {"status": a.status, "note": a.note}, key)


async def _adjust_inventory(ctx: ToolContext, a: AdjustInventoryArgs, key: str) -> dict:
    return await ctx.erp.adjust_inventory(a.model_dump(), key)


TOOLS: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec(
            "get_order",
            "Fetch an order (customer, branch, status, line items) from the ERP.",
            GetOrderArgs,
            ToolKind.READ,
            Role.STAFF,
            read=_get_order,
        ),
        ToolSpec(
            "check_stock",
            "Get on-hand stock for a SKU at one branch or all branches.",
            CheckStockArgs,
            ToolKind.READ,
            Role.STAFF,
            read=_check_stock,
        ),
        ToolSpec(
            "search_knowledge",
            "Search internal SOPs and policies the user may access. Use before proposing a "
            "change to check the rules.",
            SearchKnowledgeArgs,
            ToolKind.READ,
            Role.STAFF,
            read=_search_knowledge,
        ),
        ToolSpec(
            "create_stock_transfer",
            "Propose moving stock between branches. Requires manager approval before it runs.",
            CreateStockTransferArgs,
            ToolKind.WRITE,
            Role.STAFF,
            write=_create_transfer,
            summarize=lambda a: (
                f"โอน {a.sku} x{a.qty} จาก {a.from_branch} ไป {a.to_branch} (order {a.order_id})"
            ),
        ),
        ToolSpec(
            "update_order_status",
            "Propose changing an order status. Requires manager approval before it runs.",
            UpdateOrderStatusArgs,
            ToolKind.WRITE,
            Role.STAFF,
            write=_update_order,
            summarize=lambda a: f"เปลี่ยนสถานะ {a.order_id} เป็น {a.status}: {a.note}",
        ),
        ToolSpec(
            "adjust_inventory",
            "Propose a manual inventory adjustment. Admin only; requires approval.",
            AdjustInventoryArgs,
            ToolKind.WRITE,
            Role.ADMIN,
            write=_adjust_inventory,
            summarize=lambda a: f"ปรับสต็อก {a.sku}@{a.branch} {a.delta:+d}: {a.reason}",
        ),
    ]
}


def anthropic_tool_schemas() -> list[dict[str, Any]]:
    return [t.anthropic_schema() for t in TOOLS.values()]


async def execute_write(
    ctx: ToolContext, tool: str, args: dict[str, Any], idempotency_key: str
) -> dict[str, Any]:
    spec = TOOLS[tool]
    if spec.kind is not ToolKind.WRITE or spec.write is None:
        raise ValueError(f"{tool} is not a write tool")
    return await spec.write(ctx, spec.args_model.model_validate(args), idempotency_key)
