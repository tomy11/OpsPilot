from collections.abc import AsyncIterator

import httpx
import pytest

from opspilot_knowledge.config import KnowledgeSettings
from opspilot_knowledge.main import create_app


@pytest.fixture
def settings(tmp_path) -> KnowledgeSettings:
    return KnowledgeSettings(
        app_env="test",
        jwt_secret="test-secret-with-at-least-32-bytes!!",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}",
        llm_provider="fake",
    )


@pytest.fixture
async def api(settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://knowledge") as client:
            yield client
