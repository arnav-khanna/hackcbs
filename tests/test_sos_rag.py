"""SOS route + retrieval/model contracts, independent of live services."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from app import classification_agent as agent
from app.rag.errors import DatabaseUnavailableError, EmbeddingError, GenerationError
from app.rag.retrieval import RetrievedChunk

SOS = "SOS water is rising inside our house and we are trapped upstairs"
REPORT = (
    "Floods can cause water to rise inside houses. "
    "If safe to do so, move to higher ground. "
    "Do not enter moving floodwater. "
    "Rising water may cut off escape routes."
)
CHUNKS = [RetrievedChunk("doc-1", REPORT, "flood-report.pdf", 2, 0, 0.89)]
ANALYSIS = {
    "incidents": [{
        "incident_type": "flood", "message_quote": "water is rising inside our house",
        "report_quote": "Floods can cause water to rise inside houses.", "source_id": 1,
    }],
    "recommended_actions": [{"text": "If safe to do so, move to higher ground.", "source_id": 1}],
    "warnings": [{"text": "Do not enter moving floodwater.", "source_id": 1}],
    "situations": [{"text": "Rising water may cut off escape routes.", "source_id": 1}],
}


@pytest.fixture
def pipeline(monkeypatch):
    retrieve = AsyncMock(return_value=CHUNKS)
    generate = AsyncMock(return_value=json.dumps(ANALYSIS))
    monkeypatch.setattr(agent, "retrieve_chunks", retrieve)
    monkeypatch.setattr(agent, "generate_response", generate)
    return retrieve, generate


@pytest.fixture
def client(monkeypatch, pipeline):
    from app import main

    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "close_db", AsyncMock())
    with TestClient(main.app) as client:
        yield client


def test_generic_sos_uses_retrieved_reports_and_returns_cited_guidance(client, pipeline):
    # No flood keyword: meaning must be inferred by the model using the report.
    assert agent.classify_incident(SOS).incident_type == "unknown"
    response = client.post("/classification/classify", json={"text": SOS, "top_k": 3})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["incident_type"] == "flood"
    assert body["danger_level"] == "high"
    assert body["method"] == "rag" and body["grounded"] is True
    assert body["grounding_status"] == "grounded"
    assert body["incidents"][0]["source_id"] == 1
    assert body["recommended_actions"] == ANALYSIS["recommended_actions"]
    assert body["warnings"] == ANALYSIS["warnings"]
    assert body["situations"] == ANALYSIS["situations"]
    assert body["sources"][0]["filename"] == "flood-report.pdf"
    assert body["sources"][0]["page"] == 2
    assert body["sources"][0]["excerpt"] == REPORT
    assert "flood" in body["alert"]
    retrieve, generate = pipeline
    retrieve.assert_awaited_once_with(SOS, 3)
    call = generate.await_args
    payload = json.loads(call.args[0])
    assert payload["sos_message"] == SOS
    assert payload["reports"][0]["content"] == REPORT
    assert "negation" in call.kwargs["system"]
    assert call.kwargs["response_format"]["additionalProperties"] is False


def test_no_reports_does_not_invoke_model_or_invent_guidance(pipeline):
    retrieve, generate = pipeline
    retrieve.return_value = []
    result = asyncio.run(agent.classify_sos("SOS fire in the kitchen"))
    assert result.incident_type == "fire"
    assert not result.grounded
    assert result.grounding_status == "insufficient_context"
    assert result.recommended_actions == result.warnings == result.situations == result.sources == []
    assert "Unverified" in result.alert
    generate.assert_not_awaited()


@pytest.mark.parametrize("error", [DatabaseUnavailableError("offline"), EmbeddingError("offline")])
def test_retrieval_failure_returns_explicit_fallback(pipeline, error):
    retrieve, generate = pipeline
    retrieve.side_effect = error
    result = asyncio.run(agent.classify_sos("Fire in the house"))
    assert result.incident_type == "fire" and result.method == "keyword_rules"
    assert result.grounding_status == "unavailable"
    assert not result.grounded and not result.recommended_actions
    generate.assert_not_awaited()


def test_generation_failure_returns_explicit_fallback(client, pipeline):
    pipeline[1].side_effect = GenerationError("model missing")
    response = client.post("/classification/classify", json={"text": SOS})
    assert response.status_code == 200
    body = response.json()
    assert body["incident_type"] == "unknown"
    assert body["grounding_status"] == "unavailable"
    assert body["grounded"] is False and body["sources"] == []


@pytest.mark.parametrize("corruption", [
    "invalid_json", "unsupported_type", "missing_field", "source_out_of_range",
    "source_zero", "source_string", "invented_action", "invented_report_quote",
    "invented_sos_quote", "blank_action", "guidance_without_incident",
    "dropped_condition", "dropped_negation", "dropped_sentence_end",
])
def test_invalid_model_evidence_is_never_returned_as_guidance(pipeline, corruption):
    analysis = copy.deepcopy(ANALYSIS)
    if corruption == "invalid_json":
        raw = "not JSON"
    else:
        if corruption == "unsupported_type":
            analysis["incidents"][0]["incident_type"] = "tsunami"
        elif corruption == "missing_field":
            del analysis["warnings"]
        elif corruption.startswith("source_"):
            analysis["recommended_actions"][0]["source_id"] = {
                "source_out_of_range": 2, "source_zero": 0, "source_string": "1"
            }[corruption]
        elif corruption == "invented_action":
            analysis["recommended_actions"][0]["text"] = "Swim across the river."
        elif corruption == "invented_report_quote":
            analysis["incidents"][0]["report_quote"] = "This report mentions a fire."
        elif corruption == "invented_sos_quote":
            analysis["incidents"][0]["message_quote"] = "Our house is flooded"
        elif corruption == "blank_action":
            analysis["recommended_actions"][0]["text"] = "  "
        elif corruption == "dropped_condition":
            analysis["recommended_actions"][0]["text"] = "move to higher ground."
        elif corruption == "dropped_negation":
            analysis["warnings"][0]["text"] = "enter moving floodwater."
        elif corruption == "dropped_sentence_end":
            analysis["recommended_actions"][0]["text"] = "If safe to do so, move"
        elif corruption == "guidance_without_incident":
            analysis["incidents"] = []
        raw = json.dumps(analysis)
    pipeline[1].return_value = raw
    result = asyncio.run(agent.classify_sos(SOS))
    assert result.grounding_status == "invalid_output"
    assert not result.grounded
    assert result.recommended_actions == result.warnings == result.situations == []


def test_model_can_abstain_for_negated_or_ambiguous_sos(pipeline):
    pipeline[1].return_value = json.dumps({key: [] for key in ANALYSIS})
    result = asyncio.run(agent.classify_sos("There is no fire here; please clarify the warning"))
    assert result.incident_type == result.danger_level == "unknown"
    assert result.incidents == [] and not result.grounded
    assert result.method == "rag" and result.grounding_status == "insufficient_context"


def test_classification_can_succeed_without_inventing_missing_actions(pipeline):
    analysis = copy.deepcopy(ANALYSIS)
    for key in ("recommended_actions", "warnings", "situations"):
        analysis[key] = []
    pipeline[1].return_value = json.dumps(analysis)
    result = asyncio.run(agent.classify_sos(SOS))
    assert result.grounded and result.incident_type == "flood"
    assert "no applicable guidance" in result.alert


def test_multiple_incidents_merge_collapse_and_preserve_danger_order(pipeline):
    text = "SOS fire, earthquake and building collapse nearby"
    pipeline[0].return_value = [
        RetrievedChunk("a", "Fire report.", "fire.pdf", 1, 0, 0.9),
        RetrievedChunk("b", "Earthquake report.", "quake.pdf", 1, 0, 0.8),
        RetrievedChunk("c", "Building collapse report.", "collapse.pdf", 3, 0, 0.7),
    ]
    pipeline[1].return_value = json.dumps({
        "incidents": [
            {"incident_type": "fire", "message_quote": "fire", "report_quote": "Fire report.", "source_id": 1},
            {"incident_type": "earthquake", "message_quote": "earthquake", "report_quote": "Earthquake report.", "source_id": 2},
            {"incident_type": "earthquake", "message_quote": "building collapse", "report_quote": "Building collapse report.", "source_id": 3},
        ],
        "recommended_actions": [], "warnings": [], "situations": [],
    })
    result = asyncio.run(agent.classify_sos(text))
    assert result.incident_type == "earthquake" and result.danger_level == "critical"
    assert [(i.incident_type, i.danger_level) for i in result.incidents] == [
        ("earthquake", "critical"), ("fire", "high")
    ]
    assert result.incidents[0].matched_keywords == ["earthquake", "building collapse"]
    assert result.incidents[0].source_id == 3
    assert result.incidents[0].report_quote == "Building collapse report."


def test_report_quotes_tolerate_pdf_line_wrapping(pipeline):
    pipeline[0].return_value = [RetrievedChunk(
        "wrapped", REPORT.replace("move to higher", "move to\nhigher"), "report.pdf", 1, 0, 0.8
    )]
    result = asyncio.run(agent.classify_sos(SOS))
    assert result.grounded
    assert result.recommended_actions[0].text == ANALYSIS["recommended_actions"][0]["text"]


@pytest.mark.parametrize("top_k", [0, 21, "invalid"])
def test_retrieval_limit_is_validated_before_service_calls(client, pipeline, top_k):
    assert client.post("/classification/classify", json={"text": SOS, "top_k": top_k}).status_code == 422
    pipeline[0].assert_not_awaited()


def test_ollama_structured_format_does_not_change_plain_rag_requests(monkeypatch):
    from app.models import ollama

    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "{}"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(handle), **kw
    ))
    asyncio.run(ollama.generate_response("plain question"))
    schema = agent._Analysis.model_json_schema()
    asyncio.run(ollama.generate_response("SOS", response_format=schema))
    assert "format" not in sent[0]
    assert sent[1]["format"] == schema


@pytest.mark.parametrize("body", [{}, {"response": None}, {"response": []}, {"response": " "}])
def test_invalid_ollama_envelope_has_domain_error(monkeypatch, body):
    from app.models import ollama

    real_client = httpx.AsyncClient
    monkeypatch.setattr(ollama.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)), **kw
    ))
    with pytest.raises(GenerationError):
        asyncio.run(ollama.generate_response("SOS"))
