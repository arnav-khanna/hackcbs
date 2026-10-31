"""HTTP layer for the RAG pipeline. Contains no processing logic."""
import logging

from fastapi import APIRouter, File, HTTPException, Response, UploadFile

from app.config import (
    CHAT_MODEL,
    EMBED_MODEL,
    MAX_UPLOAD_MB,
    VECTOR_DIMENSION,
)
from app.models import database
from app.models.ollama import list_local_models, model_is_present
from app.rag.embeddings import probe_embedding_dimension
from app.rag.errors import FileTooLargeError, RagError
from app.rag.ingestion import ingest_pdf
from app.rag.rag import answer_query
from app.schemas import (
    ComponentStatus,
    HealthResponse,
    IngestResponse,
    QueryRequest,
    QueryResponse,
    SourceRef,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])

EXCERPT_CHARS = 300


@router.post("/documents", response_model=IngestResponse)
async def upload_document(file: UploadFile = File(...)):
    """Ingest one PDF: extract, chunk, embed locally, store in YugabyteDB."""
    data = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise FileTooLargeError(f"PDF exceeds the {MAX_UPLOAD_MB} MB upload limit.")
    return await ingest_pdf(file.filename or "unnamed.pdf", data)


@router.post("/query", response_model=QueryResponse)
async def query(body: QueryRequest):
    """Answer a question from the ingested documents, with source references."""
    result = await answer_query(body.question, body.top_k)
    return QueryResponse(
        answer=result["answer"],
        grounded=result["grounded"],
        model=result["model"],
        sources=[
            SourceRef(
                filename=c.filename,
                page=c.page,
                chunk_index=c.chunk_index,
                similarity=round(c.similarity, 4),
                excerpt=c.content[:EXCERPT_CHARS],
            )
            for c in result["sources"]
        ],
    )


@router.get("/health", response_model=HealthResponse)
async def health(response: Response):
    """Checks DB + schema + vector size, and that both Ollama models are present."""
    try:
        await database.init_db()
        db = ComponentStatus(ok=True, detail=f"connected, documents.embedding=vector({VECTOR_DIMENSION})")
    except RagError as exc:
        db = ComponentStatus(ok=False, detail=str(exc))

    try:
        local = await list_local_models()
    except Exception as exc:
        reason = f"Ollama unreachable: {exc!r}"
        embed = ComponentStatus(ok=False, detail=reason)
        chat = ComponentStatus(ok=False, detail=reason)
    else:
        chat_ok = model_is_present(CHAT_MODEL, local)
        chat = ComponentStatus(
            ok=chat_ok,
            detail=CHAT_MODEL if chat_ok else f"'{CHAT_MODEL}' not pulled; run: ollama pull {CHAT_MODEL}",
        )
        if not model_is_present(EMBED_MODEL, local):
            embed = ComponentStatus(
                ok=False, detail=f"'{EMBED_MODEL}' not pulled; run: ollama pull {EMBED_MODEL}"
            )
        else:
            try:
                dim = await probe_embedding_dimension()
            except RagError as exc:
                embed = ComponentStatus(ok=False, detail=str(exc))
            else:
                embed = ComponentStatus(
                    ok=dim == VECTOR_DIMENSION,
                    detail=f"{EMBED_MODEL} outputs {dim} dims; database expects {VECTOR_DIMENSION}",
                )

    ok = db.ok and embed.ok and chat.ok
    if not ok:
        response.status_code = 503
    return HealthResponse(
        status="ok" if ok else "degraded",
        database=db,
        embedding_model=embed,
        chat_model=chat,
    )
