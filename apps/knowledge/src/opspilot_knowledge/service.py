"""Ingestion and permission-aware hybrid retrieval.

Retrieval = SQL access filter -> BM25 (lexical) + cosine (vector) -> Reciprocal Rank Fusion.
Scoring runs in Python over the user's accessible chunks, which is fine for thousands of chunks.
For larger corpora, push both rankers into Postgres (pgvector `<=>` and `tsvector`) behind the
same `search` signature; the access filter stays in the WHERE clause either way.
"""

import asyncio
import hashlib
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_core.auth import Role, User
from opspilot_core.db import Database
from opspilot_knowledge.chunking import chunk_markdown, parse_front_matter
from opspilot_knowledge.embeddings import Embedder, cosine
from opspilot_knowledge.models import (
    ALL_DEPARTMENTS,
    KnowledgeAcl,
    KnowledgeChunk,
    KnowledgeDocument,
)
from opspilot_knowledge.text import tokenize

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


def _bm25(query: list[str], docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
    n = len(docs)
    avg = sum(map(len, docs)) / n if n else 0.0
    df = Counter(t for d in docs for t in set(d))
    q = set(query)
    scores, coverage = [], []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in q:
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / (avg or 1)))
        scores.append(s)
        coverage.append(len(q & tf.keys()) / len(q) if q else 0.0)
    return scores, coverage


class KnowledgeService:
    def __init__(
        self,
        db: Database,
        embedder: Embedder,
        *,
        top_k: int = 5,
        min_coverage: float = 0.5,
        min_vector: float = 0.3,
        rrf_k: int = 60,
    ) -> None:
        self.db, self.embedder = db, embedder
        self.top_k, self.min_coverage, self.min_vector, self.rrf_k = (
            top_k,
            min_coverage,
            min_vector,
            rrf_k,
        )

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
            "\x1f".join(
                [title, content, ",".join(departments), min_role.value, self.embedder.name]
            ).encode()
        ).hexdigest()

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
        vectors = await self.embedder.embed([f"{c.heading}\n{c.text}" for c in chunks], "document")

        latest = await s.scalar(
            select(KnowledgeDocument.version)
            .where(KnowledgeDocument.source == source)
            .order_by(KnowledgeDocument.version.desc())
            .limit(1)
        )
        # Old versions stay for audit/citation history but drop out of retrieval.
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
        s.add_all(
            KnowledgeChunk(
                document_id=doc.id, ordinal=i, heading=c.heading, text=c.text, embedding=v
            )
            for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))
        )
        await s.flush()
        return IngestResult(doc, created=True, chunks=len(chunks))

    async def deactivate(self, s: AsyncSession, document_id: str) -> None:
        await s.execute(
            update(KnowledgeDocument)
            .where(KnowledgeDocument.id == document_id)
            .values(active=False)
        )

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
        return created

    # ---- retrieval -------------------------------------------------------------------------

    async def list_documents(self, s: AsyncSession, user: User) -> list[KnowledgeDocument]:
        q = select(KnowledgeDocument).where(*_access_filter(user)).order_by(KnowledgeDocument.title)
        return list((await s.scalars(q)).all())

    async def search(
        self, s: AsyncSession, user: User, query: str, k: int | None = None
    ) -> list[Hit]:
        k = k or self.top_k
        rows = (
            await s.execute(
                select(KnowledgeChunk, KnowledgeDocument)
                .join(KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id)
                .where(*_access_filter(user))
            )
        ).all()
        if not rows or not query.strip():
            return []

        q_tokens = tokenize(query)
        lexical, coverage = _bm25(q_tokens, [tokenize(f"{c.heading} {c.text}") for c, _ in rows])
        [q_vec] = await self.embedder.embed([query], "query")
        vector = [
            cosine(q_vec, c.embedding) if len(c.embedding) == len(q_vec) else 0.0 for c, _ in rows
        ]

        # A chunk is a candidate only if it is plausibly about the query; otherwise we would
        # always return *something* and the answerer could cite irrelevant text.
        candidates = [
            i
            for i in range(len(rows))
            if coverage[i] >= self.min_coverage or vector[i] >= self.min_vector
        ]
        fused: dict[int, float] = dict.fromkeys(candidates, 0.0)
        for scores in (lexical, vector):
            ranked = sorted(candidates, key=lambda i: scores[i], reverse=True)
            for rank, i in enumerate(ranked):
                if scores[i] > 0:
                    fused[i] += 1.0 / (self.rrf_k + rank + 1)

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

    async def search_as(self, user: User, query: str, k: int | None = None) -> list[Hit]:
        async with self.db.sessionmaker() as s:
            return await self.search(s, user, query, k)
