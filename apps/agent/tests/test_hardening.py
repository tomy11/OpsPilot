"""Bounded approval delivery and demo-login throttling."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from temporalio.service import RPCError, RPCStatusCode

from opspilot_agent.api.delivery import deliver_due, retry_delay, start_pending
from opspilot_agent.api.login_limit import LoginLimiter
from opspilot_agent.api.main import create_app
from opspilot_agent.api.orchestrator import TemporalOrchestrator
from opspilot_agent.fake_llm import FakeLLM
from opspilot_agent.models import (
    AgentRun,
    ApprovalDelivery,
    ApprovalRequest,
    ApprovalStatus,
)
from opspilot_core.db import AuditLog, Database, utcnow


class ScriptedClient:
    """A Temporal client whose start/signal fail with a scripted RPC error (or succeed)."""

    def __init__(self, error: RPCStatusCode | None, owner: "Orchestrator") -> None:
        self.error, self.owner = error, owner

    def _raise(self) -> None:
        if self.error is not None:
            raise RPCError("scripted", self.error, b"")

    async def start_workflow(self, *args, id, **kwargs):
        self._raise()
        self.owner.started.append(id.removeprefix("approval-"))

    def get_workflow_handle(self, workflow_id):
        client = self

        class Handle:
            async def signal(self, signal_def, signal):
                client.owner.signals.append((workflow_id.removeprefix("approval-"), signal.approve))
                client._raise()

        return Handle()


class Orchestrator(TemporalOrchestrator):
    """The real Temporal orchestrator over a scripted client, so its error mapping is tested."""

    def __init__(self, error: RPCStatusCode | None = None) -> None:
        super().__init__(ScriptedClient(error, self), "q", 24)
        self.started, self.signals = [], []


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}")
    await database.create_all()
    yield database
    await database.dispose()


async def _approval(db, status=ApprovalStatus.PENDING, *, age=timedelta(0), delivery=False):
    async with db.sessionmaker() as s:
        run = AgentRun(user_id="s1", department="ops", message="m")
        s.add(run)
        await s.flush()
        when = utcnow() - age
        a = ApprovalRequest(
            run_id=run.id,
            tool="create_stock_transfer",
            args={},
            summary="t",
            requested_by="s1",
            department="ops",
            status=status,
            decided_by="m1" if status is not ApprovalStatus.PENDING else None,
            workflow_id="approval-x" if delivery else None,
            created_at=when,
            updated_at=when,
        )
        s.add(a)
        await s.flush()
        if delivery:
            s.add(ApprovalDelivery(approval_id=a.id))
        await s.commit()
        return a.id


async def _state(db, approval_id):
    async with db.sessionmaker() as s:
        a = await s.get(ApprovalRequest, approval_id)
        d = await s.get(ApprovalDelivery, approval_id)
        actions = (
            await s.scalars(select(AuditLog.action).where(AuditLog.target == approval_id))
        ).all()
        return a, d, actions


# ---- delivery ---------------------------------------------------------------------------------


def test_retry_delay_grows_with_decision_age_within_bounds():
    now = utcnow()
    assert retry_delay(now, now) == timedelta(seconds=5)
    assert retry_delay(now - timedelta(minutes=10), now) == timedelta(minutes=1)
    assert retry_delay(now - timedelta(days=2), now) == timedelta(minutes=5)


async def test_approved_decision_for_missing_workflow_fails_once(db):
    aid = await _approval(db, ApprovalStatus.APPROVED, delivery=True)
    await deliver_due(db, Orchestrator(RPCStatusCode.NOT_FOUND))
    a, d, actions = await _state(db, aid)
    assert a.status is ApprovalStatus.FAILED and "not found" in a.error
    assert d is None and actions == ["approval.failed"]


async def test_rejected_decision_for_missing_workflow_is_just_dropped(db):
    aid = await _approval(db, ApprovalStatus.REJECTED, delivery=True)
    await deliver_due(db, Orchestrator(RPCStatusCode.NOT_FOUND))
    a, d, actions = await _state(db, aid)
    assert a.status is ApprovalStatus.REJECTED and d is None and actions == []


async def test_transient_error_backs_off_instead_of_hammering(db):
    aid = await _approval(db, ApprovalStatus.APPROVED, age=timedelta(minutes=30), delivery=True)
    orch = Orchestrator(RPCStatusCode.UNAVAILABLE)
    await deliver_due(db, orch)
    await deliver_due(db, orch)  # not due yet: must not signal again
    a, d, _ = await _state(db, aid)
    assert a.status is ApprovalStatus.APPROVED and len(orch.signals) == 1
    wait = d.next_attempt.replace(tzinfo=None) - utcnow().replace(tzinfo=None)
    assert timedelta(minutes=2) < wait <= timedelta(minutes=3)  # 10% of 30 minutes


async def test_start_pending_expires_stale_and_reports_outage(db):
    stale = await _approval(db, age=timedelta(hours=25))
    fresh = await _approval(db)
    down = Orchestrator(RPCStatusCode.UNAVAILABLE)

    assert await start_pending(db, down, timeout_hours=24) is False
    a_stale, _, actions = await _state(db, stale)
    a_fresh, _, _ = await _state(db, fresh)
    assert a_stale.status is ApprovalStatus.EXPIRED and actions == ["approval.expired"]
    assert a_fresh.status is ApprovalStatus.PENDING and a_fresh.workflow_id is None

    up = Orchestrator()
    assert await start_pending(db, up, timeout_hours=24) is True
    a_fresh, _, _ = await _state(db, fresh)
    assert up.started == [fresh] and a_fresh.workflow_id == f"approval-{fresh}"


# ---- login throttling -------------------------------------------------------------------------


def test_limiter_windows():
    now = [0.0]
    lim = LoginLimiter(per_ip=3, per_user=5, window_s=60, clock=lambda: now[0])
    for _ in range(3):
        lim.failed("1.1.1.1", "manager")
    assert lim.retry_after("1.1.1.1", "manager") == 60
    assert lim.retry_after("2.2.2.2", "manager") is None  # other IP, user bucket not full
    for ip in ("2.2.2.2", "3.3.3.3"):
        lim.failed(ip, "manager")
    assert lim.retry_after("4.4.4.4", "manager") == 60  # distributed guessing hits user cap
    now[0] = 61
    assert lim.retry_after("1.1.1.1", "manager") is None  # window slid past
    lim.failed("5.5.5.5", "staff")  # the next failure sweeps every expired key
    assert set(lim._failures) == {("ip", "5.5.5.5"), ("user", "staff")}


async def test_login_is_throttled_per_client_ip(settings, erp_transport, knowledge_transport):
    settings.demo_manager_password = type(settings.demo_manager_password)("correct horse battery")
    app = create_app(
        settings,
        erp_transport=erp_transport,
        knowledge_transport=knowledge_transport,
        llm=FakeLLM(),
    )

    def client(ip):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(ip, 1234)), base_url="http://agent"
        )

    good = {"username": "manager", "password": "correct horse battery"}
    bad = good | {"password": "guess"}
    async with app.router.lifespan_context(app), client("10.0.0.1") as a, client("10.0.0.2") as b:
        for _ in range(5):
            assert (await a.post("/session/login", json=bad)).status_code == 401
        blocked = await a.post("/session/login", json=good)
        assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) > 0
        assert (await b.post("/session/login", json=good)).status_code == 200
        for name in ("x1", "x2", "x3"):  # unknown names share one bucket
            await b.post("/session/login", json={"username": name, "password": "p"})
        assert set(app.state.login_limiter._failures) == {
            ("ip", "10.0.0.1"),
            ("ip", "10.0.0.2"),  # b's success cleared it, then the 3 unknown-name failures
            ("user", "manager"),
            ("user", "?"),  # unknown names share one bucket
        }
