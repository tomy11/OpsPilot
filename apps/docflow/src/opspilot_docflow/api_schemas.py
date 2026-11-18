from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opspilot_docflow.models import DocStatus


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    media_type: str
    size: int
    uploaded_by: str
    department: str
    status: DocStatus
    extractor: str | None
    extraction: dict[str, Any] | None
    issues: list[dict[str, Any]]
    vendor_tax_id: str | None
    invoice_number: str | None
    total: float | None
    decided_by: str | None
    decision_reason: str | None
    erp_invoice_id: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class DecisionIn(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=1000)
