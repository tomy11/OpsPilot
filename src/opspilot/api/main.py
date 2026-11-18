"""FastAPI app: `uvicorn opspilot.api.main:app`."""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from opspilot.agent.erp_client import ErpClient
from opspilot.agent.fake_llm import FakeLLM
from opspilot.agent.tools import ToolContext
from opspilot.api.deps import Services
from opspilot.api.orchestrator import ApprovalOrchestrator, InlineOrchestrator, TemporalOrchestrator
from opspilot.api.routes import agent, approvals, knowledge, misc
from opspilot.core.config import Settings, get_settings
from opspilot.core.db import Database
from opspilot.core.llm import AnthropicLLM, LLMClient
from opspilot.knowledge.answer import FakeGroundedLLM
from opspilot.knowledge.embeddings import Embedder, HashingEmbedder, VoyageEmbedder
from opspilot.knowledge.service import KnowledgeService


def create_app(
    settings: Settings | None = None,
    *,
    erp_transport: httpx.AsyncBaseTransport | None = None,
    llm: LLMClient | None = None,
    knowledge_llm: LLMClient | None = None,
    embedder: Embedder | None = None,
    orchestrator: ApprovalOrchestrator | None = None,
) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO)
        db = Database(s.database_url)
        await db.create_all()
        erp = ErpClient(s.erp_base_url, timeout_s=s.erp_timeout_s, transport=erp_transport)
        emb = embedder or (
            VoyageEmbedder(s.voyage_api_key, s.embedding_model)
            if s.embedding_provider == "voyage"
            else HashingEmbedder()
        )
        kb = KnowledgeService(db, emb, top_k=s.knowledge_top_k)
        if s.knowledge_seed_on_startup:
            await kb.seed_dir()
        tools = ToolContext(erp=erp, knowledge=kb)
        real = AnthropicLLM(s) if s.llm_provider == "anthropic" else None
        model = llm or real or FakeLLM()
        kb_model = knowledge_llm or real or FakeGroundedLLM()

        orch = orchestrator
        if orch is None:
            if s.approval_backend == "temporal":
                from temporalio.client import Client

                client = await Client.connect(s.temporal_host)
                orch = TemporalOrchestrator(client, s.temporal_task_queue, s.approval_timeout_hours)
            else:
                orch = InlineOrchestrator(db, tools)

        app.state.services = Services(
            settings=s,
            db=db,
            tools=tools,
            llm=model,
            orchestrator=orch,
            knowledge=kb,
            knowledge_llm=kb_model,
        )
        try:
            yield
        finally:
            await erp.aclose()
            if isinstance(emb, VoyageEmbedder):
                await emb.aclose()
            await db.dispose()

    app = FastAPI(title="OpsPilot", version="0.1.0", lifespan=lifespan)
    app.include_router(misc.router)
    app.include_router(agent.router)
    app.include_router(approvals.router)
    app.include_router(knowledge.router)
    return app


app = create_app()
