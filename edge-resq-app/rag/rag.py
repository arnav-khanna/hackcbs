"""Query pipeline: retrieve context from YugabyteDB, answer with the local LLM."""
import logging

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from app.config import CHAT_MODEL, MAX_CONTEXT_CHARS
from app.models.ollama import get_chat_model, translate_llm_error
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

# System text is passed as a variable so braces in it are never read as template slots.
_PROMPT = ChatPromptTemplate.from_messages(
    [
        ("system", "{system}"),
        ("human", "Context:\n{context}\n\nQuestion: {question}\n\nAnswer:"),
    ]
)


def fit_chunks(chunks: list[RetrievedChunk], budget: int = MAX_CONTEXT_CHARS) -> list[RetrievedChunk]:
    """Keep the most similar chunks (input is similarity-ordered) within `budget` characters.

    Chunks are dropped whole, never cut, so a cited passage is never truncated.
    The first chunk is always kept.
    """
    kept: list[RetrievedChunk] = []
    used = 0
    for chunk in chunks:
        if kept and used + len(chunk.content) > budget:
            break
        kept.append(chunk)
        used += len(chunk.content)
    return kept


def build_context(chunks: list[RetrievedChunk]) -> str:
    blocks = []
    for n, c in enumerate(chunks, start=1):
        where = c.filename or "unknown source"
        if c.page is not None:
            where += f", page {c.page}"
        blocks.append(f"[{n}] ({where})\n{c.content}")
    return "\n\n".join(blocks)


async def answer_query(question: str, top_k: int | None = None) -> dict:
    chunks = fit_chunks(await retrieve_chunks(question, top_k))

    if not chunks:
        # Nothing relevant stored: do not let the model improvise.
        return {"answer": NO_INFO_ANSWER, "grounded": False, "model": CHAT_MODEL, "sources": []}

    chain = _PROMPT | get_chat_model(None, 600) | StrOutputParser()
    try:
        answer = (
            await chain.ainvoke(
                {"system": SYSTEM_PROMPT, "context": build_context(chunks), "question": question}
            )
        ).strip()
    except Exception as exc:
        raise translate_llm_error(exc) from exc
    if not answer:
        raise translate_llm_error(ValueError("empty response"))

    return {
        "answer": answer,
        "grounded": answer.strip('"') != NO_INFO_ANSWER,
        "model": CHAT_MODEL,
        "sources": chunks,
    }
