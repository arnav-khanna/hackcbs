import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://yugabyte:yugabyte@localhost:5433/disaster_db",
)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "qwen2.5:0.5b")
EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "120"))
TOP_K = int(os.getenv("TOP_K", "5"))
VECTOR_DIMENSION = 768
#DOCUMENTS_DIR = os.getenv("DOCUMENTS_DIR", "/app/documents")
