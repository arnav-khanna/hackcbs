from dataclasses import dataclass

from app.config import MIN_SIMILARITY, TOP_K
from app.models.database import connection
from app.rag.embeddings import embed_query

# Cosine distance (<=>) over pgvector; similarity = 1 - distance.
_SEARCH = """
    SELECT id, content, metadata, 1 - (embedding <=> %(v)s::vector) AS similarity
    FROM documents
    ORDER BY embedding <=> %(v)s::vector
    LIMIT %(k)s
"""


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    content: str
    filename: str | None
    page: int | None
    chunk_index: int | None
    similarity: float


async def retrieve_chunks(query: str, top_k: int | None = None) -> list[RetrievedChunk]:
    """Embed the query with the ingestion model and return the nearest chunks."""
    vector = await embed_query(query)
    async with connection() as conn:
        cur = await conn.execute(_SEARCH, {"v": str(vector), "k": top_k or TOP_K})
        rows = await cur.fetchall()

    chunks = []
    for row_id, content, meta, similarity in rows:
        if similarity < MIN_SIMILARITY:
            continue
        meta = meta or {}
        chunks.append(
            RetrievedChunk(
                id=str(row_id),
                content=content,
                filename=meta.get("filename") or meta.get("source"),
                page=meta.get("page"),
                chunk_index=meta.get("chunk_index"),
                similarity=float(similarity),
            )
        )
    return chunks
