"""OpsPilot Knowledge service: `uvicorn opspilot_knowledge.main:app --port 8200`."""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from opspilot_core.api import CoreState, core_router
from opspilot_core.db import Database
from opspilot_core.llm import AnthropicLLM, LLMClient
from opspilot_knowledge import models  # noqa: F401 - register tables before create_all
from opspilot_knowledge.answer import FakeGroundedLLM
from opspilot_knowledge.config import KnowledgeSettings, get_settings
from opspilot_knowledge.embeddings import (
    Embedder,
    HashingEmbedder,
    OllamaEmbedder,
    VoyageEmbedder,
)
from opspilot_knowledge.models import KnowledgeAcl
from opspilot_knowledge.routes import KnowledgeState, router
from opspilot_knowledge.service import DEFAULT_SEED_DIR, KnowledgeService


def build_embedder(s: KnowledgeSettings) -> Embedder:
    if s.embedding_provider == "ollama":
        return OllamaEmbedder(
            s.ollama_embed_url, s.ollama_embed_model, min_vector=s.knowledge_min_vector
        )
    if s.embedding_provider == "voyage":
        return VoyageEmbedder(s.voyage_api_key, s.embedding_model)
    return HashingEmbedder()


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
        emb = embedder or build_embedder(s)
        kb = KnowledgeService(db, emb, top_k=s.knowledge_top_k, min_vector=s.knowledge_min_vector)
        await kb.setup()
        if s.knowledge_seed_on_startup:
            await kb.seed_dir(
                Path(s.knowledge_seed_dir) if s.knowledge_seed_dir else DEFAULT_SEED_DIR
            )
        await kb.try_index()  # documents uploaded earlier, or embedded by another model
        indexer = asyncio.create_task(kb.run_indexer())
        model = llm or (AnthropicLLM(s) if s.llm_provider == "anthropic" else FakeGroundedLLM())

        # Managers see document events for documents their department can read (via the ACL rows),
        # and questions asked by people in their department.
        app.state.core = CoreState(
            settings=s,
            db=db,
            audit_targets=((KnowledgeAcl.document_id, KnowledgeAcl.department),),
            audit_department_prefix="knowledge.ask",
        )
        app.state.knowledge = KnowledgeState(service=kb, llm=model)
        try:
            yield
        finally:
            indexer.cancel()
            with suppress(asyncio.CancelledError):
                await indexer
            if hasattr(emb, "aclose"):
                await emb.aclose()
            await db.dispose()

    app = FastAPI(title="OpsPilot Knowledge", version="0.1.0", lifespan=lifespan)
    app.include_router(core_router)
    app.include_router(router)
    return app


app = create_app()
