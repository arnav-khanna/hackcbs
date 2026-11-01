import asyncio
import hashlib
import logging
import uuid
from pathlib import Path

from psycopg.types.json import Json

from app.config import CHUNK_OVERLAP, CHUNK_SIZE, DOCUMENTS_DIR, MAX_UPLOAD_MB
from app.models.database import connection
from app.rag.chunking import chunk_pages
from app.rag.embeddings import embed_documents
from app.rag.errors import FileTooLargeError
from app.rag.helper_functions import load_jsonl
from app.rag.pdf import extract_pages

logger = logging.getLogger(__name__)

# Stable namespace for IDs generated from CSV row numbers / PDF chunks.
ROW_NAMESPACE = uuid.UUID("3f1dca1a-3bd1-4c74-b91d-3b9bff3e7f21")

_INSERT = """
    INSERT INTO documents (id, content, metadata, embedding)
    VALUES (%s, %s, %s, %s::vector)
    ON CONFLICT (id) DO NOTHING
"""


async def store_chunks(rows: list[tuple], replace_document_id: str | None = None) -> dict:
    """Insert (id, content, metadata, embedding) rows in one transaction.

    With `replace_document_id`, rows previously stored for that PDF are deleted
    first in the same transaction, so a re-upload fully replaces the old chunks
    (including when chunking settings changed) and never leaves a mixture.
    """
    params = [(i, content, Json(meta), str(list(vec))) for i, content, meta, vec in rows]
    replaced = 0
    inserted = 0
    async with connection() as conn:
        async with conn.cursor() as cur:
            if replace_document_id:
                await cur.execute(
                    "DELETE FROM documents WHERE metadata->>'document_id' = %s",
                    (replace_document_id,),
                )
                replaced = cur.rowcount
            await cur.executemany(_INSERT, params)
            inserted = cur.rowcount
    return {"inserted": inserted, "skipped": len(params) - inserted, "replaced": replaced}


async def ingest_pdf(filename: str, data: bytes) -> dict:
    """PDF bytes -> text -> chunks -> local embeddings -> YugabyteDB."""
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise FileTooLargeError(f"PDF exceeds the {MAX_UPLOAD_MB} MB upload limit.")

    sha256 = hashlib.sha256(data).hexdigest()
    document_id = str(uuid.uuid5(ROW_NAMESPACE, f"pdf:{sha256}"))

    pages, total_pages = await asyncio.to_thread(extract_pages, data)
    chunks = chunk_pages(pages, CHUNK_SIZE, CHUNK_OVERLAP)
    logger.info(
        "Extracted %s: %d/%d pages with text, %d chunks",
        filename, len(pages), total_pages, len(chunks),
    )

    vectors = await embed_documents([c.text for c in chunks])

    rows = [
        (
            str(uuid.uuid5(ROW_NAMESPACE, f"pdf:{sha256}:{c.index}")),
            c.text,
            {
                "document_id": document_id,
                "filename": filename,
                "sha256": sha256,
                "page": c.page,
                "chunk_index": c.index,
                "total_pages": total_pages,
            },
            vec,
        )
        for c, vec in zip(chunks, vectors)
    ]
    result = await store_chunks(rows, replace_document_id=document_id)
    return {
        "status": "completed",
        "document_id": document_id,
        "filename": filename,
        "sha256": sha256,
        "total_pages": total_pages,
        "pages_with_text": len(pages),
        "chunks": len(chunks),
        **result,
    }


async def ingest_jsonl(
    jsonl_path: str | Path | None = None,
    batch_size: int = 500,
    dataset: str | None = None,
) -> dict:
    """Load pre-computed embeddings (output of `python -m app.rag.embeddings`).

    Row ids come from the row number. Without `dataset` they are the legacy ids,
    so a second file reuses ids 0..N and ON CONFLICT DO NOTHING silently skips its
    rows. Pass a distinct `dataset` per file to avoid that. Changing it for a file
    that was already loaded inserts the rows again instead of skipping them.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")

    if jsonl_path is None:
        jsonl_path = Path(DOCUMENTS_DIR) / "embeddings.jsonl"

    inserted = 0
    skipped = 0
    batch: list[tuple] = []

    async def flush():
        nonlocal inserted, skipped, batch
        if batch:
            result = await store_chunks(batch)
            inserted += result["inserted"]
            skipped += result["skipped"]
            batch = []

    for record in load_jsonl(jsonl_path):
        # Stable ID for safe reruns of the same dataset.
        key = str(record["row_id"]) if dataset is None else f"{dataset}:{record['row_id']}"
        document_id = str(uuid.uuid5(ROW_NAMESPACE, key))
        batch.append(
            (
                document_id,
                record["content"],
                {
                    **record["metadata"],
                    "row_id": record["row_id"],
                    **({"dataset": dataset} if dataset else {}),
                },
                record["embedding"],
            )
        )
        if len(batch) >= batch_size:
            await flush()
    await flush()

    return {"status": "completed", "inserted": inserted, "skipped": skipped}
