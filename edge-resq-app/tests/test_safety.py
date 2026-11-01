"""Assess accepts text-only input while preserving location-aware requests."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from app.classification_agent import classify_incident
from app.routes import safety

@pytest.fixture
def client(monkeypatch):
    async def classify(text, top_k):
        return classify_incident(text)
    monkeypatch.setattr(safety, 'classify_sos', classify)
    app = FastAPI()
    app.include_router(safety.router)
    return TestClient(app)

def test_text_only_assessment_does_not_invent_or_lookup_location(client, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail('Text-only assessment attempted a location lookup')
    monkeypatch.setattr(safety, 'geocode_place', unexpected)
    monkeypatch.setattr(safety, 'calm_safety_plan', unexpected)
    response = client.post('/safety/assess', json={'text': 'Fixture: fire in a building'})
    assert response.status_code == 200
    body = response.json()
    assert body['classification']['incident_type'] == 'fire'
    assert body['calm_steps']
    assert body['location'] is None and body['safety_plan'] is None
    assert body['location_required'] is True and 'Location:' in body['location_prompt']

def test_coordinates_still_generate_the_existing_safety_plan(client, monkeypatch):
    calls = []
    async def plan(lat, lon, disaster, country):
        calls.append((lat, lon))
        return {'ok': True, 'steps': ['Fixture plan'], 'best_place': None, 'backup_places': []}
    monkeypatch.setattr(safety, 'calm_safety_plan', plan)
    response = client.post('/safety/assess', json={'text': 'Fixture: flood', 'latitude': 0, 'longitude': 0})
    assert response.status_code == 200
    body = response.json()
    assert calls == [(0, 0)]
    assert body['location']['source'] == 'gps'
    assert body['location_required'] is False and body['location_prompt'] is None
    assert body['safety_plan']['steps'] == ['Fixture plan']

def test_place_name_still_uses_existing_geocoder(client, monkeypatch):
    async def geocode(query):
        assert query == 'Fixture town'
        return {'ok': True, 'results': [{'latitude': 1, 'longitude': 2, 'name': 'Fixture town'}]}
    async def plan(*args):
        return {'ok': True, 'steps': [], 'best_place': None, 'backup_places': []}
    monkeypatch.setattr(safety, 'geocode_place', geocode)
    monkeypatch.setattr(safety, 'calm_safety_plan', plan)
    response = client.post('/safety/assess', json={'text': 'Fixture SOS', 'place_name': 'Fixture town'})
    assert response.status_code == 200
    assert response.json()['location']['resolved_name'] == 'Fixture town'

@pytest.mark.parametrize('payload', [
    {'text': ' '}, {'text': 'x' * 2001},
    {'text': 'Fixture', 'latitude': 1},
    {'text': 'Fixture', 'longitude': 2},
    {'text': 'Fixture', 'latitude': 91, 'longitude': 0},
    {'text': 'Fixture', 'disaster_type': 'unsupported'},
])
def test_invalid_requests_remain_rejected(client, payload):
    assert client.post('/safety/assess', json=payload).status_code == 422
