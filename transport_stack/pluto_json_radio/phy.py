from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

import numpy as np

from .v2 import V2Error, decode_v2_payload

FAST_ACK_MAGIC = b"PJRA"
FAST_ACK_NONCE_BYTES = 8


@dataclass(frozen=True)
class DecodedFrame:
    message: dict[str, object]
    payload_bytes: bytes


@dataclass(frozen=True)
class CandidateFrame:
    offset: int
    symbol_index: int
    bit_errors: int
    packet: DecodedFrame


def _build_fast_acknowledgement(nonce: str) -> bytes:
    nonce_bytes = bytes.fromhex(nonce)
    if len(nonce_bytes) != FAST_ACK_NONCE_BYTES:
        raise ValueError("ACK nonce must be exactly 8 bytes")
    body = FAST_ACK_MAGIC + nonce_bytes
    return body + zlib.crc32(body).to_bytes(4, "big")


def _parse_fast_acknowledgement(frame: bytes) -> str:
    if len(frame) != 16 or not frame.startswith(FAST_ACK_MAGIC):
        raise ValueError("not a fast ACK")
    if frame[-4:] != zlib.crc32(frame[:-4]).to_bytes(4, "big"):
        raise ValueError("fast ACK CRC mismatch")
    return frame[4:12].hex()


def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    usable = bits[: (len(bits) // 8) * 8].astype(np.uint8)
    if len(usable) == 0:
        return b""
    return np.packbits(usable).tobytes()


def modulate_packet(
    frame: bytes,
    *,
    preamble: bytes,
    sample_rate_hz: int,
    samples_per_symbol: int,
    tone_deviation_hz: float,
    amplitude: float = 0.55,
) -> np.ndarray:
    bits = bytes_to_bits(preamble + frame)
    samples = np.empty(len(bits) * samples_per_symbol, dtype=np.complex64)

    phase = 0.0
    out_index = 0
    for bit in bits:
        freq = tone_deviation_hz if bit else -tone_deviation_hz
        phase_step = 2.0 * math.pi * freq / sample_rate_hz
        symbol_phase = phase + phase_step * np.arange(samples_per_symbol)
        samples[out_index : out_index + samples_per_symbol] = amplitude * np.exp(
            1j * symbol_phase
        )
        phase = float((symbol_phase[-1] + phase_step) % (2.0 * math.pi))
        out_index += samples_per_symbol

    return _add_ramps(samples, max(4, samples_per_symbol // 2))


def find_packets(
    iq: np.ndarray,
    *,
    preamble: bytes,
    sample_rate_hz: int,
    samples_per_symbol: int,
    tone_deviation_hz: float,
    max_payload_bytes: int,
    max_preamble_bit_errors: int = 0,
    stop_after_first: bool = False,
) -> list[CandidateFrame]:
    iq = np.asarray(iq, dtype=np.complex64)
    if len(iq) < samples_per_symbol * 16:
        return []

    preamble_bits = bytes_to_bits(preamble)
    candidates: list[CandidateFrame] = []
    needed_header_bits = 16
    max_frame_bits = needed_header_bits + max_payload_bytes * 8

    for offset in range(samples_per_symbol):
        bits, confidence = demodulate_fsk_bits(
            iq[offset:],
            sample_rate_hz=sample_rate_hz,
            samples_per_symbol=samples_per_symbol,
            tone_deviation_hz=tone_deviation_hz,
        )
        if len(bits) < len(preamble_bits) + needed_header_bits:
            continue

        search_limit = len(bits) - len(preamble_bits) - needed_header_bits
        if search_limit < 0:
            continue

        # Correlating the bipolar bit streams finds every preamble position in
        # NumPy's compiled code.  The previous per-symbol Python loop delayed
        # ACK generation by seconds for a 250 ms radio capture.
        bipolar_bits = bits.astype(np.int16) * 2 - 1
        bipolar_preamble = preamble_bits.astype(np.int16) * 2 - 1
        correlation = np.correlate(bipolar_bits, bipolar_preamble, mode="valid")
        valid_correlation = correlation[: search_limit + 1]
        min_correlation = len(preamble_bits) - 2 * max_preamble_bit_errors

        for symbol_index in np.flatnonzero(valid_correlation >= min_correlation):
            bit_errors = (len(preamble_bits) - int(valid_correlation[symbol_index])) // 2

            length_start = symbol_index + len(preamble_bits)
            length_bits = bits[length_start : length_start + needed_header_bits]
            payload_len = int.from_bytes(bits_to_bytes(length_bits), "big")
            if payload_len <= 0 or payload_len > max_payload_bytes:
                continue

            frame_bits_len = needed_header_bits + payload_len * 8
            if frame_bits_len > max_frame_bits:
                continue

            payload_start = length_start + needed_header_bits
            payload_end = payload_start + payload_len * 8
            if payload_end > len(bits):
                continue

            payload_bits = bits[payload_start:payload_end]
            frame_payload = bits_to_bytes(payload_bits)
            try:
                packet = DecodedFrame(decode_v2_payload(frame_payload), frame_payload)
            except V2Error:
                continue

            avg_confidence = float(np.mean(confidence[length_start:payload_end]))
            if avg_confidence < 0:
                continue
            candidates.append(
                CandidateFrame(
                    offset=offset,
                    symbol_index=symbol_index,
                    bit_errors=bit_errors,
                    packet=packet,
                )
            )
            if stop_after_first:
                return candidates

    return _dedupe_candidates(candidates)


def modulate_fast_acknowledgement(
    nonce: str,
    *,
    preamble: bytes,
    sample_rate_hz: int,
    samples_per_symbol: int,
    tone_deviation_hz: float,
) -> np.ndarray:
    return modulate_packet(
        _build_fast_acknowledgement(nonce),
        preamble=preamble,
        sample_rate_hz=sample_rate_hz,
        samples_per_symbol=samples_per_symbol,
        tone_deviation_hz=tone_deviation_hz,
    )


def find_fast_acknowledgements(
    iq: np.ndarray,
    *,
    preamble: bytes,
    sample_rate_hz: int,
    samples_per_symbol: int,
    tone_deviation_hz: float,
    max_preamble_bit_errors: int = 0,
) -> set[str]:
    """Return nonces from compact fixed-length ACK waveforms in *iq*."""
    iq = np.asarray(iq, dtype=np.complex64)
    preamble_bits = bytes_to_bits(preamble)
    ack_bits = len(_build_fast_acknowledgement("00" * 8)) * 8
    found: set[str] = set()

    for offset in range(samples_per_symbol):
        bits, _ = demodulate_fsk_bits(
            iq[offset:],
            sample_rate_hz=sample_rate_hz,
            samples_per_symbol=samples_per_symbol,
            tone_deviation_hz=tone_deviation_hz,
        )
        search_limit = len(bits) - len(preamble_bits) - ack_bits
        if search_limit < 0:
            continue
        correlation = np.correlate(
            bits.astype(np.int16) * 2 - 1,
            preamble_bits.astype(np.int16) * 2 - 1,
            mode="valid",
        )[: search_limit + 1]
        min_correlation = len(preamble_bits) - 2 * max_preamble_bit_errors
        for symbol_index in np.flatnonzero(correlation >= min_correlation):
            ack_start = symbol_index + len(preamble_bits)
            ack_frame = bits_to_bytes(bits[ack_start : ack_start + ack_bits])
            try:
                found.add(_parse_fast_acknowledgement(ack_frame))
            except ValueError:
                continue
    return found


def demodulate_fsk_bits(
    iq: np.ndarray,
    *,
    sample_rate_hz: int,
    samples_per_symbol: int,
    tone_deviation_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    usable_samples = (len(iq) // samples_per_symbol) * samples_per_symbol
    if usable_samples <= 0:
        return np.array([], dtype=np.uint8), np.array([], dtype=np.float32)

    symbols = iq[:usable_samples].reshape((-1, samples_per_symbol))
    n = np.arange(samples_per_symbol)
    plus = np.exp(-1j * 2.0 * np.pi * tone_deviation_hz * n / sample_rate_hz).astype(
        np.complex64
    )
    minus = np.exp(1j * 2.0 * np.pi * tone_deviation_hz * n / sample_rate_hz).astype(
        np.complex64
    )

    plus_energy = np.abs(np.sum(symbols * plus, axis=1)) ** 2
    minus_energy = np.abs(np.sum(symbols * minus, axis=1)) ** 2
    bits = (plus_energy > minus_energy).astype(np.uint8)
    denom = plus_energy + minus_energy + 1e-12
    confidence = (np.abs(plus_energy - minus_energy) / denom).astype(np.float32)
    return bits, confidence


def _add_ramps(samples: np.ndarray, ramp_len: int) -> np.ndarray:
    if len(samples) < ramp_len * 2:
        return samples
    ramp = np.sin(np.linspace(0, math.pi / 2, ramp_len)) ** 2
    out = samples.copy()
    out[:ramp_len] *= ramp
    out[-ramp_len:] *= ramp[::-1]
    return out


def _dedupe_candidates(candidates: list[CandidateFrame]) -> list[CandidateFrame]:
    seen: set[tuple[str, str]] = set()
    unique: list[CandidateFrame] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item.packet.message.get("nonce", ""), item.bit_errors),
    ):
        key = (
            str(candidate.packet.message.get("receiver_node_id", "")),
            str(candidate.packet.message.get("nonce", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
