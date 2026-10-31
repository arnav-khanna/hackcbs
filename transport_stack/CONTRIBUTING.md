# Contributing

## Scope

Keep changes focused on the active V2 protocol and the two-node Pluto link.
Do not reintroduce JSON/V1 fallback framing without a documented compatibility
plan.

## Development expectations

1. Keep both standalone folders functionally identical, except for intentional
   device address, node role, and private-key configuration differences.
2. Add or update tests for every protocol, crypto, PHY, or reliability change.
3. Run `PYTHONPATH=. ./.venv/bin/pytest -q` in both folders before handoff.
4. Use `--dry-run` for safe packet-construction checks before radio testing.
5. Document wire-format changes in `CURRENT_DATA_PACKET.md`,
   `V2_ARCHITECTURE.md`, and `CHANGELOG.md`.

## Code style

- Target Python 3.9+ and use type annotations for public functions.
- Prefer small, single-purpose functions and explicit validation errors.
- Keep cryptographic operations in `v2.py`; do not invent custom crypto.
- Preserve the low-latency fast ACK path unless a measured replacement exists.

## Commit guidance

Use concise imperative commit subjects, for example:

```text
Add encrypted V2 signature verification
Reduce ACK receiver allocation
Document key rotation procedure
```

Never commit private keys, captured IQ recordings, virtual environments, or
downloaded package artifacts.
