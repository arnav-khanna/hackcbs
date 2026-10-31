"""Protocol V2 binary framing and payload serialization."""
from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from typing import Any

import brotli
import zstandard as zstd
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

V2_MAGIC = b"P2"
V2_VERSION = 2
V2_COMPRESSION_THRESHOLD = 150
V2_COMPRESSION_NONE = 0
V2_COMPRESSION_ZSTD = 1
V2_COMPRESSION_BROTLI = 2
V2_ENCRYPTION_NONE = 0
V2_ENCRYPTION_X25519_AESGCM = 1
V2_ENCRYPTION_NAMES = {
    V2_ENCRYPTION_NONE: None,
    V2_ENCRYPTION_X25519_AESGCM: "x25519-aesgcm",
}
V2_BINARY_OVERHEAD = 122  # Secure prefix, inner metadata, GCM tag, signature, CRC.
V2_TYPE_CODES = {"data": 0, "ack": 1, "hello": 2, "beacon": 3, "route_request": 4, "route_reply": 5}
V2_TYPE_NAMES = {value: key for key, value in V2_TYPE_CODES.items()}
V2_FIELD_CODES = {"timestamp_unix": 1, "text": 2, "experiment": 3, "temperature": 4, "gps": 5, "battery": 6, "route": 7, "seq": 8}
V2_FIELD_NAMES = {value: key for key, value in V2_FIELD_CODES.items()}


class V2Error(ValueError):
    pass


def _varint(value: int) -> bytes:
    if value < 0:
        raise V2Error("varint cannot be negative")
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise V2Error("invalid varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _encode_value(value: Any) -> bytes:
    if value is None:
        return b"\x00"
    if value is False:
        return b"\x01"
    if value is True:
        return b"\x02"
    if isinstance(value, int):
        return b"\x03" + _varint((value << 1) ^ (value >> 63))
    if isinstance(value, float):
        return b"\x04" + struct.pack("!d", value)
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return b"\x05" + _varint(len(encoded)) + encoded
    if isinstance(value, bytes):
        return b"\x06" + _varint(len(value)) + value
    if isinstance(value, list):
        return b"\x07" + _varint(len(value)) + b"".join(_encode_value(item) for item in value)
    if isinstance(value, dict):
        return b"\x08" + _encode_map(value)
    raise V2Error(f"unsupported payload value: {type(value).__name__}")


def _encode_map(message: dict[str, Any]) -> bytes:
    encoded = bytearray(_varint(len(message)))
    for key, value in message.items():
        code = V2_FIELD_CODES.get(key, 0)
        encoded.extend(_varint(code))
        if code == 0:
            key_bytes = str(key).encode("utf-8")
            encoded.extend(_varint(len(key_bytes)))
            encoded.extend(key_bytes)
        encoded.extend(_encode_value(value))
    return bytes(encoded)


def _decode_value(data: bytes, offset: int) -> tuple[Any, int]:
    if offset >= len(data):
        raise V2Error("missing value tag")
    tag = data[offset]
    offset += 1
    if tag == 0:
        return None, offset
    if tag == 1:
        return False, offset
    if tag == 2:
        return True, offset
    if tag == 3:
        value, offset = _read_varint(data, offset)
        return (value >> 1) ^ -(value & 1), offset
    if tag == 4:
        if offset + 8 > len(data):
            raise V2Error("truncated float")
        return struct.unpack("!d", data[offset:offset + 8])[0], offset + 8
    if tag in (5, 6):
        size, offset = _read_varint(data, offset)
        if offset + size > len(data):
            raise V2Error("truncated byte string")
        value = data[offset:offset + size]
        return (value.decode("utf-8") if tag == 5 else value), offset + size
    if tag == 7:
        count, offset = _read_varint(data, offset)
        values = []
        for _ in range(count):
            value, offset = _decode_value(data, offset)
            values.append(value)
        return values, offset
    if tag == 8:
        return _decode_map(data, offset)
    raise V2Error("unknown value tag")


def _decode_map(data: bytes, offset: int = 0) -> tuple[dict[str, Any], int]:
    count, offset = _read_varint(data, offset)
    message: dict[str, Any] = {}
    for _ in range(count):
        code, offset = _read_varint(data, offset)
        if code == 0:
            size, offset = _read_varint(data, offset)
            if offset + size > len(data):
                raise V2Error("truncated map key")
            key = data[offset:offset + size].decode("utf-8")
            offset += size
        else:
            key = V2_FIELD_NAMES.get(code)
            if key is None:
                raise V2Error("unknown dictionary key code")
        value, offset = _decode_value(data, offset)
        message[key] = value
    return message, offset


def _compress(
    payload: bytes, requested: str | None, *, packet_overhead: int = V2_BINARY_OVERHEAD,
) -> tuple[int, bytes]:
    if requested not in ("auto", "zstd", "brotli", None, "none", "null", "off"):
        raise V2Error("compression_method must be auto, zstd, brotli, or none")
    if requested in (None, "none", "null", "off"):
        return V2_COMPRESSION_NONE, payload
    # Apply the policy to the complete V2 packet shown as payload_bytes by the
    # transmitter, rather than only the inner metadata payload.
    if requested == "auto" and len(payload) + packet_overhead <= V2_COMPRESSION_THRESHOLD:
        return V2_COMPRESSION_NONE, payload
    if requested == "zstd":
        return V2_COMPRESSION_ZSTD, zstd.ZstdCompressor(level=3).compress(payload)
    if requested == "brotli":
        return V2_COMPRESSION_BROTLI, brotli.compress(
            payload, quality=4, mode=brotli.MODE_TEXT
        )
    # "auto" is the only mode allowed to choose raw after comparison.
    candidates = [
        (V2_COMPRESSION_NONE, payload),
        (
            V2_COMPRESSION_BROTLI,
            brotli.compress(payload, quality=4, mode=brotli.MODE_TEXT),
        ),
    ]
    return min(candidates, key=lambda item: len(item[1]))


def _decompress(method: int, payload: bytes) -> bytes:
    if method == V2_COMPRESSION_NONE:
        return payload
    if method == V2_COMPRESSION_ZSTD:
        return zstd.ZstdDecompressor().decompress(payload)
    if method == V2_COMPRESSION_BROTLI:
        return brotli.decompress(payload)
    raise V2Error("unsupported compression enum")


def _key_bytes(value: str, label: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise V2Error(f"{label} must be base64") from exc
    if len(key) != 32:
        raise V2Error(f"{label} must decode to exactly 32 bytes")
    return key


def _link_key(private_key: str, public_key: str) -> bytes:
    try:
        shared_secret = X25519PrivateKey.from_private_bytes(
            _key_bytes(private_key, "private_key")
        ).exchange(X25519PublicKey.from_public_bytes(_key_bytes(public_key, "public_key")))
    except ValueError as exc:
        raise V2Error(f"invalid X25519 key: {exc}") from exc
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None,
        info=b"PJRA-V2-X25519-AESGCM",
    ).derive(shared_secret)


def _aead_nonce(source: int, receiver: int, packet_nonce: bytes) -> bytes:
    # A packet nonce is random and unique in normal operation.  Binding both
    # node IDs makes the derived 96-bit AES-GCM nonce link-specific as well.
    return hashlib.sha256(
        b"PJRA-V2-AEAD-NONCE" + source.to_bytes(8, "big")
        + receiver.to_bytes(8, "big") + packet_nonce
    ).digest()[:12]


def _sign(data: bytes, private_key: str) -> bytes:
    try:
        return Ed25519PrivateKey.from_private_bytes(
            _key_bytes(private_key, "signing_private_key")
        ).sign(data)
    except ValueError as exc:
        raise V2Error(f"invalid Ed25519 private key: {exc}") from exc


def _verify_signature(data: bytes, signature: bytes, public_key: str) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(
            _key_bytes(public_key, "signing_public_key")
        ).verify(signature, data)
    except Exception as exc:
        raise V2Error("V2 sender signature verification failed") from exc


def encode_v2_frame(
    *, receiver_code: int, source_code: int, nonce: str, payload: dict[str, Any],
    packet_type: str = "data", flags: int = 0, ttl: int = 10,
    max_hops: int = 15, compression_method: str | None = "auto",
    encryption_method: str | None = None, private_key: str | None = None,
    recipient_public_key: str | None = None, signing_private_key: str | None = None,
    max_payload_bytes: int = 512,
) -> tuple[bytes, dict[str, Any]]:
    try:
        nonce_bytes = bytes.fromhex(nonce)
    except ValueError as exc:
        raise V2Error("nonce must be hexadecimal") from exc
    if len(nonce_bytes) != 8 or packet_type not in V2_TYPE_CODES:
        raise V2Error("invalid nonce or packet type")
    if encryption_method != "x25519-aesgcm":
        raise V2Error("secure V2 frames require encryption_method=x25519-aesgcm")
    if not private_key or not recipient_public_key or not signing_private_key:
        raise V2Error("secure V2 requires encryption, recipient, and signing private keys")
    raw_payload = _encode_map(payload)
    compression, wire_payload = _compress(
        raw_payload,
        compression_method,
        packet_overhead=V2_BINARY_OVERHEAD,
    )
    fragment_id = int.from_bytes(nonce_bytes[:4], "big")
    # Only the destination and ACK nonce remain visible.  All sender and
    # protocol metadata, including compression and signature information, is
    # inside the authenticated ciphertext.
    header = b"".join((
        V2_MAGIC, bytes((V2_VERSION,)), int(receiver_code).to_bytes(8, "big"), nonce_bytes,
    ))
    unsigned_inner = b"".join((
        int(source_code).to_bytes(8, "big"),
        bytes((V2_TYPE_CODES[packet_type], flags & 0xFF, ttl & 0xFF, max_hops & 0xFF, compression)),
        fragment_id.to_bytes(4, "big"), _varint(1), _varint(0), wire_payload,
    ))
    # The signature is itself encrypted.  It signs the clear routing prefix
    # plus all unsigned inner content, while AES-GCM protects its transport.
    signature = _sign(header + unsigned_inner, signing_private_key)
    ciphertext = AESGCM(_link_key(private_key, recipient_public_key)).encrypt(
        _aead_nonce(source_code, receiver_code, nonce_bytes),
        unsigned_inner + signature, header,
    )
    body = header + ciphertext
    binary = body + zlib.crc32(body).to_bytes(4, "big")
    if len(binary) > max_payload_bytes:
        raise V2Error(f"V2 payload is {len(binary)} bytes; max_payload_bytes is {max_payload_bytes}")
    frame = len(binary).to_bytes(2, "big") + binary
    compression_name = {0: None, 1: "zstd", 2: "brotli"}[compression]
    display = {"protocol_version": 2, "packet_type": packet_type, "flags": flags, "ttl": ttl, "max_hops": max_hops, "source_node_id": source_code, "receiver_node_id": receiver_code, "nonce": nonce, "fragment_id": fragment_id, "total_fragments": 1, "fragment_number": 0, "encryption_method": "x25519-aesgcm", "signature_method": "ed25519", "sender_signature": base64.b64encode(signature).decode("ascii"), "compression_method": compression_name, "payload_binary_bytes": len(raw_payload), "payload": payload}
    return frame, display


def decode_v2_payload(
    binary: bytes, *, private_key: str | None = None,
    peer_public_keys_by_code: dict[int, str] | None = None,
    peer_signing_public_keys_by_code: dict[int, str] | None = None,
) -> dict[str, Any]:
    if len(binary) < V2_BINARY_OVERHEAD or not binary.startswith(V2_MAGIC):
        raise V2Error("not a V2 frame")
    body, received_crc = binary[:-4], binary[-4:]
    if zlib.crc32(body).to_bytes(4, "big") != received_crc:
        raise V2Error("V2 CRC mismatch")
    version = body[2]
    if version != V2_VERSION:
        raise V2Error("unsupported V2 header")
    receiver = int.from_bytes(body[3:11], "big")
    nonce_bytes = body[11:19]
    nonce = nonce_bytes.hex()
    header = body[:19]
    ciphertext = body[19:]
    if (private_key is None or peer_public_keys_by_code is None
            or peer_signing_public_keys_by_code is None):
        return {
            "_wire_format": "v2", "_encrypted": True, "_signed": True,
            "receiver_node_id": receiver, "nonce": nonce,
        }
    inner = None
    source = None
    signature = b""
    for candidate_source, sender_public_key in peer_public_keys_by_code.items():
        try:
            candidate = AESGCM(_link_key(private_key, sender_public_key)).decrypt(
                _aead_nonce(candidate_source, receiver, nonce_bytes), ciphertext, header
            )
        except Exception:
            continue
        if len(candidate) < 83:
            continue
        candidate_source_in_payload = int.from_bytes(candidate[:8], "big")
        if candidate_source_in_payload != candidate_source:
            continue
        candidate_signature = candidate[-64:]
        signing_key = peer_signing_public_keys_by_code.get(candidate_source)
        if signing_key is None:
            raise V2Error(f"no signing public key configured for source node {candidate_source}")
        _verify_signature(header + candidate[:-64], candidate_signature, signing_key)
        source, inner, signature = candidate_source, candidate[:-64], candidate_signature
        break
    if inner is None or source is None:
        raise V2Error("V2 authenticated decryption failed")
    if len(inner) < 19:
        raise V2Error("truncated V2 inner header")
    packet_type, flags, ttl, max_hops, compression = inner[8:13]
    if packet_type not in V2_TYPE_NAMES:
        raise V2Error("unsupported V2 packet type")
    fragment_id = int.from_bytes(inner[13:17], "big")
    total, offset = _read_varint(inner, 17)
    number, offset = _read_varint(inner, offset)
    try:
        payload_bytes = _decompress(compression, inner[offset:])
        payload, end = _decode_map(payload_bytes)
    except (ValueError, zstd.ZstdError, brotli.error) as exc:
        raise V2Error(f"invalid compressed payload: {exc}") from exc
    if end != len(payload_bytes) or total < 1 or number >= total:
        raise V2Error("invalid V2 payload or fragments")
    return {"_wire_format": "v2", "receiver_node_id": receiver, "source_node_id": source, "nonce": nonce, "packet_type": V2_TYPE_NAMES[packet_type], "flags": flags, "ttl": ttl, "max_hops": max_hops, "fragment_id": fragment_id, "total_fragments": total, "fragment_number": number, "encryption_method": "x25519-aesgcm", "signature_method": "ed25519", "sender_signature": base64.b64encode(signature).decode("ascii"), "compression_method": {0: None, 1: "zstd", 2: "brotli"}[compression], "payload": payload}


def v2_application(packet: dict[str, Any], address_by_code: dict[int, str]) -> dict[str, Any]:
    return {**packet["payload"], "receiver_address": address_by_code.get(packet["receiver_node_id"], str(packet["receiver_node_id"])), "sender_address": address_by_code.get(packet["source_node_id"], str(packet["source_node_id"])), "version": V2_VERSION, "packet_type": packet["packet_type"], "flags": packet["flags"], "fragment_id": f"{packet['fragment_id']:08x}", "total_fragments": packet["total_fragments"], "fragment_number": packet["fragment_number"], "ttl": packet["ttl"], "max_hops": packet["max_hops"], "nonce": packet["nonce"], "encryption_method": packet["encryption_method"], "signature_method": packet["signature_method"], "sender_signature": packet["sender_signature"], "compression_method": packet["compression_method"]}
