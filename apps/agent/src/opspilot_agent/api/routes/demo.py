from hmac import compare_digest
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from opspilot_agent.api.deps import ServicesDep
from opspilot_core.api import TokenOut, UserDep
from opspilot_core.auth import Role, User, issue_token

router = APIRouter(prefix="/session", tags=["demo"])


class LoginIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class SessionOut(User):
    approval_backend: Literal["temporal", "inline"]


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, svc: ServicesDep, response: Response):
    settings = svc.settings
    accounts = {
        "staff": (Role.STAFF, settings.demo_staff_password.get_secret_value()),
        "manager": (Role.MANAGER, settings.demo_manager_password.get_secret_value()),
    }
    account = accounts.get(body.username)
    if (
        account is None
        or not account[1]
        or not compare_digest(body.password.encode(), account[1].encode())
    ):
        raise HTTPException(401, "invalid username or password")
    user = User(id=f"demo-{body.username}", department="ops", role=account[0])
    response.headers["Cache-Control"] = "no-store"
    return TokenOut(access_token=issue_token(user, settings))


@router.get("/me", response_model=SessionOut)
async def me(user: UserDep, svc: ServicesDep):
    return SessionOut(**user.model_dump(), approval_backend=svc.settings.approval_backend)
