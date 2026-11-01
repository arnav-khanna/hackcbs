from functools import lru_cache

import httpx
from langchain_ollama import ChatOllama

from app.config import (
    CHAT_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_NUM_CTX,
    OLLAMA_TEMPERATURE,
    OLLAMA_TIMEOUT,
)
from app.rag.errors import GenerationError


@lru_cache(maxsize=8)
def get_chat_model(
    temperature: float | None = None, num_predict: int | None = None
) -> ChatOllama:
    """Shared LangChain chat model for every LLM call in the app.

    `num_predict` bounds output length so a runaway generation cannot eat the
    request timeout. `reasoning=False` stops thinking-style models (qwen3 etc.)
    from spending the budget on hidden reasoning tokens.
    """
    return ChatOllama(
        model=CHAT_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=OLLAMA_TEMPERATURE if temperature is None else temperature,
        num_ctx=OLLAMA_NUM_CTX,
        num_predict=num_predict,
        keep_alive=OLLAMA_KEEP_ALIVE,
        reasoning=False,
        client_kwargs={"timeout": OLLAMA_TIMEOUT},
    )


def translate_llm_error(exc: Exception) -> GenerationError:
    """Map LangChain/Ollama client failures onto the app's GenerationError."""
    status = getattr(exc, "status_code", None)
    if status == 404:
        return GenerationError(
            f"Chat model '{CHAT_MODEL}' is not available in Ollama. "
            f"Pull it with: ollama pull {CHAT_MODEL}"
        )
    if isinstance(exc, (httpx.HTTPError, ConnectionError)):
        return GenerationError(
            f"Could not reach Ollama at {OLLAMA_BASE_URL}: {exc!r}. Is it running?"
        )
    return GenerationError(f"Chat model call failed: {exc!r}")


async def warm_up() -> None:
    """Load the chat model into memory. Never raises; used at startup."""
    try:
        await get_chat_model(0.0, 1).ainvoke("ok")
    except Exception:  # Ollama may simply not be up yet
        pass


async def generate_response(
    prompt: str, system: str | None = None, response_format: dict | None = None
) -> str:
    payload = {
        "model": CHAT_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": OLLAMA_TEMPERATURE, "num_ctx": OLLAMA_NUM_CTX},
    }
    if system:
        payload["system"] = system
    if response_format is not None:
        payload["format"] = response_format

    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(f"{OLLAMA_BASE_URL}/api/generate", json=payload)
    except httpx.HTTPError as exc:
        raise GenerationError(
            f"Could not reach Ollama at {OLLAMA_BASE_URL}: {exc!r}. Is it running?"
        ) from exc

    if response.status_code == 404:
        raise GenerationError(
            f"Chat model '{CHAT_MODEL}' is not available in Ollama. "
            f"Pull it with: ollama pull {CHAT_MODEL}"
        )
    if response.is_error:
        raise GenerationError(
            f"Ollama generate failed ({response.status_code}): {response.text[:300]}"
        )
    try:
        answer = response.json()["response"]
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("missing text response")
        return answer.strip()
    except (ValueError, KeyError, TypeError) as exc:
        raise GenerationError("Ollama returned an invalid generation response.") from exc


async def list_local_models() -> set[str]:
    """Names of models present in the local Ollama (from /api/tags)."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        response.raise_for_status()
    return {m["name"] for m in response.json().get("models", [])}


def model_is_present(configured: str, local: set[str]) -> bool:
    """Ollama treats `name` and `name:latest` as the same model."""
    return configured in local or (":" not in configured and f"{configured}:latest" in local)
