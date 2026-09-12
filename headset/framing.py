"""Sony MDR message framing.

A message on the wire is:

    3e <type> <seq> <length:4 big-endian> <payload...> <checksum> 3c

The checksum is a one-byte sum of everything between the header and itself. Every
byte in that span that would collide with the header, trailer or escape byte is
escaped: the escape byte, then the original masked with 0xef. Unescaping ORs the
0x10 back in, which is why the mask and the restore are not symmetrical to read.

`decode` returns None rather than raising for anything malformed. A radio link
delivers truncated and corrupt frames, and a helper that dies on one is a helper
that dies.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER = 0x3E
TRAILER = 0x3C
ESCAPE = 0x3D
ESCAPE_MASK = 0xEF

ACK = 0x01
COMMAND_1 = 0x0C
COMMAND_2 = 0x0E

HEADER_LEN = 6  # type, seq, and the four length bytes
MIN_FRAME = 1 + HEADER_LEN + 1 + 1  # header, fixed fields, checksum, trailer


@dataclass(frozen=True)
class Message:
    type: int
    seq: int
    payload: bytes

    @property
    def kind(self) -> int:
        """The payload type byte, or -1 for an empty payload such as an ack."""
        return self.payload[0] if self.payload else -1


def escape(data: bytes) -> bytes:
    out = bytearray()
    for byte in data:
        if byte in (HEADER, TRAILER, ESCAPE):
            out.append(ESCAPE)
            out.append(byte & ESCAPE_MASK)
        else:
            out.append(byte)
    return bytes(out)


def unescape(data: bytes) -> bytes | None:
    out = bytearray()
    index = 0
    while index < len(data):
        byte = data[index]
        if byte == ESCAPE:
            index += 1
            if index >= len(data):
                return None  # an escape with nothing to escape
            out.append(data[index] | (~ESCAPE_MASK & 0xFF))
        else:
            out.append(byte)
        index += 1
    return bytes(out)


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def encode(message: Message) -> bytes:
    body = bytes((message.type, message.seq)) + struct.pack(">I", len(message.payload)) + message.payload
    return bytes((HEADER,)) + escape(body) + escape(bytes((checksum(body),))) + bytes((TRAILER,))


def ack(seq: int) -> bytes:
    """An ack answers a received sequence number with its complement."""
    return encode(Message(ACK, 1 - (seq & 1), b""))


def decode(frame: bytes) -> Message | None:
    """One complete frame, header and trailer included, to a Message or None."""
    if len(frame) < MIN_FRAME or frame[0] != HEADER or frame[-1] != TRAILER:
        return None
    body = unescape(frame[1:-1])
    if body is None or len(body) < HEADER_LEN + 1:
        return None
    expected = checksum(body[:-1])
    if body[-1] != expected:
        return None
    length = struct.unpack(">I", body[2:6])[0]
    payload = body[HEADER_LEN:-1]
    if length != len(payload):
        return None
    return Message(body[0], body[1], payload)


def split(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Pull every complete frame out of a stream buffer; return them and the rest.

    Leading noise before a header is dropped, because a resynchronising radio link
    is the normal case and there is nothing useful to do with the fragment.
    """
    frames = []
    while True:
        start = buffer.find(bytes((HEADER,)))
        if start < 0:
            return frames, b""
        end = buffer.find(bytes((TRAILER,)), start + 1)
        if end < 0:
            return frames, buffer[start:]
        # A frame is the shortest span ending at this trailer. A stray header
        # delivered by the radio just before a good frame would otherwise swallow
        # it whole: the span fails its checksum and the real frame goes with it.
        inner = buffer.rfind(bytes((HEADER,)), start + 1, end)
        if inner > start:
            start = inner
        frames.append(buffer[start:end + 1])
        buffer = buffer[end + 1:]
