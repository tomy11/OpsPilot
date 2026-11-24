"""Embeddings, re-indexing and pgvector. Tests using `database_url` run on SQLite and Postgres."""

import asyncio
import json
import math
import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

from opspilot_core.auth import Role, User
from opspilot_core.db import Database
from opspilot_core.testing import token
from opspilot_knowledge import pgvector
from opspilot_knowledge.embeddings import EmbeddingError, HashingEmbedder, OllamaEmbedder
from opspilot_knowledge.main import create_app
from opspilot_knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEmbedding,
)
from opspilot_knowledge.service import KnowledgeService, _access_filter

EVAL_CASES = [
    json.loads(line)
    for line in (Path(__file__).resolve().parents[1] / "evals" / "knowledge_cases.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
]
OPS = User(id="o", role=Role.STAFF, department="ops")
HR = User(id="h", role=Role.STAFF, department="hr")


class Meaning:
    """Stub model: texts about the same topic share a direction, whatever words they use."""

    min_vector = 0.6
    topics = {"holiday": 0, "ลาพัก": 0, "parking": 1, "จอดรถ": 1}

    def __init__(self, name="meaning", dims=4) -> None:
        self.name, self.dims, self.down = name, dims, False

    async def embed(self, texts, input_type):
        if self.down:
            raise EmbeddingError("model is down")
        out = []
        for t in texts:
            v = [0.0] * self.dims
            v[next((i for word, i in self.topics.items() if word in t), self.dims - 1)] = 1.0
            out.append(v)
        return out


@pytest.fixture
async def kb(database_url):
    db = Database(database_url)
    await db.create_all()
    service = KnowledgeService(db, Meaning())
    await service.setup()
    yield service
    await db.dispose()


async def add(kb, source, body, dept="ops", embedder=None):
    async with kb.db.sessionmaker() as s:
        r = await kb.ingest(
            s,
            source=source,
            title=source,
            content=body,
            departments=[dept],
            min_role=Role.STAFF,
            actor="t",
        )
        await s.commit()
        return r.document.id


async def sources(kb, user, query):
    return [h.source for h in await kb.search_as(user, query)]


async def models_in_use(kb):
    async with kb.db.sessionmaker() as s:
        return set((await s.scalars(select(KnowledgeEmbedding.model))).all())


# ---- Ollama client ----------------------------------------------------------------------------


async def test_ollama_embedder_batches_and_normalises():
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body["model"], len(body["input"])))
        return httpx.Response(200, json={"embeddings": [[3.0, 4.0]] * len(body["input"])})

    e = OllamaEmbedder(
        "http://ollama", "bge-m3", transport=httpx.MockTransport(handler), batch_size=2
    )
    assert await e.embed(["a", "b", "c"], "document") == [[0.6, 0.8]] * 3
    assert seen == [("/api/embed", "bge-m3", 2), ("/api/embed", "bge-m3", 1)]
    assert e.name == "ollama:bge-m3" and e.min_vector == 0.52


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": "model not found"}),
        httpx.Response(200, json={"embeddings": []}),
        httpx.Response(200, text="not json"),
    ],
)
async def test_ollama_failures_become_embedding_errors(response):
    e = OllamaEmbedder("http://ollama", "bge-m3", transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(EmbeddingError):
        await e.embed(["a"], "query")


def test_unknown_model_needs_an_explicit_threshold():
    with pytest.raises(ValueError, match="KNOWLEDGE_MIN_VECTOR"):
        OllamaEmbedder("http://ollama", "some-model")
    assert OllamaEmbedder("http://ollama", "some-model", min_vector=0.4).min_vector == 0.4


# ---- indexing ---------------------------------------------------------------------------------


async def test_search_by_meaning_without_shared_words(kb):
    await add(kb, "leave", "## A\nลาพักร้อนสิบวัน")
    await add(kb, "cars", "## B\nจอดรถชั้นสอง")
    await kb.index_missing()
    assert await sources(kb, OPS, "holiday allowance") == ["leave"]
    assert await sources(kb, OPS, "something else entirely") == []


async def test_upload_survives_a_model_outage_and_catches_up(kb):
    kb.embedder.down = True
    await add(kb, "leave", "## A\nลาพักร้อนสิบวัน annual")
    assert await kb.try_index() is False
    assert await sources(kb, OPS, "annual") == ["leave"]  # by words, query embedding down too
    assert await sources(kb, OPS, "holiday") == []
    kb.embedder.down = False
    assert await kb.try_index() is True
    assert await sources(kb, OPS, "holiday") == ["leave"]
    assert await kb.index_missing() == 0  # nothing left to do


async def test_changing_the_model_re_embeds_uploaded_documents_without_new_versions(kb):
    doc_id = await add(kb, "leave", "## A\nลาพักร้อนสิบวัน")
    await kb.index_missing()
    # Same size, different model: the stored vectors must not be compared with the new ones.
    kb.embedder = Meaning("other-model")
    assert await sources(kb, OPS, "holiday") == []
    assert await kb.index_missing() == 1
    assert await models_in_use(kb) == {"other-model"}
    assert await sources(kb, OPS, "holiday") == ["leave"]

    kb.embedder = Meaning("bigger-model", dims=9)  # and a different size
    await kb.index_missing()
    assert await sources(kb, OPS, "holiday") == ["leave"]
    assert await add(kb, "leave", "## A\nลาพักร้อนสิบวัน") == doc_id  # unchanged: same version


async def test_vectors_of_replaced_and_removed_documents_are_deleted(kb):
    await add(kb, "leave", "## A\nลาพักร้อนสิบวัน")
    await kb.index_missing()
    doc_id = await add(kb, "leave", "## A\nจอดรถชั้นสอง")
    await kb.index_missing()
    assert await sources(kb, OPS, "holiday") == []
    async with kb.db.sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(KnowledgeEmbedding)) == 1
        await kb.deactivate(s, doc_id)
        await s.commit()
        assert await s.scalar(select(func.count()).select_from(KnowledgeEmbedding)) == 0
    assert await sources(kb, OPS, "parking") == []


async def test_api_reports_whether_a_document_is_searchable_by_meaning(settings):
    model = Meaning()
    app = create_app(settings, embedder=model)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb") as api,
    ):
        h = await token(api, "m", "manager", "ops")
        doc = {"title": "T", "source": "s1", "content": "## A\nx1", "departments": ["ops"]}
        assert (await api.post("/knowledge/documents", json=doc, headers=h)).json()["indexed"]
        model.down = True
        r = await api.post("/knowledge/documents", json=doc | {"source": "s2"}, headers=h)
        assert r.status_code == 201 and r.json()["indexed"] is False


async def test_service_starts_while_the_model_is_down(settings):
    model = Meaning()
    model.down = True
    app = create_app(settings, embedder=model)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://kb") as api,
    ):
        h = await token(api, "s", "staff", "ops")
        r = await api.post("/knowledge/search", json={"query": "ลาพักร้อนได้กี่วัน"}, headers=h)
        assert r.json()[0]["source"] == "seed/leave-policy.md"


# ---- Postgres only ----------------------------------------------------------------------------


@pytest.fixture
async def pg(postgres_url):
    db = Database(postgres_url)
    await db.create_all()
    service = KnowledgeService(db, HashingEmbedder())
    await service.setup()
    yield service
    await db.dispose()


async def test_postgres_and_python_rank_the_same(pg):
    """Both databases must return the same hits, order and scores for every eval question."""
    lite = Database("sqlite+aiosqlite:///:memory:")
    await lite.create_all()
    other = KnowledgeService(lite, HashingEmbedder())
    await pg.seed_dir()
    await other.seed_dir()
    compared = 0
    for case in EVAL_CASES:
        role, dept = case["user"].split("@")
        user = User(id="u", role=Role(role), department=dept)
        a, b = [
            [
                (h.source, h.heading, h.score, h.lexical, round(h.vector, 3))
                for h in await service.search_as(user, case["question"])
            ]
            for service in (pg, other)
        ]
        assert a == b, case["id"]
        compared += len(a)
    await lite.dispose()
    assert compared > 20


async def _bulk(pg, dept: str, count: int, angle: float) -> None:
    """Many chunks for one department, all pointing close to `angle`."""
    async with pg.db.sessionmaker() as s:
        doc = KnowledgeDocument(
            source=f"bulk-{dept}",
            title=dept,
            content_hash=dept,
            departments=[dept],
            min_role="staff",
            min_role_rank=0,
            created_by="t",
        )
        s.add(doc)
        await s.flush()
        await s.execute(
            text("INSERT INTO knowledge_acl (document_id, department) VALUES (:d, :p)"),
            {"d": doc.id, "p": dept},
        )
        ids = []
        for i in range(count):
            c = KnowledgeChunk(document_id=doc.id, ordinal=i, heading=dept, text=f"{dept} {i}")
            s.add(c)
            ids.append(c)
        await s.flush()
        for i, c in enumerate(ids):
            a = angle + i * 1e-5
            s.add(
                KnowledgeEmbedding(chunk_id=c.id, model="m", vector=[math.cos(a), math.sin(a), 0.0])
            )
        await s.flush()
        await pgvector.store_vectors(s, [c.id for c in ids])
        await s.commit()
    async with pg.db.sessionmaker() as s:
        await pgvector.ensure_vector_index(s, 3)
        await s.commit()


async def test_hnsw_index_is_used_and_filtering_by_access_loses_nothing(pg):
    # 3,000 chunks HR may not read sit right next to the query; HR's own five are far away.
    await _bulk(pg, "ops", 3000, 0.0)
    await _bulk(pg, "hr", 5, 1.5)
    async with pg.db.sessionmaker() as s:
        # The planner would scan a table this small; make it take the index.
        for off in ("enable_seqscan", "enable_sort", "enable_hashjoin", "enable_mergejoin"):
            await s.execute(text(f"SET LOCAL {off} = off"))
        found = await pgvector.vector_candidates(s, _access_filter(HR), "m", [1.0, 0.0, 0.0])
        owners = set(
            (
                await s.scalars(
                    select(KnowledgeDocument.source)
                    .join(KnowledgeChunk)
                    .where(KnowledgeChunk.id.in_(found))
                )
            ).all()
        )
        scans = await s.scalar(
            text(
                "SELECT pg_stat_get_xact_numscans(oid) FROM pg_class "
                "WHERE relname = 'knowledge_embeddings_hnsw_3'"
            )
        )
        assert scans >= 1
        assert len(found) == 5 and owners == {"bulk-hr"}

        nearest = await pgvector.vector_candidates(s, _access_filter(OPS), "m", [1.0, 0.0, 0.0])
        assert len(nearest) == pgvector.CANDIDATES


async def test_lexical_shortlist_never_crosses_departments(pg):
    await add(pg, "ops-doc", "## A\nsecretword alpha", "ops")
    await add(pg, "hr-doc", "## A\nsecretword beta", "hr")
    async with pg.db.sessionmaker() as s:
        n, _, df = await pgvector.corpus_stats(s, _access_filter(HR), ["secretword", "alpha"])
        # Counts come from HR's documents only: even word statistics do not leak.
        assert (n, df) == (1, {"secretword": 1})
        ids = await pgvector.lexical_candidates(s, _access_filter(HR), {"secretword": 1.0})
        assert len(ids) == 1
    assert await sources(pg, HR, "secretword") == ["hr-doc"]


async def test_rare_words_win_a_place_in_the_lexical_shortlist(pg):
    for i in range(pgvector.CANDIDATES + 10):
        await add(pg, f"doc-{i:03}", f"## A\ncommon filler number{i}")
    await add(pg, "zz-target", "## A\ncommon zebra")
    async with pg.db.sessionmaker() as s:
        access = _access_filter(OPS)
        n, _, df = await pgvector.corpus_stats(s, access, ["common", "zebra"])
        assert (n, df["common"], df["zebra"]) == (61, 61, 1)
        ids = await pgvector.lexical_candidates(s, access, {"common": 0.1, "zebra": 5.0})
        assert len(ids) == pgvector.CANDIDATES
        target = await s.scalar(
            select(KnowledgeChunk.id)
            .join(KnowledgeDocument)
            .where(KnowledgeDocument.source == "zz-target")
        )
        assert ids[0] == target


async def test_vector_shortlist_only_compares_the_current_model(pg):
    await _bulk(pg, "ops", 3, 0.0)  # stored by model "m"
    async with pg.db.sessionmaker() as s:
        access = _access_filter(OPS)
        assert len(await pgvector.vector_candidates(s, access, "m", [1.0, 0.0, 0.0])) == 3
        assert await pgvector.vector_candidates(s, access, "other", [1.0, 0.0, 0.0]) == []


async def test_replicas_can_start_at_the_same_time(postgres_url, settings_for):
    apps = [create_app(settings_for(postgres_url)) for _ in range(3)]
    db = Database(postgres_url)
    await db.create_all()  # creating plain tables concurrently is a separate, older limitation
    await db.dispose()

    async def boot(app):
        async with app.router.lifespan_context(app):
            return await models_in_use(app.state.knowledge.service)

    results = await asyncio.gather(*(boot(a) for a in apps))
    assert results == [{"hash-512-3"}] * 3
    db = Database(postgres_url)
    async with db.sessionmaker() as s:
        active = await s.scalar(
            select(func.count(KnowledgeDocument.source.distinct())).where(
                KnowledgeDocument.active.is_(True)
            )
        )
        rows = await s.scalar(select(func.count()).where(KnowledgeDocument.active.is_(True)))
        assert active == rows == 7
    await db.dispose()


async def test_database_from_the_previous_release_is_upgraded_in_place(postgres_url, settings_for):
    """Old layout: vectors in knowledge_chunks.embedding, no token table, no extension."""
    db = Database(postgres_url)
    await db.create_all()
    async with db.engine.begin() as conn:
        await conn.execute(text("DROP TABLE knowledge_embeddings"))
    async with db.sessionmaker() as s:
        doc = KnowledgeDocument(
            source="uploaded/old",
            title="Old",
            content_hash="old-style-hash",
            departments=["ops"],
            min_role="staff",
            min_role_rank=0,
            created_by="m1",
        )
        s.add(doc)
        await s.flush()
        s.add(
            KnowledgeChunk(
                document_id=doc.id,
                ordinal=0,
                heading="Old",
                text="forklift certificate renewal",
                embedding=[0.1] * 512,
            )
        )
        await s.execute(
            text("INSERT INTO knowledge_acl (document_id, department) VALUES (:d, 'ops')"),
            {"d": doc.id},
        )
        await s.commit()
    await db.dispose()

    settings = settings_for(postgres_url)
    settings.knowledge_seed_on_startup = False  # upgrading must not depend on seeding
    for _ in range(2):  # a second start changes nothing
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            service = app.state.knowledge.service
            [hit] = await service.search_as(OPS, "forklift certificate renewal")
            assert hit.source == "uploaded/old" and hit.vector > 0.5 and hit.lexical > 0
            async with service.db.sessionmaker() as s:
                n, _, df = await pgvector.corpus_stats(s, _access_filter(OPS), ["forklift"])
                assert (n, df) == (1, {"forklift": 1})
                assert await s.scalar(select(func.max(KnowledgeDocument.version))) == 1


def test_ci_really_runs_the_postgres_tests():
    if os.getenv("CI"):
        assert os.getenv("TEST_POSTGRES_URL"), "Postgres tests would be skipped silently"
