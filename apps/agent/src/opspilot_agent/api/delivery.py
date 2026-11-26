"""Transactional outbox for the durable backends (Temporal, Cloudflare Workflows).

The API only writes to Postgres. This loop then (1) starts a workflow for each pending approval
that has none yet and (2) delivers committed decisions to it, retrying until the backend
answers. Rows are claimed with `FOR UPDATE SKIP LOCKED`, so several API replicas can run it.

Retries are bounded by meaning, not by a counter, so no schema change is needed:
- transient errors back off with the age of the decision (5 s up to 5 min);
- a pending approval whose workflow could not start within the approval timeout expires,
  exactly as it would have inside the workflow;
- an approved decision whose workflow no longer exists (WorkflowGone) can never execute, so the
  approval is marked failed and audited instead of being retried forever.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_agent.api.orchestrator import (
    ApprovalOrchestrator,
    OrchestratorUnavailable,
    WorkflowGone,
)
from opspilot_agent.approvals import ApprovalConflict, transition
from opspilot_agent.models import ApprovalDelivery, ApprovalRequest, ApprovalStatus
from opspilot_agent.worker.workflows import DecisionSignal
from opspilot_core.audit import record_audit
from opspilot_core.db import Database, utcnow

log = logging.getLogger(__name__)

MIN_BACKOFF = timedelta(seconds=5)
MAX_BACKOFF = timedelta(minutes=5)
MAX_POLL_SLEEP_S = 30.0


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)  # SQLite returns naive UTC


def retry_delay(decided_at: datetime, now: datetime) -> timedelta:
    """Back off with the age of the decision: 10% of the time it has waited, within bounds."""
    return min(MAX_BACKOFF, max(MIN_BACKOFF, (now - _aware(decided_at)) / 10))


async def _finish(
    s: AsyncSession,
    approval_id: str,
    *,
    expected: ApprovalStatus,
    new: ApprovalStatus,
    action: str,
    error: str | None = None,
) -> None:
    try:
        await transition(s, approval_id, expected=expected, new=new, error=error)
    except ApprovalConflict:
        return  # someone else moved it on; nothing to record
    await record_audit(
        s,
        actor="system",
        action=action,
        status="error" if error else "ok",
        target=approval_id,
        detail={"error": error} if error else {},
    )


async def start_pending(
    db: Database, orchestrator: ApprovalOrchestrator, *, timeout_hours: int = 24
) -> bool:
    """Start workflows for pending approvals. Returns False if Temporal could not be reached."""
    ok = True
    async with db.sessionmaker() as s:
        pending = await s.scalars(
            select(ApprovalRequest)
            .where(
                ApprovalRequest.status == ApprovalStatus.PENDING,
                ApprovalRequest.workflow_id.is_(None),
            )
            .order_by(ApprovalRequest.created_at)
            .limit(10)
            .with_for_update(skip_locked=True)
        )
        deadline = utcnow() - timedelta(hours=timeout_hours)
        for approval in pending.all():
            if _aware(approval.created_at) < deadline:
                await _finish(
                    s,
                    approval.id,
                    expected=ApprovalStatus.PENDING,
                    new=ApprovalStatus.EXPIRED,
                    action="approval.expired",
                    error="approval workflow could not be started before the approval timeout",
                )
                continue
            if not ok:
                continue  # Temporal is down: only expire the stale ones in this pass
            try:
                async with asyncio.timeout(3):
                    approval.workflow_id = await orchestrator.start(approval.id)
            except (OrchestratorUnavailable, TimeoutError) as exc:
                log.warning("Approval %s workflow start will retry: %s", approval.id, exc)
                ok = False
        await s.commit()
    return ok


async def deliver_due(db: Database, orchestrator: ApprovalOrchestrator) -> None:
    async with db.sessionmaker() as s:
        rows = await s.execute(
            select(ApprovalDelivery, ApprovalRequest)
            .join(ApprovalRequest)
            .where(ApprovalDelivery.next_attempt <= utcnow())
            .order_by(ApprovalDelivery.next_attempt)
            .limit(10)
            .with_for_update(of=ApprovalDelivery, skip_locked=True)
        )
        for delivery, approval in rows.all():
            try:
                async with asyncio.timeout(3):
                    await orchestrator.decide(
                        approval.id,
                        DecisionSignal(
                            approval.status in (ApprovalStatus.APPROVED, ApprovalStatus.EXECUTED),
                            approval.decided_by or "",
                            approval.decision_reason,
                        ),
                    )
            except (OrchestratorUnavailable, TimeoutError) as exc:
                now = utcnow()
                delivery.next_attempt = now + retry_delay(approval.updated_at, now)
                log.warning("Approval %s signal will retry: %s", approval.id, exc)
                continue
            except WorkflowGone:
                if approval.status is ApprovalStatus.APPROVED:
                    # Approved, but no workflow is left to carry it out: fail loudly, once.
                    await _finish(
                        s,
                        approval.id,
                        expected=ApprovalStatus.APPROVED,
                        new=ApprovalStatus.FAILED,
                        action="approval.failed",
                        error="approval workflow not found; decision could not be delivered",
                    )
                # Rejected or already finished: the workflow has nothing left to receive.
            await s.delete(delivery)
        await s.commit()


async def run_delivery(
    db: Database, orchestrator: ApprovalOrchestrator, *, timeout_hours: int = 24
) -> None:
    failures = 0
    while True:
        try:
            await deliver_due(db, orchestrator)
            ok = await start_pending(db, orchestrator, timeout_hours=timeout_hours)
        except Exception:
            log.exception("Approval delivery poll failed")
            ok = False
        failures = 0 if ok else failures + 1
        await asyncio.sleep(min(MAX_POLL_SLEEP_S, 2.0**failures) if failures else 1.0)
