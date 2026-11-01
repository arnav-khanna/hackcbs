import { API_CONFIG as config } from './config.mjs';
import { MobileApi, wait } from './api.mjs';
import { buildMessagePayload, assessmentContent, messageText } from './data.mjs';
const $ = id => document.getElementById(id);
const api = new MobileApi();
const state = { active: false, busy: false, health: null, lifecycle: null, receiver: null, ownsInbox: false, outgoing: new Map(), receipts: new Map(), seen: new Set() };
const set = (id, value) => { if ($(id).textContent !== value) $(id).textContent = value; };
function notice(id, text) { set(id, text); $(id).hidden = !text; }
function controls() {
  const tower = state.health?.mode === 'server';
  $('send-message').disabled = state.busy || !state.health?.running || tower || !state.ownsInbox;
  $('message-text').disabled = state.busy || tower;
}
function connection(label, tone) { set('connection-state', label); $('connection-dot').className = `dot ${tone}`; }
function count() { set('character-count', `${$('message-text').value.length} / 2000`); }
function saveDraft() { try { sessionStorage.setItem('edge-resq-message-draft', $('message-text').value); } catch {} }
try { $('message-text').value = sessionStorage.getItem('edge-resq-message-draft') || ''; } catch {}
$('message-text').addEventListener('input', () => { count(); saveDraft(); });
count();

async function healthLoop(signal) {
  while (!signal.aborted) {
    try {
      const data = await api.health(signal);
      if (typeof data.running !== 'boolean' || !['host', 'server'].includes(data.mode)) throw new Error('Open this page on the mobile Pluto API.');
      state.health = data;
      const tower = data.mode === 'server';
      $('server-note').hidden = !tower;
      if (tower) { state.receiver?.abort(); state.ownsInbox = false; }
      connection(data.backend === 'simulation' ? 'Simulation online' : tower ? 'Tower radio online' : 'Pluto radio online', 'good');
      notice('connection-error', data.stats?.last_error ? `Last transport error: ${data.stats.last_error}` : '');
      if (!tower) startReceiver();
    } catch (error) {
      if (signal.aborted) break;
      state.health = null;
      connection('Connection unavailable', 'bad');
      notice('connection-error', `${error.message} Reconnecting automatically.`);
    }
    controls();
    await wait(config.healthIntervalMs, signal);
  }
}
function startReceiver() {
  if (state.receiver || !state.active || state.health?.mode !== 'host') return;
  const controller = new AbortController(); state.receiver = controller;
  async function listen() {
    state.ownsInbox = true; controls();
    notice('receiver-error', '');
    while (!controller.signal.aborted) {
      try {
        const packet = await api.receive(controller.signal);
        if (packet && !controller.signal.aborted) {
          if (!Object.hasOwn(packet, 'json')) throw new Error('Unexpected receive response.');
          incoming(packet);
        }
        notice('receiver-error', '');
      } catch (error) {
        if (controller.signal.aborted) break;
        notice('receiver-error', `Responses temporarily unavailable: ${error.message} Retrying…`);
        await wait(config.reconnectDelayMs, controller.signal);
      }
    }
  }
  void (async () => {
    try {
      if (navigator.locks) {
        await navigator.locks.request(`edge-resq-inbox:${api.base.href}`, { ifAvailable: true }, async lock => {
          if (controller.signal.aborted) return;
          if (!lock) { notice('receiver-error', 'Another tab is receiving messages. Close that tab to use this conversation.'); return; }
          await listen();
        });
      } else await listen();
    } catch (error) {
      if (!controller.signal.aborted) notice('receiver-error', error.message);
    } finally {
      if (state.receiver === controller) { state.receiver = null; state.ownsInbox = false; controls(); }
    }
  })();
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function append(entry) {
  $('welcome')?.remove();
  const nearBottom = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 220;
  $('messages').append(entry);
  while ($('messages').children.length > config.historyLimit) $('messages').firstElementChild.remove();
  if (nearBottom) entry.scrollIntoView({ block: 'nearest' });
}
function sendState(node, text, tone = '') { node.textContent = text; node.className = `send-state ${tone}`; }
function updateReceipt(reference) {
  const node = state.outgoing.get(reference), receipt = state.receipts.get(reference);
  if (!node || !receipt) return;
  sendState(node, receipt.kind === 'ack' ? 'Tower received your message. Preparing a response…' : receipt.error ? 'The tower returned an error. See the response below.' : 'Response received from the tower.', receipt.error ? 'error' : 'good');
}
function addList(entry, title, values, sources = []) {
  const valid = values.filter(item => typeof item === 'string' || typeof item?.text === 'string');
  if (!valid.length) return;
  if (title) entry.append(element('h3', title));
  const list = document.createElement('ol');
  for (const item of valid) {
    const li = element('li', typeof item === 'string' ? item : item.text);
    if (item?.source_id) {
      const source = sources[item.source_id - 1];
      li.append(element('span', `Source ${item.source_id}${source?.filename ? ` · ${source.filename}` : ''}${source?.page != null ? ` · page ${source.page}` : ''}`, 'source-ref'));
    }
    list.append(li);
  }
  entry.append(list);
}
function incoming(packet) {
  const payload = packet.json, meta = payload?.edge_resq;
  if (typeof meta?.response_id === 'string') {
    if (state.seen.has(meta.response_id)) return;
    state.seen.add(meta.response_id);
    if (state.seen.size > 200) state.seen.delete(state.seen.values().next().value);
  }
  if (typeof meta?.reply_to === 'string') {
    state.receipts.set(meta.reply_to, { kind: meta.kind, error: Boolean(payload.error) });
    if (state.receipts.size > 100) state.receipts.delete(state.receipts.keys().next().value);
    updateReceipt(meta.reply_to);
  }
  if (meta?.kind === 'ack') return;
  const entry = element('article', undefined, 'message incoming');
  const head = element('div', undefined, 'message-head');
  head.append(element('strong', 'EDGE-RESQ'));
  if (!payload?.error) head.append(element('span', 'PROPOSED GUIDANCE', 'proposed'));
  entry.append(head);
  const content = assessmentContent(payload);
  if (content?.classification) {
    const c = content.classification;
    entry.append(element('h2', `${String(c.incident_type).replaceAll('_', ' ')} · ${c.danger_level || 'unknown'} priority`));
  }
  if (content?.alert) entry.append(element('p', content.alert));
  if (content?.steps.length) addList(entry, '', content.steps);
  if (content?.actions.length) addList(entry, 'From response documents', content.actions, content.sources);
  if (content?.warnings.length) addList(entry, 'Cautions', content.warnings, content.sources);
  if (content?.places.length) {
    entry.append(element('h3', 'Nearby locations · availability unverified'));
    for (const place of content.places) {
      const item = element('div', place.name || 'Unnamed location', 'place');
      item.append(element('small', [place.type, place.distance, place.direction, place.phone].filter(value => typeof value === 'string').join(' · ')));
      entry.append(item);
    }
  }
  if (content?.locationPrompt) entry.append(element('p', content.locationPrompt, 'location-prompt'));
  if (!content?.alert && !content?.steps.length) entry.append(element('p', messageText(payload)));
  if (payload?.error) entry.append(element('p', payload.error, 'evidence'));
  const classification = content?.classification;
  if (classification) {
    const evidence = classification.method === 'keyword_rules'
      ? 'Built-in guidance. Model classification is unavailable or unverified.'
      : classification.grounded ? 'Report-grounded guidance.' : 'Local assessment. No report-grounded guidance available.';
    entry.append(element('p', `${evidence} Human review required.`, 'evidence'));
  }
  if (meta?.incident_id) entry.append(element('p', meta.incident_id, 'evidence'));
  const details = document.createElement('details');
  details.append(element('summary', 'Response details'), element('pre', JSON.stringify(payload, null, 2)));
  entry.append(details); append(entry);
}
$('message-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('send-message').disabled) return;
  let payload;
  try { payload = buildMessagePayload($('message-text').value); }
  catch (error) { notice('connection-error', error.message); return; }
  state.busy = true; controls();
  const entry = element('article', undefined, 'message outgoing');
  entry.append(element('p', payload.text));
  const status = element('p', 'Sending over the radio…', 'send-state'); entry.append(status); append(entry);
  try {
    const result = await api.send(payload, state.lifecycle.signal);
    if (!['delivered', 'submitted'].includes(result.status) || typeof result.message_id !== 'string') throw new Error('Unrecognized delivery acknowledgement.');
    state.outgoing.set(result.message_id, status);
    if (state.outgoing.size > 100) state.outgoing.delete(state.outgoing.keys().next().value);
    sendState(status, result.status === 'delivered' ? 'Delivered to the peer radio. Waiting for the tower…' : 'Submitted without delivery confirmation. Waiting for a response…');
    updateReceipt(result.message_id);
    $('message-text').value = ''; count(); saveDraft();
  } catch (error) {
    sendState(status, `${error.message} ${error.uncertain || !error.status ? 'Delivery is uncertain. Check for a reply before sending again.' : 'The mobile API rejected the message.'}`, 'error');
  } finally { state.busy = false; controls(); }
});
function start() {
  if (state.active) return;
  state.active = true; state.lifecycle = new AbortController();
  void healthLoop(state.lifecycle.signal);
}
window.addEventListener('pagehide', () => {
  state.active = false; state.lifecycle?.abort(); state.receiver?.abort();
  state.receiver = null; state.ownsInbox = false; state.health = null; controls();
});
window.addEventListener('pageshow', start);
start();
