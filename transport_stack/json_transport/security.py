"""Reuse existing X25519/HKDF, AES-GCM and Ed25519; all are optional."""
from __future__ import annotations
import base64
import hashlib
import hmac
import os
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import yaml


def key_bytes(value, label='key'):
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception as exc:
        raise ValueError(f'{label} must be base64') from exc
    if len(raw) != 32:
        raise ValueError(f'{label} must contain 32 bytes')
    return raw


def derive(secret, salt, context):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=context).derive(secret)


class Security:
    def __init__(self, config):
        self.config = config
        self.root = None
        self.key = None
        self.signer = None
        self.verifier = None
        self.public_key = b''
        self.peer_public = b''
        self.session_token = None
        identity = {}
        if config.key_exchange['algorithm'] != 'disabled' or config.signing['algorithm'] != 'none':
            with config.path(config.local['identity_file']).open() as stream:
                document = yaml.safe_load(stream)
            identity = document.get('security', document)
            peer = document.get('link', {}).get('default_receiver_address')
            if not peer:
                raise ValueError('Identity file requires link.default_receiver_address')
        if config.key_exchange['algorithm'] == 'x25519':
            # Use the project's existing, tested static-key agreement exactly.
            from pluto_json_radio.v2 import _link_key
            private = identity['private_key']
            public = identity['peer_public_keys'][peer]
            self.root = _link_key(private, public)
            self.public_key = X25519PrivateKey.from_private_bytes(key_bytes(private)).public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self.peer_public = key_bytes(public)
        elif config.encryption['algorithm'] == 'aes256':
            value = os.environ.get(config.encryption['key_env'])
            if config.encryption['key_file']:
                value = config.path(config.encryption['key_file']).read_text().strip()
            if not value:
                raise ValueError('AES-256 with key exchange disabled requires a shared base64 32-byte key via key_env or key_file')
            self.root = key_bytes(value, 'AES shared key')
        if config.signing['algorithm'] == 'ed25519':
            self.signer = Ed25519PrivateKey.from_private_bytes(key_bytes(identity['signing_private_key']))
            self.verifier = Ed25519PublicKey.from_public_bytes(key_bytes(identity['peer_signing_public_keys'][peer]))
        self.activate(None)

    def activate(self, token):
        self.session_token = token
        self.key = derive(self.root, token, b'PLUTO-JSON-TRANSPORT-1-DATA') if self.root else None

    def sign(self, data):
        return self.signer.sign(data) if self.signer else b''

    def verify(self, signature, data):
        if not self.verifier:
            raise ValueError('Peer signing key is not configured')
        self.verifier.verify(signature, data)

    @property
    def auth_size(self):
        return 16 if self.root else (64 if self.signer else 0)

    def authenticate(self, data, *, control=False):
        secret = self.root if control else self.key
        if secret:
            return hmac.new(secret, b'JT-CONTROL' + data if control else b'JT-ACK' + data, hashlib.sha256).digest()[:16]
        return self.sign(data)

    def check_auth(self, data, tag, *, control=False):
        if len(tag) != self.auth_size:
            raise ValueError('Incorrect authentication tag length')
        if self.root:
            if not hmac.compare_digest(self.authenticate(data, control=control), tag):
                raise ValueError('Authentication failed')
        elif self.verifier:
            self.verify(tag, data)
