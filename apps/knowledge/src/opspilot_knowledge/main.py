"""OpsPilot Knowledge service: `uvicorn opspilot_knowledge.main:app --port 8200`."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from opspilot_core.api import CoreState, core_router
from opspilot_core.db import Database
from opspilot_core.llm import AnthropicLLM, LLMClient
from opspilot_knowledge import models  # noqa: F401 - register tables before create_all
from opspilot_knowledge.answer import FakeGroundedLLM
from opspilot_knowledge.config import KnowledgeSettings, get_settings
from opspilot_knowledge.embeddings import Embedder, HashingEmbedder, VoyageEmbedder
from opspilot_knowledge.routes import KnowledgeState, router
from opspilot_knowledge.service import DEFAULT_SEED_DIR, KnowledgeService


def create_app(
    settings: KnowledgeSettings | None = None,
    *,
    llm: LLMClient | None = None,
    embedder: Embedder | None = None,
) -> FastAPI:
    s = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO)
        db = Database(s.database_url)
        await db.create_all()
        emb = embedder or (
            VoyageEmbedder(s.voyage_api_key, s.embedding_model)
            if s.embedding_provider == "voyage"
            else HashingEmbedder()
        )
        kb = KnowledgeService(db, emb, top_k=s.knowledge_top_k)
        if s.knowledge_seed_on_startup:
            await kb.seed_dir(
                Path(s.knowledge_seed_dir) if s.knowledge_seed_dir else DEFAULT_SEED_DIR
            )
        model = llm or (AnthropicLLM(s) if s.llm_provider == "anthropic" else FakeGroundedLLM())

        app.state.core = CoreState(settings=s, db=db)
        app.state.knowledge = KnowledgeState(service=kb, llm=model)
        try:
            yield
        finally:
            if isinstance(emb, VoyageEmbedder):
                await emb.aclose()
            await db.dispose()

    app = FastAPI(title="OpsPilot Knowledge", version="0.1.0", lifespan=lifespan)
    app.include_router(core_router)
    app.include_router(router)
    return app


app = create_app()
