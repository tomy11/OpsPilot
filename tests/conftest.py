from collections.abc import AsyncIterator

import httpx
import pytest

from opspilot.agent.fake_llm import FakeLLM
from opspilot.api.main import create_app
from opspilot.core.config import Settings
from opspilot.mocks.erp_pos.app import create_app as create_erp_app


@pytest.fixture
def erp_app():
    return create_erp_app(error_rate=0.0, latency_ms=0)


@pytest.fixture
def erp_transport(erp_app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=erp_app)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        jwt_secret="test-secret-with-at-least-32-bytes!!",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        erp_base_url="http://erp",
        approval_backend="inline",
        llm_provider="fake",
    )


@pytest.fixture
async def api(settings, erp_transport) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, erp_transport=erp_transport, llm=FakeLLM())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield client


async def token(api: httpx.AsyncClient, user_id: str, role: str, dept: str = "ops") -> dict:
    r = await api.post(
        "/auth/dev-token", json={"user_id": user_id, "role": role, "department": dept}
    )
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
