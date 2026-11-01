#!/usr/bin/env python3
"""Tower-only adapter: existing Pluto inbox → existing tower APIs → Pluto send.

A SQLite journal preserves consumed requests and unsent results across restarts.
Radio modulation, fragmentation, ACKs and retries remain in the transport backend.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import re
import signal
import sqlite3
import time

import httpx


def log(message):
    print(f'[EDGE-RESQ bridge] {message}', flush=True)


def parse_message(packet):
    payload = packet.get('json')
    if not isinstance(payload, dict) or not isinstance(payload.get('text'), str) or not payload['text'].strip():
        raise ValueError('Expected a nonblank JSON text field.')
    text = payload['text'].strip()
    if len(text) > 2000:
        raise ValueError('Tower text limit is 2,000 characters.')
    incident = text.startswith('FIELD INCIDENT REPORT\n')
    location = None
    match = re.search(r'^Location:[ \t]*(\S[^\r\n]*)$', text, re.M | re.I)
    if match: location = match.group(1).strip()
    parent = re.match(r'Update for transport reference ([0-9a-f]{16}):\n', text)
    return {'text': text, 'kind': 'incident' if incident else 'question', 'location': location,
            'parent': parent.group(1) if parent else None}


class Journal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
              id TEXT PRIMARY KEY, source INTEGER, incident_id TEXT, kind TEXT,
              packet TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created REAL);
            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        ''')
        # A crash during model inference must not lose an already consumed report.
        self.db.execute("UPDATE requests SET status='pending' WHERE status='processing'")
        self.db.commit()

    def add(self, packet):
        parsed = parse_message(packet)
        request_id = str(packet['message_id'])
        key = f"{packet['source']}:{request_id}"
        incident_id = f'INC-{request_id}' if parsed['kind'] == 'incident' else None
        if parsed['parent']:
            parent = self.db.execute('SELECT incident_id FROM requests WHERE id=?', (f"{packet['source']}:{parsed['parent']}",)).fetchone()
            if parent: incident_id = parent['incident_id']
        metadata = {'version': 1, 'kind': 'ack', 'response_id': key + ':ack', 'reply_to': request_id, 'incident_id': incident_id}
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO requests(id,source,incident_id,kind,packet,created) VALUES(?,?,?,?,?,?)',
                                      (key, packet['source'], incident_id, parsed['kind'], json.dumps(packet), time.time())).rowcount
            if inserted:
                self.db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES(?,?)',
                    (key + ':ack', json.dumps({'text': f'Tower accepted {incident_id or "your message"}. Processing with local AI; no action has been dispatched.', 'edge_resq': metadata})))
        return bool(inserted)

    def next_request(self):
        row = self.db.execute("SELECT * FROM requests WHERE status='pending' ORDER BY created LIMIT 1").fetchone()
        if row:
            with self.db: self.db.execute("UPDATE requests SET status='processing' WHERE id=?", (row['id'],))
        return row

    def finish(self, row, result):
        packet = json.loads(row['packet'])
        incident_id = row['incident_id']
        classification = result.get('classification', {})
        if not incident_id and classification.get('incident_type') not in (None, 'unknown'):
            incident_id = 'INC-' + packet['message_id']
        result['edge_resq'] = {'version': 1, 'kind': 'result', 'response_id': row['id'] + ':result',
                               'reply_to': packet['message_id'], 'incident_id': incident_id,
                               'action_status': 'proposed'}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES(?,?)', (row['id'] + ':result', json.dumps(result)))
            self.db.execute("UPDATE requests SET status='complete',incident_id=? WHERE id=?", (incident_id, row['id']))

    def next_response(self):
        return self.db.execute('SELECT * FROM outbox WHERE sent=0 ORDER BY rowid LIMIT 1').fetchone()

    def mark_sent(self, key):
        with self.db: self.db.execute('UPDATE outbox SET sent=1 WHERE id=?', (key,))


async def tower_post(client, url, payload, timeout=180):
    response = await client.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict): raise ValueError('Tower returned an invalid JSON object.')
    return value


async def process_request(client, tower, message):
    # One application endpoint owns response generation. Optional location
    # from older radio clients is forwarded using its actual request schema.
    payload = {'text': message['text']}
    if message.get('location'):
        coords = re.fullmatch(r'\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*', message['location'])
        if coords:
            payload['latitude'], payload['longitude'] = map(float, coords.groups())
        else:
            payload['place_name'] = message['location']
    return await tower_post(client, tower + '/safety/assess', payload, timeout=240)


async def run(args):
    radio, tower = args.radio_url.rstrip('/'), args.tower_url.rstrip('/')
    journal = Journal(args.state)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM): loop.add_signal_handler(signum, stop.set)
    async with httpx.AsyncClient(trust_env=False) as client:
        async def receive():
            while not stop.is_set():
                try:
                    response = await client.get(radio + '/receive', params={'timeout_ms': 10000}, timeout=15)
                    if response.status_code == 204: continue
                    response.raise_for_status()
                    packet = response.json()
                    try:
                        if journal.add(packet): log(f"Accepted transport {packet['message_id']}; persisted for processing.")
                    except (ValueError, KeyError) as exc:
                        log('Rejected inbound payload: ' + str(exc))
                except (httpx.HTTPError, ValueError) as exc:
                    log(f'Inbox unavailable ({type(exc).__name__}); reconnecting…'); await asyncio.sleep(2)
        async def process():
            while not stop.is_set():
                row = journal.next_request()
                if not row: await asyncio.sleep(.2); continue
                try:
                    result = await process_request(client, tower, parse_message(json.loads(row['packet'])))
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    result = {'text': 'Tower processing failed. No action has been dispatched. Please review and send an update when services recover.', 'error': str(exc)[:600]}
                journal.finish(row, result)
                log(f"Completed {row['incident_id'] or row['id']}; response queued for radio delivery.")
        async def send():
            while not stop.is_set():
                row = journal.next_response()
                if not row: await asyncio.sleep(.2); continue
                try:
                    response = await client.post(radio + '/send', json=json.loads(row['payload']), timeout=60)
                    response.raise_for_status()
                    if response.json().get('status') != 'delivered':
                        raise ValueError('Peer delivery ACK is required for tower responses.')
                    journal.mark_sent(row['id']); log('Peer acknowledged ' + row['id'])
                except (httpx.HTTPError, ValueError) as exc:
                    log(f'Response retained for retry ({type(exc).__name__}); response_id stays stable for deduplication.')
                    await asyncio.sleep(3)
        log(f'READY · {radio}/receive → {tower} → {radio}/send · durable journal {args.state}')
        tasks = [asyncio.create_task(work()) for work in (receive, process, send)]
        stopped = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stopped], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopped: task.result(); raise RuntimeError('Bridge task stopped unexpectedly.')
        finally:
            for task in [*tasks, stopped]: task.cancel()
            await asyncio.gather(*tasks, stopped, return_exceptions=True)
            journal.db.close()
            log('Stopped; pending requests/responses remain in the journal.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--radio-url', required=True)
    parser.add_argument('--tower-url', required=True)
    parser.add_argument('--state', required=True)
    asyncio.run(run(parser.parse_args()))
