from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    question: str = Field(..., max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be blank")
        return v


class SourceRef(BaseModel):
    filename: str | None
    page: int | None
    chunk_index: int | None
    similarity: float
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    grounded: bool = Field(
        description="False when no relevant context was found or the model reported "
        "that the documents do not contain the answer."
    )
    model: str
    sources: list[SourceRef]


class IngestResponse(BaseModel):
    status: str
    document_id: str
    filename: str
    sha256: str
    total_pages: int
    pages_with_text: int
    chunks: int
    inserted: int
    skipped: int
    replaced: int = Field(description="Chunks of the same PDF that were replaced.")


class ComponentStatus(BaseModel):
    ok: bool
    detail: str


class HealthResponse(BaseModel):
    status: str
    database: ComponentStatus
    embedding_model: ComponentStatus
    chat_model: ComponentStatus
