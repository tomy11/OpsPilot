from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy import select

from opspilot_core.api import CoreDep, SessionDep, UserDep, require_role
from opspilot_core.audit import record_audit
from opspilot_core.auth import Role, User, approval_denied_reason
from opspilot_docflow.api_schemas import DecisionIn, DocumentOut
from opspilot_docflow.extractors import SUPPORTED
from opspilot_docflow.models import DocflowDocument, DocStatus
from opspilot_docflow.schema import Extraction
from opspilot_docflow.service import Blocked, Conflict, DocflowService

router = APIRouter(prefix="/docflow", tags=["docflow"])
Reviewer = Annotated[User, Depends(require_role(Role.MANAGER, Role.ADMIN))]


def docflow_service(request: Request) -> DocflowService:
    return request.app.state.docflow


ServiceDep = Annotated[DocflowService, Depends(docflow_service)]


def _visible(user: User, doc: DocflowDocument) -> bool:
    if user.role is Role.ADMIN:
        return True
    if user.role is Role.MANAGER:
        return doc.department == user.department
    return doc.uploaded_by == user.id


async def _load(s, user: User, document_id: str) -> DocflowDocument:
    doc = await s.get(DocflowDocument, document_id)
    if doc is None or not _visible(user, doc):  # 404 either way: ids do not leak existence
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return doc


async def _check_reviewer(s, reviewer: User, doc: DocflowDocument) -> None:
    reason = approval_denied_reason(
        reviewer, requested_by=doc.uploaded_by, department=doc.department
    )
    if reason:
        await record_audit(
            s,
            actor=reviewer.id,
            action="docflow.review",
            status="denied",
            target=doc.id,
            detail={"reason": reason},
        )
        await s.commit()
        raise HTTPException(status.HTTP_403_FORBIDDEN, reason)


@router.post("/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def upload(
    file: UploadFile, user: UserDep, core: CoreDep, svc: ServiceDep, s: SessionDep
) -> DocumentOut:
    media_type = (file.content_type or "").split(";")[0].strip()
    if media_type not in SUPPORTED:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, f"unsupported type {media_type or '?'}"
        )
    limit = core.settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file too large")
    if not data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty file")

    doc = await svc.upload(
        s,
        filename=file.filename or "upload",
        media_type=media_type,
        data=data,
        user_id=user.id,
        department=user.department,
    )
    await s.commit()
    return DocumentOut.model_validate(doc)


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    user: UserDep,
    s: SessionDep,
    status_: Annotated[DocStatus | None, Query(alias="status")] = None,
    limit: int = 100,
) -> list[DocumentOut]:
    q = select(DocflowDocument).order_by(DocflowDocument.created_at.desc()).limit(min(limit, 500))
    if status_:
        q = q.where(DocflowDocument.status == status_)
    if user.role is Role.MANAGER:
        q = q.where(DocflowDocument.department == user.department)
    elif user.role is Role.STAFF:
        q = q.where(DocflowDocument.uploaded_by == user.id)
    return [DocumentOut.model_validate(d) for d in (await s.scalars(q)).all()]


@router.get("/review-queue", response_model=list[DocumentOut])
async def review_queue(user: Reviewer, s: SessionDep) -> list[DocumentOut]:
    """Documents this reviewer may act on, oldest first. Excludes their own uploads."""
    q = (
        select(DocflowDocument)
        .where(
            DocflowDocument.status.in_([DocStatus.NEEDS_REVIEW, DocStatus.EXTRACTION_FAILED]),
            DocflowDocument.uploaded_by != user.id,
        )
        .order_by(DocflowDocument.created_at)
    )
    if user.role is Role.MANAGER:
        q = q.where(DocflowDocument.department == user.department)
    return [DocumentOut.model_validate(d) for d in (await s.scalars(q)).all()]


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(document_id: str, user: UserDep, s: SessionDep) -> DocumentOut:
    return DocumentOut.model_validate(await _load(s, user, document_id))


@router.get("/documents/{document_id}/file")
async def get_file(document_id: str, user: UserDep, s: SessionDep) -> Response:
    doc = await _load(s, user, document_id)
    return Response(
        doc.content,
        media_type=doc.media_type,
        headers={"Content-Disposition": f'inline; filename="{doc.filename}"'},
    )


@router.put("/documents/{document_id}/extraction", response_model=DocumentOut)
async def correct_extraction(
    document_id: str, body: Extraction, user: Reviewer, svc: ServiceDep, s: SessionDep
) -> DocumentOut:
    doc = await _load(s, user, document_id)
    await _check_reviewer(s, user, doc)
    try:
        await svc.correct(s, doc, body, reviewer=user.id)
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await s.commit()
    return DocumentOut.model_validate(doc)


@router.post("/documents/{document_id}/decision", response_model=DocumentOut)
async def decide(
    document_id: str, body: DecisionIn, user: Reviewer, svc: ServiceDep, s: SessionDep
) -> DocumentOut:
    doc = await _load(s, user, document_id)
    await _check_reviewer(s, user, doc)
    try:
        if body.approve:
            await svc.approve(s, doc, reviewer=user.id, reason=body.reason)
        else:
            await svc.reject(s, doc, reviewer=user.id, reason=body.reason)
    except Blocked as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await s.commit()
    return DocumentOut.model_validate(doc)


@router.post("/documents/{document_id}/retry-post", response_model=DocumentOut)
async def retry_post(
    document_id: str, user: Reviewer, svc: ServiceDep, s: SessionDep
) -> DocumentOut:
    doc = await _load(s, user, document_id)
    try:
        await svc.retry_post(s, doc, actor=user.id)
    except Conflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    await s.commit()
    return DocumentOut.model_validate(doc)
