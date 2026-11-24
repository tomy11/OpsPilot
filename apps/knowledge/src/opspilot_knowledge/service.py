"""Ingestion and permission-aware hybrid retrieval.

Retrieval = SQL access filter -> BM25 (lexical) + cosine (vector) -> Reciprocal Rank Fusion.
On SQLite every accessible chunk is scored. On Postgres, `pgvector.py` first picks a short list
with the vector and token indexes; the access filter is in the WHERE clause either way, and the
same code below scores and gates the result.

Documents are stored before they are embedded. If the embedding model is down, uploads still work
and are found by their words; `index_missing` adds the vectors once the model answers again, and
also re-embeds everything after the model is changed.
"""

import asyncio
import hashlib
import logging
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_core.auth import Role, User
from opspilot_core.db import Database
from opspilot_knowledge import pgvector
from opspilot_knowledge.chunking import chunk_markdown, parse_front_matter
from opspilot_knowledge.embeddings import Embedder, EmbeddingError, cosine
from opspilot_knowledge.models import (
    ALL_DEPARTMENTS,
    KnowledgeAcl,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeEmbedding,
)
from opspilot_knowledge.text import tokenize

log = logging.getLogger(__name__)

ROLE_RANK = {Role.STAFF: 0, Role.MANAGER: 1, Role.ADMIN: 2}
DEFAULT_SEED_DIR = Path(__file__).resolve().parents[2] / "seed"


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    title: str
    source: str
    version: int
    heading: str
    text: str
    score: float
    lexical: float
    vector: float


@dataclass
class IngestResult:
    document: KnowledgeDocument
    created: bool
    chunks: int


def can_read(user: User, doc: KnowledgeDocument) -> bool:
    depts = set(doc.departments)
    return ROLE_RANK[user.role] >= doc.min_role_rank and bool(
        depts & {user.department, ALL_DEPARTMENTS}
    )


def _access_filter(user: User):
    return (
        KnowledgeDocument.active.is_(True),
        KnowledgeDocument.min_role_rank <= ROLE_RANK[user.role],
        exists().where(
            KnowledgeAcl.document_id == KnowledgeDocument.id,
            KnowledgeAcl.department.in_([user.department, ALL_DEPARTMENTS]),
        ),
    )


def _read_markdown_dir(path: Path) -> list[tuple[str, str]]:
    return [(f.name, f.read_text(encoding="utf-8")) for f in sorted(path.glob("*.md"))]


def _idf(n: int, df: int) -> float:
    return math.log(1 + (n - df + 0.5) / (df + 0.5))


@dataclass
class Corpus:
    """Word statistics over the chunks this user can read."""

    n: int
    avg_len: float
    df: dict[str, int]

    @classmethod
    def of(cls, docs: list[list[str]]) -> "Corpus":
        n = len(docs)
        return cls(
            n, sum(map(len, docs)) / n if n else 0.0, Counter(t for d in docs for t in set(d))
        )


def _bm25(
    query: list[str], docs: list[list[str]], corpus: Corpus, k1: float = 1.2, b: float = 0.75
):
    q = set(query)
    scores, coverage = [], []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                norm = 1 - b + b * len(d) / (corpus.avg_len or 1)
                s += _idf(corpus.n, corpus.df.get(t, 0)) * tf[t] * (k1 + 1) / (tf[t] + k1 * norm)
        scores.append(s)
        coverage.append(len(q & tf.keys()) / len(q) if q else 0.0)
    return scores, coverage


def _chunk_tokens(chunk: KnowledgeChunk) -> list[str]:
    return tokenize(f"{chunk.heading} {chunk.text}")


class KnowledgeService:
    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        *,
        top_k: int = 5,
        min_coverage: float = 0.5,
        min_vector: float | None = None,
        rrf_k: int = 60,
    ) -> None:
        self.db, self.embedder = db, embedder
        self.top_k, self.min_coverage, self.rrf_k = top_k, min_coverage, rrf_k
        self.min_vector = embedder.min_vector if min_vector is None else min_vector
        self.postgres = db.engine.dialect.name == "postgresql"

    async def setup(self) -> None:
        """Create the Postgres extension, columns and indexes. Call after `create_all`."""
        if self.postgres:
            async with self.db.engine.begin() as conn:
                await pgvector.setup(conn)

    # ---- ingestion -------------------------------------------------------------------------

    async def ingest(
        self,
        s: AsyncSession,
        *,
        source: str,
        title: str,
        content: str,
        departments: list[str],
        min_role: Role,
        actor: str,
    ) -> IngestResult:
        departments = sorted({d.strip() for d in departments if d.strip()})
        if not departments:
            raise ValueError("at least one department (or '*') is required")
        digest = hashlib.sha256(
            "\x1f".join([title, content, ",".join(departments), min_role.value]).encode()
        ).hexdigest()

        if self.postgres:
            await pgvector.lock_source(s, source)
        current = await s.scalar(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source == source, KnowledgeDocument.active.is_(True)
            )
        )
        if current is not None and current.content_hash == digest:
            return IngestResult(current, created=False, chunks=0)

        chunks = chunk_markdown(content, title)
        if not chunks:
            raise ValueError("document has no text")
        latest = await s.scalar(
            select(KnowledgeDocument.version)
            .where(KnowledgeDocument.source == source)
            .order_by(KnowledgeDocument.version.desc())
            .limit(1)
        )
        # Old versions stay for audit/citation history but drop out of retrieval.
        await self._drop_vectors(s, KnowledgeDocument.source == source)
        await s.execute(
            update(KnowledgeDocument).where(KnowledgeDocument.source == source).values(active=False)
        )
        doc = KnowledgeDocument(
            source=source,
            title=title,
            version=(latest or 0) + 1,
            content_hash=digest,
            departments=departments,
            min_role=min_role.value,
            min_role_rank=ROLE_RANK[min_role],
            created_by=actor,
        )
        s.add(doc)
        await s.flush()
        s.add_all(KnowledgeAcl(document_id=doc.id, department=d) for d in departments)
        rows = [
            KnowledgeChunk(document_id=doc.id, ordinal=i, heading=c.heading, text=c.text)
            for i, c in enumerate(chunks)
        ]
        s.add_all(rows)
        await s.flush()
        if self.postgres:
            await pgvector.store_tokens(s, [(r.id, _chunk_tokens(r)) for r in rows])
        return IngestResult(doc, created=True, chunks=len(chunks))

    async def deactivate(self, s: AsyncSession, document_id: str) -> None:
        await s.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id)
            .values(active=False)
        )
        await self._drop_vectors(s, KnowledgeDocument.id == document_id)

    async def _drop_vectors(self, s: AsyncSession, condition) -> None:
        chunks = select(KnowledgeChunk.id).join(KnowledgeDocument).where(condition)
        await s.execute(delete(KnowledgeEmbedding).where(KnowledgeEmbedding.chunk_id.in_(chunks)))

    # ---- vectors ---------------------------------------------------------------------------

    async def index_missing(self, batch: int = 64) -> int:
        """Embed every active chunk that has no vector from the current model. Returns the count.

        Raises EmbeddingError when the model is unreachable; nothing already stored is lost.
        """
        if self.postgres:
            await self._backfill_tokens()
        done = 0
        while True:
            async with self.db.sessionmaker() as s:
                chunks = (
                    await s.scalars(
                        select(KnowledgeChunk)
                        .join(KnowledgeDocument)
                        .outerjoin(
                            KnowledgeEmbedding, KnowledgeEmbedding.chunk_id == KnowledgeChunk.id
                        )
                        .where(
                            KnowledgeDocument.active.is_(True),
                            or_(
                                KnowledgeEmbedding.chunk_id.is_(None),
                                KnowledgeEmbedding.model != self.embedder.name,
                            ),
                        )
                        .order_by(KnowledgeChunk.id)
                        .limit(batch)
                    )
                ).all()
            if not chunks:
                return done
            # No transaction is open while the model works.
            vectors = await self.embedder.embed(
                [f"{c.heading}\n{c.text}" for c in chunks], "document"
            )
            try:
                async with self.db.sessionmaker() as s:
                    if self.postgres:
                        await pgvector.lock_vectors(s)
                    for c, v in zip(chunks, vectors, strict=True):
                        await s.merge(
                            KnowledgeEmbedding(chunk_id=c.id, model=self.embedder.name, vector=v)
                        )
                    await s.flush()
                    if self.postgres:
                        await pgvector.store_vectors(s, [c.id for c in chunks])
                    await s.commit()
            except IntegrityError:
                continue  # another process stored the same chunks first
            if self.postgres:
                async with self.db.sessionmaker() as s:
                    await pgvector.ensure_vector_index(s, len(vectors[0]))
                    await s.commit()
            done += len(chunks)

    async def _backfill_tokens(self) -> None:
        async with self.db.sessionmaker() as s:
            missing = (
                await s.scalars(
                    select(KnowledgeChunk)
                    .join(KnowledgeDocument)
                    .outerjoin(pgvector.TOKENS, pgvector.TOKENS.c.chunk_id == KnowledgeChunk.id)
                    .where(KnowledgeDocument.active.is_(True), pgvector.TOKENS.c.chunk_id.is_(None))
                )
            ).all()
            await pgvector.store_tokens(s, [(c.id, _chunk_tokens(c)) for c in missing])
            await s.commit()

    async def try_index(self) -> bool:
        try:
            await self.index_missing()
            return True
        except EmbeddingError as exc:
            log.warning("vectors not updated, will retry: %s", exc)
            return False

    async def run_indexer(self, every_s: float = 30.0) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.try_index()
            except Exception:
                log.exception("vector indexing failed")

    # ---- seed ------------------------------------------------------------------------------

    async def seed_dir(self, path: Path = DEFAULT_SEED_DIR, actor: str = "system") -> int:
        """Ingest every `*.md` in `path` (front-matter sets title/departments/min_role)."""
        files = await asyncio.to_thread(_read_markdown_dir, path)
        created = 0
        async with self.db.sessionmaker() as s:
            for name, raw in files:
                parsed = parse_front_matter(raw)
                meta = parsed.meta
                result = await self.ingest(
                    s,
                    source=f"seed/{name}",
                    title=meta.get("title", name.removesuffix(".md")),
                    content=parsed.body,
                    departments=meta.get("departments", ALL_DEPARTMENTS).split(","),
                    min_role=Role(meta.get("min_role", "staff")),
                    actor=actor,
                )
                created += result.created
            await s.commit()
        await self.try_index()
        return created

    # ---- retrieval -------------------------------------------------------------------------

    async def list_documents(self, s: AsyncSession, user: User) -> list[KnowledgeDocument]:
        q = select(KnowledgeDocument).where(*_access_filter(user)).order_by(KnowledgeDocument.title)
        return list((await s.scalars(q)).all())

    async def search(
        self, s: AsyncSession, user: User, query: str, k: int | None = None
    ) -> list[Hit]:
        k = k or self.top_k
        if not query.strip():
            return []
        try:
            [q_vec] = await self.embedder.embed([query], "query")
        except EmbeddingError as exc:
            log.warning("searching by words only: %s", exc)
            q_vec = None
        q_tokens = tokenize(query)
        if self.postgres:
            rows, vector, corpus = await self._shortlist(s, user, sorted(set(q_tokens)), q_vec)
        else:
            rows, vector, corpus = await self._everything(s, user, q_vec)
        if not rows:
            return []
        lexical, coverage = _bm25(q_tokens, [_chunk_tokens(c) for c, _ in rows], corpus)

        # A chunk is a candidate only if one ranker finds it plausibly about the query; otherwise
        # we would always return *something* and the answerer could cite irrelevant text. Each
        # ranker votes only for chunks that pass its own gate: shared letter pairs are noise
        # below the coverage gate, and would push down a chunk that matches by meaning.
        fused: dict[int, float] = {}
        for scores, gate, minimum in (
            (lexical, coverage, self.min_coverage),
            (vector, vector, self.min_vector),
        ):
            passing = [i for i in range(len(rows)) if gate[i] >= minimum and scores[i] > 0]
            ranked = sorted(passing, key=lambda i: scores[i], reverse=True)
            for rank, i in enumerate(ranked):
                fused[i] = fused.get(i, 0.0) + 1.0 / (self.rrf_k + rank + 1)

        top = sorted(fused, key=lambda i: fused[i], reverse=True)[:k]
        return [
            Hit(
                chunk_id=rows[i][0].id,
                document_id=rows[i][1].id,
                title=rows[i][1].title,
                source=rows[i][1].source,
                version=rows[i][1].version,
                heading=rows[i][0].heading,
                text=rows[i][0].text,
                score=round(fused[i], 5),
                lexical=round(lexical[i], 4),
                vector=round(vector[i], 4),
            )
            for i in top
        ]

    async def _everything(self, s: AsyncSession, user: User, q_vec: list[float] | None):
        rows = (
            await s.execute(
                select(KnowledgeChunk, KnowledgeDocument, KnowledgeEmbedding.vector)
                .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
                .outerjoin(
                    KnowledgeEmbedding,
                    (KnowledgeEmbedding.chunk_id == KnowledgeChunk.id)
                    & (KnowledgeEmbedding.model == self.embedder.name),
                )
                .where(*_access_filter(user))
            )
        ).all()
        vector = [cosine(q_vec, v) if q_vec and v else 0.0 for _, _, v in rows]
        corpus = Corpus.of([_chunk_tokens(c) for c, _, _ in rows])
        return [(c, d) for c, d, _ in rows], vector, corpus

    async def _shortlist(
        self, s: AsyncSession, user: User, words: list[str], q_vec: list[float] | None
    ):
        access = _access_filter(user)
        corpus = Corpus(*await pgvector.corpus_stats(s, access, words))
        idf = {t: _idf(corpus.n, df) for t, df in corpus.df.items()}
        ids = set(await pgvector.lexical_candidates(s, access, idf))
        columns = [KnowledgeChunk, KnowledgeDocument]
        if q_vec:
            ids |= set(await pgvector.vector_candidates(s, access, self.embedder.name, q_vec))
            columns.append(1 - pgvector.distance(len(q_vec), q_vec))
        stmt = (
            select(*columns)
            .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
            .outerjoin(
                KnowledgeEmbedding,
                (KnowledgeEmbedding.chunk_id == KnowledgeChunk.id)
                & (KnowledgeEmbedding.model == self.embedder.name),
            )
            .where(KnowledgeChunk.id.in_(ids), *access)
            .order_by(KnowledgeChunk.id)
        )
        found = (await s.execute(stmt)).all()
        vector = [float(r[2] or 0.0) if q_vec else 0.0 for r in found]
        return [(r[0], r[1]) for r in found], vector, corpus

    async def search_as(self, user: User, query: str, k: int | None = None) -> list[Hit]:
        async with self.db.sessionmaker() as s:
            return await self.search(s, user, query, k)
