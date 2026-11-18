"""Mock ERP/POS service.

In-memory orders, inventory, vendors, purchase orders and AP invoices, Idempotency-Key support on writes, and chaos knobs
(CHAOS_ERROR_RATE, CHAOS_LATENCY_MS) to exercise retries and failure handling.
"""

import asyncio
import copy
import os
import random
import uuid
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

SEED_ORDERS = {
    "ORD-1001": {
        "id": "ORD-1001",
        "customer": "Cafe Amazon Silom",
        "branch": "silom",
        "status": "pending",
        "items": [{"sku": "SKU-COFFEE", "qty": 10}, {"sku": "SKU-MILK", "qty": 4}],
    },
    "ORD-1002": {
        "id": "ORD-1002",
        "customer": "Bangna Office",
        "branch": "bangna",
        "status": "pending",
        "items": [{"sku": "SKU-MILK", "qty": 2}],
    },
}

SEED_INVENTORY = {
    ("silom", "SKU-COFFEE"): 3,
    ("silom", "SKU-MILK"): 20,
    ("bangna", "SKU-COFFEE"): 50,
    ("bangna", "SKU-MILK"): 15,
    ("ladprao", "SKU-COFFEE"): 8,
}

SEED_VENDORS = {
    "0105550123451": {
        "tax_id": "0105550123451",
        "name": "บริษัท สยามคอฟฟี่ จำกัด",
        "vendor_id": "V-001",
    },
    "0105560987653": {
        "tax_id": "0105560987653",
        "name": "Thai Dairy Supply Co., Ltd.",
        "vendor_id": "V-002",
    },
}

SEED_POS = {
    "PO-2001": {
        "id": "PO-2001",
        "vendor_tax_id": "0105550123451",
        "branch": "silom",
        "status": "open",
        "lines": [{"sku": "SKU-COFFEE", "qty": 20, "unit_price": 450.0}],
        "total_before_vat": 9000.0,
    },
    "PO-2002": {
        "id": "PO-2002",
        "vendor_tax_id": "0105560987653",
        "branch": "bangna",
        "status": "open",
        "lines": [{"sku": "SKU-MILK", "qty": 30, "unit_price": 85.0}],
        "total_before_vat": 2550.0,
    },
}

OrderStatus = Literal["pending", "confirmed", "on_hold", "cancelled", "fulfilled"]


class StockTransferIn(BaseModel):
    sku: str
    qty: int = Field(gt=0)
    from_branch: str
    to_branch: str
    order_id: str | None = None


class OrderPatchIn(BaseModel):
    status: OrderStatus
    note: str | None = None


class InventoryAdjustIn(BaseModel):
    branch: str
    sku: str
    delta: int
    reason: str


class ApInvoiceLineIn(BaseModel):
    description: str
    sku: str | None = None
    qty: float
    unit_price: float
    amount: float


class ApInvoiceIn(BaseModel):
    vendor_tax_id: str
    invoice_number: str
    invoice_date: str
    due_date: str | None = None
    po_number: str | None = None
    currency: str = "THB"
    subtotal: float
    vat_amount: float
    total: float
    lines: list[ApInvoiceLineIn]
    source_document_id: str


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.orders = copy.deepcopy(SEED_ORDERS)
        self.inventory = dict(SEED_INVENTORY)
        self.transfers: list[dict] = []
        self.vendors = copy.deepcopy(SEED_VENDORS)
        self.purchase_orders = copy.deepcopy(SEED_POS)
        self.ap_invoices: list[dict] = []
        self.idempotency: dict[str, dict] = {}
        self.write_count = 0


def create_app(error_rate: float | None = None, latency_ms: int | None = None) -> FastAPI:
    app = FastAPI(title="Mock ERP/POS")
    store = Store()
    app.state.store = store
    app.state.error_rate = (
        error_rate if error_rate is not None else float(os.getenv("CHAOS_ERROR_RATE", "0"))
    )
    app.state.latency_ms = (
        latency_ms if latency_ms is not None else int(os.getenv("CHAOS_LATENCY_MS", "0"))
    )

    @app.middleware("http")
    async def chaos(request: Request, call_next):
        if request.url.path.startswith("/_admin") or request.url.path == "/health":
            return await call_next(request)
        if app.state.latency_ms:
            await asyncio.sleep(random.uniform(0, app.state.latency_ms) / 1000)
        if random.random() < app.state.error_rate:
            return JSONResponse({"detail": "chaos: upstream unavailable"}, status_code=503)
        return await call_next(request)

    def replay(key: str | None, scope: str) -> dict | None:
        return store.idempotency.get(f"{scope}:{key}") if key else None

    def remember(key: str | None, scope: str, body: dict) -> None:
        if key:
            store.idempotency[f"{scope}:{key}"] = body

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str):
        order = store.orders.get(order_id)
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        return order

    @app.get("/inventory")
    async def get_inventory(sku: str, branch: str | None = None):
        rows = [
            {"branch": b, "sku": s, "on_hand": q}
            for (b, s), q in store.inventory.items()
            if s == sku and (branch is None or b == branch)
        ]
        if branch and not rows:
            rows = [{"branch": branch, "sku": sku, "on_hand": 0}]
        return {"sku": sku, "stock": rows}

    @app.post("/stock-transfers", status_code=201)
    async def create_transfer(
        body: StockTransferIn, idempotency_key: str | None = Header(default=None)
    ):
        if cached := replay(idempotency_key, "transfer"):
            return cached
        if body.from_branch == body.to_branch:
            raise HTTPException(422, "from_branch and to_branch must differ")
        available = store.inventory.get((body.from_branch, body.sku), 0)
        if available < body.qty:
            raise HTTPException(409, f"insufficient stock at {body.from_branch}: {available}")
        store.inventory[(body.from_branch, body.sku)] = available - body.qty
        key = (body.to_branch, body.sku)
        store.inventory[key] = store.inventory.get(key, 0) + body.qty
        transfer = {"id": f"TRF-{uuid.uuid4().hex[:8]}", "status": "completed", **body.model_dump()}
        store.transfers.append(transfer)
        store.write_count += 1
        remember(idempotency_key, "transfer", transfer)
        return transfer

    @app.patch("/orders/{order_id}")
    async def patch_order(
        order_id: str, body: OrderPatchIn, idempotency_key: str | None = Header(default=None)
    ):
        if cached := replay(idempotency_key, f"order:{order_id}"):
            return cached
        order = store.orders.get(order_id)
        if not order:
            raise HTTPException(404, f"order {order_id} not found")
        if order["status"] in ("cancelled", "fulfilled"):
            raise HTTPException(409, f"order is already {order['status']}")
        order["status"] = body.status
        if body.note:
            order["note"] = body.note
        store.write_count += 1
        result = copy.deepcopy(order)
        remember(idempotency_key, f"order:{order_id}", result)
        return result

    @app.post("/inventory/adjustments", status_code=201)
    async def adjust_inventory(
        body: InventoryAdjustIn, idempotency_key: str | None = Header(default=None)
    ):
        if cached := replay(idempotency_key, "adjust"):
            return cached
        key = (body.branch, body.sku)
        new_qty = store.inventory.get(key, 0) + body.delta
        if new_qty < 0:
            raise HTTPException(409, "adjustment would make stock negative")
        store.inventory[key] = new_qty
        store.write_count += 1
        result = {"branch": body.branch, "sku": body.sku, "on_hand": new_qty, "reason": body.reason}
        remember(idempotency_key, "adjust", result)
        return result

    @app.get("/vendors/{tax_id}")
    async def get_vendor(tax_id: str):
        vendor = store.vendors.get(tax_id)
        if not vendor:
            raise HTTPException(404, f"vendor {tax_id} not found")
        return vendor

    @app.get("/purchase-orders/{po_id}")
    async def get_purchase_order(po_id: str):
        po = store.purchase_orders.get(po_id)
        if not po:
            raise HTTPException(404, f"purchase order {po_id} not found")
        return po

    @app.get("/ap-invoices")
    async def find_ap_invoices(vendor_tax_id: str, invoice_number: str):
        return [
            i
            for i in store.ap_invoices
            if i["vendor_tax_id"] == vendor_tax_id and i["invoice_number"] == invoice_number
        ]

    @app.post("/ap-invoices", status_code=201)
    async def create_ap_invoice(
        body: ApInvoiceIn, idempotency_key: str | None = Header(default=None)
    ):
        if cached := replay(idempotency_key, "ap-invoice"):
            return cached
        if body.vendor_tax_id not in store.vendors:
            raise HTTPException(422, f"unknown vendor {body.vendor_tax_id}")
        if any(
            i["vendor_tax_id"] == body.vendor_tax_id and i["invoice_number"] == body.invoice_number
            for i in store.ap_invoices
        ):
            raise HTTPException(409, "duplicate invoice for this vendor")
        invoice = {"id": f"AP-{uuid.uuid4().hex[:8]}", "status": "posted", **body.model_dump()}
        store.ap_invoices.append(invoice)
        if body.po_number in store.purchase_orders:
            store.purchase_orders[body.po_number]["status"] = "invoiced"
        store.write_count += 1
        remember(idempotency_key, "ap-invoice", invoice)
        return invoice

    @app.post("/_admin/reset")
    async def reset():
        store.reset()
        return {"ok": True}

    return app


app = create_app()
