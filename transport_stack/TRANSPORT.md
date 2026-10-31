# Fast JSON transport

This adds `json_transport/` and `run.py` alongside the original `pluto_json_radio/` package. Original V2 crypto, keys, packet layout, transmitter, receiver and chat commands are preserved. Both folders use identical transport source and YAML; local identity files intentionally differ.

## Start

On computer/Pluto 1 (`oka_`):

```sh
source .venv/bin/activate
python -m pip install -r requirements-transport.txt
python run.py --mode host
```

On computer/Pluto 2 (`oka`):

```sh
source .venv/bin/activate
python -m pip install -r requirements-transport.txt
python run.py --mode server
```

Both roles can send and receive. The mode selects node IDs, API/UDP ports and Pluto serial/URI; it does not restrict message direction. Stop the old radio/chat process before starting a new process on the same Pluto. The adapter automatically locates the configured USB serial, including after a reconnect. Set `usb_serial: null` to use `uri` over the network.

The existing native libiio runtime is still required for hardware. Simulation does not import or initialize the SDR runtime. Keep one Uvicorn worker; multiple workers cannot share a Pluto.

## FastAPI

For everyday use, open `http://127.0.0.1:8000/` (Pluto 1) and `http://127.0.0.1:8001/` (Pluto 2). Each console has a full JSON editor and a live receive view. Reception continues while the page is open, including during sends. JSON syntax is validated before transmission. Delivery ACKs, retransmissions, API errors and radio connection state are shown directly. The live view uses long polling, consumes the same inbox as `/receive`, and shows the most recent 100 messages; use one receive consumer per node. Closing/reloading a page cancels its pending receive read. The view is not a durable message archive.

Open `http://127.0.0.1:8000/docs` on the host or `http://127.0.0.1:8001/docs` on the server.

```sh
# Arbitrary JSON object, including nested objects and arrays:
curl -s http://127.0.0.1:8000/send -H 'Content-Type: application/json' \
  -d '{"text":"hello","sensor":{"temperature":23.4},"values":[1,2,3]}'

# Read one received object on the other endpoint (204 if empty):
curl -s 'http://127.0.0.1:8001/receive?timeout_ms=5000'

# Reverse direction:
curl -s http://127.0.0.1:8001/send -H 'Content-Type: application/json' \
  -d '{"reply":"received"}'
curl -s 'http://127.0.0.1:8000/receive?timeout_ms=5000'

curl -s http://127.0.0.1:8000/health
```

`POST /send` accepts the JSON object directly. A reliable send returns `status: delivered` only after the destination reassembles, verifies/decrypts, parses and enqueues the object. `transport_latency_ms` measures that confirmed send, including codec/session work and retransmissions, excluding HTTP and time waiting behind another send. `queue_wait_ms` reports that wait separately. A 504 means delivery is uncertain: the message might have arrived while its ACK was lost. Avoid automatically reissuing an application operation with a new message ID if duplication would be harmful.

`GET /receive` removes one object from the bounded in-memory inbox and returns its `json`, message ID, source and receive timestamp. It is a consuming queue, so multiple consumers divide messages. `GET /health` reports features, counts, receive queue depth, incomplete messages and bounded-buffer drops. The API binds to loopback by default and has no application authentication.

## Independent configuration

Edit `configs/transport.yaml` on both endpoints and restart them. Use the same wire/feature settings, with the proper local identity on each computer.

| Feature | Configuration | Choices |
|---|---|---|
| Compression | `compression.algorithm` | `none`, `brotli`, `zstd`, `lz4` |
| Encryption | `encryption.algorithm` | `none`, `aes256` (AES-256-GCM) |
| Key exchange | `key_exchange.algorithm` | `disabled`, `x25519` |
| Session establishment | `session.enabled` | `false`, `true` |
| Public-key distribution | `key_distribution.enabled` | `false`, `true` |
| Signatures | `signing.algorithm` | `none`, `ed25519` |
| ACK/retries | `reliability.enabled` | `true`, `false` |

The default fast profile has compression, encryption, key exchange, sessions, distribution and signatures off. Reliable fragmentation remains on. These settings do not alter the original secure V2 commands.

Examples:

- Compression alone: select `lz4` or `zstd`, leave encryption and key exchange off. Small messages can bypass compression through `min_bytes`. `skip_if_larger` sends raw JSON when compression grows the payload.
- AES without key exchange: select `aes256`, keep key exchange `disabled`, and give both endpoints the same base64 32-byte key through `PLUTO_AES_KEY` or `encryption.key_file`. Generate a shared key with `python -c 'import os,base64; print(base64.b64encode(os.urandom(32)).decode())'`. Provision it locally on both computers; no key exchange or distribution is performed.
- Existing identities: run `python run.py --mode host --config configs/transport.secure.yaml` in `oka_` and the corresponding server command in `oka`. The secure preset uses the existing static X25519/HKDF key agreement, AES-256-GCM and Ed25519 signatures. Session establishment and key distribution remain off.
- X25519 without AES: select `x25519`, leave encryption `none`. The derived key can authenticate ACKs/session control while the JSON payload remains unencrypted. This is intentionally a valid independent configuration.
- Optional sessions: set `session.enabled: true`, then `POST /session` on the host, or let its first send establish the session. The server responds. HELLO/ACK exchanges a random session salt, authenticated using the preconfigured root key (or signing identity); HKDF derives a session key. With both key exchange and encryption disabled, an optional plain HELLO/ACK is also supported.
- Optional public-key distribution: set it on together with sessions and X25519. The HELLO exchange includes each node's X25519 public key and checks it against the existing pinned peer key. It does not automatically trust unknown keys or distribute private keys. This control can stay off with either X25519 or AES enabled.

`identity_file` references the existing node-specific YAML containing `security` and `link.default_receiver_address`. Both peers' public keys must already be configured, exactly as in the original project. Sessions use the existing static X25519 identities; they do not provide ephemeral forward secrecy. Restart both endpoints together to establish a fresh optional session. X25519 and Ed25519 are classical algorithms, not post-quantum cryptography.

Encryption/signature policies are enforced on receive, so a secure receiver rejects plaintext or unsigned downgrade packets. AES packets use independent random 96-bit nonces; message ID, sender, destination and envelope metadata are authenticated. ACK/control messages use HMAC when key material is configured, otherwise Ed25519 when enabled, otherwise CRC only. The plain fast profile provides neither confidentiality nor cryptographic peer authentication.

## Hardware-free simulation

In two terminals on one computer (the same folder is fine for the plain profile):

```sh
python run.py --mode host --simulate
python run.py --mode server --simulate
```

Use the same `/send` and `/receive` endpoints. This is UDP datagram simulation with the exact same codec, fragments, reliability and API used by radio. In YAML, `simulation.loss_rate`, `duplicate_rate`, `delay_ms`, `jitter_ms`, and `seed` control loss, duplication, delay and reordering. Set rates to fractions, e.g. `0.1` for 10%.

For secure simulation, use the two folders so their original node-specific identities differ, or explicitly point each mode's `identity_file` to its corresponding identity. Default simulation ports are 19001/19002 and API ports 8000/8001. Use `--port` to override an API port. The shipped datagram simulation is local to one computer; use radio for the two-computer deployment.

## Performance and reliability

The wire path is compact UTF-8 JSON via orjson → optional compression → optional AES-GCM/signature → fragments. Compression/encryption/signing occur once per message. There is no JSON/base64 wrapper per radio packet. The Pluto context and capture DMA remain open; modulation is vectorized. Capture and decoding have separate bounded queues to keep the device draining during processing. IIO RX/TX calls are serialized per device.

Selective ACKs carry a bitmap for blocks of 32 fragment indices. The sender batches up to `window_size`, asks for an ACK at the end of the batch, and retransmits missing fragments. The receiver ACKs duplicates again, including a lost final ACK, while enqueuing the JSON once within the dedup cache lifetime. No ACK mode returns `submitted`, which does not claim delivery.

Keep in-flight data below the radio's capture/CPU capacity. Larger packets/windows improve air efficiency but can increase losses or ACK recovery time. `rx_buffer_size`, `rx_kernel_buffers`, `samples_per_symbol`, interframe gaps, guard time and optional burst copies are configurable. At 2 MS/s and 8 samples/symbol, the raw BFSK bit rate is 250 kbit/s (31.25 kB/s), before preambles, packet headers, pauses, ACKs and retries. Sub-50 ms is a small-message target, not a guarantee during RF loss, retries, key establishment or queued traffic.

Reassembly has configured message, byte, count and inactivity-time limits. New fragments refresh the inactivity timer, so active large transfers are not cut off merely because they take longer than 30 seconds. Decompression checks the advertised decoded size and bounds its output. The inbox applies backpressure: a full inbox prevents final delivery confirmation until a consumer drains it. Data, duplicate suppression and sessions are held in memory; they do not survive process restart. Restart loses queued messages, and duplicate suppression is bounded by both cache size and TTL. This is a direct two-node transport, not routing, a durable broker or a general networking stack.

The transport uses a new `JT` wire format and its own receiver. It cannot exchange messages with a running legacy V2 receiver. Existing V2 commands remain available when both endpoints use that protocol.

## Reproduce checks

```sh
python -m pytest -q
python tools/benchmark_transport.py --backend memory --messages 100
python tools/benchmark_transport.py --backend udp --messages 100
# Stop existing radio processes first:
python tools/benchmark_transport.py --backend radio --messages 50
```

For hardware benchmarks the tool opens both Plutos locally. The intended deployment uses one `run.py` process per endpoint/computer. With secure identities, pass `--config` from `oka_` and `--peer-config` from `oka`. Latency benchmarks await complete reassembly ACKs, verify every received JSON object and alternate directions. Memory/UDP results are not RF results. See the delivered performance report for measurements on this setup.

## Implementation references

FastAPI lifespan management follows the [official FastAPI guide](https://fastapi.tiangolo.com/advanced/events/). Bounded LZ4 decoding uses the [python-lz4 frame API](https://python-lz4.readthedocs.io/en/stable/lz4.frame.html).
