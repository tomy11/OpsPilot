"""FastAPI app: `uvicorn aiw.api.main:app`."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from aiw.agent.erp_client import ErpClient
from aiw.agent.fake_llm import FakeLLM
from aiw.agent.sop import SopIndex
from aiw.agent.tools import ToolContext
from aiw.api.deps import Services
from aiw.api.orchestrator import ApprovalOrchestrator, InlineOrchestrator, TemporalOrchestrator
from aiw.api.routes import agent, approvals, misc
from aiw.core.config import Settings, get_settings
from aiw.core.db import Database
from aiw.core.llm import AnthropicLLM, LLMClient


def create_app(
    settings: Settings | None = None,
    *,
    erp_transport: httpx.AsyncBaseTransport | None = None,
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
        tools = ToolContext(erp=erp, sop=SopIndex())
        model = llm or (AnthropicLLM(s) if s.llm_provider == "anthropic" else FakeLLM())

        orch = orchestrator
        if orch is None:
            if s.approval_backend == "temporal":
                from temporalio.client import Client

                client = await Client.connect(s.temporal_host)
                orch = TemporalOrchestrator(client, s.temporal_task_queue, s.approval_timeout_hours)
            else:
                orch = InlineOrchestrator(db, tools)

        app.state.services = Services(settings=s, db=db, tools=tools, llm=model, orchestrator=orch)
        try:
            yield
        finally:
            await erp.aclose()
            await db.dispose()

    app = FastAPI(title="Enterprise AI Automation Hub", version="0.1.0", lifespan=lifespan)
    app.include_router(misc.router)
    app.include_router(agent.router)
    app.include_router(approvals.router)
    return app


app = create_app()
