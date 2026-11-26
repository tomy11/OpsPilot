"""Cloudflare Workflows backend: the agent's side of the contract with workers/approvals."""

import json

import httpx
import pytest

from opspilot_agent import approvals
from opspilot_agent.api.delivery import deliver_due, start_pending
from opspilot_agent.api.main import create_app
from opspilot_agent.api.orchestrator import (
    CloudflareOrchestrator,
    OrchestratorUnavailable,
    WorkflowGone,
)
from opspilot_agent.fake_llm import FakeLLM
from opspilot_agent.models import ApprovalDelivery, ApprovalRequest, ApprovalStatus
from opspilot_agent.worker.workflows import DecisionSignal
from opspilot_core.testing import token

TRANSFER = "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna"
WORKER_SECRET = "worker-secret-" + "w" * 24
CALLBACK_SECRET = "callback-secret-" + "c" * 24


def _orchestrator(handler) -> tuple[CloudflareOrchestrator, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    orch = CloudflareOrchestrator(
        "http://worker/", WORKER_SECRET, 24, transport=httpx.MockTransport(record)
    )
    return orch, seen


# ---- orchestrator -> Worker -------------------------------------------------------------------


@pytest.mark.parametrize("code", [200, 201])
async def test_start_creates_instance_and_accepts_an_existing_one(code):
    orch, seen = _orchestrator(lambda r: httpx.Response(code, json={"id": "approval-a1"}))
    assert await orch.start("a1") == "approval-a1"
    request = seen[0]
    assert request.url == "http://worker/approvals"
    assert request.headers["authorization"] == f"Bearer {WORKER_SECRET}"
    assert json.loads(request.content) == {"approval_id": "a1", "timeout_hours": 24}


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (lambda r: httpx.Response(202), None),
        (lambda r: httpx.Response(404), WorkflowGone),
        (lambda r: httpx.Response(409), WorkflowGone),
        (lambda r: httpx.Response(500), OrchestratorUnavailable),
        (lambda r: httpx.Response(401), OrchestratorUnavailable),
    ],
)
async def test_decide_maps_worker_answers(answer, error):
    orch, seen = _orchestrator(answer)
    call = orch.decide("a1", DecisionSignal(True, "m1", "ok"))
    if error is None:
        await call
    else:
        with pytest.raises(error):
            await call
    assert seen[0].url == "http://worker/approvals/a1/decision"
    assert json.loads(seen[0].content) == {"approve": True, "approver_id": "m1", "reason": "ok"}


async def test_unreachable_or_failing_worker_is_retryable():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    orch, _ = _orchestrator(down)
    with pytest.raises(OrchestratorUnavailable):
        await orch.start("a1")
    orch, _ = _orchestrator(lambda r: httpx.Response(503))
    with pytest.raises(OrchestratorUnavailable):
        await orch.start("a1")


# ---- the whole loop: outbox -> Worker, Workflow -> /internal callbacks ------------------------


class FakeWorker:
    """Stands in for workers/approvals: remembers instances and the decisions they received."""

    def __init__(self) -> None:
        self.instances: dict[str, list[dict]] = {}
        self.finished: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        parts = request.url.path.strip("/").split("/")
        if len(parts) == 1:
            iid = "approval-" + body["approval_id"]
            existed = iid in self.instances
            self.instances.setdefault(iid, [])
            return httpx.Response(200 if existed else 201, json={"id": iid})
        iid = "approval-" + parts[1]
        if iid not in self.instances:
            return httpx.Response(404)
        if iid in self.finished:
            return httpx.Response(409)
        self.instances[iid].append(body)
        return httpx.Response(202, json={"id": iid})


@pytest.fixture
def cloudflare_settings(settings):
    settings.approval_backend = "cloudflare"
    settings.approvals_worker_secret = type(settings.approvals_worker_secret)(WORKER_SECRET)
    settings.approvals_callback_secret = type(settings.approvals_callback_secret)(CALLBACK_SECRET)
    return settings


@pytest.fixture
async def cloud(cloudflare_settings, erp_transport):
    worker = FakeWorker()
    orch = CloudflareOrchestrator(
        "http://worker", WORKER_SECRET, 24, transport=httpx.MockTransport(worker)
    )
    cloudflare_settings.approval_backend = "inline"  # no background poller: tests drive it
    app = create_app(
        cloudflare_settings, erp_transport=erp_transport, llm=FakeLLM(), orchestrator=orch
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as api,
    ):
        cloudflare_settings.approval_backend = "cloudflare"
        yield api, app.state.core.db, orch, worker


CALLBACK = {"Authorization": f"Bearer {CALLBACK_SECRET}"}


async def _approved(api, db, orch):
    staff = await token(api, "s1", "staff")
    run = (await api.post("/agent/runs", json={"message": TRANSFER}, headers=staff)).json()
    approval_id = run["approvals"][0]["id"]
    assert await start_pending(db, orch) is True
    manager = await token(api, "m1", "manager")
    r = await api.post(
        f"/approvals/{approval_id}/decision", json={"approve": True}, headers=manager
    )
    assert r.status_code == 200 and r.json()["status"] == "approved"  # executed later
    return approval_id


async def test_decision_reaches_the_workflow_and_callback_executes_once(cloud, erp_app):
    api, db, orch, worker = cloud
    approval_id = await _approved(api, db, orch)
    await deliver_due(db, orch)
    assert worker.instances[f"approval-{approval_id}"] == [
        {"approve": True, "approver_id": "m1", "reason": None}
    ]
    async with db.sessionmaker() as s:
        assert await s.get(ApprovalDelivery, approval_id) is None

    # The Workflow's execute step, then a retry of it: one ERP write, same answer.
    first = await api.post(f"/internal/approvals/{approval_id}/execute", headers=CALLBACK)
    again = await api.post(f"/internal/approvals/{approval_id}/execute", headers=CALLBACK)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json() and first.json()["status"] == "executed"
    assert erp_app.state.store.write_count == 1

    # Late bookkeeping steps leave a finished approval alone.
    r = await api.post(f"/internal/approvals/{approval_id}/expire", headers=CALLBACK)
    assert r.json() == {"status": "executed"}
    r = await api.post(
        f"/internal/approvals/{approval_id}/fail", json={"error": "late"}, headers=CALLBACK
    )
    assert r.json() == {"status": "executed"}


async def test_erp_outage_is_retryable_then_permanent_errors_stop(cloud, erp_app):
    api, db, orch, _ = cloud
    approval_id = await _approved(api, db, orch)
    erp_app.state.error_rate = 1.0
    r = await api.post(f"/internal/approvals/{approval_id}/execute", headers=CALLBACK)
    assert r.status_code == 503  # the Workflow backs off and calls again
    erp_app.state.error_rate = 0.0

    async def refused(*args):
        raise httpx.ConnectError("All connection attempts failed")  # ERP host is down

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(approvals, "execute_approved", refused)
        r = await api.post(f"/internal/approvals/{approval_id}/execute", headers=CALLBACK)
    assert r.status_code == 503

    r = await api.post(
        f"/internal/approvals/{approval_id}/fail", json={"error": "gave up"}, headers=CALLBACK
    )
    assert r.json() == {"status": "failed"}
    r = await api.post(f"/internal/approvals/{approval_id}/execute", headers=CALLBACK)
    assert r.status_code == 422  # failed is final: the Workflow must not retry
    assert erp_app.state.store.write_count == 0


async def test_timeout_expires_a_pending_approval(cloud):
    api, db, orch, _ = cloud
    staff = await token(api, "s1", "staff")
    run = (await api.post("/agent/runs", json={"message": TRANSFER}, headers=staff)).json()
    approval_id = run["approvals"][0]["id"]
    r = await api.post(f"/internal/approvals/{approval_id}/expire", headers=CALLBACK)
    assert r.json() == {"status": "expired"}
    async with db.sessionmaker() as s:
        assert (await s.get(ApprovalRequest, approval_id)).status is ApprovalStatus.EXPIRED


async def test_decision_for_finished_workflow_fails_the_approval(cloud):
    api, db, orch, worker = cloud
    approval_id = await _approved(api, db, orch)
    worker.finished.add(f"approval-{approval_id}")
    await deliver_due(db, orch)
    async with db.sessionmaker() as s:
        approval = await s.get(ApprovalRequest, approval_id)
        assert approval.status is ApprovalStatus.FAILED and "not found" in approval.error


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": CALLBACK_SECRET}]
)
async def test_callbacks_need_the_callback_secret(cloud, headers):
    api, *_ = cloud
    r = await api.post("/internal/approvals/a1/expire", headers=headers)
    assert r.status_code == 401


async def test_callbacks_do_not_exist_on_other_backends(api):
    r = await api.post("/internal/approvals/a1/expire", headers=CALLBACK)
    assert r.status_code == 404


async def test_cloudflare_backend_refuses_to_start_without_secrets(settings):
    settings.approval_backend = "cloudflare"
    app = create_app(settings, llm=FakeLLM())
    with pytest.raises(ValueError, match="APPROVALS_WORKER_SECRET"):
        async with app.router.lifespan_context(app):
            pass


async def test_prod_needs_two_long_distinct_secrets(cloudflare_settings):
    cloudflare_settings.app_env = "prod"
    cloudflare_settings.approvals_callback_secret = cloudflare_settings.approvals_worker_secret
    app = create_app(cloudflare_settings, llm=FakeLLM())
    with pytest.raises(ValueError, match="two different"):
        async with app.router.lifespan_context(app):
            pass
