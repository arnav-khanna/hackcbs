"""Persistent Pluto RX/TX with the existing noncoherent 2-FSK demodulator."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
import fcntl
from pathlib import Path
import queue
import tempfile
import threading
import time
import numpy as np

from .wire import Frame


class RadioLink:
    def __init__(self, config):
        self.config = config
        self.queue = None
        self.tx_queue = queue.Queue(maxsize=32)
        self.iq_queue = queue.Queue(maxsize=128)
        self.capture_epoch = 0
        self.stop = threading.Event()
        self.threads = []
        self.sdr = None
        self.lock = None
        self.overlap = np.array([], dtype=np.complex64)
        self.samples_received = 0
        self.seen = OrderedDict()
        self.dropped = 0
        self._tone_cache = {}
        self.preamble = bytes.fromhex(config.radio['preamble_hex'])
        self.sps = config.radio['samples_per_symbol']
        self.rate = config.radio['sample_rate_hz']
        self.overlap_size = (len(self.preamble) + 2 + config.limits['packet_bytes']) * 8 * self.sps

    def _open(self):
        from pluto_json_radio.native_libiio import prepare_libiio
        from pluto_json_radio.config import DeviceConfig
        from pluto_json_radio.pluto import make_pluto
        probe = prepare_libiio()
        if not probe.ok:
            raise RuntimeError(probe.detail)
        serial = self.config.local['usb_serial']
        uri = self.config.local['uri']
        if serial:
            import iio
            matches = [uri for uri, description in iio.scan_contexts().items()
                       if uri.startswith('usb:') and f'serial={serial}' in description]
            if len(matches) != 1:
                raise RuntimeError(f'Cannot find Pluto USB serial {serial}. Set usb_serial: null to use a network URI.')
            uri = matches[0]
        name = serial or ''.join(c if c.isalnum() else '-' for c in uri)
        self.lock = (Path(tempfile.gettempdir()) / f'pluto-json-{name}.lock').open('w')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            self.lock = None
            raise RuntimeError('This Pluto is already in use by another JSON transport process') from exc
        r = self.config.radio
        device = DeviceConfig(uri, r['center_frequency_hz'], self.rate, r['rf_bandwidth_hz'],
                              r['tx_gain_db'], r['rx_gain_db'], r['rx_buffer_size'])
        self.sdr = make_pluto(device)
        self.sdr._ctx.set_timeout(1000)
        self.sdr._rxadc.set_kernel_buffers_count(r['rx_kernel_buffers'])
        self.sdr._txdac.set_kernel_buffers_count(r['tx_kernel_buffers'])
        self.sdr.rx()  # Prime the persistent capture buffer once.
        self.uri = uri

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=1024)
        try:
            await asyncio.to_thread(self._open)
        except Exception:
            await self.close()
            raise
        for target in (self._rx_worker, self._decode_worker):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def modulate(self, packet):
        # Vectorize the existing continuous-phase BFSK waveform: one NumPy
        # cumulative sum replaces the legacy Python loop for every bit.
        data = len(packet).to_bytes(2, 'big') + packet
        bits = np.unpackbits(np.frombuffer(self.preamble + data, dtype=np.uint8))
        step = 2 * np.pi * self.config.radio['tone_deviation_hz'] / self.rate
        increments = np.repeat((bits.astype(np.float64)*2-1)*step, self.sps)
        phase = np.cumsum(increments) - increments
        iq = np.asarray(0.55 * np.exp(1j*phase), dtype=np.complex64)
        from pluto_json_radio.phy import _add_ramps
        return _add_ramps(iq, max(4, self.sps//2))

    def decode_iq(self, chunk):
        from pluto_json_radio.phy import bits_to_bytes
        iq = np.concatenate((self.overlap, chunk)) if self.overlap.size else chunk
        origin = self.samples_received - len(self.overlap)
        self.samples_received += len(chunk)
        preamble_bits = np.unpackbits(np.frombuffer(self.preamble, dtype=np.uint8))
        bipolar_preamble = preamble_bits.astype(np.int16)*2-1
        result = []
        keep_from = max(0, len(iq) - (len(preamble_bits)+18)*self.sps)
        # Compute sliding matched-tone energy once for every timing offset,
        # instead of multiplying and summing the whole capture sps times.
        if len(iq) < self.sps:
            self.overlap = iq.copy()
            return result
        tone = self._tone_cache.get(len(iq))
        if tone is None:
            step = 2*np.pi*self.config.radio['tone_deviation_hz']/self.rate
            tone = np.exp(-1j*step*np.arange(len(iq))).astype(np.complex64)
            if len(self._tone_cache) >= 32:
                self._tone_cache.clear()
            self._tone_cache[len(iq)] = tone
        energies = []
        for oscillator in (tone, tone.conj()):
            cumulative = np.empty(len(iq)+1, dtype=np.complex128)
            cumulative[0] = 0
            np.cumsum(iq*oscillator, dtype=np.complex128, out=cumulative[1:])
            energies.append(np.abs(cumulative[self.sps:]-cumulative[:-self.sps])**2)
        decisions = (energies[0] > energies[1]).astype(np.uint8)
        for offset in range(self.sps):
            bits = decisions[offset::self.sps]
            search_size = len(bits) - len(preamble_bits) - 16 + 1
            if search_size <= 0:
                continue
            correlation = np.correlate(bits.astype(np.int16)*2-1, bipolar_preamble, mode='valid')[:search_size]
            for symbol in np.flatnonzero(correlation >= len(preamble_bits)-8):
                length_start = symbol + len(preamble_bits)
                length = int.from_bytes(bits_to_bytes(bits[length_start:length_start+16]), 'big')
                end = length_start + 16 + length*8
                if not 0 < length <= self.config.limits['packet_bytes']:
                    continue
                if end > len(bits):
                    keep_from = min(keep_from, max(0, offset+int(symbol)*self.sps-self.sps))
                    continue
                raw = bits_to_bytes(bits[length_start+16:end])
                try:
                    frame = Frame.decode(raw, self.config.limits['packet_bytes'])
                except ValueError:
                    continue
                position = origin + offset + int(symbol)*self.sps
                key = (frame.source, frame.message_id, frame.kind, frame.index, raw[-4:])
                bucket = position//(4*self.sps)
                if any(abs(self.seen.get(key+(candidate,), -10**18)-position) < self.sps*2
                       for candidate in (bucket-1, bucket, bucket+1)):
                    continue
                self.seen[key+(bucket,)] = position
                result.append(raw)
        self.overlap = iq[max(keep_from, len(iq)-self.overlap_size):].copy()
        for key, position in list(self.seen.items()):
            if position < self.samples_received-len(self.overlap)-self.sps*2:
                del self.seen[key]
        return result

    def _enqueue(self, packet):
        try:
            self.queue.put_nowait(packet)
        except asyncio.QueueFull:
            self.dropped += 1

    def _rx_worker(self):
        try:
            while not self.stop.is_set():
                # Keep device I/O on one worker; sample decoding runs
                # separately and never accesses the device context.
                try:
                    packets, future = self.tx_queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    self._transmit(packets, future)
                chunk = np.asarray(self.sdr.rx(), dtype=np.complex64)
                try:
                    self.iq_queue.put_nowait((self.capture_epoch, chunk))
                except queue.Full:
                    self.dropped += 1
                    self.capture_epoch += 1
        except Exception as exc:
            if not self.stop.is_set():
                self.loop.call_soon_threadsafe(self._enqueue, exc)

    def _decode_worker(self):
        epoch = 0
        try:
            while not self.stop.is_set():
                try:
                    capture_epoch, chunk = self.iq_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if epoch != capture_epoch:
                    self.overlap = np.array([], dtype=np.complex64)
                    self.seen.clear()
                    epoch = capture_epoch
                for packet in self.decode_iq(chunk):
                    self.loop.call_soon_threadsafe(self._enqueue, packet)
        except Exception as exc:
            if not self.stop.is_set():
                self.loop.call_soon_threadsafe(self._enqueue, exc)

    @staticmethod
    def _complete(future, error=None):
        if not future.done():
            if error:
                future.set_exception(error)
            else:
                future.set_result(None)

    def _transmit(self, packets, future):
        try:
            waves = [self.modulate(packet) for packet in packets] * self.config.radio['burst_copies']
            gap = np.zeros(int(self.rate*self.config.radio['interframe_gap_ms']/1000), dtype=np.complex64)
            iq = np.concatenate([piece for index,wave in enumerate(waves)
                                 for piece in ((gap, wave) if index else (wave,))])
            iq = np.concatenate((iq, np.zeros(int(self.rate*self.config.radio['tail_padding_ms']/1000), dtype=np.complex64)))
            self.sdr.tx(np.asarray(iq*(2**14), dtype=np.complex64))
            time.sleep(len(iq)/self.rate + self.config.radio['tx_guard_ms']/1000)
            self.sdr.tx_destroy_buffer()
            self.loop.call_soon_threadsafe(self._complete, future)
        except Exception as exc:
            self.loop.call_soon_threadsafe(self._complete, future, exc)

    async def send_many(self, packets):
        if self.stop.is_set():
            raise RuntimeError('Radio has stopped')
        future = self.loop.create_future()
        try:
            self.tx_queue.put_nowait((packets, future))
        except queue.Full as exc:
            raise RuntimeError('Radio transmit queue is full') from exc
        await future

    async def receive(self):
        packet = await self.queue.get()
        if isinstance(packet, Exception):
            raise packet
        return packet

    def airtime(self, packets):
        return sum((len(self.preamble)+2+len(p))*8*self.sps/self.rate for p in packets)

    async def close(self):
        self.stop.set()
        for thread in self.threads:
            await asyncio.to_thread(thread.join, 2)
        if self.sdr:
            self.sdr.rx_destroy_buffer()
            self.sdr.tx_destroy_buffer()
            self.sdr = None
        if self.lock:
            self.lock.close()
            self.lock = None
