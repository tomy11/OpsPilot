from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.agent.tools import ToolContext
from opspilot.api.orchestrator import ApprovalOrchestrator
from opspilot.core.auth import AuthError, Role, User, decode_token
from opspilot.core.config import Settings
from opspilot.core.db import Database
from opspilot.core.llm import LLMClient


@dataclass
class Services:
    settings: Settings
    db: Database
    tools: ToolContext
    llm: LLMClient
    orchestrator: ApprovalOrchestrator


def services(request: Request) -> Services:
    return request.app.state.services


async def db_session(svc: Annotated[Services, Depends(services)]) -> AsyncIterator[AsyncSession]:
    async with svc.db.sessionmaker() as s:
        yield s


_bearer = HTTPBearer(auto_error=False)


def current_user(
    svc: Annotated[Services, Depends(services)],
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    try:
        return decode_token(creds.credentials, svc.settings)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc


def require_role(*roles: Role):
    def check(user: Annotated[User, Depends(current_user)]) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "insufficient role")
        return user

    return check


ServicesDep = Annotated[Services, Depends(services)]
SessionDep = Annotated[AsyncSession, Depends(db_session)]
UserDep = Annotated[User, Depends(current_user)]
