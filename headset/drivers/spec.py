"""What a driver is, in types.

A `Record` is one question the device can be asked and the answer decoded. A
`Control` is one thing that can be changed. They are separate because a single
record usually carries several controls: the Sony noise record holds the mode, the
ambient level and focus-on-voice in one message, and writing any of them means
restating all three.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

COMMAND_1 = 0x0C
COMMAND_2 = 0x0E


@dataclass(frozen=True)
class Record:
    id: str
    request: bytes
    decode: Callable[[bytes], dict | None]
    provides: tuple[str, ...]
    message_type: int = COMMAND_1
    # Records worth re-reading on a timer rather than only at connect.
    poll_seconds: float = 0.0
    # A record the device may not have; absence is normal, not an error.
    optional: bool = True

    @property
    def reply(self) -> int:
        """A GET's answer carries the request's own code plus one."""
        return (self.request[0] + 1) & 0xFF

    @property
    def notification(self) -> int:
        """The device announces a change someone made on the headset with plus three."""
        return (self.request[0] + 3) & 0xFF

    def matches(self, message) -> bool:
        """Whether an inbound message is this record's answer or announcement.

        The payload type alone is not enough to tell records apart. Speak-to-chat
        and pause-on-removal both answer as 0xf7 and differ only in the sub-byte
        they echo back, so matching on the type alone routed one record's reply to
        the other's decoder, which then declined it and left the real record looking
        like a feature the headset does not have.
        """
        if message.type != self.message_type:
            return False
        if message.kind not in (self.reply, self.notification):
            return False
        if len(self.request) > 1:
            return len(message.payload) > 1 and message.payload[1] == self.request[1]
        return True


@dataclass(frozen=True)
class Control:
    id: str
    encode: Callable[[object, dict], tuple[int, bytes]]
    # The record whose reply confirms this control exists.
    record: str
    # Controls sharing a coalescing key supersede each other in the queue.
    key: str = ""
    # A control the hardware acknowledges and ignores: offered read-only.
    honoured: bool = True
    # Only meaningful while this predicate holds of the current state.
    available: Callable[[dict], bool] | None = None


@dataclass(frozen=True)
class Driver:
    id: str
    name: str
    service_uuid: str
    init: bytes
    records: tuple[Record, ...]
    controls: tuple[Control, ...] = ()
    claims: Callable[[str], bool] = field(default=lambda name: False)
    # Records to read once the init handshake is answered, in order.
    identify: Callable[[bytes], dict] | None = None

    def record(self, record_id: str) -> Record | None:
        for record in self.records:
            if record.id == record_id:
                return record
        return None

    def control(self, control_id: str) -> Control | None:
        for control in self.controls:
            if control.id == control_id:
                return control
        return None
