"""How an approval moves forward after a human decides.

TemporalOrchestrator: durable workflow per approval (production path).
InlineOrchestrator: executes in-process right after approval (tests, local dev without Temporal).
"""

import logging
from typing import Protocol

from temporalio.client import Client

from opspilot_agent import approvals
from opspilot_agent.erp_client import ErpError
from opspilot_agent.tools import ToolContext
from opspilot_agent.worker.workflows import ApprovalInput, ApprovalWorkflow, DecisionSignal
from opspilot_core.db import Database

log = logging.getLogger(__name__)


class ApprovalOrchestrator(Protocol):
    async def start(self, approval_id: str) -> str | None: ...
    async def decide(self, approval_id: str, signal: DecisionSignal) -> None: ...


class InlineOrchestrator:
    def __init__(self, db: Database, tools: ToolContext) -> None:
        self.db = db
        self.tools = tools

    async def start(self, approval_id: str) -> str | None:
        return None

    async def decide(self, approval_id: str, signal: DecisionSignal) -> None:
        if not signal.approve:
            return
        try:
            await approvals.execute_approved(self.db, self.tools, approval_id)
        except approvals.PermanentActionError:
            pass  # already recorded as failed
        except ErpError as exc:  # retryable but retries exhausted
            await approvals.mark_failed(self.db, approval_id, str(exc))


class TemporalOrchestrator:
    def __init__(self, client: Client, task_queue: str, timeout_hours: int) -> None:
        self.client = client
        self.task_queue = task_queue
        self.timeout_hours = timeout_hours

    @staticmethod
    def workflow_id(approval_id: str) -> str:
        return f"approval-{approval_id}"

    async def start(self, approval_id: str) -> str | None:
        handle = await self.client.start_workflow(
            ApprovalWorkflow.run,
            ApprovalInput(approval_id=approval_id, timeout_hours=self.timeout_hours),
            id=self.workflow_id(approval_id),
            task_queue=self.task_queue,
        )
        return handle.id

    async def decide(self, approval_id: str, signal: DecisionSignal) -> None:
        handle = self.client.get_workflow_handle(self.workflow_id(approval_id))
        await handle.signal(ApprovalWorkflow.decide, signal)
