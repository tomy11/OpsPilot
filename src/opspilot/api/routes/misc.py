from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from opspilot.api.deps import ServicesDep, SessionDep, require_role
from opspilot.api.schemas import AuditOut, DevTokenIn, TokenOut
from opspilot.core.auth import Role, User, issue_token
from opspilot.core.db import AuditLog

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@router.post("/auth/dev-token", response_model=TokenOut, tags=["auth"])
async def dev_token(body: DevTokenIn, svc: ServicesDep) -> TokenOut:
    """Issue a token for any user/role. Disabled in prod; replace with a real IdP (OIDC)."""
    if svc.settings.app_env == "prod":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    user = User(id=body.user_id, role=body.role, department=body.department)
    return TokenOut(access_token=issue_token(user, svc.settings))


@router.get("/audit", response_model=list[AuditOut], tags=["audit"])
async def list_audit(
    s: SessionDep,
    _: Annotated[User, Depends(require_role(Role.MANAGER, Role.ADMIN))],
    target: str | None = None,
    limit: int = 100,
) -> list[AuditOut]:
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(limit, 500))
    if target:
        q = q.where(AuditLog.target == target)
    return [AuditOut.model_validate(a) for a in (await s.scalars(q)).all()]
