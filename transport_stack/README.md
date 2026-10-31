# Pluto Secure Radio

Secure, low-latency packet radio for paired ADALM-Pluto SDRs. The active
protocol is V2 only: compact binary frames, optional payload compression,
X25519/AES-256-GCM confidentiality, encrypted Ed25519 sender signatures, and
a dedicated fast ACK waveform with bounded retransmissions.

## Status

This is a two-node direct-link implementation. It does not currently relay or
route through intermediate nodes; `TTL` and hop fields are authenticated
private metadata for future mesh work.

## Documentation

- [Current packet reference](CURRENT_DATA_PACKET.md) — exact on-air layout,
  visible fields, encryption, signing, compression, ACK behavior.
- [V2 architecture](V2_ARCHITECTURE.md) — implementation and protocol details.
- [Contributing guide](CONTRIBUTING.md) — development and test expectations.
- [Security policy](SECURITY.md) — key handling and vulnerability reporting.
- [Code of conduct](CODE_OF_CONDUCT.md) — collaboration and safety standards.
- [Changelog](CHANGELOG.md) — user-visible protocol changes.

## Run

Start the receiver on the destination Pluto:

```bash
PYTHONPATH=. ./.venv/bin/python -m pluto_json_radio.rx \
  --config configs/pluto.example.yaml
```

Send a message from the source Pluto:

```bash
PYTHONPATH=. ./.venv/bin/python -m pluto_json_radio.tx \
  --config configs/pluto.example.yaml \
  --text "hello"
```

Build and inspect a frame without using a Pluto:

```bash
PYTHONPATH=. ./.venv/bin/python -m pluto_json_radio.tx \
  --config configs/pluto.example.yaml \
  --text "radio test" --dry-run
```

## Link-quality features

`pluto_json_radio.features` provides reusable calculations for RSSI, SNR,
BER, packet loss rate, average latency, and remaining RF bandwidth. Use
`FeatureTracker` to combine IQ observations and packet results into one
feature snapshot:

```python
import numpy as np

from pluto_json_radio.features import FeatureTracker

tracker = FeatureTracker(
    # Use 1.0 for normalized IQ samples. For raw Pluto samples, use the
    # receiver ADC full-scale value instead.
    full_scale=1.0,
    configured_bandwidth_hz=1_000_000,
    symbol_rate_hz=250_000,
    tone_deviation_hz=125_000,
)

# signal_iq is a signal-plus-noise capture. SNR also needs a noise-only
# capture; omit noise_iq when it is unavailable and SNR remains None.
tracker.observe_iq(signal_iq, noise_iq=noise_iq)

# Record one result per expected packet, including packets that time out.
tracker.record_packet(
    received=True,
    bit_errors=1,
    total_bits=100,
    latency_seconds=0.025,
)
tracker.record_packet(received=False)

features = tracker.get_features()
print(features.as_dict())
```

To capture live values directly from the attached Pluto, keep the peer quiet
for the noise baseline and run:

```bash
PYTHONPATH=. ./.venv/bin/python -m pluto_json_radio.features \
  --config configs/pluto.example.yaml \
  --seconds 10 --noise-seconds 1 --json
```

Run the same command from the `oka_` folder for its Pluto. Add
`--expected-packets N` when you know how many packets should arrive during
the capture; this enables packet-loss calculation. BER is calculated from
detected preamble errors. Passive capture cannot determine end-to-end ACK
latency. When packets contain the default `timestamp_unix` field and both
clocks are synchronized, `average_latency_ms` reports one-way receive age;
otherwise it is `null`. The existing `tx` command prints measured ACK
completion time.

To measure all six values in one run using both attached Plutos, run this
from `oka` (the peer config points at `oka_`):

```bash
PYTHONPATH=. ./.venv/bin/python -m pluto_json_radio.features \
  --test-link \
  --config configs/pluto.example.yaml \
  --peer-config /Users/rajivkhanna/Downloads/oka_/configs/pluto.example.yaml \
  --packets 5 --json
```

This active test sends authenticated probes, measures ACK-based packet loss
and latency, and reports receiver-side RSSI, SNR, and BER. Both Plutos must
be idle before the noise-baseline capture begins.

The snapshot fields use these units: RSSI is dBFS by default, SNR is dB, BER
and packet loss are fractions from 0.0 to 1.0, average latency is milliseconds,
and remaining bandwidth is Hz. To report calibrated RSSI in dBm, pass the
dBm value corresponding to 0 dBFS as `rssi_reference_dbm`. Remaining RF
bandwidth is calculated as configured bandwidth minus the estimated 2-FSK
occupied bandwidth using Carson's rule.

For individual calculations, import `calculate_rssi`, `calculate_snr`,
`calculate_ber`, `calculate_packet_loss_rate`, `calculate_average_latency`,
or `calculate_remaining_bandwidth` from the same module.

## Configuration and keys

Create the local active configuration from the safe template:

```bash
cp configs/pluto.example.yaml.template configs/pluto.example.yaml
```

Each standalone folder has its own active configuration. The folders must use
matching PHY and protocol settings. Their Pluto URI, local node role, and
private keys intentionally differ. The active file is intentionally ignored by
Git.

Generate replacement key pairs with:

```bash
PYTHONPATH=. ./.venv/bin/python -m pluto_json_radio.keys --kind all
```

Keep `private_key` and `signing_private_key` on their owning node. Exchange
only public keys and place them in the peer-public-key maps. Restart receivers
after a key change.

## Verify

```bash
PYTHONPATH=. ./.venv/bin/pytest -q
```

The test suite verifies encrypted V2 encode/decode, hidden metadata, sender
signature rejection on tampering, 2-FSK recovery, and fast ACK recovery.

## Project layout

```text
pluto_json_radio/
  tx.py       sender, retransmission, ACK matching
  rx.py       continuous receiver, verification, ACK transmission
  features.py link-quality and RF-capacity feature calculations
  v2.py       confidential binary packet codec
  phy.py      2-FSK modem and fast ACK waveform
  pluto.py    ADALM-Pluto runtime integration
  keys.py     X25519 and Ed25519 key generator
configs/      node-specific Pluto and key configuration
tests/        V2 protocol and PHY tests
```

## Compatibility

Both peers must run the same V2 wire layout. Update and restart both folders
together when changing protocol code or cryptographic key configuration.

## Optional high-speed JSON transport

See [TRANSPORT.md](TRANSPORT.md) for `python run.py --mode host` / `--mode server`, FastAPI, simulation, independent algorithm settings, fragmentation and benchmarks. The existing V2 commands above remain unchanged.
