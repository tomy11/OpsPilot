from collections.abc import AsyncIterator

import httpx
import pytest

from opspilot_agent.api.main import create_app
from opspilot_agent.config import AgentSettings
from opspilot_agent.fake_llm import FakeLLM
from opspilot_erp_mock.app import create_app as create_erp_app
from opspilot_knowledge.config import KnowledgeSettings
from opspilot_knowledge.main import create_app as create_knowledge_app

JWT_SECRET = "test-secret-with-at-least-32-bytes!!"  # shared by both services, as in prod
# (test_knowledge_integration.py repeats it: importlib mode cannot import conftest)


@pytest.fixture
def erp_app():
    return create_erp_app(error_rate=0.0, latency_ms=0)


@pytest.fixture
def erp_transport(erp_app) -> httpx.ASGITransport:
    return httpx.ASGITransport(app=erp_app)


@pytest.fixture
async def knowledge_transport(tmp_path) -> AsyncIterator[httpx.ASGITransport]:
    """The real Knowledge service, in-process, reached over HTTP like in production."""
    app = create_knowledge_app(
        KnowledgeSettings(
            app_env="test",
            jwt_secret=JWT_SECRET,
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}",
        )
    )
    async with app.router.lifespan_context(app):
        yield httpx.ASGITransport(app=app)


@pytest.fixture
def settings(tmp_path) -> AgentSettings:
    return AgentSettings(
        app_env="test",
        jwt_secret=JWT_SECRET,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}",
        erp_base_url="http://erp",
        knowledge_base_url="http://knowledge",
        approval_backend="inline",
        llm_provider="fake",
    )


@pytest.fixture
async def api(settings, erp_transport, knowledge_transport) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        settings,
        erp_transport=erp_transport,
        knowledge_transport=knowledge_transport,
        llm=FakeLLM(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield client
