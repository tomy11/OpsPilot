"""Knowledge tables.

Access control lives on the document and is copied into `knowledge_acl` rows (one per allowed
department, `*` = everyone) so retrieval can filter in SQL *before* scoring: chunks a user may not
read never leave the database, so they cannot leak into ranking, prompts or citations.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from opspilot.core.db import Base, new_id, utcnow

ALL_DEPARTMENTS = "*"


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(256), index=True)  # stable key across versions
    title: Mapped[str] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer, default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    departments: Mapped[list[Any]] = mapped_column(default=list)
    min_role: Mapped[str] = mapped_column(String(16))
    min_role_rank: Mapped[int] = mapped_column(Integer, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeAcl(Base):
    __tablename__ = "knowledge_acl"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), primary_key=True
    )
    department: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(512))
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[Any]] = mapped_column(default=list)
