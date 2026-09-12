"""Google Fast Pair Message Stream: the one cross-vendor channel that exists.

Both a Sony WH-1000XM5 and a Nothing Ear (open) advertise this service, which is
two unrelated manufacturers speaking one protocol, and that is the whole reason
it is worth implementing. It carries per-earbud battery, which no standard
Bluetooth path can give, and a noise-control extension whose first two bytes are
the headset telling you which modes it has and which of them you may set.

Framing is plain, with no acknowledgement, no sequence number and no checksum:

    <group:1> <code:1> <length:2 big-endian> <data...>

The authenticated wrapper that used to guard the noise extension was removed from
the specification in April 2024, which is why a desktop can speak this at all.

References: Google's Message Stream, Device Information and Hearable Controls
extension pages. The frame shapes below were checked against a real headset; the
battery layout matches Google's own worked example byte for byte.
"""
from __future__ import annotations

import contextlib
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from ..errors import HeadsetError
from .spec import COMMAND_1, Control, Driver, Record

SERVICE_UUID = "df21fe2c-2515-4fdb-8886-f12c4d67927c"

GROUP_DEVICE_INFORMATION = 0x03
GROUP_HEARABLE = 0x08

CODE_MODEL_ID = 0x01
CODE_BATTERY = 0x03
CODE_ANC_GET = 0x11
CODE_ANC_SET = 0x12
CODE_ANC_NOTIFY = 0x13

BATTERY_LEVEL_MASK = 0x7F
BATTERY_CHARGING = 0x80
BATTERY_UNKNOWN = 0x7F
BATTERY_LEVEL_MAX = 100
CASE_ABSENT = 0xFF

# Google numbers these from the most significant bit, so bit 0 is 0x80. Reading
# them the other way round is the obvious mistake and would silently offer the
# wrong modes.
ANC_TRANSPARENT = 0x80
ANC_ADAPTIVE = 0x40
ANC_OFF = 0x20
ANC_ON = 0x08

NOISE_BITS = (
    ("off", ANC_OFF),
    ("transparent", ANC_TRANSPARENT),
    ("adaptive", ANC_ADAPTIVE),
    ("anc", ANC_ON),
)
SEEKER_VERSION = 0x02


@dataclass(frozen=True)
class Message:
    type: int
    seq: int
    payload: bytes

    @property
    def kind(self) -> int:
        return self.payload[0] if self.payload else -1

    @property
    def group(self) -> int:
        return self.type

    @property
    def code(self) -> int:
        return self.seq


def encode(group: int, code: int, data: bytes = b"") -> bytes:
    return bytes((group, code)) + struct.pack(">H", len(data)) + data


# Nothing in this protocol is remotely this big. A length beyond it is a frame
# boundary that has been lost, and since the stream carries no sync byte the only
# way back is to drop one byte and look again.
MAX_DATA = 512


def split(buffer: bytes) -> tuple[list[Message], bytes]:
    """Every whole frame in a stream buffer, and what is left over.

    A frame claiming more data than this protocol ever carries is not a frame we
    are short of bytes for; it is a lost boundary. Waiting for it stalls every
    real frame behind it, so resynchronise instead.
    """
    out = []
    while len(buffer) >= 4:
        group, code = buffer[0], buffer[1]
        length = struct.unpack(">H", buffer[2:4])[0]
        if length > MAX_DATA:
            buffer = buffer[1:]
            continue
        if len(buffer) < 4 + length:
            break
        out.append(Message(group, code, buffer[4:4 + length]))
        buffer = buffer[4 + length:]
    return out, buffer


# --------------------------------------------------------------------- session


@dataclass
class StreamSession:
    """A plain frame stream. No acknowledgements, so no discipline to keep."""

    address: str
    channel: int
    on_message: Callable[[Message], None] | None = None
    on_log: Callable[[str], None] | None = None
    connect_timeout: float = 12.0
    # How long without a frame counts as the headset having finished introducing
    # itself.
    quiet_for: float = 0.8

    sock: socket.socket | None = None
    alive: bool = False
    buffer: bytes = b""
    outbox: bytes = b""
    queue: deque = field(default_factory=deque)
    sent: list = field(default_factory=list)
    reads_per_turn: int = 32

    def connect(self) -> None:
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.settimeout(self.connect_timeout)
            sock.connect((self.address, self.channel))
            sock.setblocking(False)
        except OSError as error:
            with contextlib.suppress(OSError):
                sock.close()
            raise HeadsetError(
                f"could not open the headset's message stream: {error.strerror or error}") from error
        self.sock = sock
        self.alive = True
        self.buffer = b""
        self.outbox = b""
        self.queue.clear()
        self.sent.clear()

    def close(self) -> None:
        self.alive = False
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None
        self.queue.clear()
        for on_done in self.sent:
            if on_done:
                on_done("failed", None)
        self.sent.clear()

    def fileno(self) -> int:
        if self.sock is None:
            raise HeadsetError("the message stream is not open")
        return self.sock.fileno()

    def busy(self) -> bool:
        return bool(self.queue or self.outbox or self.sent)

    def deadline(self) -> float | None:
        return 0.0 if self.busy() else None

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    # The Device seam. `get` exists so a driver can ask, but nothing here needs a
    # reply to be matched to a request, because frames arrive on their own.
    def submit(self, payload: bytes, message_type: int = COMMAND_1, expect=None,
               key: str = "", on_done=None, label: str = ""):
        self.queue.append((bytes(payload), on_done))
        return None

    def get(self, payload: bytes, message_type: int = COMMAND_1, **kwargs):
        return self.submit(payload, message_type, **kwargs)

    def set(self, payload: bytes, message_type: int = COMMAND_1, **kwargs):
        return self.submit(payload, message_type, **kwargs)

    def flush(self) -> None:
        while self.outbox:
            try:
                sent = self.sock.send(self.outbox)
            except BlockingIOError:
                return
            except OSError as error:
                self.alive = False
                raise HeadsetError(f"the message stream dropped: {error.strerror or error}") from error
            if sent <= 0:
                return
            self.outbox = self.outbox[sent:]

    def pump(self) -> int:
        """One turn. Returns how many frames arrived, so a caller can tell quiet
        from busy; comparing the leftover buffer could not, because a stream of
        whole frames leaves the same remainder every time.
        """
        if not self.alive or self.sock is None:
            raise HeadsetError("the message stream is not open")
        seen = 0
        # A budget, so a headset talking continuously cannot keep this from
        # returning and starve the queued writes behind it.
        for _ in range(self.reads_per_turn):
            try:
                chunk = self.sock.recv(4096)
            except BlockingIOError:
                break
            except OSError as error:
                self.alive = False
                raise HeadsetError(f"the message stream dropped: {error.strerror or error}") from error
            if not chunk:
                self.alive = False
                raise HeadsetError("the headset closed the message stream")
            self.buffer += chunk
            frames, self.buffer = split(self.buffer)
            seen += len(frames)
            for frame in frames:
                if self.on_message:
                    self.on_message(frame)
        while self.queue:
            payload, on_done = self.queue.popleft()
            self.outbox += payload
            self.sent.append(on_done)
        before = len(self.outbox)
        self.flush()
        # Only once the bytes have actually left. Reporting a write as taken while
        # it sits in a buffer that a close would discard is a lie.
        if not self.outbox and before:
            for on_done in self.sent:
                if on_done:
                    on_done("acked", None)
            self.sent.clear()
        return seen

    def run_until_idle(self, timeout: float = 4.0) -> None:
        """Pump for a while. There is nothing to wait for, so this is a listen.

        A Fast Pair headset volunteers what it knows as soon as the channel
        opens, and answers nothing on demand, so the only way to learn what it
        supports is to listen for a moment.
        """
        import select

        end = time.monotonic() + timeout
        quiet_since = None
        while time.monotonic() < end:
            select.select([self.sock], [], [], 0.2)
            arrived = self.pump()
            if self.busy() or arrived:
                quiet_since = None
                continue
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since > self.quiet_for:
                return


# -------------------------------------------------------------------- decoding


def battery_part(value: int) -> dict | None:
    if value == CASE_ABSENT:
        return None
    level = value & BATTERY_LEVEL_MASK
    # 0x7f is the protocol's "unknown"; anything above a hundred is not a
    # percentage at all and was being shown as one.
    if level == BATTERY_UNKNOWN or level > BATTERY_LEVEL_MAX:
        return None
    return {"level": level, "charging": bool(value & BATTERY_CHARGING)}


def decode_battery(payload: bytes) -> dict | None:
    """Three bytes, one per part: bit seven charging, the rest a percentage.

    A frame saying a part is now unknown is news, so the keys are always written,
    with None where nothing is known. Returning nothing for that case left the
    panel showing the last reading as though it were current.
    """
    if len(payload) < 3:
        return None
    parts = {}
    for name, value in zip(("left", "right", "case"), payload[:3]):
        part = battery_part(value)
        if part is not None:
            parts[name] = part
    worn = [p["level"] for name, p in parts.items() if name in ("left", "right")]
    return {
        "battery_parts": parts or None,
        "battery": min(worn) if worn else None,
        "charging": any(p["charging"] for name, p in parts.items()
                        if name in ("left", "right")) if worn else None,
    }


def decode_model(payload: bytes) -> dict | None:
    if len(payload) < 3:
        return None
    return {"model_id": payload[:3].hex()}


def decode_noise(payload: bytes) -> dict | None:
    """Version, the modes it has, the modes you may set, and the one it is in."""
    if len(payload) < 4:
        return None
    offered, settable, current = payload[1], payload[2], payload[3]
    modes = [name for name, bit in NOISE_BITS if offered & bit]
    if not modes:
        return None
    active = next((name for name, bit in NOISE_BITS if current & bit), None)
    return {
        "noise": active,
        "noise_modes": modes,
        "noise_settable": [name for name, bit in NOISE_BITS if settable & bit],
    }


# Setting a noise mode is deliberately not implemented yet.
#
# The specification's Set request is a seeker version byte followed by the
# control data, and it does not say what the capability bytes should carry on the
# way in. No headset here has noise cancelling to try it against: the Nothing Ear
# (open) is an open-ear model with none, and the Sony has a driver of its own that
# already does it properly. Writing a frame of that shape and hoping is how you
# put an untested guess on somebody else's hardware.
#
# What is implemented is the reading, which is checked and useful: the headset
# says which modes it has and which it is in. Enabling the write needs one
# headset with noise cancelling and no vendor driver, and the verify harness.


def _is(group: int, *codes: int):
    def match(message) -> bool:
        return message.group == group and message.code in codes
    return match


RECORDS = (
    Record("battery", None, decode_battery, ("battery", "charging", "battery_parts"),
           match=_is(GROUP_DEVICE_INFORMATION, CODE_BATTERY)),
    Record("model", None, decode_model, ("model_id",),
           match=_is(GROUP_DEVICE_INFORMATION, CODE_MODEL_ID)),
    Record("noise", None, decode_noise, ("noise", "noise_modes", "noise_settable"),
           match=_is(GROUP_HEARABLE, CODE_ANC_NOTIFY)),
)

CONTROLS = ()


def claims(name: str) -> bool:
    # Nothing is claimed by name. This protocol is identified by the service a
    # device advertises, which is proved by opening it.
    return False


DRIVER = Driver(
    id="fast-pair",
    name="Fast Pair",
    service_uuid=SERVICE_UUID,
    init=None,
    records=RECORDS,
    controls=CONTROLS,
    claims=claims,
    session=StreamSession,
    fallback=True,
)
