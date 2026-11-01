"""Isolated bridge contract and durable-queue tests; no production mock data."""
import asyncio
import importlib.util
import json
from pathlib import Path
import httpx
import pytest

spec = importlib.util.spec_from_file_location('tower_bridge', Path(__file__).resolve().parents[2] / 'scripts/tower_bridge.py')
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

def packet(text='FIELD INCIDENT REPORT\nLocation: TEST AREA\nDescription: TEST ONLY', ref='1234567890abcdef'):
    return {'message_id': ref, 'source': 1, 'json': {'text': text}}

def test_requests_and_unsent_results_survive_restart_with_stable_ids(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    journal = bridge.Journal(path)
    assert journal.add(packet()) and not journal.add(packet())
    row = journal.next_request()
    assert row['incident_id'] == 'INC-1234567890abcdef'
    journal.db.close()
    journal = bridge.Journal(path)
    row = journal.next_request()
    assert row is not None
    journal.finish(row, {'classification': {'incident_type': 'fire'}})
    ack = journal.next_response()
    assert json.loads(ack['payload'])['edge_resq']['kind'] == 'ack'
    journal.mark_sent(ack['id'])
    result = journal.next_response()
    metadata = json.loads(result['payload'])['edge_resq']
    assert metadata['reply_to'] == '1234567890abcdef' and metadata['action_status'] == 'proposed'
    journal.db.close()
    journal = bridge.Journal(path)
    assert journal.next_response()['id'] == result['id']
    journal.mark_sent(result['id'])
    assert journal.next_response() is None and journal.next_request() is None
    journal.db.close()

def test_update_keeps_original_incident_reference(tmp_path):
    journal = bridge.Journal(tmp_path / 'journal.sqlite3')
    journal.add(packet())
    journal.add(packet('Update for transport reference 1234567890abcdef:\nTEST ONLY question', 'abcdef1234567890'))
    first = journal.next_request(); journal.finish(first, {'text': 'test'})
    update = journal.next_request()
    assert update['incident_id'] == first['incident_id'] and update['kind'] == 'question'
    journal.db.close()

@pytest.mark.parametrize('payload', [{}, {'text': ''}, {'text': 'x' * 2001}, {'text': ['wrong shape']}])
def test_invalid_text_is_rejected(payload):
    with pytest.raises(ValueError): bridge.parse_message({'json': payload})

@pytest.mark.parametrize('text,expected', [
    ('SOS fixture message', {'text': 'SOS fixture message'}),
    ('SOS fixture message\nLocation: Test town', {'text': 'SOS fixture message\nLocation: Test town', 'place_name': 'Test town'}),
    ('SOS fixture message\nLocation: 28.6, 77.2', {'text': 'SOS fixture message\nLocation: 28.6, 77.2', 'latitude': 28.6, 'longitude': 77.2}),
])
def test_all_messages_use_safety_assess_only(text, expected):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        assert request.url.path == '/safety/assess'
        assert json.loads(request.content) == expected
        return httpx.Response(200, json={'classification': {'incident_type': 'fire'}, 'calm_steps': ['Fixture instruction'], 'safety_plan': None})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await bridge.process_request(client, 'http://tower.test', bridge.parse_message(packet(text)))
            assert result['calm_steps'] == ['Fixture instruction']
    asyncio.run(scenario())
    assert paths == ['/safety/assess']

def test_assessment_http_error_is_not_silently_replaced_with_another_endpoint():
    def handler(request):
        assert request.url.path == '/safety/assess'
        return httpx.Response(502, json={'detail': 'Fixture service unavailable'})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await bridge.process_request(client, 'http://tower.test', bridge.parse_message(packet('Fixture SOS')))
    asyncio.run(scenario())
