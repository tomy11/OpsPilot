from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from opspilot_core.auth import Role


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    source: str = Field(
        min_length=1,
        max_length=256,
        description="Stable key; re-uploading it creates a new version",
    )
    content: str = Field(min_length=1, max_length=500_000, description="Markdown or plain text")
    departments: list[str] = Field(
        min_length=1, description="Departments that may read it, or ['*']"
    )
    min_role: Role = Role.STAFF


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    title: str
    version: int
    departments: list[str]
    min_role: Role
    created_by: str
    created_at: datetime


class IngestOut(BaseModel):
    document: DocumentOut
    created: bool
    chunks: int


class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=10)


class HitOut(BaseModel):
    document_id: str
    title: str
    source: str
    version: int
    section: str
    text: str
    score: float
    lexical: float
    vector: float


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class CitationOut(BaseModel):
    n: int
    document_id: str
    title: str
    source: str
    version: int
    section: str
    snippet: str


class AskOut(BaseModel):
    answer: str
    grounded: bool
    reason: str | None
    citations: list[CitationOut]
