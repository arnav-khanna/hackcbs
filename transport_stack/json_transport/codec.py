"""Serialize/compress once, encrypt/sign once, then fragment the result."""
from __future__ import annotations
import io
import os
import struct
import orjson
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PREFIX = struct.Struct('!BBBI')
COMPRESSIONS = {'none': 0, 'brotli': 1, 'zstd': 2, 'lz4': 3}


class Codec:
    def __init__(self, config, security):
        self.config = config
        self.security = security
        self.compressor = None
        algorithm = config.compression['algorithm']
        level = config.compression['level']
        if algorithm == 'zstd':
            import zstandard
            self.compressor = zstandard.ZstdCompressor(level=level)
        if algorithm == 'brotli' and not 0 <= level <= 11:
            raise ValueError('Brotli level must be 0..11')
        if algorithm == 'lz4' and not 0 <= level <= 16:
            raise ValueError('LZ4 level must be 0..16')

    def compress(self, raw):
        option = self.config.compression
        algorithm = option['algorithm']
        if algorithm == 'none' or len(raw) < option['min_bytes']:
            return 0, raw
        if algorithm == 'brotli':
            import brotli
            packed = brotli.compress(raw, quality=option['level'])
        elif algorithm == 'zstd':
            packed = self.compressor.compress(raw)
        else:
            import lz4.frame
            packed = lz4.frame.compress(raw, compression_level=option['level'])
        if option['skip_if_larger'] and len(packed) >= len(raw):
            return 0, raw
        return COMPRESSIONS[algorithm], packed

    def decompress(self, code, packed, expected):
        if not 0 < expected <= self.config.limits['max_message_bytes']:
            raise ValueError('Invalid decoded size')
        if code == 0:
            raw = packed
        elif code == 1:
            import brotli
            dec = brotli.Decompressor()
            raw = dec.process(packed, output_buffer_limit=expected + 1)
            if not dec.is_finished():
                raise ValueError('Brotli output exceeds limit or is truncated')
        elif code == 2:
            import zstandard
            # Streaming read bounds allocation even when a frame advertises a huge size.
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(packed)) as reader:
                raw = reader.read(expected + 1)
        elif code == 3:
            import lz4.frame
            dec = lz4.frame.LZ4FrameDecompressor()
            raw = dec.decompress(packed, max_length=expected + 1)
            if not dec.eof or dec.unused_data:
                raise ValueError('Invalid or oversized LZ4 frame')
        else:
            raise ValueError('Unknown compression algorithm')
        if len(raw) != expected:
            raise ValueError('Decoded size does not match envelope')
        return raw

    @staticmethod
    def context(message_id, source, destination):
        return b'JT-MESSAGE-1' + struct.pack('!QBB', message_id, source, destination)

    def encode(self, obj, message_id):
        if not isinstance(obj, dict):
            raise ValueError('A JSON object is required')
        raw = orjson.dumps(obj)
        if len(raw) > self.config.limits['max_message_bytes']:
            raise ValueError('JSON exceeds max_message_bytes')
        code, packed = self.compress(raw)
        encryption = int(self.config.encryption['algorithm'] == 'aes256')
        signing = int(self.security.signer is not None)
        prefix = PREFIX.pack(code, encryption, signing, len(raw))
        context = self.context(message_id, self.config.local['node_id'], self.config.local['peer_id'])
        if encryption:
            nonce = os.urandom(12)
            packed = nonce + AESGCM(self.security.key).encrypt(nonce, packed, context + prefix)
        body = prefix + packed
        return body + self.security.sign(context + body), len(raw)

    def decode(self, blob, message_id, source, destination):
        if len(blob) < PREFIX.size:
            raise ValueError('Truncated JSON envelope')
        code, encryption, signing, expected = PREFIX.unpack(blob[:PREFIX.size])
        if encryption != int(self.config.encryption['algorithm'] == 'aes256'):
            raise ValueError('Encryption policy mismatch')
        if signing != int(self.config.signing['algorithm'] == 'ed25519'):
            raise ValueError('Signature policy mismatch')
        context = self.context(message_id, source, destination)
        if signing:
            self.security.verify(blob[-64:], context + blob[:-64])
            blob = blob[:-64]
        packed = blob[PREFIX.size:]
        if encryption:
            if len(packed) < 28:
                raise ValueError('Truncated AES-GCM ciphertext')
            packed = AESGCM(self.security.key).decrypt(packed[:12], packed[12:], context + blob[:PREFIX.size])
        raw = self.decompress(code, packed, expected)
        obj = orjson.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError('Decoded JSON is not an object')
        return obj
