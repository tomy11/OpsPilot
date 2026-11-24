from hmac import compare_digest
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from opspilot_agent.api.deps import ServicesDep
from opspilot_agent.credits import to_credits
from opspilot_agent.models import ChatAccount
from opspilot_core.api import SessionDep, TokenOut, UserDep
from opspilot_core.auth import Role, User, issue_token

router = APIRouter(prefix="/session", tags=["demo"])


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class SessionOut(User):
    approval_backend: Literal["temporal", "inline"]
    invited: bool = False
    label: str | None = None
    credits: float | None = None  # None = not limited by credits


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, svc: ServicesDep, request: Request, response: Response):
    settings = svc.settings
    accounts = {
        "staff": (Role.STAFF, settings.demo_staff_password.get_secret_value()),
        "manager": (Role.MANAGER, settings.demo_manager_password.get_secret_value()),
    }
    limiter = request.app.state.login_limiter
    ip = request.client.host if request.client else "unknown"
    # Unknown usernames share one bucket so random names cannot grow the limiter's memory.
    username = body.username if body.username in accounts else "?"
    if wait := limiter.retry_after(ip, username):
        raise HTTPException(
            429, "too many failed sign-in attempts, try again later", {"Retry-After": str(wait)}
        )
    account = accounts.get(body.username)
    if (
        account is None
        or not account[1]
        or not compare_digest(body.password.encode(), account[1].encode())
    ):
        limiter.failed(ip, username)
        raise HTTPException(401, "invalid username or password")
    limiter.succeeded(ip)
    user = User(id=f"demo-{body.username}", department="ops", role=account[0])
    response.headers["Cache-Control"] = "no-store"
    return TokenOut(access_token=issue_token(user, settings))


@router.get("/me", response_model=SessionOut)
async def me(user: UserDep, svc: ServicesDep, s: SessionDep):
    account = await s.get(ChatAccount, user.id)
    return SessionOut(
        **user.model_dump(),
        approval_backend=svc.settings.approval_backend,
        invited=account is not None,
        label=account.label if account else None,
        credits=to_credits(account.balance_micro) if account and svc.credits else None,
    )
