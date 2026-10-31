"""Bounded reassembly and selective repeat in windows of up to 32 fragments."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field, replace
import hmac
import logging
import math
import os
import secrets
import struct
import time

from .codec import Codec
from .security import Security
from .wire import Frame, DATA, ACK, HELLO, HELLO_ACK, OVERHEAD

ACK_BODY = struct.Struct('!IB')
LOGGER = logging.getLogger(__name__)


@dataclass
class Assembly:
    count: int
    parts: dict = field(default_factory=dict)
    size: int = 0
    updated: float = field(default_factory=time.monotonic)


@dataclass
class Outbound:
    count: int
    acknowledged: set = field(default_factory=set)
    complete: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)


class Transport:
    def __init__(self, config, link):
        self.config, self.link = config, link
        self.security = Security(config)
        self.codec = Codec(config, self.security)
        self.inbox = asyncio.Queue(maxsize=config.limits['receive_queue'])
        self.assemblies = {}
        self.completed = OrderedDict()
        self.pending = {}
        self.buffer_bytes = 0
        self.send_lock = asyncio.Lock()
        self.session_ready = asyncio.Event()
        self.session_future = None
        self.session_message_id = None
        self.session_nonce = None
        self.task = None
        self.cleanup_task = None
        self.running = False
        self.stats = dict(messages_sent=0, messages_received=0, frames_sent=0, frames_received=0,
                          retransmissions=0, invalid_frames=0, rejected_messages=0,
                          expired_messages=0, send_failures=0, last_error=None)

    async def start(self):
        await self.link.start()
        self.running = True
        self.task = asyncio.create_task(self._reader())
        self.cleanup_task = asyncio.create_task(self._cleaner())

    async def close(self):
        self.running = False
        for state in self.pending.values():
            state.changed.set()
        for task in (self.task, self.cleanup_task):
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in (self.task, self.cleanup_task) if t), return_exceptions=True)
        await self.link.close()

    def _clean(self):
        now = time.monotonic()
        for key, state in list(self.assemblies.items()):
            if now - state.updated > self.config.limits['reassembly_timeout_s']:
                self.buffer_bytes -= state.size
                del self.assemblies[key]
                self.stats['expired_messages'] += 1
        ttl = self.config.limits['dedup_ttl_s']
        for key, value in list(self.completed.items()):
            if now - value[0] > ttl:
                del self.completed[key]
            else:
                break

    async def _cleaner(self):
        while True:
            await asyncio.sleep(min(1, self.config.limits['reassembly_timeout_s']))
            self._clean()

    async def _send_frames(self, frames):
        packets = [frame.encode() for frame in frames]
        if any(len(packet) > self.config.limits['packet_bytes'] for packet in packets):
            raise ValueError('Transport packet exceeds configured MTU')
        await self.link.send_many(packets)
        self.stats['frames_sent'] += len(packets)

    async def _reader(self):
        while self.running:
            try:
                raw = await self.link.receive()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats['last_error'] = f'Link failed: {exc}'
                self.running = False
                LOGGER.exception('Radio receive failed')
                return
            try:
                frame = Frame.decode(raw, self.config.limits['packet_bytes'])
                if (frame.source, frame.destination) != (self.config.local['peer_id'], self.config.local['node_id']):
                    continue
                self.stats['frames_received'] += 1
                if frame.kind == ACK:
                    self._accept_ack(frame)
                elif frame.kind in (HELLO, HELLO_ACK):
                    await self._session_frame(frame)
                elif not self.config.session['enabled'] or self.session_ready.is_set():
                    await self._accept_data(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.stats['invalid_frames'] += 1
                self.stats['last_error'] = f'{type(exc).__name__}: {exc}'
                LOGGER.debug('Rejected packet: %s', exc)

    def _accept_ack(self, frame):
        state = self.pending.get(frame.message_id)
        if not state or frame.count != state.count or frame.index % 32 or frame.index >= state.count:
            return
        size = ACK_BODY.size
        if len(frame.payload) != size + self.security.auth_size:
            raise ValueError('Invalid ACK length')
        body, tag = frame.payload[:size], frame.payload[size:]
        self.security.check_auth(frame.header() + body, tag)
        bitmap, complete = ACK_BODY.unpack(body)
        if complete not in (0, 1):
            raise ValueError('Invalid ACK status')
        for offset in range(min(32, state.count-frame.index)):
            if bitmap & (1 << offset):
                state.acknowledged.add(frame.index + offset)
        state.complete = bool(complete)
        state.changed.set()

    async def _ack(self, frame, parts, complete):
        if not self.config.reliability['enabled'] or not frame.request_ack:
            return
        base = (frame.index // 32) * 32
        bitmap = sum(1 << offset for offset in range(min(32, frame.count-base))
                     if complete or base + offset in parts)
        body = ACK_BODY.pack(bitmap, int(complete))
        ack = Frame(ACK, frame.destination, frame.source, frame.message_id, base, frame.count, body)
        ack = replace(ack, payload=body + self.security.authenticate(ack.header() + body))
        await self._send_frames([ack])

    async def _accept_data(self, frame):
        key = (frame.source, frame.message_id)
        if key in self.completed:
            if self.completed[key][1] == frame.count:
                await self._ack(frame, (), True)
            return
        chunk_limit = self.config.limits['packet_bytes'] - OVERHEAD
        max_wire = self.config.limits['max_message_bytes'] * 2 + 1024
        if frame.count > math.ceil(max_wire / chunk_limit) or not frame.payload:
            raise ValueError('Invalid fragment count or empty fragment')
        state = self.assemblies.get(key)
        if state is None:
            if len(self.assemblies) >= self.config.limits['max_pending_messages']:
                self._clean()
                if len(self.assemblies) >= self.config.limits['max_pending_messages']:
                    return
            state = self.assemblies[key] = Assembly(frame.count)
        if state.count != frame.count:
            raise ValueError('Fragment count changed')
        if frame.index in state.parts:
            if state.parts[frame.index] != frame.payload:
                raise ValueError('Conflicting duplicate fragment')
        else:
            if state.size + len(frame.payload) > max_wire or self.buffer_bytes + len(frame.payload) > self.config.limits['max_buffer_bytes']:
                self.stats['rejected_messages'] += 1
                return
            state.parts[frame.index] = frame.payload
            state.size += len(frame.payload)
            state.updated = time.monotonic()
            self.buffer_bytes += len(frame.payload)
        complete = len(state.parts) == state.count
        if complete:
            if self.inbox.full():
                # No delivery ACK until the application inbox can accept the message.
                await self._ack(frame, state.parts, False)
                return
            try:
                obj = self.codec.decode(b''.join(state.parts[i] for i in range(state.count)),
                                        frame.message_id, frame.source, frame.destination)
            except Exception:
                self.stats['rejected_messages'] += 1
                self.buffer_bytes -= state.size
                del self.assemblies[key]
                raise
            self.inbox.put_nowait({'message_id': f'{frame.message_id:016x}', 'source': frame.source,
                                   'received_at_unix': time.time(), 'json': obj})
            self.stats['messages_received'] += 1
            self.buffer_bytes -= state.size
            del self.assemblies[key]
            self.completed[key] = (time.monotonic(), frame.count)
            while len(self.completed) > self.config.limits['dedup_entries']:
                self.completed.popitem(last=False)
        await self._ack(frame, state.parts, complete)

    async def _session_frame(self, frame):
        if not self.config.session['enabled']:
            return
        data_length = 16 + (32 if self.config.key_distribution['enabled'] else 0)
        body, tag = frame.payload[:data_length], frame.payload[data_length:]
        if len(body) != data_length:
            raise ValueError('Invalid session frame')
        self.security.check_auth(frame.header() + body, tag, control=True)
        token = body[:16]
        if self.config.key_distribution['enabled'] and not hmac.compare_digest(body[16:], self.security.peer_public):
            raise ValueError('Distributed public key differs from pinned peer key')
        if frame.kind == HELLO and self.config.mode == 'server':
            if self.session_ready.is_set() and token != self.security.session_token:
                raise ValueError('Session already active; restart both endpoints to establish a fresh session')
            self.security.activate(token)
            self.session_ready.set()
            response_body = token + (self.security.public_key if self.config.key_distribution['enabled'] else b'')
            reply = Frame(HELLO_ACK, frame.destination, frame.source, frame.message_id, payload=response_body)
            reply = replace(reply, payload=response_body + self.security.authenticate(reply.header() + response_body, control=True))
            await self._send_frames([reply])
        elif frame.kind == HELLO_ACK and self.config.mode == 'host':
            if frame.message_id == self.session_message_id and token == self.session_nonce:
                self.security.activate(token)
                self.session_ready.set()
                if self.session_future and not self.session_future.done():
                    self.session_future.set_result(True)

    async def _establish(self):
        if not self.config.session['enabled']:
            return {'enabled': False, 'established': False}
        if self.session_ready.is_set():
            return {'enabled': True, 'established': True}
        if self.config.mode == 'server':
            try:
                await asyncio.wait_for(self.session_ready.wait(), self.config.session['timeout_ms']/1000)
            except asyncio.TimeoutError as exc:
                raise TimeoutError('Establish the session from host first') from exc
        else:
            self.session_message_id = secrets.randbits(64)
            self.session_nonce = os.urandom(16)
            self.session_future = asyncio.get_running_loop().create_future()
            body = self.session_nonce + (self.security.public_key if self.config.key_distribution['enabled'] else b'')
            frame = Frame(HELLO, self.config.local['node_id'], self.config.local['peer_id'], self.session_message_id, payload=body)
            frame = replace(frame, payload=body + self.security.authenticate(frame.header() + body, control=True))
            for _ in range(self.config.session['retries'] + 1):
                await self._send_frames([frame])
                try:
                    await asyncio.wait_for(asyncio.shield(self.session_future), self.config.session['timeout_ms']/1000)
                    break
                except asyncio.TimeoutError:
                    continue
            if not self.session_ready.is_set():
                self.session_future.cancel()
                raise TimeoutError('Session establishment timed out')
        return {'enabled': True, 'established': True}

    async def establish(self):
        async with self.send_lock:
            return await self._establish()

    async def send(self, obj):
        queued = time.perf_counter()
        async with self.send_lock:
            if not self.running:
                raise RuntimeError('Transport is not running')
            started = time.perf_counter()
            await self._establish()
            msgid = secrets.randbits(64)
            blob, raw_bytes = self.codec.encode(obj, msgid)
            chunk = self.config.limits['packet_bytes'] - OVERHEAD
            count = math.ceil(len(blob) / chunk)
            if not 1 <= count <= 65535:
                raise ValueError('Message requires too many fragments')
            frames = [Frame(DATA, self.config.local['node_id'], self.config.local['peer_id'],
                            msgid, index, count, blob[index*chunk:(index+1)*chunk]) for index in range(count)]
            reliable = self.config.reliability['enabled']
            state = self.pending[msgid] = Outbound(count)
            retransmissions = 0
            try:
                window = self.config.reliability['window_size']
                cursor = 0
                while cursor < count and not state.complete:
                    end = min(count, cursor + window, (cursor//32 + 1)*32)
                    batch_indices = list(range(cursor, end))
                    retries = 0
                    while True:
                        missing = [i for i in batch_indices if i not in state.acknowledged]
                        if not missing:
                            if end == count and not state.complete:
                                missing = [end - 1]  # Probe for final delivery confirmation.
                            else:
                                break
                        if retries:
                            retransmissions += len(missing)
                            self.stats['retransmissions'] += len(missing)
                        outgoing = [replace(frames[i], request_ack=reliable and j == len(missing)-1)
                                    for j, i in enumerate(missing)]
                        state.changed.clear()
                        await self._send_frames(outgoing)
                        if not reliable:
                            break
                        deadline = time.monotonic() + self.config.reliability['ack_timeout_ms']/1000
                        while time.monotonic() < deadline:
                            if state.complete or (all(i in state.acknowledged for i in batch_indices) and end < count):
                                break
                            if not self.running:
                                raise RuntimeError('Transport stopped during send')
                            try:
                                await asyncio.wait_for(state.changed.wait(), max(0.001, deadline-time.monotonic()))
                                state.changed.clear()
                            except asyncio.TimeoutError:
                                break
                        if state.complete or (all(i in state.acknowledged for i in batch_indices) and end < count):
                            break
                        if retries >= self.config.reliability['max_retries']:
                            raise TimeoutError(f'No delivery ACK for message {msgid:016x}; delivery is uncertain')
                        retries += 1
                    cursor = end
                self.stats['messages_sent'] += 1
                return {'message_id': f'{msgid:016x}', 'status': 'delivered' if reliable else 'submitted',
                        'json_bytes': raw_bytes, 'encoded_bytes': len(blob), 'fragments': count,
                        'retransmitted_fragments': retransmissions,
                        'transport_latency_ms': round((time.perf_counter()-started)*1000, 3),
                        'queue_wait_ms': round((started-queued)*1000, 3)}
            except Exception:
                self.stats['send_failures'] += 1
                raise
            finally:
                self.pending.pop(msgid, None)

    def health(self):
        return {'running': self.running, 'mode': self.config.mode, 'backend': self.config.backend,
                'node_id': self.config.local['node_id'], 'peer_id': self.config.local['peer_id'],
                'compression': self.config.compression['algorithm'], 'encryption': self.config.encryption['algorithm'],
                'key_exchange': self.config.key_exchange['algorithm'], 'signing': self.config.signing['algorithm'],
                'session_enabled': self.config.session['enabled'], 'session_established': self.session_ready.is_set(),
                'key_distribution_enabled': self.config.key_distribution['enabled'],
                'acknowledgements': self.config.reliability['enabled'],
                'inbox_messages': self.inbox.qsize(), 'incomplete_messages': len(self.assemblies),
                'reassembly_bytes': self.buffer_bytes, 'link_dropped_buffers': getattr(self.link, 'dropped', 0), 'stats': dict(self.stats)}
