from typing import Literal

from pydantic import BaseModel, Field, field_validator


IncidentType = Literal[
    "fire", "earthquake", "flood", "cyclone", "glacial_lake_outburst", "drought"
]
DangerLevel = Literal["unknown", "moderate", "high", "critical"]


class ClassificationRequest(BaseModel):
    text: str = Field(
        ..., max_length=2000, description="SOS message to classify using ingested reports."
    )
    top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("text must not be blank")
        return v


class IncidentMatch(BaseModel):
    incident_type: IncidentType
    danger_level: DangerLevel
    matched_keywords: list[str]
    source_id: int | None = Field(default=None, description="One-based index into sources.")
    report_quote: str | None = None


class SourceRef(BaseModel):
    filename: str | None
    page: int | None
    chunk_index: int | None
    similarity: float
    excerpt: str


class ReportGuidance(BaseModel):
    text: str = Field(
        description="Complete sentence from the cited report; a list item is prefixed by its lead-in line."
    )
    source_id: int = Field(ge=1, description="One-based index into sources.")


class EmergencyGuidance(BaseModel):
    """Static, built-in playbook steps (see app.rag.playbook)."""

    source: Literal["built_in_playbook"] = "built_in_playbook"
    incident_type: str = Field(description="Playbook key: an incident type or 'general'.")
    phase: Literal["active", "aftermath"]
    headline: str = Field(description="One-line instruction to show first.")
    call: list[str] = Field(description="Emergency numbers.")
    do_now: list[str] = Field(description="Ordered, most urgent first.")
    avoid: list[str]
    next_steps: list[str] = Field(default_factory=list)
    disclaimer: str


class ClassificationResponse(BaseModel):
    incident_type: IncidentType | Literal["unknown"]
    danger_level: DangerLevel = Field(
        description="Provisional type-based danger label, not measured severity."
    )
    incidents: list[IncidentMatch]
    reason: str
    method: Literal["keyword_rules", "rag"] = "keyword_rules"
    grounded: bool = Field(
        default=False,
        description="Report and SOS evidence passed citation checks; not a guarantee of model correctness.",
    )
    grounding_status: Literal[
        "not_requested", "grounded", "insufficient_context", "unavailable", "invalid_output"
    ] = "not_requested"
    alert: str = ""
    recommended_actions: list[ReportGuidance] = Field(default_factory=list)
    warnings: list[ReportGuidance] = Field(default_factory=list)
    situations: list[ReportGuidance] = Field(default_factory=list)
    emergency_guidance: EmergencyGuidance | None = None
    additional_guidance: list[EmergencyGuidance] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    model: str | None = None


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
