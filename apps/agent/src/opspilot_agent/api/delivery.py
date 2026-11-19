import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select
from temporalio.service import RPCError, RPCStatusCode

from opspilot_agent.api.orchestrator import ApprovalOrchestrator
from opspilot_agent.models import ApprovalDelivery, ApprovalRequest, ApprovalStatus
from opspilot_agent.worker.workflows import DecisionSignal
from opspilot_core.db import Database, utcnow

log = logging.getLogger(__name__)


async def start_pending(db: Database, orchestrator: ApprovalOrchestrator) -> None:
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
        for approval in pending:
            try:
                async with asyncio.timeout(3):
                    approval.workflow_id = await orchestrator.start(approval.id)
            except (RPCError, TimeoutError) as exc:
                log.warning("Approval %s workflow start will retry: %s", approval.id, exc)
                break
        await s.commit()


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
        for delivery, approval in rows:
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
            except (RPCError, TimeoutError) as exc:
                finished = approval.status not in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
                if not (
                    finished and isinstance(exc, RPCError) and exc.status == RPCStatusCode.NOT_FOUND
                ):
                    delivery.next_attempt = utcnow() + timedelta(seconds=5)
                    log.warning("Approval %s signal will retry: %s", approval.id, exc)
                    continue
            await s.delete(delivery)
        await s.commit()


async def run_delivery(db: Database, orchestrator: ApprovalOrchestrator) -> None:
    while True:
        try:
            await deliver_due(db, orchestrator)
            await start_pending(db, orchestrator)
        except Exception:
            log.exception("Approval delivery poll failed")
        await asyncio.sleep(1)
