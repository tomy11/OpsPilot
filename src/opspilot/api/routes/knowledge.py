from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from opspilot.api.deps import ServicesDep, SessionDep, UserDep, require_role
from opspilot.api.schemas import (
    AskIn,
    AskOut,
    CitationOut,
    DocumentIn,
    DocumentOut,
    HitOut,
    IngestOut,
    SearchIn,
)
from opspilot.core.audit import record_audit
from opspilot.core.auth import Role, User
from opspilot.knowledge.answer import answer_question
from opspilot.knowledge.models import KnowledgeDocument
from opspilot.knowledge.service import Hit, can_read

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
Publisher = Annotated[User, Depends(require_role(Role.MANAGER, Role.ADMIN))]


def _manages(user: User, departments: list[str]) -> bool:
    """Admins publish anywhere; a manager only for their own department (not company-wide)."""
    return user.role is Role.ADMIN or set(departments) == {user.department}


def _hit_out(h: Hit) -> HitOut:
    return HitOut(
        document_id=h.document_id,
        title=h.title,
        source=h.source,
        version=h.version,
        section=h.heading,
        text=h.text,
        score=h.score,
        lexical=h.lexical,
        vector=h.vector,
    )


@router.post("/documents", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
async def ingest_document(
    body: DocumentIn, user: Publisher, svc: ServicesDep, s: SessionDep
) -> IngestOut:
    if not _manages(user, body.departments):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "managers can only publish to their own department"
        )
    try:
        result = await svc.knowledge.ingest(
            s,
            source=body.source,
            title=body.title,
            content=body.content,
            departments=body.departments,
            min_role=body.min_role,
            actor=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    await record_audit(
        s,
        actor=user.id,
        action="knowledge.document.ingested",
        status="created" if result.created else "unchanged",
        target=result.document.id,
        detail={
            "source": body.source,
            "version": result.document.version,
            "departments": result.document.departments,
            "min_role": body.min_role.value,
            "chunks": result.chunks,
        },
    )
    await s.commit()
    return IngestOut(
        document=DocumentOut.model_validate(result.document),
        created=result.created,
        chunks=result.chunks,
    )


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(user: UserDep, svc: ServicesDep, s: SessionDep) -> list[DocumentOut]:
    return [DocumentOut.model_validate(d) for d in await svc.knowledge.list_documents(s, user)]


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_document(
    document_id: str, user: Publisher, svc: ServicesDep, s: SessionDep
) -> None:
    doc = await s.get(KnowledgeDocument, document_id)
    # 404 (not 403) for documents the caller cannot even read, so ids do not leak existence.
    if doc is None or not doc.active or not can_read(user, doc):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if not _manages(user, doc.departments):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not allowed to remove this document")
    await svc.knowledge.deactivate(s, document_id)
    await record_audit(
        s,
        actor=user.id,
        action="knowledge.document.deactivated",
        status="ok",
        target=document_id,
        detail={"source": doc.source, "version": doc.version},
    )
    await s.commit()


@router.post("/search", response_model=list[HitOut])
async def search(body: SearchIn, user: UserDep, svc: ServicesDep, s: SessionDep) -> list[HitOut]:
    return [_hit_out(h) for h in await svc.knowledge.search(s, user, body.query, body.k)]


@router.post("/ask", response_model=AskOut)
async def ask(body: AskIn, user: UserDep, svc: ServicesDep, s: SessionDep) -> AskOut:
    hits = await svc.knowledge.search(s, user, body.question)
    result = await answer_question(svc.knowledge_llm, body.question, hits)
    await record_audit(
        s,
        actor=user.id,
        action="knowledge.ask",
        status="grounded" if result.grounded else (result.reason or "not_grounded"),
        detail={
            "question": body.question,
            "retrieved": [h.document_id for h in hits],
            "cited": [c.hit.document_id for c in result.citations],
            "usage": result.usage,
        },
    )
    await s.commit()
    return AskOut(
        answer=result.text,
        grounded=result.grounded,
        reason=result.reason,
        citations=[
            CitationOut(
                n=c.n,
                document_id=c.hit.document_id,
                title=c.hit.title,
                source=c.hit.source,
                version=c.hit.version,
                section=c.hit.heading,
                snippet=c.hit.text[:300],
            )
            for c in result.citations
        ],
    )
