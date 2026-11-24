from typing import Annotated

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from pydantic import BaseModel, Field

from opspilot_agent import accounts
from opspilot_agent.api.deps import ServicesDep
from opspilot_agent.config import AgentSettings
from opspilot_agent.models import ChatAccount
from opspilot_core.api import SessionDep, TokenOut
from opspilot_core.auth import issue_token

router = APIRouter(prefix="/session", tags=["invites"])
COOKIE = "opspilot_session"
SessionCookie = Annotated[str | None, Cookie(alias=COOKIE)]


class RedeemIn(BaseModel):
    token: str = Field(min_length=20, max_length=128)


def _token(account: ChatAccount, settings: AgentSettings) -> TokenOut:
    short = settings.model_copy(update={"jwt_ttl_minutes": settings.invite_token_minutes})
    return TokenOut(access_token=issue_token(accounts.to_user(account), short))


@router.post("/redeem", response_model=TokenOut)
async def redeem(
    body: RedeemIn, svc: ServicesDep, s: SessionDep, request: Request, response: Response
):
    limiter = request.app.state.login_limiter
    ip = request.client.host if request.client else "unknown"
    if wait := limiter.retry_after(ip):
        raise HTTPException(429, "too many attempts, try again later", {"Retry-After": str(wait)})
    result = await accounts.redeem(s, body.token, svc.settings.invite_session_days)
    if result is None:
        # Tokens are 256-bit, so only the per-IP window applies: a per-account bucket would let
        # anyone lock every invitee out.
        limiter.failed(ip)
        raise HTTPException(410, "ลิงก์เชิญนี้ใช้แล้ว หมดอายุ หรือไม่ถูกต้อง")
    account, session_token = result
    await s.commit()
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        COOKIE,
        session_token,
        max_age=svc.settings.invite_session_days * 86_400,
        path="/session",
        httponly=True,
        secure=svc.settings.app_env == "prod",
        samesite="strict",
    )
    return _token(account, svc.settings)


@router.post("/refresh", response_model=TokenOut)
async def refresh(
    svc: ServicesDep, s: SessionDep, response: Response, session: SessionCookie = None
):
    account = await accounts.resume(s, session) if session else None
    if account is None:
        raise HTTPException(401, "session expired")
    response.headers["Cache-Control"] = "no-store"
    return _token(account, svc.settings)


@router.post("/logout", status_code=204)
async def logout(s: SessionDep, response: Response, session: SessionCookie = None) -> None:
    if session:
        await accounts.end_session(s, session)
        await s.commit()
    response.delete_cookie(COOKIE, path="/session")
