"""Classifier and FastAPI integration tests; no database or Ollama required."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.classification_agent import classify_incident
from app.config import INCIDENT_DANGER_LEVELS
from app.rag.errors import DatabaseUnavailableError


@pytest.mark.parametrize(
    "text,incident_type,danger_level",
    [
        ("Fire in the market", "fire", "high"),
        ("Wildfires near the village", "fire", "high"),
        ("An earthquake was reported", "earthquake", "high"),
        ("Aftershocks continue", "earthquake", "high"),
        ("Building collapse in the market", "earthquake", "critical"),
        ("A building has collapsed", "earthquake", "critical"),
        ("Collapsed buildings on the road", "earthquake", "critical"),
        ("Flood in the village", "flood", "high"),
        ("The road is flooded", "flood", "high"),
        ("A cyclone is approaching", "cyclone", "high"),
        ("Glacial Lake outburst", "glacial_lake_outburst", "critical"),
        ("GLOF warning received", "glacial_lake_outburst", "critical"),
        ("Drought across the district", "drought", "moderate"),
    ],
)
def test_supported_incidents(text, incident_type, danger_level):
    result = classify_incident(text)
    assert result.incident_type == incident_type
    assert result.danger_level == danger_level
    assert len(result.incidents) == 1
    assert result.incidents[0].matched_keywords
    assert result.method == "keyword_rules"


def test_normalizes_case_hyphens_unicode_and_whitespace():
    result = classify_incident("  ＧＬＡＣＩＡＬ-LAKE\n OUTBURST!  ")
    assert result.incident_type == "glacial_lake_outburst"
    assert result.incidents[0].matched_keywords == ["glacial lake outburst"]


@pytest.mark.parametrize("text", ["Help is needed", "firefighter", "backfire", "floodlight", "cycloneproof"])
def test_unrecognized_text_and_substrings_are_unknown(text):
    result = classify_incident(text)
    assert result.incident_type == "unknown"
    assert result.danger_level == "unknown"
    assert result.incidents == []
    assert "cannot be determined" in result.reason


def test_multiple_incidents_use_highest_danger_regardless_of_mention_order():
    result = classify_incident("Drought followed by flood and a building collapse; fire reported.")
    assert result.incident_type == "earthquake"
    assert result.danger_level == "critical"
    assert [(i.incident_type, i.danger_level) for i in result.incidents] == [
        ("earthquake", "critical"), ("fire", "high"), ("flood", "high"), ("drought", "moderate")
    ]


def test_earthquake_and_collapse_merge_without_lowering_danger():
    result = classify_incident("Earthquake, building collapse, earthquake and building collapse.")
    assert len(result.incidents) == 1
    assert result.danger_level == "critical"
    assert set(result.incidents[0].matched_keywords) == {"earthquake", "building collapse"}


def test_equal_danger_has_stable_primary_independent_of_text_order():
    first = classify_incident("Flood and fire")
    second = classify_incident("Fire and flood")
    assert first.incident_type == second.incident_type == "fire"
    assert first.incidents == second.incidents


@pytest.mark.parametrize("phrase", ["Glacial lake outburst flood", "Glacial-lake outburst floods"])
def test_glacial_flood_phrase_does_not_duplicate_generic_flood(phrase):
    result = classify_incident(phrase)
    assert [i.incident_type for i in result.incidents] == ["glacial_lake_outburst"]


def test_separate_flood_report_is_retained_alongside_glacial_incident():
    result = classify_incident("Glacial lake outburst flood upstream; flooding in another district.")
    assert [i.incident_type for i in result.incidents] == ["glacial_lake_outburst", "flood"]
    assert result.incidents[1].matched_keywords == ["flooding"]


def test_danger_mapping_can_be_adjusted_in_configuration(monkeypatch):
    monkeypatch.setitem(INCIDENT_DANGER_LEVELS, "drought", "critical")
    result = classify_incident("Fire and drought reported")
    assert result.incident_type == "drought"
    assert result.danger_level == "critical"


@pytest.mark.parametrize("text", ["", " \n\t ", "x" * 2001])
def test_direct_call_validates_input(text):
    with pytest.raises(ValidationError):
        classify_incident(text)


@pytest.fixture
def client(monkeypatch):
    from app import main

    # Exercise the real app lifespan with a DB outage. Classification should
    # still be served, and should never attempt model inference.
    monkeypatch.setattr(main, "init_db", AsyncMock(side_effect=DatabaseUnavailableError("DB offline")))
    monkeypatch.setattr(main, "close_db", AsyncMock())
    monkeypatch.setattr("app.classification_agent.retrieve_chunks", AsyncMock(return_value=[]))
    monkeypatch.setattr("httpx.AsyncClient.post", AsyncMock(side_effect=AssertionError("Unexpected HTTP call")))
    with TestClient(main.app) as test_client:
        yield test_client


def test_endpoint_runs_in_existing_app_without_database_or_ollama(client):
    response = client.post("/classification/classify", json={"text": "Fire and building collapse"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["incident_type"] == "earthquake"
    assert body["danger_level"] == "critical"
    assert body["method"] == "keyword_rules"
    assert body["grounded"] is False
    assert body["grounding_status"] == "insufficient_context"
    assert body["recommended_actions"] == []
    assert body["incidents"][0]["matched_keywords"] == ["building collapse"]
    assert len(body["incidents"]) == 2


def test_endpoint_returns_unknown_for_unmatched_report(client):
    response = client.post("/classification/classify", json={"text": "Request assistance"})
    assert response.status_code == 200
    assert response.json()["danger_level"] == "unknown"


@pytest.mark.parametrize(
    "body",
    [{}, {"text": ""}, {"text": " \n\t "}, {"text": "x" * 2001}, {"text": None}, {"text": 123}],
)
def test_endpoint_rejects_invalid_report(client, body):
    assert client.post("/classification/classify", json=body).status_code == 422


def test_classification_and_existing_rag_routes_are_documented(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/classification/classify" in paths
    assert {"/rag/documents", "/rag/query", "/rag/health"}.issubset(paths)
    assert client.post("/rag/query", json={"question": "   "}).status_code == 422
