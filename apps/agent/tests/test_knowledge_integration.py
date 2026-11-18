"""Agent -> Knowledge over HTTP: the agent searches with the user's identity, not its own."""

import httpx

from opspilot_agent.api.main import create_app
from opspilot_agent.config import AgentSettings
from opspilot_agent.fake_llm import FakeLLM
from opspilot_agent.knowledge_client import KnowledgeClient
from opspilot_core.auth import Role, User
from opspilot_core.testing import token

JWT_SECRET = "test-secret-with-at-least-32-bytes!!"

MESSAGE = "เช็ค ORD-1001 ถ้าของไม่พอโอนจาก bangna"


def _search_step(run: dict) -> dict:
    return next(s for s in run["steps"] if s.get("tool") == "search_knowledge")


async def test_agent_finds_sop_for_ops_staff(api):
    h = await token(api, "s1", "staff", "ops")
    run = (await api.post("/agent/runs", json={"message": MESSAGE}, headers=h)).json()
    assert _search_step(run)["status"] == "ok"


async def test_agent_search_uses_the_callers_access(knowledge_transport):
    """Call the client directly: a finance user must not get the ops-only SOP via the agent."""
    kc = KnowledgeClient(
        "http://knowledge",
        AgentSettings(jwt_secret=JWT_SECRET),
        transport=knowledge_transport,
    )
    try:
        q = "who approves a stock transfer"
        ops = await kc.search(User(id="o", role=Role.STAFF, department="ops"), q)
        fin = await kc.search(User(id="f", role=Role.STAFF, department="finance"), q)
    finally:
        await kc.aclose()
    assert ops[0]["source"] == "seed/stock-transfer.md"
    assert all(h["source"] != "seed/stock-transfer.md" for h in fin)


async def test_agent_survives_knowledge_outage(settings, erp_transport):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = create_app(
        settings,
        erp_transport=erp_transport,
        knowledge_transport=httpx.MockTransport(down),
        llm=FakeLLM(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as api:
            h = await token(api, "s1", "staff", "ops")
            r = await api.post("/agent/runs", json={"message": MESSAGE}, headers=h)
    assert r.status_code == 200
    # The run still completes and still proposes the transfer for approval.
    assert r.json()["approvals"]
