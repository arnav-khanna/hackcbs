"""Run the real LangChain chain against a fake Ollama HTTP server.

This checks what actually goes over the wire (JSON schema, options, few-shot
messages) and that the reply is parsed, without needing a real model.
"""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import classification_agent as agent
from app.models import ollama


class _Handler(BaseHTTPRequestHandler):
    requests: list = []
    reply: str = "{}"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, body))
        line = json.dumps({
            "model": body.get("model"), "created_at": "2026-01-01T00:00:00Z",
            "message": {"role": "assistant", "content": type(self).reply},
            "done": True, "done_reason": "stop",
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        self.wfile.write((line + "\n").encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_ollama(monkeypatch):
    _Handler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(ollama, "OLLAMA_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    ollama.get_chat_model.cache_clear()
    agent._chains.clear()
    yield _Handler
    server.shutdown()
    ollama.get_chat_model.cache_clear()
    agent._chains.clear()


def test_triage_request_and_response_over_the_wire(fake_ollama):
    fake_ollama.reply = json.dumps({
        "situation": "Water is entering the house.",
        "incidents": [{"incident_type": "flood", "evidence": "water is coming in"}],
    })
    result = asyncio.run(agent._triage_chain().ainvoke({"sos": "help, water is coming in"}))
    assert [i.incident_type for i in result.incidents] == ["flood"]

    path, body = fake_ollama.requests[0]
    assert path == "/api/chat"
    # the enum is enforced by Ollama's schema-constrained decoding
    schema = body["format"]
    assert set(schema["properties"]) == {"situation", "incidents"}
    assert "flood" in json.dumps(schema) and "glacial_lake_outburst" in json.dumps(schema)
    assert body["options"]["temperature"] == 0.0 and body["options"]["num_ctx"] == 4096
    assert body["options"]["num_predict"] == 350
    assert body["keep_alive"] == "30m"
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "system" and roles[-1] == "user"
    assert roles.count("assistant") == len(agent._TRIAGE_SHOTS)  # few-shot examples sent intact
    assert body["messages"][-1]["content"] == "SOS message:\nhelp, water is coming in"
    # JSON braces in the examples were not eaten by the prompt template
    assert any('"incident_type":"flood"' in m["content"] for m in body["messages"] if m["role"] == "assistant")


def test_full_pipeline_through_http(fake_ollama, monkeypatch):
    from app.rag.retrieval import RetrievedChunk
    fake_ollama.reply = json.dumps({
        "situation": "s", "incidents": [{"incident_type": "flood", "evidence": "water is coming in"}],
    })

    async def fake_retrieve(q, k=None):
        return [RetrievedChunk("1", "Move to higher ground immediately.", "r.pdf", 1, 0, 0.9)]
    monkeypatch.setattr(agent, "retrieve_chunks", fake_retrieve)

    # one fake server serves both calls; switch the reply between them
    calls = {"n": 0}
    orig = _Handler.do_POST

    def do_post(self):
        _Handler.reply = fake_ollama.reply if calls["n"] == 0 else json.dumps(
            {"actions": [1], "warnings": [], "situations": []})
        calls["n"] += 1
        orig(self)
    monkeypatch.setattr(_Handler, "do_POST", do_post)

    r = asyncio.run(agent.classify_sos("Help, water is coming in"))
    assert r.incident_type == "flood" and r.grounding_status == "grounded"
    assert r.recommended_actions[0].text == "Move to higher ground immediately."
    selection_body = fake_ollama.requests[1][1]
    assert "[1] Move to higher ground immediately." in selection_body["messages"][-1]["content"]
