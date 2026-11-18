from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from opspilot_docflow.config import DocflowSettings
from opspilot_docflow.main import create_app
from opspilot_erp_mock.app import create_app as create_erp_app

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


@pytest.fixture
def erp_app():
    return create_erp_app(error_rate=0.0, latency_ms=0)


@pytest.fixture
def settings(tmp_path) -> DocflowSettings:
    return DocflowSettings(
        app_env="test",
        jwt_secret="test-secret-with-at-least-32-bytes!!",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'docflow.db'}",
        erp_base_url="http://erp",
        llm_provider="fake",
    )


@pytest.fixture
async def api(settings, erp_app) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, erp_transport=httpx.ASGITransport(app=erp_app))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://docflow") as client:
            yield client
