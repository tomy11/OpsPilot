"""OpsPilot DocFlow service: `uvicorn opspilot_docflow.main:app --port 8300`."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from opspilot_core.api import CoreState, core_router
from opspilot_core.db import Database
from opspilot_core.llm import AnthropicLLM
from opspilot_docflow.config import DocflowSettings, get_settings
from opspilot_docflow.erp import ErpClient
from opspilot_docflow.extractors import ClaudeExtractor, Extractor, TextExtractor
from opspilot_docflow.models import DocflowDocument
from opspilot_docflow.routes import router
from opspilot_docflow.service import DocflowService


def create_app(
    settings: DocflowSettings | None = None,
    *,
    erp_transport: httpx.AsyncBaseTransport | None = None,
    extractor: Extractor | None = None,
) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO)
        db = Database(s.database_url)
        await db.create_all()
        erp = ErpClient(s.erp_base_url, timeout_s=s.erp_timeout_s, transport=erp_transport)
        ext = extractor or (
            ClaudeExtractor(AnthropicLLM(s)) if s.llm_provider == "anthropic" else TextExtractor()
        )
        app.state.core = CoreState(
            settings=s, db=db, audit_targets=((DocflowDocument.id, DocflowDocument.department),)
        )
        app.state.docflow = DocflowService(
            erp=erp, extractor=ext, vat_rate=s.vat_rate, auto_post_limit=s.auto_post_limit
        )
        try:
            yield
        finally:
            await erp.aclose()
            await db.dispose()

    app = FastAPI(title="OpsPilot DocFlow", version="0.1.0", lifespan=lifespan)
    app.include_router(core_router)
    app.include_router(router)
    return app


app = create_app()
