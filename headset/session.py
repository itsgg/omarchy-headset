"""One RFCOMM control session, with the acknowledgement discipline MDR requires.

Every command is acknowledged, the acknowledgement carries the sequence number the
next command must use, and a command arriving from the device must be acknowledged
with the complement of its own. So exactly one request may be on the wire at a time;
the rest queue. A slider drag that ignores this puts twenty frames in flight and the
device answers none of them.

Three outcomes matter, and they are not the same thing:

    replied   the device answered, and the answer is the value
    acked     the device took it, and no answer was expected
    silent    the device acknowledged and then said nothing

`silent` is the useful one. It is how an unsupported feature announces itself: the
device takes the request politely and discards it. Every capability this helper
claims was established by a request that came back `replied` rather than `silent`.
"""
from __future__ import annotations

import contextlib
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from . import framing
from .errors import HeadsetError

REPLIED = "replied"
ACKED = "acked"
SILENT = "silent"
FAILED = "failed"

# Resolved once rather than reached for inside connect(). A Python built without
# Bluetooth has none of these, and an AttributeError four frames down reads as a
# bug in this program rather than as a platform that cannot do Bluetooth at all.
# It is also what lets a test drive the whole stack on a fake socket anywhere.
# How many reads one turn may take before it hands control back. A headset that
# always has another chunk ready would otherwise hold this loop for ever. The
# buffer stays bounded either way, but nothing else in the helper runs while it
# spins: no request is written, no deadline fires, and the panel stops being
# answered. The caller selects on the socket and comes straight back, so a turn
# that ends early costs a pass through the loop and loses nothing. fastpair has
# had the same bound, and the same number, since it was written.
READS_PER_TURN = 32

AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", None)
BTPROTO_RFCOMM = getattr(socket, "BTPROTO_RFCOMM", None)

SOL_RFCOMM = 18
RFCOMM_LM = 0x03
RFCOMM_LM_AUTH = 0x0002
RFCOMM_LM_ENCRYPT = 0x0004


def reply_code(payload: bytes) -> int:
    """The payload type a GET's answer carries: the request's own code plus one."""
    if not payload:
        raise HeadsetError("cannot derive a reply code for an empty request")
    return (payload[0] + 1) & 0xFF


@dataclass
class Request:
    payload: bytes
    message_type: int = framing.COMMAND_1
    expect: int | None = None
    key: str = ""
    on_done: Callable[[str, bytes | None], None] | None = None
    attempts: int = 0
    sent_at: float = 0.0
    acked: bool = False
    acked_at: float = 0.0
    label: str = ""

    def finish(self, status: str, payload: bytes | None = None) -> None:
        if self.on_done:
            self.on_done(status, payload)


@dataclass
class Session:
    address: str
    channel: int
    on_message: Callable[[framing.Message], None] | None = None
    on_log: Callable[[str], None] | None = None

    ack_timeout: float = 1.0
    reply_timeout: float = 1.6
    max_attempts: int = 3
    connect_timeout: float = 12.0

    sock: socket.socket | None = None
    seq: int = 0
    alive: bool = False
    buffer: bytes = b""
    outbox: bytes = b""
    queue: deque = field(default_factory=deque)
    outstanding: Request | None = None
    # One entry per frame put on the wire and not yet acknowledged. A
    # retransmission earns a second acknowledgement, and without this the
    # duplicate lands on whatever is outstanding by then and completes it.
    awaiting: deque = field(default_factory=deque)

    # ------------------------------------------------------------------ lifecycle

    def connect(self) -> None:
        if AF_BLUETOOTH is None or BTPROTO_RFCOMM is None:
            raise HeadsetError("this Python was built without Bluetooth support")
        sock = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
        try:
            with contextlib.suppress(OSError):
                sock.setsockopt(SOL_RFCOMM, RFCOMM_LM, RFCOMM_LM_AUTH | RFCOMM_LM_ENCRYPT)
            sock.settimeout(self.connect_timeout)
            sock.connect((self.address, self.channel))
            sock.setblocking(False)
        except OSError as error:
            # Closing our own half-open socket is not tidiness. Leave it and the
            # next attempt fails with EBUSY against this process, not the headset,
            # and one transient failure becomes a permanent one.
            with contextlib.suppress(OSError):
                sock.close()
            reason = error.strerror or str(error)
            if error.errno in (16, 110):  # EBUSY, ETIMEDOUT
                raise HeadsetError(
                    f"the headset's control channel is busy ({reason}); another device "
                    "or phone app is probably holding it"
                ) from error
            raise HeadsetError(f"could not open the headset's control channel: {reason}") from error
        self.sock = sock
        self.alive = True
        self.buffer = b""
        self.outbox = b""
        self.seq = 0
        self.queue.clear()
        self.awaiting.clear()
        self.outstanding = None

    def close(self) -> None:
        self.alive = False
        if self.sock is not None:
            with contextlib.suppress(OSError):
                self.sock.close()
            self.sock = None
        self._abandon("the session closed")

    def fileno(self) -> int:
        if self.sock is None:
            raise HeadsetError("the session is not open")
        return self.sock.fileno()

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def _abandon(self, reason: str) -> None:
        pending = list(self.queue)
        self.queue.clear()
        self.awaiting.clear()
        if self.outstanding is not None:
            pending.insert(0, self.outstanding)
            self.outstanding = None
        for request in pending:
            request.finish(FAILED, None)
        if pending:
            self._log(f"{len(pending)} request(s) dropped: {reason}")

    # ------------------------------------------------------------------- requests

    def submit(
        self,
        payload: bytes,
        message_type: int = framing.COMMAND_1,
        expect: int | None = None,
        key: str = "",
        on_done: Callable[[str, bytes | None], None] | None = None,
        label: str = "",
    ) -> Request:
        request = Request(
            payload=bytes(payload),
            message_type=message_type,
            expect=expect,
            key=key,
            on_done=on_done,
            label=label or (payload[:2].hex(" ") if payload else ""),
        )
        if key:
            # A superseded write is not worth sending. Dragging a slider queues one
            # value per pixel and only the last one is a fact about the world.
            for index, queued in enumerate(self.queue):
                if queued.key == key:
                    self.queue[index].finish(FAILED, None)
                    self.queue[index] = request
                    return request
        self.queue.append(request)
        return request

    def get(self, payload: bytes, message_type: int = framing.COMMAND_1, **kwargs) -> Request:
        return self.submit(payload, message_type, expect=reply_code(payload), **kwargs)

    def set(self, payload: bytes, message_type: int = framing.COMMAND_1, **kwargs) -> Request:
        return self.submit(payload, message_type, expect=None, **kwargs)

    def busy(self) -> bool:
        return self.outstanding is not None or bool(self.queue)

    def deadline(self) -> float | None:
        """When `pump` must run again even with nothing to read."""
        if self.outstanding is None:
            return 0.0 if self.queue else None
        limit = self.ack_timeout if not self.outstanding.acked else self.reply_timeout
        return max(0.0, self.outstanding.sent_at + limit - time.monotonic())

    def _write(self, data: bytes) -> None:
        """Queue bytes and push what the socket will take.

        The socket is non-blocking, so a send can refuse for want of buffer
        space. Swallowing that loses the frame: an acknowledgement dropped that
        way stops the headset sending any further announcements, because it
        waits for one before it sends the next.
        """
        self.outbox += data
        self.flush()

    def flush(self) -> None:
        while self.outbox:
            try:
                sent = self.sock.send(self.outbox)
            except BlockingIOError:
                return
            except OSError as error:
                self.alive = False
                raise HeadsetError(f"the headset link dropped while writing: {error.strerror or error}") from error
            if sent <= 0:
                return
            self.outbox = self.outbox[sent:]

    def _send(self, request: Request) -> None:
        request.attempts += 1
        request.sent_at = time.monotonic()
        request.acked = False
        request.acked_at = 0.0
        frame = framing.encode(framing.Message(request.message_type, self.seq, request.payload))
        self.awaiting.append(request)
        self._write(frame)

    def _complete(self, status: str, payload: bytes | None = None) -> None:
        request = self.outstanding
        self.outstanding = None
        if request is not None:
            request.finish(status, payload)

    def _pump_queue(self) -> None:
        # Not until every frame already sent has been acknowledged. A reply can
        # arrive before its acknowledgement, and sending the next request then
        # would put two on the wire and use a sequence number the headset has
        # not yet handed back.
        if self.outstanding is None and self.queue and not self.awaiting:
            self.outstanding = self.queue.popleft()
            self._send(self.outstanding)

    # -------------------------------------------------------------------- reading

    def _receive(self) -> None:
        for _ in range(READS_PER_TURN):
            try:
                chunk = self.sock.recv(4096)
            except BlockingIOError:
                return
            except OSError as error:
                self.alive = False
                raise HeadsetError(f"the headset link dropped: {error.strerror or error}") from error
            if not chunk:
                self.alive = False
                raise HeadsetError("the headset closed the control channel")
            self.buffer += chunk
            frames, self.buffer = framing.split(self.buffer)
            for frame in frames:
                message = framing.decode(frame)
                if message is None:
                    self._log(f"discarded a malformed frame: {frame[:24].hex(' ')}")
                    continue
                self._handle(message)

    def _handle(self, message: framing.Message) -> None:
        if message.type == framing.ACK:
            self.seq = message.seq
            # This acknowledgement belongs to the oldest frame still waiting for
            # one, which after a retransmission is not necessarily the request
            # that happens to be outstanding now.
            earned = self.awaiting.popleft() if self.awaiting else None
            if earned is not None and earned is self.outstanding and not earned.acked:
                earned.acked = True
                earned.acked_at = time.monotonic()
                if earned.expect is None:
                    self._complete(ACKED)
            return
        # Anything that is not an ack is a command from the device and must be
        # acknowledged before it will send another.
        self._write(framing.ack(message.seq))
        if self.on_message:
            self.on_message(message)
        if self.outstanding is not None and self.outstanding.expect == message.kind:
            self._complete(REPLIED, message.payload)

    def pump(self) -> None:
        """One turn: drain the socket, honour timeouts, then send what is next."""
        if not self.alive or self.sock is None:
            raise HeadsetError("the session is not open")
        self._receive()
        self.flush()
        request = self.outstanding
        if request is not None:
            now = time.monotonic()
            if not request.acked and now - request.sent_at > self.ack_timeout:
                if request.attempts >= self.max_attempts:
                    self._complete(FAILED)
                    self.alive = False
                    raise HeadsetError("the headset stopped acknowledging commands")
                self._log(f"retransmitting {request.label} (attempt {request.attempts + 1})")
                self._send(request)
            # The wait for a reply starts when the headset took the request, not
            # when it was first sent. Measuring from the send made a retransmitted
            # request look unsupported a moment after it was finally acknowledged.
            elif request.acked and now - request.acked_at > self.reply_timeout:
                # Took it and said nothing: the feature is not really there.
                self._complete(SILENT)
        self._pump_queue()

    def run_until_idle(self, timeout: float = 8.0) -> None:
        """Pump until the queue empties. For the CLI and the verification harness."""
        import select

        end = time.monotonic() + timeout
        self._pump_queue()
        while self.busy() and time.monotonic() < end:
            wait = self.deadline()
            budget = min(end - time.monotonic(), 0.25 if wait is None else max(0.01, wait))
            select.select([self.sock], [], [], max(0.0, budget))
            self.pump()
        if self.busy():
            self._abandon("timed out waiting for the headset")
