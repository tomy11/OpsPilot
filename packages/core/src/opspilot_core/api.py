"""FastAPI pieces every OpsPilot service shares: auth dependencies and the common router.

A service puts `CoreState` on `app.state.core` in its lifespan and includes `core_router`.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_core.auth import AuthError, Role, User, decode_token, issue_token
from opspilot_core.config import CoreSettings
from opspilot_core.db import AuditLog, Database


@dataclass
class CoreState:
    settings: CoreSettings
    db: Database
    audit_targets: tuple[tuple[Any, Any], ...] = ()
    audit_department_prefix: str | None = None


def core_state(request: Request) -> CoreState:
    return request.app.state.core


CoreDep = Annotated[CoreState, Depends(core_state)]


async def db_session(core: CoreDep) -> AsyncIterator[AsyncSession]:
    async with core.db.sessionmaker() as s:
        yield s


_bearer = HTTPBearer(auto_error=False)


def current_user(
    core: CoreDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return decode_token(creds.credentials, core.settings)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


def require_role(*roles: Role):
    def check(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return check


SessionDep = Annotated[AsyncSession, Depends(db_session)]
UserDep = Annotated[User, Depends(current_user)]


class DevTokenIn(BaseModel):
    user_id: str
    role: Role
    department: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    actor: str
    action: str
    target: str | None
    status: str
    detail: dict[str, Any]
    created_at: datetime


core_router = APIRouter()


@core_router.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@core_router.get("/ready")
async def ready(s: SessionDep):
    await s.execute(text("SELECT 1"))
    return {"ok": True}


@core_router.post("/auth/dev-token", response_model=TokenOut, tags=["auth"])
async def dev_token(body: DevTokenIn, core: CoreDep) -> TokenOut:
    """Issue a token for any user/role. Disabled in prod; replace with a real IdP (OIDC)."""
    if core.settings.app_env == "prod":
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    user = User(id=body.user_id, role=body.role, department=body.department)
    return TokenOut(access_token=issue_token(user, core.settings))


@core_router.get("/audit", response_model=list[AuditOut], tags=["audit"])
async def list_audit(
    s: SessionDep,
    core: CoreDep,
    user: Annotated[User, Depends(require_role(Role.MANAGER, Role.ADMIN))],
    target: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[AuditOut]:
    q: Select = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(max(1, min(limit, 500)))
    if user.role is Role.MANAGER:
        allowed = [
            AuditLog.target.in_(select(id_column).where(department_column == user.department))
            for id_column, department_column in core.audit_targets
        ]
        if core.audit_department_prefix:
            allowed.append(
                AuditLog.action.startswith(core.audit_department_prefix)
                & (
                    (AuditLog.detail["department"].as_string() == user.department)
                    | (AuditLog.actor == user.id)
                )
            )
        q = q.where(or_(*allowed) if allowed else AuditLog.actor == user.id)
    if target:
        q = q.where(AuditLog.target == target)
    if action:
        q = q.where(AuditLog.action == action)
    return [AuditOut.model_validate(a) for a in (await s.scalars(q)).all()]
