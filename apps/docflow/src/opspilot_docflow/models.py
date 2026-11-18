import enum
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Float, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_core.db import Base, new_id, utcnow


class DocStatus(enum.StrEnum):
    EXTRACTION_FAILED = "extraction_failed"  # reviewer can type the fields in by hand
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"  # transient: posting to the ERP
    POSTED = "posted"
    POST_FAILED = "post_failed"  # retryable from the API
    REJECTED = "rejected"


class DocflowDocument(Base):
    __tablename__ = "docflow_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(String(256))
    media_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)  # move to object storage for real volumes
    uploaded_by: Mapped[str] = mapped_column(String(64), index=True)
    department: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[DocStatus] = mapped_column(Enum(DocStatus, native_enum=False), index=True)
    extractor: Mapped[str | None] = mapped_column(String(64))
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    original_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # model output
    issues: Mapped[list[Any]] = mapped_column(default=list)
    vendor_tax_id: Mapped[str | None] = mapped_column(String(13), index=True)
    invoice_number: Mapped[str | None] = mapped_column(String(64), index=True)
    total: Mapped[float | None] = mapped_column(Float)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    erp_invoice_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
