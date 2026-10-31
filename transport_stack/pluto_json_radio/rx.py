from __future__ import annotations

import argparse
import json
import sys
import time

import numpy as np

from .config import load_config
from .phy import find_packets, modulate_fast_acknowledgement
from .pluto import make_pluto, receive_samples, transmit_once
from .v2 import V2Error, decode_v2_payload, v2_application


def _ack_iq(message: dict, config, preamble: bytes):
    # The ACK only needs the request nonce.  A fixed 16-byte waveform with a
    # CRC is both faster and substantially easier to recover than JSON.
    iq = modulate_fast_acknowledgement(
        message["nonce"],
        preamble=preamble,
        sample_rate_hz=config.device.sample_rate_hz,
        samples_per_symbol=config.phy.samples_per_symbol,
        tone_deviation_hz=config.phy.tone_deviation_hz,
    )
    if config.phy.ack_burst_copies == 1:
        return iq
    gap = np.zeros(int(config.device.sample_rate_hz * 0.005), dtype=np.complex64)
    return np.concatenate(
        [part for index in range(config.phy.ack_burst_copies) for part in (iq, gap)
         if index < config.phy.ack_burst_copies - 1 or part is iq]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Receive JSON packets with ADALM-Pluto.")
    parser.add_argument("--config", default="configs/pluto.example.yaml")
    parser.add_argument(
        "--accept-all",
        action="store_true",
        help="Print packets even when receiver_address does not match local_address.",
    )
    parser.add_argument("--once", action="store_true", help="Exit after the first accepted packet.")
    parser.add_argument(
        "--max-preamble-bit-errors",
        type=int,
        default=None,
        help="Override configured preamble bit-error tolerance when searching.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    max_preamble_bit_errors = (
        config.phy.max_preamble_bit_errors
        if args.max_preamble_bit_errors is None
        else args.max_preamble_bit_errors
    )
    preamble = bytes.fromhex(config.phy.preamble_hex)
    sdr = make_pluto(config.device)

    seen_nonces: set[str] = set()
    print(
        f"listening as {config.link.local_address!r} on {config.device.center_frequency_hz} Hz",
        flush=True,
    )
    overlap = np.array([], dtype=np.complex64)
    overlap_samples = int(config.device.sample_rate_hz * config.phy.rx_overlap_seconds)

    while True:
        # Collect a complete capture before demodulating.  This proven path
        # keeps a whole burst together and avoids skipping packets between
        # throttled scan windows.
        fresh_iq = receive_samples(sdr, config.phy.capture_seconds)
        iq = np.concatenate((overlap, fresh_iq)) if overlap.size else fresh_iq
        candidates = find_packets(
            iq,
            preamble=preamble,
            sample_rate_hz=config.device.sample_rate_hz,
            samples_per_symbol=config.phy.samples_per_symbol,
            tone_deviation_hz=config.phy.tone_deviation_hz,
            max_payload_bytes=config.phy.max_payload_bytes,
            max_preamble_bit_errors=max_preamble_bit_errors,
            stop_after_first=True,
        )

        # Retain enough history to detect a packet whose preamble arrived at
        # the end of this capture and whose payload continues in the next one.
        overlap = iq[-overlap_samples:].copy() if overlap_samples else overlap

        for candidate in candidates:
            if candidate.packet.message.get("_wire_format") == "v2":
                address_by_code = {
                    int(code): address for address, code in config.link.value_codes["receiver_id"].items()
                }
                decoded_v2 = candidate.packet.message
                if decoded_v2.get("_encrypted") or decoded_v2.get("_signed"):
                    local_code = config.link.value_codes["receiver_id"].get(
                        config.link.local_address
                    )
                    if decoded_v2.get("receiver_node_id") != local_code:
                        continue
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
                        continue
                if (config.security.peer_signing_public_keys
                        and decoded_v2.get("signature_method") != "ed25519"):
                    # Configuring peer signing identities makes signatures
                    # mandatory; do not silently accept an unsigned downgrade.
                    continue
                message = v2_application(decoded_v2, address_by_code)
            else:
                continue
            if message.get("packet_type") == "ack":
                continue
            nonce = str(message.get("nonce", ""))
            addressed_to_me = message.get("receiver_address") == config.link.local_address
            # Only the addressed receiver sends an ACK.  Duplicates are ACKed
            # again, because the prior ACK may have been lost.
            if addressed_to_me and isinstance(message.get("sender_address"), str):
                # Respond in the waveform that carried this request.  This is
                # what lets an updated 32-sample receiver serve a 16-sample
                # legacy sender during a staged rollout.
                ack_iq = _ack_iq(message, config, preamble)
                for ack_index in range(config.phy.ack_followup_count):
                    transmit_once(sdr, ack_iq)
                    if ack_index + 1 < config.phy.ack_followup_count:
                        time.sleep(config.phy.ack_followup_gap_seconds)
                print(f"ACK sent for nonce={nonce}", flush=True)

            if not addressed_to_me and not args.accept_all:
                continue

            if nonce in seen_nonces:
                continue
            seen_nonces.add(nonce)

            print(json.dumps(message, indent=2, ensure_ascii=False, sort_keys=True), flush=True)
            if args.once:
                return 0


if __name__ == "__main__":
    sys.exit(main())
