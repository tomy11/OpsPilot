"""Document lifecycle: upload -> extract -> validate -> (auto-post | review) -> post to ERP.

    upload ──extract ok──▶ needs_review ──approve──▶ approved ──post──▶ posted
       │                     │   ▲  │                          └──fail──▶ post_failed ──retry──┘
       │                     │   └──┘ correct (re-validate)
       └─extract fails──▶ extraction_failed ──correct──▶ needs_review
                           needs_review / extraction_failed ──reject──▶ rejected

A clean document (no issues at all) at or below `auto_post_limit` skips review. Every status
change is a conditional UPDATE on the expected current status, so two reviewers clicking at
once get one success and one 409. The ERP Idempotency-Key is the document id, so a retried
post can never create a second AP invoice.
"""

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_core.audit import record_audit
from opspilot_docflow.erp import ErpClient, ErpError
from opspilot_docflow.extractors import ExtractionError, Extractor
from opspilot_docflow.models import DocflowDocument, DocStatus
from opspilot_docflow.schema import Extraction
from opspilot_docflow.validation import ErpContext, Issue, has_errors, validate

SYSTEM = "system"


class Conflict(Exception):
    """The document is not in a status that allows this action (or someone else got there first)."""


class Blocked(Exception):
    """The action is not allowed because validation errors remain."""


@dataclass
class DocflowService:
    erp: ErpClient
    extractor: Extractor
    vat_rate: float = 0.07
    auto_post_limit: float = 5000.0

    # ---- helpers ---------------------------------------------------------------------------

    async def _transition(
        self,
        s: AsyncSession,
        doc: DocflowDocument,
        allowed: set[DocStatus],
        to: DocStatus,
        **values: Any,
    ) -> None:
        result = await s.execute(
            update(DocflowDocument)
            .where(DocflowDocument.id == doc.id, DocflowDocument.status.in_(allowed))
            .values(status=to, **values)
        )
        if result.rowcount != 1:
            raise Conflict(f"document is {doc.status.value}, cannot move to {to.value}")
        await s.refresh(doc)

    async def _context(self, s: AsyncSession, doc_id: str, x: Extraction) -> ErpContext:
        # An ERP outage downgrades a check to an "unchecked" warning instead of failing the upload
        # or (worse) reporting a real vendor as unknown.
        ctx = ErpContext()
        if x.vendor_tax_id:
            try:
                ctx.vendor = await self.erp.get_vendor(x.vendor_tax_id)
            except ErpError:
                ctx.failed.add("vendor")
        if x.po_number:
            try:
                ctx.purchase_order = await self.erp.get_purchase_order(x.po_number)
            except ErpError:
                ctx.failed.add("purchase_order")
        if x.vendor_tax_id and x.invoice_number:
            try:
                found = await self.erp.find_invoices(x.vendor_tax_id, x.invoice_number)
            except ErpError:
                ctx.failed.add("duplicates")
                found = []
            refs = [i["id"] for i in found]
            others = await s.scalars(
                select(DocflowDocument.id).where(
                    DocflowDocument.vendor_tax_id == x.vendor_tax_id,
                    DocflowDocument.invoice_number == x.invoice_number,
                    DocflowDocument.id != doc_id,
                    DocflowDocument.status != DocStatus.REJECTED,
                )
            )
            ctx.duplicate_refs = refs + [f"docflow:{d}" for d in others.all()]
        return ctx

    async def _validate(self, s: AsyncSession, doc_id: str, x: Extraction) -> list[Issue]:
        return validate(x, await self._context(s, doc_id, x), vat_rate=self.vat_rate)

    @staticmethod
    def _summary(x: Extraction) -> dict[str, Any]:
        return {
            "vendor_tax_id": x.vendor_tax_id,
            "invoice_number": x.invoice_number,
            "total": x.total,
        }

    # ---- lifecycle -------------------------------------------------------------------------

    async def upload(
        self,
        s: AsyncSession,
        *,
        filename: str,
        media_type: str,
        data: bytes,
        user_id: str,
        department: str,
    ) -> DocflowDocument:
        doc = DocflowDocument(
            filename=filename,
            media_type=media_type,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content=data,
            uploaded_by=user_id,
            department=department,
            status=DocStatus.EXTRACTION_FAILED,
        )
        s.add(doc)
        await s.flush()
        await record_audit(
            s,
            actor=user_id,
            action="docflow.uploaded",
            status="ok",
            target=doc.id,
            detail={"filename": filename, "media_type": media_type, "size": len(data)},
        )

        try:
            result = await self.extractor.extract(
                filename=filename, media_type=media_type, data=data
            )
        except ExtractionError as exc:
            doc.extractor, doc.error = self.extractor.name, f"extraction failed: {exc}"
            await record_audit(
                s,
                actor=SYSTEM,
                action="docflow.extracted",
                status="failed",
                target=doc.id,
                detail={"extractor": self.extractor.name, "error": str(exc)},
            )
            await s.flush()
            return doc

        x = result.data
        issues = await self._validate(s, doc.id, x)
        doc.extractor = result.extractor
        doc.extraction = doc.original_extraction = x.model_dump()
        doc.issues = [i.as_dict() for i in issues]
        doc.vendor_tax_id, doc.invoice_number, doc.total = (
            x.vendor_tax_id,
            x.invoice_number,
            x.total,
        )
        doc.status = DocStatus.NEEDS_REVIEW
        await record_audit(
            s,
            actor=SYSTEM,
            action="docflow.extracted",
            status="ok",
            target=doc.id,
            detail={
                "extractor": result.extractor,
                "usage": result.usage,
                **self._summary(x),
                "issues": [i.code for i in issues],
            },
        )
        await s.flush()

        if not issues and x.total is not None and x.total <= self.auto_post_limit:
            await self._transition(
                s,
                doc,
                {DocStatus.NEEDS_REVIEW},
                DocStatus.APPROVED,
                decided_by=SYSTEM,
                decision_reason=f"auto: clean and total <= {self.auto_post_limit:,.0f}",
            )
            await record_audit(
                s,
                actor=SYSTEM,
                action="docflow.approved",
                status="auto",
                target=doc.id,
                detail=self._summary(x),
            )
            await self.post(s, doc)
        return doc

    async def correct(
        self, s: AsyncSession, doc: DocflowDocument, x: Extraction, *, reviewer: str
    ) -> DocflowDocument:
        """Reviewer edits fields. Re-validated from scratch; the diff is audited (and is eval data)."""
        before = doc.extraction or {}
        after = x.model_dump()
        changed = sorted(k for k in after if before.get(k) != after.get(k))
        issues = await self._validate(s, doc.id, x)
        await self._transition(
            s,
            doc,
            {DocStatus.NEEDS_REVIEW, DocStatus.EXTRACTION_FAILED},
            DocStatus.NEEDS_REVIEW,
            extraction=after,
            issues=[i.as_dict() for i in issues],
            error=None,
            vendor_tax_id=x.vendor_tax_id,
            invoice_number=x.invoice_number,
            total=x.total,
        )
        await record_audit(
            s,
            actor=reviewer,
            action="docflow.corrected",
            status="ok",
            target=doc.id,
            detail={
                "fields": changed,
                "before": {k: before.get(k) for k in changed},
                "after": {k: after.get(k) for k in changed},
                "issues": [i.code for i in issues],
            },
        )
        return doc

    async def approve(
        self, s: AsyncSession, doc: DocflowDocument, *, reviewer: str, reason: str | None
    ) -> DocflowDocument:
        if doc.status is DocStatus.NEEDS_REVIEW and has_errors(doc.issues):
            raise Blocked("fix or reject: validation errors remain")
        await self._transition(
            s,
            doc,
            {DocStatus.NEEDS_REVIEW},
            DocStatus.APPROVED,
            decided_by=reviewer,
            decision_reason=reason,
        )
        await record_audit(
            s,
            actor=reviewer,
            action="docflow.approved",
            status="ok",
            target=doc.id,
            detail={"reason": reason, "warnings": [i["code"] for i in doc.issues]},
        )
        await self.post(s, doc)
        return doc

    async def reject(
        self, s: AsyncSession, doc: DocflowDocument, *, reviewer: str, reason: str | None
    ) -> DocflowDocument:
        await self._transition(
            s,
            doc,
            {DocStatus.NEEDS_REVIEW, DocStatus.EXTRACTION_FAILED},
            DocStatus.REJECTED,
            decided_by=reviewer,
            decision_reason=reason,
        )
        await record_audit(
            s,
            actor=reviewer,
            action="docflow.rejected",
            status="ok",
            target=doc.id,
            detail={"reason": reason},
        )
        return doc

    async def retry_post(
        self, s: AsyncSession, doc: DocflowDocument, *, actor: str
    ) -> DocflowDocument:
        await self._transition(s, doc, {DocStatus.POST_FAILED}, DocStatus.APPROVED, error=None)
        await record_audit(s, actor=actor, action="docflow.post_retry", status="ok", target=doc.id)
        await self.post(s, doc)
        return doc

    async def post(self, s: AsyncSession, doc: DocflowDocument) -> None:
        x = Extraction.model_validate(doc.extraction)
        body = {
            "vendor_tax_id": x.vendor_tax_id,
            "invoice_number": x.invoice_number,
            "invoice_date": x.invoice_date,
            "due_date": x.due_date,
            "po_number": x.po_number,
            "currency": (x.currency or "THB").upper().replace("บาท", "THB"),
            "subtotal": x.subtotal or 0.0,
            "vat_amount": x.vat_amount or 0.0,
            "total": x.total,
            "lines": [li.model_dump() for li in x.line_items],
            "source_document_id": doc.id,
        }
        try:
            invoice = await self.erp.post_invoice(body, idempotency_key=doc.id)
        except ErpError as exc:
            await self._transition(
                s, doc, {DocStatus.APPROVED}, DocStatus.POST_FAILED, error=str(exc)
            )
            await record_audit(
                s,
                actor=SYSTEM,
                action="docflow.posted",
                status="failed",
                target=doc.id,
                detail={"error": exc.detail, "erp_status": exc.status, "retryable": exc.retryable},
            )
            return
        await self._transition(
            s, doc, {DocStatus.APPROVED}, DocStatus.POSTED, erp_invoice_id=invoice["id"]
        )
        await record_audit(
            s,
            actor=SYSTEM,
            action="docflow.posted",
            status="ok",
            target=doc.id,
            detail={"erp_invoice_id": invoice["id"], **self._summary(x)},
        )
