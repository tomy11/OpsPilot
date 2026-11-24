import os
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from opspilot_knowledge.config import KnowledgeSettings
from opspilot_knowledge.main import create_app

# e.g. postgresql+asyncpg://opspilot:opspilot@localhost:55432/opspilot (needs pgvector). CI sets it.
POSTGRES = os.getenv("TEST_POSTGRES_URL", "")


@pytest.fixture
async def postgres_url() -> AsyncIterator[str]:
    """A new, empty database on the test server, dropped afterwards."""
    if not POSTGRES:
        pytest.skip("TEST_POSTGRES_URL is not set")
    admin = create_async_engine(POSTGRES, isolation_level="AUTOCOMMIT")
    name = f"kb_{uuid.uuid4().hex}"
    async with admin.connect() as conn:
        await conn.execute(text(f"CREATE DATABASE {name}"))
    try:
        yield POSTGRES.rsplit("/", 1)[0] + f"/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        await admin.dispose()


@pytest.fixture(params=["sqlite", "postgres"])
def database_url(request, tmp_path) -> str:
    """Every test that touches the database runs on both."""
    if request.param == "postgres":
        return request.getfixturevalue("postgres_url")
    return f"sqlite+aiosqlite:///{tmp_path / 'knowledge.db'}"


@pytest.fixture
def settings_for():
    def build(url: str) -> KnowledgeSettings:
        return KnowledgeSettings(
            _env_file=None,
            app_env="test",
            jwt_secret="test-secret-with-at-least-32-bytes!!",
            database_url=url,
            llm_provider="fake",
        )

    return build


@pytest.fixture
def settings(database_url, settings_for) -> KnowledgeSettings:
    return settings_for(database_url)


@pytest.fixture
async def api(settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://knowledge") as client:
            yield client
