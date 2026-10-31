# V2 Confidential Data Packet Reference

## Status at a glance

| Property | Current behavior |
| --- | --- |
| Wire representation | Compact binary, never JSON |
| Payload secrecy | AES-256-GCM encryption |
| Sender authentication | Ed25519 signature, encrypted with the packet |
| Peer key agreement | X25519 + HKDF-SHA256 |
| Payload compression | Raw, Zstandard, Brotli, or adaptive auto mode |
| Radio framing | 2-FSK, preamble, 16-bit PHY length, CRC-32 |
| ACK | Dedicated short nonce ACK for low latency |
| Waveform | 16 samples/symbol |

> Important: the JSON printed by `pluto_json_radio.tx` is a local diagnostic
> view. It is not a JSON serialization of the radio frame and it is not the
> set of values visible to another radio.

---

## Exact on-air layout

```text
┌────────────────────── PHY / clear prefix ──────────────────────┐
│ PHY preamble │ PHY length │ P2 │ V2 │ receiver ID │ nonce       │
│              │ 2 bytes   │ 2B │ 1B │ 8 bytes     │ 8 bytes      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────── AES-256-GCM ciphertext ───────────────────┐
│ source ID │ type │ flags │ TTL │ hops │ compression │ fragment │
│ fragment counters │ typed binary payload │ Ed25519 signature   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                     AES-GCM tag (16 bytes) | CRC-32 (4 bytes)
```

The PHY length surrounds the packet at the modem layer. It gives the receiver
the packet boundary, so no redundant application `payload_length` field is
sent.

## What an unauthenticated observer can and cannot read

| Field | On air | Why |
| --- | --- | --- |
| `P2`, V2 version | Clear | Identifies the packet format. |
| Receiver node ID | Clear | Lets a radio decide whether it is the intended recipient. |
| Nonce | Clear | Matches the very short ACK and derives the AES-GCM nonce. |
| CRC-32 | Clear | Quickly rejects corrupted RF frames; it provides no secrecy. |
| Sender/source ID | **Encrypted** | The receiver discovers it only after successful decryption. |
| Packet type | **Encrypted** | Data, ACK-like protocol information, and future route types remain private. |
| Flags, TTL, max hops | **Encrypted** | Routing/protocol metadata is private. |
| Fragment ID, total, number | **Encrypted** | Fragmentation metadata is private. |
| Compression method | **Encrypted** | Raw/Zstd/Brotli selection is private. |
| Application payload | **Encrypted** | Includes `text`, `experiment`, timestamps, and custom metadata. |
| Ed25519 sender signature | **Encrypted** | The signature exists, but is not observable on air. |

For the current range-test example, an unauthenticated parser can produce only:

```json
{
  "_wire_format": "v2",
  "_encrypted": true,
  "_signed": true,
  "receiver_node_id": 1,
  "nonce": "3d10a3ed89a48100"
}
```

It cannot recover `source_node_id`, `compression_method`, `fragment_id`,
`packet_type`, `sender_signature`, or any payload field.

---

## Private encrypted content

After AES-GCM authentication/decryption, the receiver obtains this compact
binary structure:

```text
source ID (8 bytes)
packet type (1 byte enum)
flags (1 byte)
TTL (1 byte)
max hops (1 byte)
compression method (1 byte enum)
fragment ID (4 bytes)
total fragments (varint)
fragment number (varint)
compressed or raw typed payload
Ed25519 sender signature (64 bytes)
```

The signature covers the clear routing prefix plus every private field above,
except the signature bytes themselves. It is then encrypted together with this
private content. AES-GCM authenticates the ciphertext; Ed25519 additionally
proves the configured sender signed the recovered protocol content.

### Typed payload encoding

Application metadata is not JSON on air. Known field names use numeric codes:

| Metadata field | Binary code |
| --- | --- |
| `timestamp_unix` | `1` |
| `text` | `2` |
| `experiment` | `3` |
| `temperature` | `4` |
| `gps` | `5` |
| `battery` | `6` |
| `route` | `7` |
| `seq` | `8` |

Values have type tags: integers use varints, strings use a byte length plus
UTF-8 bytes, floats use IEEE-754, and nested maps/lists are recursively typed.
Unknown field names remain possible, but are sent as UTF-8 only for that field.

---

## Transmission flow

```text
Application metadata
        ↓
Typed / dictionary-coded binary payload
        ↓
Adaptive or chosen compression
        ↓
Private V2 metadata is assembled
        ↓
Ed25519 signs clear prefix + private metadata
        ↓
Private metadata + signature encrypted with AES-256-GCM
        ↓
CRC-32, PHY length, 2-FSK modulation
        ↓
ADALM-Pluto transmission
```

`auto` leaves frames at or below 150 total packet bytes raw. Above that it
tries Brotli and uses it only if it is smaller. `zstd`, `brotli`, and `none`
remain explicit benchmark overrides.

## Reception flow

```text
ADALM-Pluto samples
        ↓
2-FSK frame detection, PHY-length validation, CRC-32
        ↓
Check visible receiver ID
        ↓
Try configured peer X25519 public keys
        ↓
AES-GCM decrypt/authenticate
        ↓
Confirm recovered source ID matches the successful peer key
        ↓
Verify recovered encrypted Ed25519 sender signature
        ↓
Decompress and decode typed payload
        ↓
Deliver once and immediately send fast ACK
```

The source ID is hidden, so the receiver tries its configured peer keys. In
the current two-Pluto setup, that is one peer key and adds negligible work.

---

## Why the TX terminal prints the large JSON object

The sender prints a friendly inspection record after creating the binary frame:

```json
{
  "source_node_id": 2,
  "packet_type": "data",
  "compression_method": null,
  "sender_signature": "...",
  "payload": { "experiment": "range-test", "text": "test1" }
}
```

This is useful for debugging local construction. It does **not** mean these
fields are sent as clear JSON or clear binary. They are part of the encrypted
ciphertext described above.

## Fast ACK exception

The return ACK is intentionally a separate small plaintext waveform:

```text
PJRA | request nonce (8 bytes) | CRC-32
```

It carries no sender ID, payload, compression information, routing fields, or
sender signature. Keeping it short and simple is what allows rapid ACK
detection and retransmission decisions.

## Waveform profile

Both named deployments use the same 16-sample-per-symbol 2-FSK waveform as the
known-good Pluto deployment.  The encrypted V2 bytes are independent of this
sample timing, but both radios must use the same PHY profile.  The active
configuration therefore sets `compatibility_samples_per_symbol: []` and sends
one 16-sample waveform per packet.

## Operational requirements

- Both folders must run this same secure V2 layout.
- Each YAML keeps its own X25519 and Ed25519 private keys secret.
- Each YAML contains the peer's X25519 and Ed25519 public keys.
- Restart receivers after changing keys or this packet format.
- The encrypted 64-byte signature and 16-byte GCM tag are intentional fixed
  airtime overhead for confidentiality and sender authentication.

## Optional JT transport (separate wire format)

The new `json_transport` package uses `JT`, version 1, alongside this unchanged V2 implementation. Its fixed 18-byte header is magic (2), version (1), kind/ACK-request flags (1), source (1), destination (1), random message ID (8), fragment index (2), and count (2), all integers in network byte order. A 4-byte CRC32 covers header and payload. The PHY adds a 2-byte length and the existing BFSK preamble. DATA carries fragments of a once-encoded JSON envelope: compression (1), encryption (1), signature flag (1), original JSON byte length (4), then optional AES-GCM nonce/ciphertext/tag and optional 64-byte Ed25519 signature. ACK carries a 32-bit bitmap and one delivery-complete byte, plus optional authentication. HELLO/HELLO_ACK exchange a 16-byte session salt, optional pinned X25519 public key, and optional authentication. See TRANSPORT.md and json_transport/wire.py for details. JT receivers and V2 receivers are selected separately; there is no automatic protocol downgrade.
