"""Ask a remote device which RFCOMM channel carries a service, and cache the answer.

BlueZ exposes no API for a remote service record, and Arch no longer ships `sdptool`
in bluez-utils, so this speaks SDP itself over L2CAP PSM 1. Only one request is
needed: ServiceSearchAttribute for a 128-bit UUID, asking for every attribute.

Guessing the channel instead is not an option. A wrong channel refuses only after a
slow blocking connect, and a device that has been walked channel by channel is
reported to start refusing the right one too. So: ask, then remember.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import struct
import uuid as uuidlib
from pathlib import Path

from .errors import HeadsetError, UnsupportedDevice

SDP_PSM = 1
SERVICE_SEARCH_ATTRIBUTE_REQUEST = 0x06
SERVICE_SEARCH_ATTRIBUTE_RESPONSE = 0x07
RFCOMM_UUID = 0x0003
MAX_ATTRIBUTE_BYTES = 0xFFFF
CONNECT_TIMEOUT = 15.0


def _element_uuid128(value: str) -> bytes:
    return b"\x1c" + uuidlib.UUID(value).bytes


def _element_sequence(payload: bytes) -> bytes:
    if len(payload) > 0xFF:
        raise HeadsetError("SDP request sequence is too long to encode")
    return b"\x35" + bytes((len(payload),)) + payload


def _element_uint32(value: int) -> bytes:
    return b"\x0a" + struct.pack(">I", value)


def _request(service_uuid: str, transaction: int, continuation: bytes) -> bytes:
    params = (
        _element_sequence(_element_uuid128(service_uuid))
        + struct.pack(">H", MAX_ATTRIBUTE_BYTES)
        + _element_sequence(_element_uint32(0x0000FFFF))
        + continuation
    )
    return bytes((SERVICE_SEARCH_ATTRIBUTE_REQUEST,)) + struct.pack(">HH", transaction, len(params)) + params


def rfcomm_channels(record: bytes) -> list[int]:
    """Every RFCOMM channel in a service record.

    A ProtocolDescriptorList entry for RFCOMM is the 16-bit UUID 0x0003 followed by
    a one-byte channel, so `19 00 03 08 <channel>`. Scanning for that pattern rather
    than walking the whole element tree is deliberate: the tree is only needed to
    reach this one number, and a parser for all of SDP is a parser to maintain.
    """
    found = []
    for index in range(len(record) - 4):
        if record[index] == 0x19 and record[index + 1] == 0x00 and record[index + 2] == RFCOMM_UUID:
            if record[index + 3] == 0x08:
                found.append(record[index + 4])
    return found


def service_record(address: str, service_uuid: str) -> bytes:
    """The concatenated attribute lists for one service UUID on one device."""
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_SEQPACKET, socket.BTPROTO_L2CAP)
    sock.settimeout(CONNECT_TIMEOUT)
    try:
        sock.connect((address, SDP_PSM))
        record = b""
        continuation = b"\x00"
        transaction = 1
        while True:
            sock.send(_request(service_uuid, transaction, continuation))
            response = sock.recv(4096)
            if len(response) < 5 or response[0] != SERVICE_SEARCH_ATTRIBUTE_RESPONSE:
                raise HeadsetError("the device answered SDP with something else")
            length = struct.unpack(">H", response[3:5])[0]
            params = response[5:5 + length]
            if len(params) < 2:
                raise HeadsetError("the device sent a truncated SDP response")
            count = struct.unpack(">H", params[0:2])[0]
            record += params[2:2 + count]
            continuation = params[2 + count:]
            # One byte of continuation state is the terminator; more means come back.
            if len(continuation) <= 1 or transaction > 16:
                return record
            transaction += 1
    except OSError as error:
        raise HeadsetError(f"could not ask {address} about its services: {error.strerror or error}") from error
    finally:
        # A half-open L2CAP socket left behind is how the next attempt fails
        # against our own leftovers rather than against the device.
        with contextlib.suppress(OSError):
            sock.close()


def _cache_path(address: str) -> Path | None:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    folder = Path(base) / "omarchy-headset"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
    except OSError:
        return None
    return folder / (address.replace(":", "").lower() + ".json")


def _cache_read(path: Path, service_uuid: str) -> int | None:
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(handle, "r") as stream:
            cached = json.load(stream)
    except (OSError, ValueError):
        return None
    channel = cached.get(service_uuid) if isinstance(cached, dict) else None
    return channel if isinstance(channel, int) and 1 <= channel <= 30 else None


def _cache_write(path: Path, service_uuid: str, channel: int) -> None:
    existing = {}
    with contextlib.suppress(OSError, ValueError):
        with open(path, "r") as stream:
            loaded = json.load(stream)
            if isinstance(loaded, dict):
                existing = loaded
    existing[service_uuid] = channel
    # Exclusive, no-follow, and named for this process: the read side already
    # refused to follow a symlink and the write side did not, and two processes
    # resolving the same headset shared one temporary name.
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return
    try:
        with os.fdopen(handle, "w") as stream:
            json.dump(existing, stream)
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def channel_for(address: str, service_uuid: str, use_cache: bool = True) -> int:
    """The RFCOMM channel for a service, from the cache or from the device."""
    path = _cache_path(address)
    if use_cache and path is not None:
        cached = _cache_read(path, service_uuid)
        if cached is not None:
            return cached
    channels = rfcomm_channels(service_record(address, service_uuid))
    if not channels:
        # It answered the query and has no such service. A device whose services
        # were merely not resolved yet fails the query itself, further up, and
        # that stays worth retrying; this does not.
        raise UnsupportedDevice("this device does not advertise a headset control service")
    channel = channels[0]
    if path is not None:
        _cache_write(path, service_uuid, channel)
    return channel
