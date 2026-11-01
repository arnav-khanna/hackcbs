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
| `POST /classification/classify` | `{"text": "SOS ...", "top_k": 5}` → report-grounded incident classification, provisional danger level, displayable alert, cited actions, warnings, and situation details. |

```bash
curl -F "file=@guide.pdf" localhost:8000/rag/documents
curl -H 'content-type: application/json' -d '{"question":"What do I do in an earthquake?"}' localhost:8000/rag/query
```

Interactive docs: `http://localhost:8000/docs`.

## SOS classification and report-based guidance

Upload emergency reports or response manuals through `POST /rag/documents` first.
The existing PDF ingestion pipeline stores their text and embeddings in YugabyteDB.
`POST /classification/classify` then runs:

1. Retrieve the most relevant report passages for the SOS message.
2. Ask the local Ollama model to interpret the SOS using those passages, including
   descriptions without disaster keywords, such as water rising inside a house.
3. Validate structured output, SOS evidence, report quotes, and numbered sources.
4. Return a classification and alert with report-based actions, warnings (what to
   avoid), and applicable situations or hazards.

This is retrieval-augmented generation, not model fine-tuning. Uploading or
replacing reports updates the knowledge available to subsequent requests without
retraining model weights. Use reports that include response procedures if you
want actionable guidance; historical incident reports may provide no instructions.

```bash
curl -F "file=@emergency-report.pdf" localhost:8000/rag/documents
curl -H 'content-type: application/json' \
  -d '{"text":"SOS water is rising inside our house and we are trapped upstairs", "top_k":5}' \
  localhost:8000/classification/classify
```

The JSON response includes:

| Field | Meaning |
|---|---|
| `incident_type`, `danger_level`, `incidents` | Primary category, provisional type-based priority, and all supported incidents. |
| `method` | `rag` for report-based interpretation; `keyword_rules` for an unverified fallback. |
| `grounded`, `grounding_status` | Whether SOS/report evidence passed citation validation and whether context or services were unavailable. |
| `alert` | Message the calling application can display to the user. This endpoint does not dispatch push notifications, SMS, or rescue requests. |
| `recommended_actions`, `warnings`, `situations` | Lists of `{ "text": "verbatim report passage", "source_id": 1 }`; unavailable information has an empty list. |
| `sources` | Retrieved passages, filenames, pages, chunk indexes, and similarity. `source_id` is the one-based index into this list. |
| `reason`, `model` | Explanation of the classification method and local model used for successful RAG analysis. |

Each report-based incident also includes a `report_quote` and `source_id`;
`matched_keywords` holds quoted SOS evidence in RAG mode. Guidance must occur in
its cited report and preserve complete sentences, including conditions and
warnings. Invalid JSON, unsupported categories, invented quotes, and invalid
source references are rejected. These checks verify provenance; they do not prove
that the model interpreted an SOS or chose applicable advice correctly. Evaluate
real SOS examples and report quality before relying on the results.

Supported categories remain fire, earthquake (including building collapse),
flood, cyclone, glacial lake outburst (including GLOF), and drought. The danger
mapping in `edge-resq-app/config.py` remains provisional:

| Incident category | Provisional danger level |
|---|---|
| Building collapse, glacial lake outburst / GLOF | `critical` |
| Fire, earthquake, flood, cyclone | `high` |
| Drought | `moderate` |
| Insufficient evidence | `unknown` |

These are type-based priorities, not measured severity. Building collapse is
categorized under `earthquake` without establishing its cause. All incidents
are ordered by descending danger, then alphabetically by category.

If retrieval finds no passages, services fail, or model evidence is invalid,
the endpoint returns `grounded: false`, an explicit status, and no report-based
guidance. The existing `classify_incident(text)` function supplies an unverified
keyword fallback in those cases; it cannot interpret negation, timing, or actual
severity. If the model finds insufficient evidence in available reports, it
returns `unknown` rather than falling back to keywords that may be negated.
An `unknown` classification does not indicate safety.

For other backend consumers, use
`await app.classification_agent.classify_sos(text, top_k=None)` for retrieval and
guidance, or `app.classification_agent.classify_incident(text)` for keyword rules
alone. SOS text must be nonblank and at most 2,000 characters; optional `top_k`
must be 1–20. Invalid requests return HTTP 422.

## Configuration (environment variables, defaults in `edge-resq-app/config.py`)

`DATABASE_URL`, `OLLAMA_BASE_URL`, `OLLAMA_CHAT_MODEL` (`qwen2.5:0.5b`), `OLLAMA_EMBED_MODEL` (`nomic-embed-text`), `CHUNK_SIZE` / `CHUNK_OVERLAP` (characters), `TOP_K`, `MIN_SIMILARITY` (0 = off), `VECTOR_DIMENSION` (768), `MAX_UPLOAD_MB`, `OLLAMA_NUM_CTX`, `OLLAMA_TEMPERATURE`, `EMBED_DOC_PREFIX` / `EMBED_QUERY_PREFIX`.

If you change the embedding model, change `VECTOR_DIMENSION` **and** `vector(768)` in `db/schema.sql` together and re-embed everything; the app refuses to run on a mismatch.

## Tests

```bash
pip install -r requirements.txt pytest reportlab
python -m pytest tests -q     # needs Postgres+pgvector (or YugabyteDB) on localhost:5433
```

Integration tests use a stub Ollama (`tests/stub_ollama.py`), so they verify the pipeline, not model quality.

Classifier/API and PDF unit tests can run without database or model services:

```bash
python -m pytest tests/test_classification.py tests/test_sos_rag.py tests/test_pdf_chunking.py -q
```

## Notes

- The package is imported as `app` (the Dockerfile copies `edge-resq-app/` to `/edge-resq-app/app`). To run without Docker, symlink: `ln -s edge-resq-app app && uvicorn app.main:app`.
- Scanned (image-only) PDFs are rejected with a clear error; OCR is not included.
- `python -m app.rag.embeddings --input data.csv` (original CSV tool, needs `pandas`) + `app.rag.ingestion.ingest_jsonl()` still load pre-computed embeddings.
