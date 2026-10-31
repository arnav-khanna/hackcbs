# Security Policy

## Supported protocol

Only the current V2 implementation is supported. It uses X25519 key agreement,
HKDF-SHA256, AES-256-GCM, and Ed25519 signatures.

## Key handling

- `private_key` is an X25519 private key; never share it.
- `signing_private_key` is an Ed25519 private key; never share it.
- Share only the corresponding public keys.
- Use `pluto_json_radio.keys --kind all` to rotate keys.
- Restart both receivers after changing public-key mappings.

The supplied local configuration files contain working development keys. Before
publishing, copying to an untrusted machine, or committing to a remote
repository, rotate those keys and replace private values with local secrets.

## Reporting vulnerabilities

Do not disclose suspected key exposure, authentication bypass, decryption
failure, replay weakness, or remote-code-execution issues in public logs.
Report them privately to the project maintainer with a minimal reproduction,
affected version, and any safe mitigations.

## Security boundaries

Data frames encrypt source identity, packet metadata, payload, and signature.
The receiver ID and nonce remain visible for destination selection and the
low-latency ACK mechanism. The fast ACK itself is plaintext by design and is
not a proof of message authenticity.
