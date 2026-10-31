"""Stand-in for Ollama used by the integration tests (the real models cannot be
downloaded in the sandbox these tests were developed in).

Embeddings are deterministic hashed bag-of-words vectors, so texts that share
words are genuinely closer under cosine distance. Chat answers are canned; the
last request is recorded so tests can inspect the prompt the pipeline built.
"""
import math
import re
import zlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()
state = {"dim": 768, "generate_calls": 0, "last_generate": None, "fail_generate": False}


def embed_text(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        vec[zlib.crc32(word.encode()) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@app.get("/api/tags")
async def tags():
    return {"models": [{"name": "qwen2.5:0.5b"}, {"name": "nomic-embed-text:latest"}]}


@app.post("/api/embed")
async def embed(request: Request):
    body = await request.json()
    if body["model"] not in ("nomic-embed-text", "nomic-embed-text:latest"):
        return JSONResponse({"error": "model not found"}, status_code=404)
    return {"embeddings": [embed_text(t, state["dim"]) for t in body["input"]]}


@app.post("/api/generate")
async def generate(request: Request):
    body = await request.json()
    state["generate_calls"] += 1
    state["last_generate"] = body
    if state["fail_generate"]:
        return JSONResponse({"error": "boom"}, status_code=500)
    return {"response": "STUB ANSWER citing [1]", "done": True}


@app.post("/_control")
async def control(request: Request):
    state.update(await request.json())
    return {k: v for k, v in state.items() if k != "last_generate"}


@app.get("/_state")
async def get_state():
    return state
