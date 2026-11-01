// Isolated contract tests; fixtures never enter the production UI.
// Run from transport_stack: node --test tests/frontend.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { MobileApi, ApiError } from '../json_transport/frontend/api.mjs';
import { API_CONFIG } from '../json_transport/frontend/config.mjs';
import { buildMessagePayload, assessmentContent, messageText } from '../json_transport/frontend/data.mjs';
const client = fetcher => new MobileApi(API_CONFIG, fetcher, 'http://mobile.local');

test('the single textbox sends the actual text object with a 2000-character limit', () => {
  assert.deepEqual(buildMessagePayload(' Hello '), { text: 'Hello' });
  assert.equal(buildMessagePayload('x'.repeat(2000)).text.length, 2000);
  assert.throws(() => buildMessagePayload(' '));
  assert.throws(() => buildMessagePayload('x'.repeat(2001)));
});
test('text-only safety assessments preserve guidance and request location explicitly', () => {
  const payload = { classification: { incident_type: 'fire', danger_level: 'high', emergency_guidance: { avoid: ['Fixture caution'] } }, calm_steps: ['Fixture instruction'], safety_plan: null, location_prompt: 'Fixture location prompt' };
  const content = assessmentContent(payload);
  assert.deepEqual(content.steps, ['Fixture instruction']);
  assert.deepEqual(content.places, []);
  assert.equal(content.locationPrompt, 'Fixture location prompt');
  assert.deepEqual(content.warnings, ['Fixture caution']);
});
test('located assessment decodes actual safety plan fields without inventing availability', () => {
  const place = { name: 'Fixture hospital', type: 'Hospital' };
  const content = assessmentContent({ safety_plan: { best_place: place, backup_places: [], steps: ['Fixture instruction'] } });
  assert.deepEqual(content.places, [place]);
  assert.deepEqual(content.steps, ['Fixture instruction']);
});
test('unknown responses and markup remain displayable as plain text', () => {
  const payload = { sensor: { temperature: 12 } };
  assert.equal(messageText(payload), JSON.stringify(payload, null, 2));
  assert.equal(messageText({ text: '<b>Radio reply</b>' }), '<b>Radio reply</b>');
  assert.equal(assessmentContent(null), null);
});
test('only configured mobile origin is allowed', () => {
  assert.throws(() => new MobileApi({ ...API_CONFIG, baseUrl: 'http://tower.local' }, () => {}, 'http://mobile.local'), /mobile API origin/);
});
test('send uses /send and raw JSON without an invented envelope', async () => {
  let calls = 0;
  const api = client(async (url, options) => {
    calls++;
    assert.equal(url.href, 'http://mobile.local/send');
    assert.equal(options.method, 'POST');
    assert.equal(options.headers['Content-Type'], 'application/json');
    assert.deepEqual(JSON.parse(options.body), { text: 'test' });
    return new Response(JSON.stringify({ status: 'delivered', message_id: 'abc' }));
  });
  assert.equal((await api.send({ text: 'test' })).status, 'delivered');
  assert.equal(calls, 1);
});
test('receive uses existing long poll parameter and handles HTTP 204', async () => {
  const api = client(async url => {
    assert.equal(url.href, 'http://mobile.local/receive?timeout_ms=15000');
    return new Response(null, { status: 204 });
  });
  assert.equal(await api.receive(), null);
});
test('HTTP 504 is uncertain, retains backend detail and never retries POST', async () => {
  let calls = 0;
  const api = client(async () => { calls++; return new Response(JSON.stringify({ detail: 'ACK lost' }), { status: 504 }); });
  await assert.rejects(api.send({ text: 'test' }), error => error instanceof ApiError && error.status === 504 && error.uncertain && /ACK lost/.test(error.message));
  assert.equal(calls, 1);
});
test('HTTP 400 is a definite rejection', async () => {
  const api = client(async () => new Response('{"detail":"Invalid JSON"}', { status: 400 }));
  await assert.rejects(api.send({ text: 'test' }), error => error.status === 400 && !error.uncertain);
});
test('stopped transport health retains diagnostics', async () => {
  const api = client(async () => new Response('{"running":false,"stats":{"last_error":"Link failed"}}', { status: 503 }));
  await assert.rejects(api.health(), error => error.data.stats.last_error === 'Link failed');
});
test('malformed successful POST response cannot be reported as delivered', async () => {
  const api = client(async () => new Response('<html>proxy error</html>'));
  await assert.rejects(api.send({ text: 'test' }), error => error.uncertain && /non-JSON/.test(error.message));
});
test('network errors remain uncertain without automatic reissue', async () => {
  let calls = 0;
  const api = client(async () => { calls++; throw new TypeError('network disconnected'); });
  await assert.rejects(api.send({ text: 'test' }), error => error.uncertain && /Cannot reach/.test(error.message));
  assert.equal(calls, 1);
});
test('timeout aborts pending send and marks delivery uncertain', async () => {
  const api = new MobileApi({ ...API_CONFIG, sendTimeoutMs: 5 }, async (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
  }), 'http://mobile.local');
  await assert.rejects(api.send({ text: 'test' }), error => error.uncertain && /timed out/.test(error.message));
});
