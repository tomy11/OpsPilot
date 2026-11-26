"""How an approval moves forward after a human decides.

TemporalOrchestrator: durable workflow per approval on a Temporal server.
CloudflareOrchestrator: the same workflow on Cloudflare Workflows (apps/approvals-workflow).
InlineOrchestrator: executes in-process right after approval (tests, local dev without either).

The durable backends raise only the two errors below, so the outbox in `delivery.py` treats
them the same way.
"""

import logging
from typing import Protocol

import httpx
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from opspilot_agent import approvals
from opspilot_agent.erp_client import ErpError
from opspilot_agent.tools import ToolContext
from opspilot_agent.worker.workflows import ApprovalInput, ApprovalWorkflow, DecisionSignal
from opspilot_core.db import Database

log = logging.getLogger(__name__)


class OrchestratorUnavailable(Exception):
    """The backend did not answer; the same call may succeed later."""


class WorkflowGone(Exception):
    """The approval's workflow no longer exists or has finished, so it cannot take a decision."""


class ApprovalOrchestrator(Protocol):
    async def start(self, approval_id: str) -> str | None: ...
    async def decide(self, approval_id: str, signal: DecisionSignal) -> None: ...


def workflow_id(approval_id: str) -> str:
    return f"approval-{approval_id}"


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

    workflow_id = staticmethod(workflow_id)

    async def start(self, approval_id: str) -> str | None:
        wid = workflow_id(approval_id)
        try:
            await self.client.start_workflow(
                ApprovalWorkflow.run,
                ApprovalInput(approval_id=approval_id, timeout_hours=self.timeout_hours),
                id=wid,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                task_queue=self.task_queue,
            )
        except WorkflowAlreadyStartedError:
            pass  # An earlier start succeeded before the database commit.
        except RPCError as exc:
            raise OrchestratorUnavailable(str(exc)) from exc
        return wid

    async def decide(self, approval_id: str, signal: DecisionSignal) -> None:
        handle = self.client.get_workflow_handle(workflow_id(approval_id))
        try:
            await handle.signal(ApprovalWorkflow.decide, signal)
        except RPCError as exc:
            if exc.status == RPCStatusCode.NOT_FOUND:
                raise WorkflowGone(str(exc)) from exc
            raise OrchestratorUnavailable(str(exc)) from exc


class CloudflareOrchestrator:
    """Talks to the approvals Worker, which owns the Workflow binding.

    The Worker, not the Cloudflare REST API, is the entry point: the agent then needs no
    Cloudflare API token, and `wrangler dev` behaves the same as the deployed Worker.
    """

    def __init__(
        self,
        base_url: str,
        secret: str,
        timeout_hours: int,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_hours = timeout_hours
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {secret}"},
            timeout=timeout_s,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            response = await self.http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise OrchestratorUnavailable(f"approvals worker unreachable: {exc}") from exc
        if response.status_code in (401, 403):
            # A wrong secret will not fix itself, but dropping decisions would be worse.
            log.error("Approvals worker refused the shared secret (%s)", response.status_code)
        return response

    async def start(self, approval_id: str) -> str | None:
        response = await self._post(
            "/approvals", {"approval_id": approval_id, "timeout_hours": self.timeout_hours}
        )
        if response.status_code not in (200, 201):  # 200 = it already existed
            raise OrchestratorUnavailable(f"workflow start: HTTP {response.status_code}")
        return response.json()["id"]

    async def decide(self, approval_id: str, signal: DecisionSignal) -> None:
        response = await self._post(
            f"/approvals/{approval_id}/decision",
            {"approve": signal.approve, "approver_id": signal.approver_id, "reason": signal.reason},
        )
        if response.status_code in (404, 409):  # missing, or finished and not waiting
            raise WorkflowGone(f"workflow {workflow_id(approval_id)}: HTTP {response.status_code}")
        if response.status_code != 202:
            raise OrchestratorUnavailable(f"workflow decision: HTTP {response.status_code}")
