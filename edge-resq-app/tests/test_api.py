"""HTTP-level check: the playbook must reach the API client."""
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda

from app import classification_agent as agent
from app.main import app


def test_classify_endpoint_returns_playbook_even_with_everything_down(monkeypatch):
    def boom(_):
        raise RuntimeError("down")
    monkeypatch.setattr(agent, "_triage_chain", lambda: RunnableLambda(boom))

    with TestClient(app) as client:  # startup tolerates a missing DB / Ollama
        body = client.post("/classification/classify", json={"text": "my shop is on fire"}).json()
    assert body["incident_type"] == "fire"
    assert body["emergency_guidance"]["do_now"] and "112" in " ".join(body["emergency_guidance"]["call"])
    assert body["grounding_status"] == "unavailable"


def test_blank_message_is_rejected():
    with TestClient(app) as client:
        assert client.post("/classification/classify", json={"text": "   "}).status_code == 422
