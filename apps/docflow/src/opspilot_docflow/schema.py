"""What we extract from an invoice or receipt.

The JSON schema is written by hand (not generated) so it stays inside what structured outputs
accept: plain types, enums, nullable via `anyOf` with null, every property required, no extras.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

DocType = Literal["tax_invoice", "invoice", "receipt", "other"]


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    sku: str | None
    qty: float
    unit_price: float
    amount: float


class Extraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_type: DocType
    vendor_name: str | None
    vendor_tax_id: str | None
    invoice_number: str | None
    invoice_date: str | None  # YYYY-MM-DD as printed (converted from Buddhist era if needed)
    due_date: str | None
    po_number: str | None
    currency: str | None
    line_items: list[LineItem]
    subtotal: float | None
    vat_amount: float | None
    total: float | None


def _nullable(t: str) -> dict[str, Any]:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


EXTRACTION_SCHEMA: dict[str, Any] = _obj(
    {
        "doc_type": {"type": "string", "enum": ["tax_invoice", "invoice", "receipt", "other"]},
        "vendor_name": _nullable("string"),
        "vendor_tax_id": _nullable("string"),
        "invoice_number": _nullable("string"),
        "invoice_date": _nullable("string"),
        "due_date": _nullable("string"),
        "po_number": _nullable("string"),
        "currency": _nullable("string"),
        "line_items": {
            "type": "array",
            "items": _obj(
                {
                    "description": {"type": "string"},
                    "sku": _nullable("string"),
                    "qty": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number"},
                }
            ),
        },
        "subtotal": _nullable("number"),
        "vat_amount": _nullable("number"),
        "total": _nullable("number"),
    }
)
