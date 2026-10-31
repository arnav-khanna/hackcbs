from __future__ import annotations

import argparse
import json
import secrets
import sys
import time

import numpy as np

from .config import MAX_RESENDS, load_config
from .v2 import V2Error, encode_v2_frame
from .phy import find_fast_acknowledgements, modulate_packet
from .pluto import make_pluto, prime_receiver, receive_until, transmit_once


def _packet_iq(frame: bytes, config, preamble: bytes):
    return frame, modulate_packet(
        frame,
        preamble=preamble,
        sample_rate_hz=config.device.sample_rate_hz,
        samples_per_symbol=config.phy.samples_per_symbol,
        tone_deviation_hz=config.phy.tone_deviation_hz,
    )


def _initial_burst(iq: np.ndarray, copies: int, sample_rate_hz: int) -> np.ndarray:
    if copies == 1:
        return iq
    gap = np.zeros(int(sample_rate_hz * 0.005), dtype=np.complex64)
    return np.concatenate([part for index in range(copies) for part in (iq, gap) if index < copies - 1 or part is iq])


def _has_matching_ack(iq, *, nonce: str, config, preamble: bytes) -> bool:
    # ACKs use a short, fixed-size CRC-protected frame.  Check it first: it
    # avoids JSON framing/decoding on the latency-critical return path.
    if nonce in find_fast_acknowledgements(
        iq,
        preamble=preamble,
        sample_rate_hz=config.device.sample_rate_hz,
        samples_per_symbol=config.phy.samples_per_symbol,
        tone_deviation_hz=config.phy.tone_deviation_hz,
        max_preamble_bit_errors=config.phy.max_preamble_bit_errors,
    ):
        return True

    return False


def _parse_message(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("--message and --metadata-json must be JSON objects") from exc
    if not isinstance(value, dict):
        raise ValueError("--message and --metadata-json must be JSON objects")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Transmit a JSON packet with ADALM-Pluto.")
    parser.add_argument("--config", default="configs/pluto.example.yaml")
    parser.add_argument(
        "--message",
        help="Full JSON object to send. crc/nonce/default fields are added or replaced.",
    )
    parser.add_argument("--to", dest="receiver_address", help="Receiver address override.")
    parser.add_argument("--text", default=None, help="Convenience text field.")
    parser.add_argument(
        "--compression",
        choices=("auto", "zstd", "brotli", "none"),
        default=None,
        help="Compression override; auto compares raw, Zstandard, and Brotli above 300 payload bytes.",
    )
    parser.add_argument(
        "--metadata-json",
        default=None,
        help="Extra JSON object merged into the transmitted message.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build packet but do not use Pluto.")
    parser.add_argument(
        "--max-resends",
        type=int,
        default=None,
        help="Maximum retransmissions after the initial send (0–4; overrides config).",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.max_resends is not None and args.max_resends < 0:
        parser.error("--max-resends cannot be negative")
    max_resends = min(
        config.phy.max_resends if args.max_resends is None else args.max_resends,
        MAX_RESENDS,
    )

    if args.message:
        message = _parse_message(args.message)
    else:
        message = {}

    if args.metadata_json:
        extra = _parse_message(args.metadata_json)
        message.update(extra)

    if args.receiver_address:
        message["receiver_address"] = args.receiver_address
    if args.text is not None:
        message["text"] = args.text
    if args.compression is not None:
        message["compression_method"] = args.compression

    normalized = dict(message)
    normalized.setdefault("receiver_address", config.link.default_receiver_address)
    normalized.setdefault("text", "")
    normalized.setdefault("nonce", secrets.token_hex(8))
    normalized.setdefault("timestamp_unix", time.time())
    normalized["packet_type"] = "data"
    normalized["sender_address"] = config.link.local_address
    preamble = bytes.fromhex(config.phy.preamble_hex)
    transport_fields = {"receiver_address", "sender_address", "version", "packet_type", "nonce", "ttl", "max_hops", "flags", "compression_method"}
    payload = {key: value for key, value in normalized.items() if key not in transport_fields}
    receiver_code = config.link.value_codes["receiver_id"][normalized["receiver_address"]]
    source_code = config.link.value_codes["source_id"][normalized["sender_address"]]
    try:
        frame, transmitted_packet = encode_v2_frame(
            receiver_code=receiver_code, source_code=source_code, nonce=normalized["nonce"],
            payload=payload, packet_type="data", flags=int(normalized.get("flags", 0)),
            ttl=int(normalized.get("ttl", 10)), max_hops=int(normalized.get("max_hops", 15)),
            compression_method=normalized.get("compression_method", "auto"),
            encryption_method=config.security.encryption_method,
            private_key=config.security.private_key,
            recipient_public_key=config.security.peer_public_keys.get(
                normalized["receiver_address"]
            ),
            signing_private_key=config.security.signing_private_key,
            max_payload_bytes=config.phy.max_payload_bytes,
        )
    except (KeyError, ValueError, V2Error) as exc:
        parser.error(str(exc))
    frame, iq = _packet_iq(frame, config, preamble)
    iq = _initial_burst(iq, config.phy.initial_burst_copies, config.device.sample_rate_hz)

    print(json.dumps(transmitted_packet, indent=2, ensure_ascii=False, sort_keys=True))
    print(
        f"payload_bytes={len(frame) - 2} total_samples={len(iq)} "
        f"initial_burst_copies={config.phy.initial_burst_copies}"
    )

    if args.dry_run:
        return 0

    sdr = make_pluto(config.device)
    # Arm RX before the request.  Pluto starts capture when its RX buffer is
    # created, so doing this after TX can lose a very fast return ACK.
    prime_receiver(sdr)
    request_started = time.monotonic()
    for attempt in range(max_resends + 1):
        transmit_once(sdr, iq)
        if attempt == 0:
            print("sent initial packet; waiting for ACK")
        else:
            print(f"ACK timeout; resend {attempt}/{max_resends}; waiting for ACK")
        if receive_until(
            sdr,
            config.phy.ack_wait_seconds,
            lambda ack_iq: _has_matching_ack(
                ack_iq,
                nonce=normalized["nonce"],
                config=config,
                preamble=preamble,
            ),
        ):
            elapsed = time.monotonic() - request_started
            print(f"ACK received after {attempt} resend(s)")
            print(f"request_completed_in_seconds={elapsed:.3f}")
            return 0

    elapsed = time.monotonic() - request_started
    print(f"no ACK after initial send and {max_resends} resend(s)", file=sys.stderr)
    print(f"request_completed_in_seconds={elapsed:.3f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
