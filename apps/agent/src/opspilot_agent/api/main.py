"""OpsPilot Agent service: `uvicorn opspilot_agent.api.main:app --port 8000`."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from opspilot_agent import models  # noqa: F401 - register tables before create_all
from opspilot_agent.api.deps import Services
from opspilot_agent.api.orchestrator import (
    ApprovalOrchestrator,
    InlineOrchestrator,
    TemporalOrchestrator,
)
from opspilot_agent.api.routes import agent, approvals
from opspilot_agent.config import AgentSettings, get_settings
from opspilot_agent.erp_client import ErpClient
from opspilot_agent.fake_llm import FakeLLM
from opspilot_agent.knowledge_client import KnowledgeClient
from opspilot_agent.tools import ToolContext
from opspilot_core.api import CoreState, core_router
from opspilot_core.db import Database
from opspilot_core.llm import AnthropicLLM, LLMClient


def create_app(
    settings: AgentSettings | None = None,
    *,
    erp_transport: httpx.AsyncBaseTransport | None = None,
    knowledge_transport: httpx.AsyncBaseTransport | None = None,
    llm: LLMClient | None = None,
    orchestrator: ApprovalOrchestrator | None = None,
) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO)
        db = Database(s.database_url)
        await db.create_all()
        erp = ErpClient(s.erp_base_url, timeout_s=s.erp_timeout_s, transport=erp_transport)
        kb = KnowledgeClient(
            s.knowledge_base_url,
            s,
            timeout_s=s.knowledge_timeout_s,
            transport=knowledge_transport,
        )
        tools = ToolContext(erp=erp, knowledge=kb)
        model = llm or (AnthropicLLM(s) if s.llm_provider == "anthropic" else FakeLLM())

        orch = orchestrator
        if orch is None:
            if s.approval_backend == "temporal":
                from temporalio.client import Client

                client = await Client.connect(s.temporal_host)
                orch = TemporalOrchestrator(client, s.temporal_task_queue, s.approval_timeout_hours)
            else:
                orch = InlineOrchestrator(db, tools)

        app.state.core = CoreState(settings=s, db=db)
        app.state.services = Services(settings=s, db=db, tools=tools, llm=model, orchestrator=orch)
        try:
            yield
        finally:
            await erp.aclose()
            await kb.aclose()
            await db.dispose()

    app = FastAPI(title="OpsPilot Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(core_router)
    app.include_router(agent.router)
    app.include_router(approvals.router)
    return app


app = create_app()
