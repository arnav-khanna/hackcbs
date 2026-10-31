"""Fixed binary header, fragment indices, selective ACK bitmap and CRC32."""
from __future__ import annotations
from dataclasses import dataclass
import struct
import zlib

HEADER = struct.Struct('!2sBBBBQHH')
CRC = struct.Struct('!I')
DATA, ACK, HELLO, HELLO_ACK = 1, 2, 3, 4
REQUEST_ACK = 0x80
OVERHEAD = HEADER.size + CRC.size


@dataclass(frozen=True)
class Frame:
    kind: int
    source: int
    destination: int
    message_id: int
    index: int = 0
    count: int = 1
    payload: bytes = b''
    request_ack: bool = False

    def header(self):
        return HEADER.pack(b'JT', 1, self.kind | (REQUEST_ACK if self.request_ack else 0),
                           self.source, self.destination, self.message_id, self.index, self.count)

    def encode(self):
        data = self.header() + self.payload
        return data + CRC.pack(zlib.crc32(data))

    @classmethod
    def decode(cls, data, limit=4096):
        if not OVERHEAD <= len(data) <= limit:
            raise ValueError('Invalid packet length')
        if CRC.unpack(data[-4:])[0] != zlib.crc32(data[:-4]):
            raise ValueError('Packet CRC mismatch')
        magic, version, flags, source, destination, msgid, index, count = HEADER.unpack(data[:HEADER.size])
        kind = flags & 0x7f
        if magic != b'JT' or version != 1 or kind not in (DATA, ACK, HELLO, HELLO_ACK):
            raise ValueError('Unknown transport packet')
        if not count or not source or not destination or source == destination:
            raise ValueError('Invalid fragment header')
        if kind == DATA and index >= count:
            raise ValueError('Fragment index out of range')
        return cls(kind, source, destination, msgid, index, count, data[HEADER.size:-4], bool(flags & REQUEST_ACK))
