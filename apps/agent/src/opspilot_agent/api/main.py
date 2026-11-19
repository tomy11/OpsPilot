"""OpsPilot Agent service: `uvicorn opspilot_agent.api.main:app --port 8000`."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from opspilot_agent.api.delivery import run_delivery
from opspilot_agent.api.deps import Services
from opspilot_agent.api.orchestrator import (
    ApprovalOrchestrator,
    InlineOrchestrator,
    TemporalOrchestrator,
)
from opspilot_agent.api.routes import agent, approvals, demo
from opspilot_agent.config import AgentSettings, get_settings
from opspilot_agent.erp_client import ErpClient
from opspilot_agent.fake_llm import FakeLLM
from opspilot_agent.knowledge_client import KnowledgeClient
from opspilot_agent.models import AgentRun, ApprovalRequest
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
        if s.app_env == "prod":
            passwords = [
                s.demo_staff_password.get_secret_value(),
                s.demo_manager_password.get_secret_value(),
            ]
            if len(s.jwt_secret.encode()) < 32:
                raise ValueError("JWT_SECRET must have at least 32 bytes")
            if any(passwords) and (min(map(len, passwords)) < 16 or passwords[0] == passwords[1]):
                raise ValueError("configure distinct demo passwords of at least 16 characters")
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

        app.state.core = CoreState(
            settings=s,
            db=db,
            audit_targets=(
                (AgentRun.id, AgentRun.department),
                (ApprovalRequest.id, ApprovalRequest.department),
            ),
        )
        app.state.services = Services(settings=s, db=db, tools=tools, llm=model, orchestrator=orch)
        delivery = (
            asyncio.create_task(run_delivery(db, orch))
            if s.approval_backend == "temporal"
            else None
        )
        try:
            yield
        finally:
            if delivery is not None:
                delivery.cancel()
                with suppress(asyncio.CancelledError):
                    await delivery
            await erp.aclose()
            await kb.aclose()
            await db.dispose()

    app = FastAPI(title="OpsPilot Agent", version="0.1.0", lifespan=lifespan)
    app.include_router(core_router)
    app.include_router(agent.router)
    app.include_router(approvals.router)
    app.include_router(demo.router)
    app.mount(
        "/",
        StaticFiles(
            directory=Path(__file__).resolve().parents[1] / "web", html=True, check_dir=False
        ),
        name="web",
    )
    return app


app = create_app()
