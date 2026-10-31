-- Migration 001: RAG storage.
--
-- Matches the table shape the existing ingestion code was written against
-- (documents: id, content, metadata, embedding). One row per text chunk;
-- the parent PDF and page are carried in `metadata`:
--   document_id, filename, sha256, page, chunk_index, total_pages
--
-- Idempotent: safe to run on every startup.
-- vector(768) must equal VECTOR_DIMENSION in config.py (checked at startup).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id        UUID PRIMARY KEY,
    content   TEXT NOT NULL,
    metadata  JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(768) NOT NULL
);
