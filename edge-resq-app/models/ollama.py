import httpx

from app.config import CHAT_MODEL, OLLAMA_BASE_URL


def generate_response(prompt: str) -> str:
    response = httpx.post(
        f"{OLLAMA_BASE_URL}/api/generate",
        json={
            "model": CHAT_MODEL,
            "prompt": prompt,
            "stream": False,
        },
        timeout=180,
    )
    response.raise_for_status()

    return response.json()["response"]
