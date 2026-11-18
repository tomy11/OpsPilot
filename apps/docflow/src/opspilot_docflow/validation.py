"""Deterministic checks on an extraction. The LLM reads; this code decides.

`validate` is a pure function: the caller gathers ERP context (vendor master, PO, duplicates)
first, so every rule is unit-testable without HTTP. Errors block approval; warnings send the
document to a human but a reviewer may still approve it.
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

from opspilot_docflow.schema import Extraction

Severity = Literal["error", "warning"]
TOLERANCE = 0.01  # one satang


@dataclass(frozen=True)
class Issue:
    code: str
    severity: Severity
    field: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ErpContext:
    vendor: dict[str, Any] | None = None
    purchase_order: dict[str, Any] | None = None
    duplicate_refs: list[str] | None = None  # ERP invoice ids or other DocFlow document ids
    failed: set[str] = field(default_factory=set)  # lookups the ERP could not answer


def thai_tax_id_valid(tax_id: str) -> bool:
    """13 digits with the Revenue Department mod-11 check digit."""
    if not re.fullmatch(r"\d{13}", tax_id):
        return False
    total = sum(int(tax_id[i]) * (13 - i) for i in range(12))
    return (11 - total % 11) % 10 == int(tax_id[12])


def _norm(name: str) -> str:
    name = name.lower()
    for noise in (
        "บริษัท",
        "จำกัด",
        "(มหาชน)",
        "co.,",
        "co.",
        "ltd.",
        "ltd",
        "company",
        "limited",
        ",",
    ):
        name = name.replace(noise, " ")
    return " ".join(name.split())


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def validate(
    x: Extraction, ctx: ErpContext, *, vat_rate: float = 0.07, today: date | None = None
) -> list[Issue]:
    today = today or date.today()
    issues: list[Issue] = []

    def err(code: str, field: str | None, msg: str) -> None:
        issues.append(Issue(code, "error", field, msg))

    def warn(code: str, field: str | None, msg: str) -> None:
        issues.append(Issue(code, "warning", field, msg))

    if x.doc_type == "other":
        err("not_an_invoice", "doc_type", "เอกสารนี้ไม่ใช่ใบแจ้งหนี้/ใบกำกับภาษี/ใบเสร็จ")

    for f in ("vendor_tax_id", "invoice_number", "invoice_date", "total"):
        if getattr(x, f) in (None, ""):
            err("missing_field", f, f"ไม่พบ {f} ในเอกสาร")
    if not x.line_items:
        err("missing_field", "line_items", "ไม่พบรายการสินค้า")

    # Vendor
    if x.vendor_tax_id:
        if not thai_tax_id_valid(x.vendor_tax_id):
            err("tax_id_invalid", "vendor_tax_id", f"เลขผู้เสียภาษี {x.vendor_tax_id} ไม่ถูกต้อง")
        elif "vendor" in ctx.failed:
            warn("vendor_unchecked", "vendor_tax_id", "ตรวจทะเบียนผู้ขายกับ ERP ไม่ได้ชั่วคราว")
        elif ctx.vendor is None:
            err("vendor_unknown", "vendor_tax_id", "ไม่พบผู้ขายนี้ในทะเบียนผู้ขายของ ERP")
        elif x.vendor_name and _norm(x.vendor_name) != _norm(ctx.vendor["name"]):
            warn(
                "vendor_name_mismatch",
                "vendor_name",
                f"ชื่อผู้ขายในเอกสาร '{x.vendor_name}' ไม่ตรงกับ ERP '{ctx.vendor['name']}'",
            )

    # Arithmetic
    for i, li in enumerate(x.line_items):
        if abs(li.qty * li.unit_price - li.amount) > TOLERANCE:
            err(
                "line_amount_mismatch",
                f"line_items[{i}].amount",
                f"รายการที่ {i + 1}: {li.qty:g} x {li.unit_price:,.2f} ≠ {li.amount:,.2f}",
            )
    if x.subtotal is not None and x.line_items:
        lines = round(sum(li.amount for li in x.line_items), 2)
        if abs(lines - x.subtotal) > TOLERANCE:
            err(
                "subtotal_mismatch",
                "subtotal",
                f"ผลรวมรายการ {lines:,.2f} ≠ subtotal {x.subtotal:,.2f}",
            )
    if x.subtotal is not None and x.vat_amount:
        expected = round(x.subtotal * vat_rate, 2)
        if abs(expected - x.vat_amount) > TOLERANCE:
            err(
                "vat_mismatch",
                "vat_amount",
                f"VAT ควรเป็น {expected:,.2f} แต่เอกสารระบุ {x.vat_amount:,.2f}",
            )
    if x.doc_type == "tax_invoice" and not x.vat_amount:
        warn("vat_missing", "vat_amount", "ใบกำกับภาษีแต่ไม่พบยอด VAT")
    if x.subtotal is not None and x.total is not None:
        expected_total = round(x.subtotal + (x.vat_amount or 0), 2)
        if abs(expected_total - x.total) > TOLERANCE:
            err(
                "total_mismatch",
                "total",
                f"subtotal + VAT = {expected_total:,.2f} ≠ total {x.total:,.2f}",
            )

    # Dates
    inv_date, due = _parse_date(x.invoice_date), _parse_date(x.due_date)
    if x.invoice_date and inv_date is None:
        err("date_invalid", "invoice_date", f"วันที่ '{x.invoice_date}' ไม่ใช่รูปแบบ YYYY-MM-DD")
    if inv_date and inv_date > today:
        err("date_in_future", "invoice_date", f"วันที่เอกสาร {inv_date} อยู่ในอนาคต")
    if x.due_date and due is None:
        err("date_invalid", "due_date", f"วันครบกำหนด '{x.due_date}' ไม่ใช่รูปแบบ YYYY-MM-DD")
    if inv_date and due and due < inv_date:
        err("due_before_invoice", "due_date", "วันครบกำหนดก่อนวันที่เอกสาร")

    if x.currency and x.currency.upper() not in ("THB", "บาท"):
        warn("foreign_currency", "currency", f"สกุลเงิน {x.currency} ต้องตรวจอัตราแลกเปลี่ยน")

    # Duplicates
    if "duplicates" in ctx.failed:
        warn("duplicate_unchecked", "invoice_number", "ตรวจใบแจ้งหนี้ซ้ำกับ ERP ไม่ได้ชั่วคราว")
    if ctx.duplicate_refs:
        err(
            "duplicate_invoice",
            "invoice_number",
            f"ใบแจ้งหนี้เลขนี้ของผู้ขายนี้มีอยู่แล้ว: {', '.join(ctx.duplicate_refs)}",
        )

    # Purchase order match (2-way: vendor + amount)
    if not x.po_number:
        warn("no_po", "po_number", "ไม่มีเลขใบสั่งซื้อ (PO) ต้องให้ผู้อนุมัติยืนยัน")
    elif "purchase_order" in ctx.failed:
        warn("po_unchecked", "po_number", "ตรวจ PO กับ ERP ไม่ได้ชั่วคราว")
    elif ctx.purchase_order is None:
        err("po_not_found", "po_number", f"ไม่พบ PO {x.po_number} ใน ERP")
    else:
        po = ctx.purchase_order
        if x.vendor_tax_id and po["vendor_tax_id"] != x.vendor_tax_id:
            err("po_vendor_mismatch", "po_number", f"PO {po['id']} เป็นของผู้ขายรายอื่น")
        if po["status"] != "open":
            err("po_not_open", "po_number", f"PO {po['id']} สถานะ {po['status']} (ไม่ใช่ open)")
        if x.subtotal is not None:
            po_total = po["total_before_vat"]
            if abs(x.subtotal - po_total) > max(TOLERANCE, po_total * 0.02):
                warn(
                    "po_amount_mismatch",
                    "subtotal",
                    f"ยอดก่อน VAT {x.subtotal:,.2f} ต่างจาก PO {po_total:,.2f} เกิน 2%",
                )
    return issues


def has_errors(issues: list[Issue] | list[dict[str, Any]]) -> bool:
    return any((i.severity if isinstance(i, Issue) else i["severity"]) == "error" for i in issues)
