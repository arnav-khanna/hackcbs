"""Generate X25519 key pairs for the V2 encrypted radio link."""
from __future__ import annotations

import argparse
import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


def generate_x25519_keypair() -> dict[str, str]:
    """Return one base64-encoded X25519 private/public key pair."""
    private_key = X25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "private_key": base64.b64encode(private_bytes).decode("ascii"),
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }


def generate_ed25519_keypair() -> dict[str, str]:
    """Return one base64-encoded Ed25519 signing private/public key pair."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return {
        "private_key": base64.b64encode(private_bytes).decode("ascii"),
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate an X25519 key pair for V2 AES-GCM encryption."
    )
    parser.add_argument(
        "--json", action="store_true", help="Print JSON instead of YAML-ready text."
    )
    parser.add_argument(
        "--kind", choices=("all", "encryption", "signing"), default="all",
        help="Generate X25519 encryption keys, Ed25519 signing keys, or both.",
    )
    args = parser.parse_args(argv)
    pairs = {}
    if args.kind in ("all", "encryption"):
        pairs["encryption"] = generate_x25519_keypair()
    if args.kind in ("all", "signing"):
        pairs["signing"] = generate_ed25519_keypair()
    if args.json:
        print(json.dumps(pairs, indent=2))
        return 0
    print("# Keep all private keys on this node. Share only public keys with peers.")
    print("security:")
    if "encryption" in pairs:
        print("  encryption_method: x25519-aesgcm")
        print(f'  private_key: "{pairs["encryption"]["private_key"]}"')
        print(f'# X25519 public key to share: "{pairs["encryption"]["public_key"]}"')
    if "signing" in pairs:
        print(f'  signing_private_key: "{pairs["signing"]["private_key"]}"')
        print(f'# Ed25519 signing public key to share: "{pairs["signing"]["public_key"]}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
