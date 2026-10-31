import httpx

from app.config import (
    CHAT_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_NUM_CTX,
    OLLAMA_TEMPERATURE,
    OLLAMA_TIMEOUT,
)
from app.rag.errors import GenerationError


async def generate_response(prompt: str, system: str | None = None) -> str:
    payload = {
        "model": CHAT_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": OLLAMA_TEMPERATURE, "num_ctx": OLLAMA_NUM_CTX},
    }
    if system:
        payload["system"] = system

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
    return response.json()["response"].strip()


async def list_local_models() -> set[str]:
    """Names of models present in the local Ollama (from /api/tags)."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
        response.raise_for_status()
    return {m["name"] for m in response.json().get("models", [])}


def model_is_present(configured: str, local: set[str]) -> bool:
    """Ollama treats `name` and `name:latest` as the same model."""
    return configured in local or (":" not in configured and f"{configured}:latest" in local)
