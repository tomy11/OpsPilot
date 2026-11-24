"""Postgres side of retrieval: pgvector for meaning, a GIN token index for words.

SQLite (tests, local demo) scores every accessible chunk in Python. On Postgres this module picks
a short list in SQL instead, and the service scores that list with the same code, so both
databases rank alike. The access filter is part of every statement here: a chunk the user may not
read is never a candidate.

The extra columns and indexes are added at startup and only if missing, so existing databases are
upgraded in place. There is one HNSW index per vector size, because pgvector indexes need a fixed
size and different models produce different sizes.
"""

from typing import Any

from sqlalchemy import Float, Text, and_, bindparam, cast, column, func, select, table, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.types import UserDefinedType

from opspilot_knowledge.models import KnowledgeChunk, KnowledgeDocument, KnowledgeEmbedding

CANDIDATES = 50  # per ranker
MAX_INDEXED_DIMS = 2000  # pgvector HNSW limit for `vector`
MIN_VERSION = (0, 8)  # iterative index scans, needed because results are filtered by access
LOCK_KEY = 7_203_118  # arbitrary; serialises DDL between replicas

TOKENS = table(
    "knowledge_tokens",
    column("chunk_id", Text),
    column("tokens", ARRAY(Text)),
    column("length"),
)
EMB = table("knowledge_embeddings", column("chunk_id"), column("model"), column("dims"))


class Vector(UserDefinedType):
    cache_ok = True

    def __init__(self, dims: int) -> None:
        self.dims = int(dims)

    def get_col_spec(self, **kw: Any) -> str:
        return f"vector({self.dims})"


async def setup(conn: AsyncConnection) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
    await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    version = await conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))
    if tuple(int(x) for x in version.split(".")[:2]) < MIN_VERSION:
        raise RuntimeError(f"pgvector {version} is too old, 0.8 or newer is required")
    for ddl in (
        "ALTER TABLE knowledge_embeddings ADD COLUMN IF NOT EXISTS vec vector",
        "ALTER TABLE knowledge_embeddings ADD COLUMN IF NOT EXISTS dims integer",
        "CREATE TABLE IF NOT EXISTS knowledge_tokens ("
        "chunk_id varchar(32) PRIMARY KEY REFERENCES knowledge_chunks(id) ON DELETE CASCADE, "
        "tokens text[] NOT NULL, length integer NOT NULL)",
        "CREATE INDEX IF NOT EXISTS knowledge_tokens_gin ON knowledge_tokens USING gin (tokens)",
    ):
        await conn.execute(text(ddl))


async def lock_vectors(s: AsyncSession) -> None:
    """Writers of vectors and builders of the index take turns, across all replicas."""
    await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})


async def lock_source(s: AsyncSession, source: str) -> None:
    """One ingest at a time per source, so two replicas cannot both create a version."""
    await s.execute(
        text("SELECT pg_advisory_xact_lock(:k, hashtext(:s))"), {"k": 7203, "s": source}
    )


async def ensure_vector_index(s: AsyncSession, dims: int) -> None:
    """Run in its own transaction: building an index while holding row locks can deadlock."""
    name = f"knowledge_embeddings_hnsw_{int(dims)}"
    if dims > MAX_INDEXED_DIMS:
        return  # searched exactly instead
    if await s.scalar(text("SELECT to_regclass(:n)"), {"n": name}) is not None:
        return
    await lock_vectors(s)
    await s.execute(
        text(
            f"CREATE INDEX IF NOT EXISTS {name} ON knowledge_embeddings "
            f"USING hnsw ((CAST(vec AS vector({int(dims)}))) vector_cosine_ops) "
            f"WHERE dims = {int(dims)}"
        )
    )


async def store_vectors(s: AsyncSession, chunk_ids: list[str]) -> None:
    """Copy the JSON vectors of these chunks into the pgvector column."""
    await s.execute(
        text(
            "UPDATE knowledge_embeddings SET vec = CAST(CAST(vector AS text) AS vector), "
            "dims = json_array_length(vector) WHERE chunk_id = ANY(:ids)"
        ),
        {"ids": chunk_ids},
    )


async def store_tokens(s: AsyncSession, rows: list[tuple[str, list[str]]]) -> None:
    for chunk_id, tokens in rows:
        await s.execute(
            text(
                "INSERT INTO knowledge_tokens (chunk_id, tokens, length) VALUES (:c, :t, :n) "
                "ON CONFLICT (chunk_id) DO NOTHING"
            ),
            {"c": chunk_id, "t": sorted(set(tokens)), "n": len(tokens)},
        )


def distance(dims: int, query: list[float]):
    """`vec::vector(N) <=> query`, spelled exactly like the HNSW index expression."""
    vec = cast(column("vec", _selectable=KnowledgeEmbedding.__table__), Vector(dims))
    literal = "[" + ",".join(repr(float(x)) for x in query) + "]"
    return vec.op("<=>", return_type=Float)(cast(bindparam("q", literal, Text), Vector(dims)))


def _accessible(access: tuple, *cols):
    return (
        select(*cols)
        .select_from(KnowledgeChunk)
        .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(*access)
    )


async def vector_candidates(
    s: AsyncSession, access: tuple, model: str, query: list[float], limit: int = CANDIDATES
) -> list[str]:
    # Keep walking the index until enough *permitted* rows are found; without this a user whose
    # documents are far from the query would get fewer results than they have access to.
    await s.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    dims = len(query)
    stmt = (
        _accessible(access, KnowledgeChunk.id)
        .join(KnowledgeEmbedding, KnowledgeEmbedding.chunk_id == KnowledgeChunk.id)
        .where(
            KnowledgeEmbedding.model == model,
            text(f"knowledge_embeddings.dims = {int(dims)}"),  # literal: matches the partial index
        )
        .order_by(distance(dims, query))
        .limit(limit)
    )
    return list((await s.scalars(stmt)).all())


async def corpus_stats(
    s: AsyncSession, access: tuple, tokens: list[str]
) -> tuple[int, float, dict[str, int]]:
    """Corpus size, mean length and document frequencies over what this user can read."""
    base = _accessible(access, func.count(), func.avg(TOKENS.c.length)).join(
        TOKENS, TOKENS.c.chunk_id == KnowledgeChunk.id
    )
    n, avg = (await s.execute(base)).one()
    word = func.unnest(TOKENS.c.tokens).table_valued("t").render_derived(name="w")
    df = (
        _accessible(access, word.c.t, func.count())
        .join(TOKENS, TOKENS.c.chunk_id == KnowledgeChunk.id)
        .join(word, text("true"))
        .where(TOKENS.c.tokens.overlap(cast(tokens, ARRAY(Text))), word.c.t.in_(tokens))
        .group_by(word.c.t)
    )
    return int(n), float(avg or 0.0), {t: int(c) for t, c in (await s.execute(df)).all()}


async def lexical_candidates(
    s: AsyncSession, access: tuple, idf: dict[str, float], limit: int = CANDIDATES
) -> list[str]:
    if not idf:
        return []
    words = cast(list(idf), ARRAY(Text))
    weights = cast(list(idf.values()), ARRAY(Float))
    w = func.unnest(words, weights).table_valued("t", "idf").render_derived(name="w")
    stmt = (
        _accessible(access, KnowledgeChunk.id)
        .join(TOKENS, TOKENS.c.chunk_id == KnowledgeChunk.id)
        .join(w, and_(w.c.t == func.any(TOKENS.c.tokens)))
        .where(TOKENS.c.tokens.overlap(words))
        .group_by(KnowledgeChunk.id)
        .order_by(func.sum(w.c.idf).desc(), KnowledgeChunk.id)
        .limit(limit)
    )
    return list((await s.scalars(stmt)).all())
