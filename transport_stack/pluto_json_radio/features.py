"""Link-quality and capacity features for the Pluto JSON radio.

The functions in this module are deliberately independent from the SDR
object, so they can be used with recorded IQ samples and in tests.  RSSI is
reported in dBFS by default; pass ``rssi_reference_dbm`` to convert that
value to dBm for a calibrated receiver.  SNR requires a signal capture and a
noise-only capture because an IQ buffer does not contain enough information
to infer an absolute noise floor reliably on its own.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import secrets
import sys
import threading
import time
from typing import Iterable

import numpy as np


_EPSILON = 1e-12


def _mean_power(iq: np.ndarray) -> float | None:
    samples = np.asarray(iq)
    if samples.size == 0:
        return None
    power = np.abs(samples.astype(np.complex128, copy=False)) ** 2
    finite_power = power[np.isfinite(power)]
    if finite_power.size == 0:
        return None
    return float(np.mean(finite_power))


def calculate_rssi(
    iq: np.ndarray,
    *,
    full_scale: float = 1.0,
    rssi_reference_dbm: float | None = None,
) -> float | None:
    """Return average IQ power in dBFS, or dBm when calibrated.

    ``full_scale`` must match the numeric scale of the samples.  Use ``1.0``
    for normalized samples, or the receiver's ADC full-scale value for raw
    Pluto samples.  ``rssi_reference_dbm`` is the dBm value corresponding to
    0 dBFS and is normally obtained from a calibration measurement.
    """
    if full_scale <= 0:
        raise ValueError("full_scale must be positive")
    power = _mean_power(iq)
    if power is None or power <= 0:
        return None
    rssi_dbfs = 10.0 * math.log10(power / (full_scale**2))
    if rssi_reference_dbm is not None:
        return rssi_dbfs + float(rssi_reference_dbm)
    return rssi_dbfs


def calculate_snr(
    signal_iq: np.ndarray,
    noise_iq: np.ndarray | None,
) -> float | None:
    """Return SNR in dB from a signal-plus-noise capture and noise capture.

    If ``noise_iq`` is omitted or empty, ``None`` is returned rather than an
    unreliable estimate.  The signal power is calculated as total power
    minus the measured noise power.
    """
    total_power = _mean_power(signal_iq)
    noise_power = _mean_power(noise_iq) if noise_iq is not None else None
    if total_power is None or noise_power is None or noise_power <= 0:
        return None
    signal_power = max(total_power - noise_power, _EPSILON)
    return 10.0 * math.log10(signal_power / noise_power)


def calculate_ber(bit_errors: int, total_bits: int) -> float | None:
    """Return bit-error rate, or ``None`` when no bits were measured."""
    if bit_errors < 0 or total_bits < 0:
        raise ValueError("bit_errors and total_bits cannot be negative")
    if bit_errors > total_bits:
        raise ValueError("bit_errors cannot exceed total_bits")
    if total_bits == 0:
        return None
    return float(bit_errors) / float(total_bits)


def calculate_packet_loss_rate(
    packets_sent: int,
    packets_received: int,
) -> float | None:
    """Return packet loss as a fraction in the range 0.0 to 1.0."""
    if packets_sent < 0 or packets_received < 0:
        raise ValueError("packet counts cannot be negative")
    if packets_received > packets_sent:
        raise ValueError("packets_received cannot exceed packets_sent")
    if packets_sent == 0:
        return None
    return float(packets_sent - packets_received) / float(packets_sent)


def calculate_average_latency(
    latencies_seconds: Iterable[float],
) -> float | None:
    """Return mean latency in milliseconds, or ``None`` when there are none."""
    values = [float(value) for value in latencies_seconds]
    if not values:
        return None
    if any(value < 0 or not math.isfinite(value) for value in values):
        raise ValueError("latencies must be finite and non-negative")
    return float(np.mean(values) * 1000.0)


def estimate_fsk_occupied_bandwidth(
    symbol_rate_hz: float,
    tone_deviation_hz: float,
) -> float:
    """Estimate 2-FSK occupied bandwidth using Carson's rule in Hz."""
    if symbol_rate_hz <= 0 or tone_deviation_hz < 0:
        raise ValueError("symbol rate must be positive and deviation non-negative")
    return 2.0 * (float(tone_deviation_hz) + float(symbol_rate_hz))


def calculate_remaining_bandwidth(
    configured_bandwidth_hz: float,
    occupied_bandwidth_hz: float,
) -> float:
    """Return unused configured RF bandwidth in Hz, never below zero."""
    if configured_bandwidth_hz < 0 or occupied_bandwidth_hz < 0:
        raise ValueError("bandwidth values cannot be negative")
    return max(float(configured_bandwidth_hz) - float(occupied_bandwidth_hz), 0.0)


def _decode_candidate_message(candidate, config) -> dict[str, object] | None:
    """Authenticate and decode one PHY candidate for a configured receiver."""
    from .v2 import V2Error, decode_v2_payload, v2_application

    address_by_code = {
        int(code): address
        for address, code in config.link.value_codes["receiver_id"].items()
    }
    decoded_v2 = candidate.packet.message
    local_code = config.link.value_codes["receiver_id"].get(
        config.link.local_address
    )
    if decoded_v2.get("_encrypted") or decoded_v2.get("_signed"):
        if decoded_v2.get("receiver_node_id") != local_code:
            return None
        peer_public_keys_by_code = {
            int(code): config.security.peer_public_keys[address]
            for address, code in config.link.value_codes["source_id"].items()
            if address in config.security.peer_public_keys
        }
        peer_signing_public_keys_by_code = {
            int(code): config.security.peer_signing_public_keys[address]
            for address, code in config.link.value_codes["source_id"].items()
            if address in config.security.peer_signing_public_keys
        }
        try:
            decoded_v2 = decode_v2_payload(
                candidate.packet.payload_bytes,
                private_key=config.security.private_key,
                peer_public_keys_by_code=peer_public_keys_by_code,
                peer_signing_public_keys_by_code=peer_signing_public_keys_by_code,
            )
        except V2Error:
            return None
    if (
        config.security.peer_signing_public_keys
        and decoded_v2.get("signature_method") != "ed25519"
    ):
        return None
    return v2_application(decoded_v2, address_by_code)


def _candidate_iq(iq: np.ndarray, candidate, preamble: bytes, samples_per_symbol: int) -> np.ndarray:
    """Extract the detected packet waveform from a capture buffer."""
    packet_start = candidate.offset + candidate.symbol_index * samples_per_symbol
    packet_bits = len(preamble) * 8 + 16 + len(candidate.packet.payload_bytes) * 8
    packet_end = packet_start + packet_bits * samples_per_symbol
    return iq[packet_start:packet_end]


def _ack_iq(message: dict[str, object], config, preamble: bytes) -> np.ndarray:
    from .phy import modulate_fast_acknowledgement

    iq = modulate_fast_acknowledgement(
        str(message["nonce"]),
        preamble=preamble,
        sample_rate_hz=config.device.sample_rate_hz,
        samples_per_symbol=config.phy.samples_per_symbol,
        tone_deviation_hz=config.phy.tone_deviation_hz,
    )
    if config.phy.ack_burst_copies == 1:
        return iq
    gap = np.zeros(int(config.device.sample_rate_hz * 0.005), dtype=np.complex64)
    parts = []
    for index in range(config.phy.ack_burst_copies):
        parts.append(iq)
        if index + 1 < config.phy.ack_burst_copies:
            parts.append(gap)
    return np.concatenate(parts)


def _wait_for_ack(sdr, nonce: str, config, preamble: bytes, timeout: float) -> bool:
    from .phy import find_fast_acknowledgements
    from .pluto import receive_chunk

    deadline = time.monotonic() + timeout
    chunks: list[np.ndarray] = []
    while time.monotonic() < deadline:
        chunk = receive_chunk(sdr)
        if not chunk.size:
            continue
        chunks.append(chunk)
        if len(chunks) > 3:
            chunks.pop(0)
        if nonce in find_fast_acknowledgements(
            np.concatenate(chunks),
            preamble=preamble,
            sample_rate_hz=config.device.sample_rate_hz,
            samples_per_symbol=config.phy.samples_per_symbol,
            tone_deviation_hz=config.phy.tone_deviation_hz,
            max_preamble_bit_errors=config.phy.max_preamble_bit_errors,
        ):
            return True
    return False


def _build_probe(config, sequence: int) -> tuple[str, np.ndarray]:
    from .phy import modulate_packet
    from .v2 import V2Error, encode_v2_frame

    receiver_address = config.link.default_receiver_address
    sender_address = config.link.local_address
    nonce = secrets.token_hex(8)
    payload = {
        "text": "feature link probe",
        "seq": sequence,
        "timestamp_unix": time.time(),
    }
    receiver_code = config.link.value_codes["receiver_id"][receiver_address]
    source_code = config.link.value_codes["source_id"][sender_address]
    try:
        frame, _ = encode_v2_frame(
            receiver_code=receiver_code,
            source_code=source_code,
            nonce=nonce,
            payload=payload,
            packet_type="data",
            compression_method="none",
            encryption_method=config.security.encryption_method,
            private_key=config.security.private_key,
            recipient_public_key=config.security.peer_public_keys.get(receiver_address),
            signing_private_key=config.security.signing_private_key,
            max_payload_bytes=config.phy.max_payload_bytes,
        )
    except (KeyError, ValueError, V2Error) as exc:
        raise RuntimeError(f"could not build link probe: {exc}") from exc
    preamble = bytes.fromhex(config.phy.preamble_hex)
    iq = modulate_packet(
        frame,
        preamble=preamble,
        sample_rate_hz=config.device.sample_rate_hz,
        samples_per_symbol=config.phy.samples_per_symbol,
        tone_deviation_hz=config.phy.tone_deviation_hz,
    )
    return nonce, iq


@dataclass(frozen=True)
class RadioFeatures:
    """A snapshot of link-quality features and their native units."""

    rssi_dbfs: float | None = None
    rssi_dbm: float | None = None
    snr_db: float | None = None
    ber: float | None = None
    packet_loss_rate: float | None = None
    average_latency_ms: float | None = None
    remaining_bandwidth_hz: float | None = None

    def as_dict(self) -> dict[str, float | None]:
        """Return feature names suitable for logging or a data frame."""
        return {
            "rssi": self.rssi_dbm if self.rssi_dbm is not None else self.rssi_dbfs,
            "rssi_dbfs": self.rssi_dbfs,
            "rssi_dbm": self.rssi_dbm,
            "snr": self.snr_db,
            "snr_db": self.snr_db,
            "ber": self.ber,
            "packet_loss_rate": self.packet_loss_rate,
            "average_latency_ms": self.average_latency_ms,
            "remaining_bandwidth_hz": self.remaining_bandwidth_hz,
        }


class FeatureTracker:
    """Accumulate link observations and return rolling aggregate features.

    Call :meth:`observe_iq` for each received signal window, then call
    :meth:`record_packet` once per expected packet.  A timeout is represented
    by ``received=False``.  Latency is the elapsed time from transmission to
    ACK in seconds.
    """

    def __init__(
        self,
        *,
        full_scale: float = 1.0,
        rssi_reference_dbm: float | None = None,
        configured_bandwidth_hz: float | None = None,
        occupied_bandwidth_hz: float | None = None,
        symbol_rate_hz: float | None = None,
        tone_deviation_hz: float | None = None,
    ) -> None:
        if full_scale <= 0:
            raise ValueError("full_scale must be positive")
        self.full_scale = float(full_scale)
        self.rssi_reference_dbm = rssi_reference_dbm
        self.configured_bandwidth_hz = configured_bandwidth_hz
        self.occupied_bandwidth_hz = occupied_bandwidth_hz
        if occupied_bandwidth_hz is None and (
            symbol_rate_hz is not None and tone_deviation_hz is not None
        ):
            self.occupied_bandwidth_hz = estimate_fsk_occupied_bandwidth(
                symbol_rate_hz, tone_deviation_hz
            )

        self._rssi_dbfs: float | None = None
        self._rssi_dbm: float | None = None
        self._snr_db: float | None = None
        self._bit_errors = 0
        self._total_bits = 0
        self._packets_sent = 0
        self._packets_received = 0
        self._latencies_seconds: list[float] = []

    def observe_iq(
        self,
        signal_iq: np.ndarray,
        *,
        noise_iq: np.ndarray | None = None,
    ) -> None:
        """Update RSSI and, when supplied, SNR from IQ captures."""
        self._rssi_dbfs = calculate_rssi(signal_iq, full_scale=self.full_scale)
        if self.rssi_reference_dbm is not None and self._rssi_dbfs is not None:
            self._rssi_dbm = self._rssi_dbfs + float(self.rssi_reference_dbm)
        else:
            self._rssi_dbm = None
        if noise_iq is not None:
            self._snr_db = calculate_snr(signal_iq, noise_iq)

    def record_packet(
        self,
        *,
        received: bool,
        bit_errors: int = 0,
        total_bits: int = 0,
        latency_seconds: float | None = None,
    ) -> None:
        """Record one expected packet and its optional decode statistics."""
        if bit_errors < 0 or total_bits < 0 or bit_errors > total_bits:
            raise ValueError("invalid bit error counts")
        self._packets_sent += 1
        if received:
            self._packets_received += 1
        self._bit_errors += bit_errors
        self._total_bits += total_bits
        if latency_seconds is not None:
            latency = float(latency_seconds)
            if latency < 0 or not math.isfinite(latency):
                raise ValueError("latency_seconds must be finite and non-negative")
            self._latencies_seconds.append(latency)

    def get_features(self) -> RadioFeatures:
        """Return the current aggregate feature snapshot."""
        remaining_bandwidth_hz = None
        if (
            self.configured_bandwidth_hz is not None
            and self.occupied_bandwidth_hz is not None
        ):
            remaining_bandwidth_hz = calculate_remaining_bandwidth(
                self.configured_bandwidth_hz,
                self.occupied_bandwidth_hz,
            )
        return RadioFeatures(
            rssi_dbfs=self._rssi_dbfs,
            rssi_dbm=self._rssi_dbm,
            snr_db=self._snr_db,
            ber=calculate_ber(self._bit_errors, self._total_bits),
            packet_loss_rate=calculate_packet_loss_rate(
                self._packets_sent, self._packets_received
            ),
            average_latency_ms=calculate_average_latency(self._latencies_seconds),
            remaining_bandwidth_hz=remaining_bandwidth_hz,
        )


def _live_feature_snapshot(
    config_path: str,
    *,
    seconds: float,
    noise_seconds: float,
    full_scale: float,
    rssi_reference_dbm: float | None,
    expected_packets: int | None,
) -> RadioFeatures:
    """Capture live IQ from one configured Pluto and calculate features."""
    from .config import load_config
    from .phy import find_packets
    from .pluto import make_pluto, receive_chunk, receive_samples
    from .v2 import V2Error, decode_v2_payload, v2_application

    if seconds <= 0 or noise_seconds < 0:
        raise ValueError("seconds must be positive and noise_seconds cannot be negative")
    if expected_packets is not None and expected_packets < 0:
        raise ValueError("expected_packets cannot be negative")

    config = load_config(config_path)
    sdr = make_pluto(config.device)
    # Keep the transmitter quiet during this interval.  The baseline is used
    # as the noise-only reference for SNR.
    noise_iq = receive_samples(sdr, noise_seconds) if noise_seconds else None
    tracker = FeatureTracker(
        full_scale=full_scale,
        rssi_reference_dbm=rssi_reference_dbm,
        configured_bandwidth_hz=config.device.rf_bandwidth_hz,
        symbol_rate_hz=(
            config.device.sample_rate_hz / config.phy.samples_per_symbol
        ),
        tone_deviation_hz=config.phy.tone_deviation_hz,
    )
    preamble = bytes.fromhex(config.phy.preamble_hex)
    address_by_code = {
        int(code): address
        for address, code in config.link.value_codes["receiver_id"].items()
    }
    peer_public_keys_by_code = {
        int(code): config.security.peer_public_keys[address]
        for address, code in config.link.value_codes["source_id"].items()
        if address in config.security.peer_public_keys
    }
    peer_signing_public_keys_by_code = {
        int(code): config.security.peer_signing_public_keys[address]
        for address, code in config.link.value_codes["source_id"].items()
        if address in config.security.peer_signing_public_keys
    }
    local_code = config.link.value_codes["receiver_id"].get(
        config.link.local_address
    )
    overlap = np.array([], dtype=np.complex64)
    overlap_samples = int(
        config.device.sample_rate_hz * config.phy.rx_overlap_seconds
    )
    seen_nonces: set[str] = set()
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        fresh_iq = receive_chunk(sdr)
        if not fresh_iq.size:
            continue
        iq = np.concatenate((overlap, fresh_iq)) if overlap.size else fresh_iq
        tracker.observe_iq(iq, noise_iq=noise_iq)
        candidates = find_packets(
            iq,
            preamble=preamble,
            sample_rate_hz=config.device.sample_rate_hz,
            samples_per_symbol=config.phy.samples_per_symbol,
            tone_deviation_hz=config.phy.tone_deviation_hz,
            max_payload_bytes=config.phy.max_payload_bytes,
            max_preamble_bit_errors=config.phy.max_preamble_bit_errors,
            stop_after_first=True,
        )
        overlap = iq[-overlap_samples:].copy() if overlap_samples else overlap
        for candidate in candidates:
            nonce = str(candidate.packet.message.get("nonce", ""))
            if nonce in seen_nonces:
                continue
            decoded_v2 = candidate.packet.message
            if decoded_v2.get("_encrypted") or decoded_v2.get("_signed"):
                if decoded_v2.get("receiver_node_id") != local_code:
                    continue
                try:
                    decoded_v2 = decode_v2_payload(
                        candidate.packet.payload_bytes,
                        private_key=config.security.private_key,
                        peer_public_keys_by_code=peer_public_keys_by_code,
                        peer_signing_public_keys_by_code=peer_signing_public_keys_by_code,
                    )
                except V2Error:
                    continue
            if (
                config.security.peer_signing_public_keys
                and decoded_v2.get("signature_method") != "ed25519"
            ):
                continue
            message = v2_application(decoded_v2, address_by_code)
            latency_seconds = None
            timestamp = message.get("timestamp_unix")
            if isinstance(timestamp, (int, float)) and math.isfinite(float(timestamp)):
                elapsed = time.time() - float(timestamp)
                if elapsed >= 0:
                    latency_seconds = elapsed
            seen_nonces.add(nonce)
            # The available decoder error count covers the preamble.  Report
            # that measured portion rather than pretending payload BER is
            # available from a successfully authenticated frame.
            tracker.record_packet(
                received=True,
                bit_errors=candidate.bit_errors,
                total_bits=len(preamble) * 8,
                latency_seconds=latency_seconds,
            )

    if expected_packets is not None:
        received_packets = len(seen_nonces)
        if received_packets > expected_packets:
            raise ValueError(
                "expected_packets is smaller than packets observed during capture"
            )
        for _ in range(expected_packets - received_packets):
            tracker.record_packet(received=False)
    return tracker.get_features()


def _active_link_test(
    config_path: str,
    peer_config_path: str,
    *,
    packets: int,
    noise_seconds: float,
    full_scale: float,
    rssi_reference_dbm: float | None,
    gap_seconds: float,
) -> RadioFeatures:
    """Run a two-Pluto probe and combine receiver and sender measurements."""
    from .config import load_config
    from .phy import find_packets
    from .pluto import make_pluto, prime_receiver, receive_chunk, receive_samples, transmit_once

    if packets <= 0:
        raise ValueError("packets must be positive")
    if noise_seconds < 0 or gap_seconds < 0:
        raise ValueError("noise_seconds and gap_seconds cannot be negative")

    sender_config = load_config(config_path)
    receiver_config = load_config(peer_config_path)
    sender_sdr = make_pluto(sender_config.device)
    receiver_sdr = make_pluto(receiver_config.device)
    sender_tracker = FeatureTracker(
        configured_bandwidth_hz=sender_config.device.rf_bandwidth_hz,
        symbol_rate_hz=(
            sender_config.device.sample_rate_hz
            / sender_config.phy.samples_per_symbol
        ),
        tone_deviation_hz=sender_config.phy.tone_deviation_hz,
    )
    receiver_tracker = FeatureTracker(
        full_scale=full_scale,
        rssi_reference_dbm=rssi_reference_dbm,
        configured_bandwidth_hz=receiver_config.device.rf_bandwidth_hz,
        symbol_rate_hz=(
            receiver_config.device.sample_rate_hz
            / receiver_config.phy.samples_per_symbol
        ),
        tone_deviation_hz=receiver_config.phy.tone_deviation_hz,
    )
    receiver_stop = threading.Event()
    receiver_ready = threading.Event()
    receiver_errors: list[BaseException] = []
    receiver_preamble = bytes.fromhex(receiver_config.phy.preamble_hex)
    receiver_seen_nonces: set[str] = set()

    def receive_probes() -> None:
        try:
            noise_iq = (
                receive_samples(receiver_sdr, noise_seconds)
                if noise_seconds
                else None
            )
            receiver_ready.set()
            overlap = np.array([], dtype=np.complex64)
            overlap_samples = int(
                receiver_config.device.sample_rate_hz
                * receiver_config.phy.rx_overlap_seconds
            )
            while not receiver_stop.is_set():
                fresh_iq = receive_samples(
                    receiver_sdr, receiver_config.phy.capture_seconds
                )
                if not fresh_iq.size:
                    continue
                iq = (
                    np.concatenate((overlap, fresh_iq))
                    if overlap.size
                    else fresh_iq
                )
                candidates = find_packets(
                    iq,
                    preamble=receiver_preamble,
                    sample_rate_hz=receiver_config.device.sample_rate_hz,
                    samples_per_symbol=receiver_config.phy.samples_per_symbol,
                    tone_deviation_hz=receiver_config.phy.tone_deviation_hz,
                    max_payload_bytes=receiver_config.phy.max_payload_bytes,
                    max_preamble_bit_errors=receiver_config.phy.max_preamble_bit_errors,
                    stop_after_first=True,
                )
                overlap = (
                    iq[-overlap_samples:].copy()
                    if overlap_samples
                    else overlap
                )
                for candidate in candidates:
                    message = _decode_candidate_message(candidate, receiver_config)
                    if message is None:
                        continue
                    nonce = str(message.get("nonce", ""))
                    if nonce in receiver_seen_nonces:
                        continue
                    receiver_seen_nonces.add(nonce)
                    packet_iq = _candidate_iq(
                        iq,
                        candidate,
                        receiver_preamble,
                        receiver_config.phy.samples_per_symbol,
                    )
                    receiver_tracker.observe_iq(packet_iq, noise_iq=noise_iq)
                    latency_seconds = None
                    timestamp = message.get("timestamp_unix")
                    if isinstance(timestamp, (int, float)) and math.isfinite(float(timestamp)):
                        elapsed = time.time() - float(timestamp)
                        if elapsed >= 0:
                            latency_seconds = elapsed
                    receiver_tracker.record_packet(
                        received=True,
                        bit_errors=candidate.bit_errors,
                        total_bits=len(receiver_preamble) * 8,
                        latency_seconds=latency_seconds,
                    )
                    transmit_once(
                        receiver_sdr,
                        _ack_iq(message, receiver_config, receiver_preamble),
                    )
        except BaseException as exc:
            receiver_errors.append(exc)
            receiver_ready.set()

    receiver_thread = threading.Thread(target=receive_probes, daemon=True)
    receiver_thread.start()
    if not receiver_ready.wait(timeout=max(noise_seconds + 10.0, 10.0)):
        receiver_stop.set()
        receiver_thread.join(timeout=2.0)
        raise RuntimeError("receiver Pluto did not become ready")
    if receiver_errors:
        receiver_stop.set()
        receiver_thread.join(timeout=2.0)
        raise RuntimeError(f"receiver probe failed: {receiver_errors[0]}")

    try:
        prime_receiver(sender_sdr)
        sender_preamble = bytes.fromhex(sender_config.phy.preamble_hex)
        for sequence in range(packets):
            nonce, probe_iq = _build_probe(sender_config, sequence)
            started = time.monotonic()
            transmit_once(sender_sdr, probe_iq)
            acknowledged = _wait_for_ack(
                sender_sdr,
                nonce,
                sender_config,
                sender_preamble,
                sender_config.phy.ack_wait_seconds,
            )
            elapsed = time.monotonic() - started
            sender_tracker.record_packet(
                received=acknowledged,
                latency_seconds=elapsed if acknowledged else None,
            )
            if gap_seconds:
                time.sleep(gap_seconds)
    finally:
        receiver_stop.set()
        receiver_thread.join(timeout=3.0)

    if receiver_errors:
        raise RuntimeError(f"receiver probe failed: {receiver_errors[0]}")
    sender_features = sender_tracker.get_features()
    receiver_features = receiver_tracker.get_features()
    return RadioFeatures(
        rssi_dbfs=receiver_features.rssi_dbfs,
        rssi_dbm=receiver_features.rssi_dbm,
        snr_db=receiver_features.snr_db,
        ber=receiver_features.ber,
        packet_loss_rate=sender_features.packet_loss_rate,
        average_latency_ms=sender_features.average_latency_ms,
        remaining_bandwidth_hz=receiver_features.remaining_bandwidth_hz,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Display live link-quality features from an ADALM-Pluto."
    )
    parser.add_argument("--config", default="configs/pluto.example.yaml")
    parser.add_argument(
        "--test-link",
        action="store_true",
        help="Run an active probe using this Pluto and the peer Pluto.",
    )
    parser.add_argument(
        "--peer-config",
        default=None,
        help="Peer config path required by --test-link.",
    )
    parser.add_argument(
        "--packets",
        type=int,
        default=5,
        help="Probe packets for --test-link (default: 5).",
    )
    parser.add_argument(
        "--gap-seconds",
        type=float,
        default=0.05,
        help="Gap between active probes (default: 0.05).",
    )
    parser.add_argument(
        "--seconds",
        type=float,
        default=5.0,
        help="Seconds of live IQ to capture (default: 5).",
    )
    parser.add_argument(
        "--noise-seconds",
        type=float,
        default=0.5,
        help="Quiet baseline capture used for SNR (default: 0.5).",
    )
    parser.add_argument(
        "--full-scale",
        type=float,
        default=2048.0,
        help="ADC full-scale for raw Pluto IQ samples (default: 2048).",
    )
    parser.add_argument(
        "--rssi-reference-dbm",
        type=float,
        default=None,
        help="Calibrated dBm value corresponding to 0 dBFS.",
    )
    parser.add_argument(
        "--expected-packets",
        type=int,
        default=None,
        help="Expected packets during the capture, for packet-loss calculation.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print one JSON object instead of human-readable lines.",
    )
    args = parser.parse_args(argv)

    try:
        if args.test_link:
            if not args.peer_config:
                parser.error("--peer-config is required with --test-link")
            features = _active_link_test(
                args.config,
                args.peer_config,
                packets=args.packets,
                noise_seconds=args.noise_seconds,
                full_scale=args.full_scale,
                rssi_reference_dbm=args.rssi_reference_dbm,
                gap_seconds=args.gap_seconds,
            )
        else:
            features = _live_feature_snapshot(
                args.config,
                seconds=args.seconds,
                noise_seconds=args.noise_seconds,
                full_scale=args.full_scale,
                rssi_reference_dbm=args.rssi_reference_dbm,
                expected_packets=args.expected_packets,
            )
    except Exception as exc:
        print(f"Feature capture failed: {exc}", file=sys.stderr)
        return 1

    values = {
        "rssi_dbfs": features.rssi_dbfs,
        "rssi_dbm": features.rssi_dbm,
        "snr_db": features.snr_db,
        "ber": features.ber,
        "packet_loss_rate": features.packet_loss_rate,
        "average_latency_ms": features.average_latency_ms,
        "remaining_bandwidth_hz": features.remaining_bandwidth_hz,
    }
    if args.json:
        print(json.dumps(values, sort_keys=True))
    else:
        for name, value in values.items():
            print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
