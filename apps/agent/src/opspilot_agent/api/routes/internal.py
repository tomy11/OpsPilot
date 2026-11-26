"""Callbacks for the Cloudflare approvals Workflow (apps/approvals-workflow).

These are the Temporal activities as HTTP endpoints: the Workflow keeps the durable state
(waiting, timers, retries) and calls back here for anything that touches Postgres or the ERP.
Each call is safe to repeat, because every step may be retried.

Status codes are the retry contract with the Workflow:
- 200: done (or already done);
- 422: permanent, do not retry (the approval is already recorded as failed);
- 503: transient, retry later;
- 404: the backend is not "cloudflare" or no callback secret is configured.
"""

import hmac
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from opspilot_agent import approvals
from opspilot_agent.api.deps import ServicesDep
from opspilot_agent.erp_client import ErpError
from opspilot_agent.models import ApprovalRequest


def _caller(request: Request, svc: ServicesDep) -> None:
    expected = svc.settings.approvals_callback_secret.get_secret_value()
    if svc.settings.approval_backend != "cloudflare" or not expected:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    scheme, _, given = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid callback secret")


router = APIRouter(
    prefix="/internal/approvals",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(_caller)],
)

ApprovalId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class FailIn(BaseModel):
    error: str = Field(max_length=2000)


@router.post("/{approval_id}/execute")
async def execute(approval_id: ApprovalId, svc: ServicesDep) -> dict:
    try:
        result = await approvals.execute_approved(svc.db, svc.tools, approval_id)
    except approvals.PermanentActionError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (ErpError, httpx.HTTPError) as exc:  # ERP down or retryable: call again later
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return {"status": "executed", "result": result}


@router.post("/{approval_id}/expire")
async def expire(approval_id: ApprovalId, svc: ServicesDep) -> dict:
    """Pending -> expired. Answers the current status if a decision got there first."""
    return {"status": await approvals.expire(svc.db, approval_id)}


@router.post("/{approval_id}/fail")
async def fail(approval_id: ApprovalId, body: FailIn, svc: ServicesDep) -> dict:
    """Approved -> failed. Answers the current status (a finished approval is left alone)."""
    await approvals.mark_failed(svc.db, approval_id, body.error)
    async with svc.db.sessionmaker() as s:
        approval = await s.get(ApprovalRequest, approval_id)
    if approval is None:
        raise HTTPException(422, "approval not found")
    return {"status": approval.status.value}
