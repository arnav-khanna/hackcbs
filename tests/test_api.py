"""Integration tests: FastAPI + real PostgreSQL/pgvector + stub Ollama."""
import json
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from test_pdf_chunking import make_pdf

DB_URL = os.environ["DATABASE_URL"]
ADMIN_URL = psycopg.conninfo.make_conninfo(DB_URL, dbname="yugabyte")

FLOOD = [
    "FLOOD EVACUATION PROCEDURE",
    "1. Move to higher ground above the flood level immediately.",
    "2. Carry drinking water and a battery powered radio.",
    "3. Switch off electricity and gas before leaving the building.",
    "Never walk through moving flood water deeper than 15 cm.",
    "Flood water may hide open drains and debris so use a stick to test the ground.",
    "Avoid driving through flooded roads because vehicles can float away quickly.",
    "Listen to the radio for evacuation routes announced by the district authority.",
    "Families should agree a meeting point on high ground before the monsoon season.",
    "Page 1",
]
QUAKE = [
    "EARTHQUAKE SAFETY",
    "During an earthquake drop, cover and hold on under sturdy furniture.",
    "Stay at least 2 m from windows and keep a clear area of 4 m² around the shelter point.",
    "After shaking stops check for injuries and leave through the safest exit.",
    "Do not use lifts. Expect aftershocks and drop cover and hold on again.",
    "If outdoors move away from buildings, street lights and utility wires.",
    "If driving stop in an open area away from bridges and overpasses.",
    "Page 2",
]
AID = [
    "FIRST AID BLEEDING",
    "Apply direct pressure to the wound with a clean cloth to stop bleeding.",
    "Do not remove a soaked cloth; add another layer on top and keep pressing.",
    "Raise the injured limb above heart level if there is no suspected fracture.",
    "Call the emergency number if bleeding does not stop after ten minutes.",
    "Page 3",
]
DOC = make_pdf([FLOOD, QUAKE, AID])


@pytest.fixture(scope="module")
def client(stub_ollama):
    # Start from no database at all to prove the app bootstraps it.
    with psycopg.connect(ADMIN_URL, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS disaster_db WITH (FORCE)")
    stub_ollama.control(dim=768, fail_generate=False)
    from app.main import app

    with TestClient(app) as c:
        yield c


def rows():
    with psycopg.connect(DB_URL) as c:
        return c.execute(
            "SELECT content, metadata FROM documents ORDER BY (metadata->>'chunk_index')::int"
        ).fetchall()


def upload(client, data=DOC, name="field-guide.pdf"):
    return client.post("/rag/documents", files={"file": (name, data, "application/pdf")})


def test_health_ok_reports_observed_dimension(client):
    r = client.get("/rag/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert "768 dims" in body["embedding_model"]["detail"]


def test_query_on_empty_store_does_not_call_llm(client, stub_ollama):
    before = stub_ollama.state()["generate_calls"]
    r = client.post("/rag/query", json={"question": "How do I evacuate?"})
    assert r.status_code == 200
    body = r.json()
    assert body["grounded"] is False and body["sources"] == []
    assert "do not contain enough information" in body["answer"]
    assert stub_ollama.state()["generate_calls"] == before


def test_upload_pdf_stores_chunks_with_page_metadata(client):
    r = upload(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_pages"] == 3 and body["pages_with_text"] == 3
    assert body["chunks"] >= 3 and body["inserted"] == body["chunks"]
    assert body["skipped"] == 0 and body["replaced"] == 0

    stored = rows()
    assert len(stored) == body["chunks"]
    assert {m["page"] for _, m in stored} == {1, 2, 3}
    assert all(m["filename"] == "field-guide.pdf" and m["document_id"] == body["document_id"] for _, m in stored)
    assert all(len(c) <= 800 for c, _ in stored)
    assert any("4 m²" in c for c, _ in stored)  # unit preserved
    assert not any("Page 1" in c or "Page 2" in c for c, _ in stored)  # footers removed


def test_reupload_replaces_instead_of_duplicating(client):
    first = len(rows())
    r = upload(client)
    assert r.status_code == 200
    assert r.json()["replaced"] == first and r.json()["inserted"] == first
    assert len(rows()) == first


def test_vector_column_matches_embedding_size(client):
    with psycopg.connect(DB_URL) as c:
        dim = c.execute("SELECT vector_dims(embedding) FROM documents LIMIT 1").fetchone()[0]
    assert dim == 768


@pytest.mark.parametrize(
    "question,page",
    [
        ("what should I do during an earthquake drop cover and hold on", 2),
        ("how to move to higher ground above the flood level", 1),
        ("how to stop bleeding with direct pressure", 3),
    ],
)
def test_query_retrieves_right_page_and_prompts_llm_with_context(client, stub_ollama, question, page):
    r = client.post("/rag/query", json={"question": question, "top_k": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["answer"] == "STUB ANSWER citing [1]" and body["grounded"] is True
    assert body["model"] == "qwen2.5:0.5b"
    top = body["sources"][0]
    assert top["page"] == page and top["filename"] == "field-guide.pdf"
    assert len(body["sources"]) == 3
    assert [s["similarity"] for s in body["sources"]] == sorted(
        (s["similarity"] for s in body["sources"]), reverse=True
    )

    sent = stub_ollama.state()["last_generate"]
    assert sent["model"] == "qwen2.5:0.5b" and sent["stream"] is False
    assert "ONLY from the numbered context" in sent["system"]
    assert f"(field-guide.pdf, page {page})" in sent["prompt"]
    assert f"Question: {question}" in sent["prompt"]


def test_invalid_uploads_return_clear_errors(client):
    assert upload(client, b"plain text", "notes.txt").status_code == 422
    assert upload(client, b"", "empty.pdf").status_code == 400
    blank = upload(client, make_pdf([[], []]), "scan.pdf")
    assert blank.status_code == 422 and "no extractable text" in blank.json()["detail"]
    assert upload(client, make_pdf([["x"]], encrypt="pw"), "locked.pdf").status_code == 422
    assert client.post("/rag/documents").status_code == 422  # no file field


def test_query_validation(client):
    assert client.post("/rag/query", json={"question": "   "}).status_code == 422
    assert client.post("/rag/query", json={"question": "ok", "top_k": 0}).status_code == 422
    assert client.post("/rag/query", json={"question": "x" * 2001}).status_code == 422


def test_llm_failure_is_502_not_500(client, stub_ollama):
    stub_ollama.control(fail_generate=True)
    try:
        r = client.post("/rag/query", json={"question": "flood evacuation"})
        assert r.status_code == 502 and "Ollama generate failed" in r.json()["detail"]
    finally:
        stub_ollama.control(fail_generate=False)


def test_ollama_unreachable_is_502(client, monkeypatch):
    monkeypatch.setattr("app.rag.embeddings.OLLAMA_BASE_URL", "http://127.0.0.1:9")
    r = client.post("/rag/query", json={"question": "flood"})
    assert r.status_code == 502 and "Could not reach Ollama" in r.json()["detail"]


def test_database_unreachable_is_503(client, monkeypatch):
    from app.models import database

    monkeypatch.setattr(database, "DATABASE_URL", "postgresql://yugabyte:yugabyte@127.0.0.1:9/disaster_db")
    monkeypatch.setattr(database, "_ready", False)
    r = client.post("/rag/query", json={"question": "flood"})
    assert r.status_code == 503 and "Cannot reach YugabyteDB" in r.json()["detail"]
    assert client.get("/rag/health").status_code == 503


def test_embedding_dimension_mismatch_is_detected(client, stub_ollama):
    before = len(rows())
    stub_ollama.control(dim=384)
    try:
        r = upload(client, make_pdf([["A different document about tsunami warnings."]]), "other.pdf")
        assert r.status_code == 500 and "384-dim" in r.json()["detail"] and "expects 768" in r.json()["detail"]
        assert len(rows()) == before  # nothing half-written
        h = client.get("/rag/health")
        assert h.status_code == 503 and h.json()["embedding_model"]["ok"] is False
        assert "384 dims" in h.json()["embedding_model"]["detail"]
    finally:
        stub_ollama.control(dim=768)


def test_ingest_jsonl_is_idempotent(client, tmp_path):
    import asyncio

    from app.rag.ingestion import ingest_jsonl

    path = tmp_path / "e.jsonl"
    vec = [0.01] * 768
    path.write_text(
        "\n".join(json.dumps({"row_id": i, "text": f"row {i}", "metadata": {"source": "csv"}, "embedding": vec}) for i in range(2)),
        encoding="utf-8",
    )
    first = asyncio.run(ingest_jsonl(path))
    second = asyncio.run(ingest_jsonl(path))
    assert (first["inserted"], first["skipped"]) == (2, 0)
    assert (second["inserted"], second["skipped"]) == (0, 2)
