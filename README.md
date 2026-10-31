# EDGE-RESQ — RAG API

PDF → text → chunks → local Nomic embeddings → YugabyteDB (pgvector) → retrieval → local Qwen2.5-0.5B via Ollama, exposed through FastAPI. Everything runs on the tower-side machine; no external AI APIs.

## Run locally (Docker Compose)

```bash
docker compose up -d --build
# one-time: pull the local models into the Ollama container
docker compose exec ollama ollama pull qwen2.5:0.5b
docker compose exec ollama ollama pull nomic-embed-text
curl localhost:8000/rag/health        # must say "ok" before ingesting
```

On first start the API creates the `disaster_db` database, enables `vector`, and creates the `documents` table (`edge-resq-app/db/schema.sql`, idempotent).

## API

| Route | Purpose |
|---|---|
| `POST /rag/documents` | multipart field `file`: one PDF. Returns document id, page/chunk counts, inserted/replaced counts. Re-uploading the same PDF replaces its chunks. |
| `POST /rag/query` | `{"question": "...", "top_k": 5}` → `answer`, `grounded`, `sources[]` (filename, page, similarity, excerpt). |
| `GET /rag/health` | DB reachable, vector size matches, both Ollama models pulled, **observed** embedding size. 503 if anything is wrong. |

```bash
curl -F "file=@guide.pdf" localhost:8000/rag/documents
curl -H 'content-type: application/json' -d '{"question":"What do I do in an earthquake?"}' localhost:8000/rag/query
```

Interactive docs: `http://localhost:8000/docs`.

## Configuration (environment variables, defaults in `edge-resq-app/config.py`)

`DATABASE_URL`, `OLLAMA_BASE_URL`, `OLLAMA_CHAT_MODEL` (`qwen2.5:0.5b`), `OLLAMA_EMBED_MODEL` (`nomic-embed-text`), `CHUNK_SIZE` / `CHUNK_OVERLAP` (characters), `TOP_K`, `MIN_SIMILARITY` (0 = off), `VECTOR_DIMENSION` (768), `MAX_UPLOAD_MB`, `OLLAMA_NUM_CTX`, `OLLAMA_TEMPERATURE`, `EMBED_DOC_PREFIX` / `EMBED_QUERY_PREFIX`.

If you change the embedding model, change `VECTOR_DIMENSION` **and** `vector(768)` in `db/schema.sql` together and re-embed everything; the app refuses to run on a mismatch.

## Tests

```bash
pip install -r requirements.txt pytest reportlab
python -m pytest tests -q     # needs Postgres+pgvector (or YugabyteDB) on localhost:5433
```

Integration tests use a stub Ollama (`tests/stub_ollama.py`), so they verify the pipeline, not model quality.

## Notes

- The package is imported as `app` (the Dockerfile copies `edge-resq-app/` to `/edge-resq-app/app`). To run without Docker, symlink: `ln -s edge-resq-app app && uvicorn app.main:app`.
- Scanned (image-only) PDFs are rejected with a clear error; OCR is not included.
- `python -m app.rag.embeddings --input data.csv` (original CSV tool, needs `pandas`) + `app.rag.ingestion.ingest_jsonl()` still load pre-computed embeddings.
