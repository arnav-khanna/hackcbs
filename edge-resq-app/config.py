import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://yugabyte:yugabyte@localhost:5433/disaster_db",
)
# Database used only to CREATE DATABASE for the target in DATABASE_URL when it
# does not exist yet (docker-compose does not create `disaster_db`).
DATABASE_ADMIN_DB = os.getenv("DATABASE_ADMIN_DB", "yugabyte")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# 0.5B models cannot reliably follow a structured-output prompt, so the default
# is 3B. Run `ollama pull qwen2.5:3b`; use a 7B model if the device can hold it.
CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:3b")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "180"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
OLLAMA_TEMPERATURE = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
# Keep the model loaded between SOS requests so the first one is not a cold start.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
# Hard cap on retrieved report text sent to the model per call, in characters
# (~4 chars per token). Without a cap, top_k=20 overflows OLLAMA_NUM_CTX and
# Ollama silently truncates the prompt, dropping the instructions.
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "6000"))

# Nomic embedding models are trained with task prefixes; documents and queries
# must use their respective prefix. Set both to "" to disable.
EMBED_DOC_PREFIX = os.getenv("EMBED_DOC_PREFIX", "search_document: ")
EMBED_QUERY_PREFIX = os.getenv("EMBED_QUERY_PREFIX", "search_query: ")
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))

# CHUNK_SIZE / CHUNK_OVERLAP are measured in characters.
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))
TOP_K = int(os.getenv("TOP_K", "5"))
# Chunks with cosine similarity below this are treated as "not relevant".
# 0.0 disables the filter. Calibrate against real documents before raising it.
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.0"))

# Must match the output size of EMBED_MODEL and the vector(N) column in
# db/schema.sql. This is verified at startup, never assumed.
VECTOR_DIMENSION = int(os.getenv("VECTOR_DIMENSION", "768"))

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
# Only used by app.rag.ingestion.ingest_jsonl (pre-computed CSV embeddings).
DOCUMENTS_DIR = os.getenv("DOCUMENTS_DIR", "/app/documents")

# Provisional keyword-based danger labels, not measured incident severity.
INCIDENT_DANGER_LEVELS = {
    "fire": "high",
    "earthquake": "high",
    "building_collapse": "critical",
    "flood": "high",
    "cyclone": "high",
    "glacial_lake_outburst": "critical",
    "drought": "moderate",
}
