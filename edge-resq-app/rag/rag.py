"""Query pipeline: retrieve context from YugabyteDB, answer with the local LLM."""
import logging

from app.config import CHAT_MODEL
from app.models.ollama import generate_response
from app.rag.retrieval import RetrievedChunk, retrieve_chunks

logger = logging.getLogger(__name__)

NO_INFO_ANSWER = (
    "The ingested documents do not contain enough information to answer this question."
)

SYSTEM_PROMPT = (
    "You are an emergency-documentation assistant. Answer ONLY from the numbered "
    "context passages provided. Keep instructions, steps, numbers and warnings "
    "exactly as written in the context. Cite the passages you use like [1] or [2]. "
    "Do not use outside knowledge. If the context does not contain the answer, "
    f'reply exactly: "{NO_INFO_ANSWER}"'
)


def build_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    blocks = []
    for n, c in enumerate(chunks, start=1):
        where = c.filename or "unknown source"
        if c.page is not None:
            where += f", page {c.page}"
        blocks.append(f"[{n}] ({where})\n{c.content}")
    context = "\n\n".join(blocks)
    return f"Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"


async def answer_query(question: str, top_k: int | None = None) -> dict:
    chunks = await retrieve_chunks(question, top_k)

    if not chunks:
        # Nothing relevant stored: do not let the model improvise.
        return {"answer": NO_INFO_ANSWER, "grounded": False, "model": CHAT_MODEL, "sources": []}

    answer = await generate_response(build_prompt(question, chunks), system=SYSTEM_PROMPT)
    return {
        "answer": answer,
        "grounded": answer.strip().strip('"') != NO_INFO_ANSWER,
        "model": CHAT_MODEL,
        "sources": chunks,
    }
