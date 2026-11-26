import re
from datetime import timedelta

import httpx
import pytest

from opspilot_agent.api.delivery import deliver_due
from opspilot_agent.api.main import WEB_DIST, create_app
from opspilot_agent.api.orchestrator import OrchestratorUnavailable
from opspilot_agent.fake_llm import FakeLLM
from opspilot_agent.models import ApprovalDelivery, ApprovalRequest
from opspilot_core.db import utcnow
from opspilot_core.testing import token
from opspilot_erp_mock.app import create_app as create_erp_app

TRANSFER = "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna"


class IntermittentOrchestrator:
    attempts = 0

    async def decide(self, approval_id, signal):
        self.attempts += 1
        assert signal.approve and signal.approver_id == "demo-manager"
        if self.attempts == 1:
            raise OrchestratorUnavailable("temporarily unavailable")


async def test_staging_auth_and_decision_is_persisted_before_retry(settings, erp_transport):
    settings.app_env = "prod"
    settings.demo_staff_password = type(settings.demo_staff_password)("staff password test")
    settings.demo_manager_password = type(settings.demo_manager_password)("manager password test")
    orchestrator = IntermittentOrchestrator()
    app = create_app(settings, erp_transport=erp_transport, orchestrator=orchestrator)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as api,
    ):
        assert (await api.get("/ready")).status_code == 200
        assert (
            await api.post(
                "/auth/dev-token", json={"user_id": "x", "role": "admin", "department": "ops"}
            )
        ).status_code == 404
        assert (
            await api.post("/session/login", json={"username": "manager", "password": "wrong"})
        ).status_code == 401

        async def login(username):
            response = await api.post(
                "/session/login",
                json={"username": username, "password": f"{username} password test"},
            )
            assert response.status_code == 200
            return {"Authorization": f"Bearer {response.json()['access_token']}"}

        staff, manager = await login("staff"), await login("manager")
        approval = (
            await api.post("/agent/runs", headers=staff, json={"message": TRANSFER})
        ).json()["approvals"][0]
        db = app.state.core.db
        async with db.sessionmaker() as session:
            row = await session.get(ApprovalRequest, approval["id"])
            row.workflow_id = "approval-" + row.id
            await session.commit()
        settings.approval_backend = (
            "temporal"  # test delivery manually without the background poller
        )
        response = await api.post(
            f"/approvals/{approval['id']}/decision", headers=manager, json={"approve": True}
        )
        assert response.json()["status"] == "approved"
        assert orchestrator.attempts == 0
        await deliver_due(db, orchestrator)
        async with db.sessionmaker() as session:
            delivery = await session.get(ApprovalDelivery, approval["id"])
            assert delivery is not None
            delivery.next_attempt = utcnow() - timedelta(seconds=1)
            await session.commit()
        await deliver_due(db, orchestrator)
        async with db.sessionmaker() as session:
            assert await session.get(ApprovalDelivery, approval["id"]) is None
        assert orchestrator.attempts == 2


@pytest.mark.skipif(not (WEB_DIST / "index.html").is_file(), reason="frontend build not present")
async def test_frontend_build_is_served(api):
    response = await api.get("/")
    assert response.status_code == 200
    scripts = re.findall(r'src="(/assets/[^"]+\.js)"', response.text)
    styles = re.findall(r'href="(/assets/[^"]+\.css)"', response.text)
    assert scripts and styles
    for path in scripts + styles:
        assert (await api.get(path)).status_code == 200


async def test_mock_sandbox_persists_write_and_idempotency_across_restart(tmp_path):
    state_path = str(tmp_path / "erp.db")
    payload = {
        "sku": "SKU-COFFEE",
        "qty": 7,
        "from_branch": "bangna",
        "to_branch": "silom",
        "order_id": "ORD-1001",
    }
    headers = {"X-OpsPilot-Sandbox": "a" * 32, "Idempotency-Key": "approval"}
    first = create_erp_app(state_path=state_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url="http://erp"
    ) as api:
        response = await api.post("/stock-transfers", json=payload, headers=headers)
        assert response.status_code == 201
        transfer = response.json()
    restarted = create_erp_app(state_path=state_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://erp"
    ) as api:
        replay = await api.post("/stock-transfers", json=payload, headers=headers)
        assert replay.json() == transfer
        path = "/inventory?branch=silom&sku=SKU-COFFEE"
        stock = await api.get(path, headers=headers)
        assert stock.json()["stock"][0]["on_hand"] == 10
        fresh = await api.get(path, headers={"X-OpsPilot-Sandbox": "b" * 32})
        assert fresh.json()["stock"][0]["on_hand"] == 3


async def test_agent_runs_are_isolated_in_their_own_erp_sandbox(
    settings, knowledge_transport, tmp_path
):
    """ERP_ISOLATE_RUNS: every run (agent tools, approval execution, inventory view) hits its own
    seeded ERP scope, so repeated demo runs start from the same stock and never touch the shared one."""
    erp = create_erp_app(state_path=str(tmp_path / "erp.db"))
    settings.erp_isolate_runs = True
    app = create_app(
        settings,
        erp_transport=httpx.ASGITransport(app=erp),
        knowledge_transport=knowledge_transport,
        llm=FakeLLM(),
    )

    def stock(rows: dict) -> dict:
        return {r["branch"]: r["on_hand"] for r in rows["stock"]}

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://agent") as api,
    ):
        staff, manager = await token(api, "s1", "staff"), await token(api, "m1", "manager")
        for _ in range(2):
            run = (await api.post("/agent/runs", json={"message": TRANSFER}, headers=staff)).json()
            [approval] = run["approvals"]
            assert approval["args"]["qty"] == 7  # each run sees the seeded 3 units at silom
            path = f"/approvals/{approval['id']}/inventory"
            assert stock((await api.get(path, headers=manager)).json()) == {
                "silom": 3,
                "bangna": 50,
            }

            decided = await api.post(
                f"/approvals/{approval['id']}/decision", json={"approve": True}, headers=manager
            )
            assert decided.json()["status"] == "executed"
            assert stock((await api.get(path, headers=manager)).json()) == {
                "silom": 10,
                "bangna": 43,
            }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=erp), base_url="http://erp"
    ) as e:
        shared = stock((await e.get("/inventory?sku=SKU-COFFEE")).json())
    assert shared == {"silom": 3, "bangna": 50, "ladprao": 8}
