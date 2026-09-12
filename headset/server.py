"""One process owns the headset; every other one watches over a socket.

A bar widget is instantiated once per monitor, so two panels mean two helpers, and
a headset has exactly one control channel. The first process to win the election
opens the channel and serves state; the rest relay. Both roles are this one verb,
so the panel does not have to know or care which it got.

The owner outlives the widget that started it. If that widget goes away while
another is still watching, the owner keeps the session up rather than dropping it
and making everyone else reconnect.
"""
from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import socket
import sys
import time

from . import drivers, ipc
from .device import Device
from .errors import HeadsetError, UnsupportedDevice
from .state import State

BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
CLIENT_OUTBOX_LIMIT = 256 * 1024
IDLE_TICK = 0.5


def _log(text: str) -> None:
    """To stderr, which the shell collects, even when the helper recovers.

    A fault that is only ever visible in a state field the next success clears is a
    fault nobody can report.
    """
    sys.stderr.write(f"omarchy-headset: {text}\n")
    sys.stderr.flush()


class Client:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.inbox = b""
        self.outbox = b""
        self.alive = True

    def send(self, data: bytes) -> None:
        if not self.alive:
            return
        self.outbox += data
        if len(self.outbox) > CLIENT_OUTBOX_LIMIT:
            self.alive = False
            return
        self.flush()

    def flush(self) -> None:
        while self.outbox and self.alive:
            try:
                sent = self.sock.send(self.outbox)
            except BlockingIOError:
                return
            except OSError:
                self.alive = False
                return
            self.outbox = self.outbox[sent:]

    def read_lines(self) -> list[str]:
        lines = []
        while True:
            try:
                chunk = self.sock.recv(4096)
            except BlockingIOError:
                break
            except OSError:
                self.alive = False
                break
            if not chunk:
                self.alive = False
                break
            self.inbox += chunk
            while b"\n" in self.inbox:
                line, self.inbox = self.inbox.split(b"\n", 1)
                lines.append(line.decode("utf-8", "replace"))
        return lines

    def close(self) -> None:
        self.alive = False
        with contextlib.suppress(OSError):
            self.sock.close()


class Stdin:
    """Line-buffered non-blocking stdin that can tell 'nothing yet' from 'gone'."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stdin
        self.fd = self.stream.fileno()
        self.buffer = b""
        self.open = True
        os.set_blocking(self.fd, False)

    def read_lines(self) -> list[str]:
        lines = []
        while True:
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                # No data now. Not the same thing as end of input, and treating the
                # two alike is how a helper exits for no reason.
                break
            except OSError:
                self.open = False
                break
            if chunk == b"":
                self.open = False
                break
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                lines.append(line.decode("utf-8", "replace"))
        return lines


def parse_command(text: str) -> dict:
    """Accept JSON from the panel and words from a terminal."""
    text = text.strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except ValueError:
            raise HeadsetError(f"could not read that as JSON: {text[:60]}")
        return payload if isinstance(payload, dict) else {}
    words = text.split(None, 2)
    verb = words[0]
    if verb == "refresh":
        return {"refresh": True}
    if verb == "replace":
        return {"replace": True}
    if verb == "toggle" and len(words) == 2:
        return {"toggle": words[1]}
    if verb == "set" and len(words) == 3:
        raw = words[2]
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        return {"set": {words[1]: value}}
    raise HeadsetError(f"unknown command {text!r}")


class Owner:
    def __init__(self, address: str, name: str, driver_id: str = "", listener=None, stdin=None):
        driver = drivers.by_id(driver_id) if driver_id else drivers.for_device(name, address)
        if driver is None:
            raise HeadsetError(f"no driver claims {name or address!r}")
        self.address = address
        self.name = name
        self.driver = driver
        self.listener = listener
        self.stdin = stdin
        self.clients: list[Client] = []
        self.state = State()
        self.device: Device | None = None
        self.error = ""
        self.backoff = BACKOFF_START
        self.next_attempt = 0.0
        self.next_poll: dict[str, float] = {}
        self.stdout_open = True
        self.running = True
        # Set when the headset is one this driver can never talk to. Retrying that
        # every minute for the life of the session helps nobody.
        self.hopeless = False
        # True while the opening read is still in flight. Publishing during it
        # shows a panel a device that is connected and has no settings yet.
        self.quiet = False
        self.version = ipc.code_version()

    # ----------------------------------------------------------------- publishing

    def payload(self) -> dict:
        device = self.device
        controls = {}
        snapshot = self.state.snapshot()
        for control in self.driver.controls:
            supported = bool(device and device.support.get(control.record))
            controls[control.id] = {
                "supported": supported,
                "writable": supported and control.honoured,
                "available": supported and (control.available is None or control.available(snapshot)),
            }
        return {
            "type": "state",
            "version": self.version,
            "device": {
                "address": self.address,
                "name": self.name,
                "driver": self.driver.id,
                "channel": device.channel if device else 0,
            },
            "connected": bool(device and device.connected),
            "write_ready": bool(device and device.connected and device.ready),
            "support": dict(device.support) if device else {},
            "controls": controls,
            "state": snapshot,
            "pending": self.state.unconfirmed(),
            "ignored": self.state.ignored(),
            "unsupported": self.hopeless,
            "error": self.error,
        }

    def publish(self) -> None:
        line = (json.dumps(self.payload(), sort_keys=True) + "\n").encode()
        if self.stdout_open:
            try:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            except (BrokenPipeError, OSError):
                # The widget that started us has gone. Others may still be here.
                self.stdout_open = False
        for client in list(self.clients):
            client.send(line)
        self._reap()

    def _reap(self) -> None:
        for client in [c for c in self.clients if not c.alive]:
            client.close()
            self.clients.remove(client)

    # -------------------------------------------------------------------- session

    def _on_change(self, changed: dict) -> None:
        if self.state.observe(changed) and not self.quiet:
            self.publish()

    def connect(self) -> None:
        device = Device(address=self.address, driver=self.driver, name=self.name,
                        on_log=_log, on_change=self._on_change)
        device.open()
        self.device = device
        self.state.forget()
        self.quiet = True
        try:
            device.read()
        finally:
            self.quiet = False
        self.state.observe(dict(device.state))
        now = time.monotonic()
        self.next_poll = {record.id: now + record.poll_seconds
                          for record in self.driver.records if record.poll_seconds > 0}
        self.error = ""
        self.backoff = BACKOFF_START
        self.publish()

    def drop(self, reason: str) -> None:
        if self.device is not None:
            self.device.close()
            self.device = None
        self.error = reason
        self.next_attempt = time.monotonic() + self.backoff
        self.backoff = min(BACKOFF_MAX, self.backoff * 2)
        _log(f"{reason}; retrying in {int(self.next_attempt - time.monotonic())}s")
        self.publish()

    # ------------------------------------------------------------------- commands

    def apply(self, values: dict) -> None:
        """Write features, one frame per group of features that share a message."""
        if self.device is None:
            self.error = "the headset is not connected"
            self.publish()
            return
        snapshot = dict(self.state.snapshot())
        snapshot.update(values)
        groups: dict[str, dict] = {}
        problems = []
        for feature, value in values.items():
            control = self.driver.control(feature)
            if control is None or not self.device.supports(feature):
                problems.append(f"this headset has no {feature.replace('_', ' ')}")
                continue
            if not control.honoured:
                problems.append(f"{feature.replace('_', ' ')} is read-only on this headset")
                continue
            if control.available is not None and not control.available(snapshot):
                # The device takes these and discards them outside the right mode,
                # so refusing plainly beats writing into a void.
                problems.append(f"{feature.replace('_', ' ')} only applies in ambient mode")
                continue
            # Keyed by the encoder as well as the coalescing key: two controls
            # can share a key and still be different messages, and grouping them
            # together let the first encoder run with the second's values and
            # drop them without a word.
            slot = (control.key or control.id, id(control.encode))
            group = groups.setdefault(slot, {"control": control, "values": {}})
            group["values"][feature] = value
        for group in groups.values():
            control = group["control"]
            try:
                message_type, frame = control.encode(group["values"], self.state.snapshot())
            except (ValueError, TypeError) as error:
                problems.append(str(error))
                continue
            self.device.session.set(frame, message_type, key=control.key,
                                    label=f"set {control.id}")
            self.state.expect(group["values"])
        self.error = "; ".join(problems)
        self.publish()

    def handle(self, text: str) -> None:
        try:
            command = parse_command(text)
        except HeadsetError as error:
            self.error = str(error)
            self.publish()
            return
        if command.get("replace") and command.get("version") not in (None, self.version):
            _log("a newer helper asked to take over")
            self.running = False
            return
        if command.get("refresh"):
            if self.device is not None:
                self.device.read()
            return
        if command.get("toggle"):
            feature = command["toggle"]
            current = self.state.snapshot().get(feature)
            self.apply({feature: not bool(current)})
            return
        values = command.get("set")
        if isinstance(values, dict) and values:
            self.apply(values)

    # ----------------------------------------------------------------------- loop

    def accept(self) -> None:
        while True:
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            sock.setblocking(False)
            client = Client(sock)
            self.clients.append(client)
            client.send((json.dumps(self.payload(), sort_keys=True) + "\n").encode())

    def _timeout(self) -> float:
        waits = [IDLE_TICK]
        if self.device is None:
            # Nothing to wake for when there will be no further attempt. Adding
            # the "and not hopeless" to the condition alone dropped through to
            # the branch below, which assumes a device.
            if not self.hopeless:
                waits.append(max(0.05, self.next_attempt - time.monotonic()))
        else:
            deadline = self.device.deadline()
            if deadline is not None:
                waits.append(max(0.01, deadline))
            for moment in self.next_poll.values():
                waits.append(max(0.05, moment - time.monotonic()))
        return max(0.01, min(waits))

    def poll_records(self) -> None:
        if self.device is None:
            return
        now = time.monotonic()
        due = [record_id for record_id, moment in self.next_poll.items() if now >= moment]
        for record_id in due:
            record = self.driver.record(record_id)
            self.next_poll[record_id] = now + record.poll_seconds
            self.device.session.get(record.request, record.message_type, label=record_id)

    def stop(self, *_) -> None:
        self.running = False

    def loop(self) -> int:
        # Without this a terminated owner leaves its socket behind. The next owner
        # unlinks a stale one under the lock, so it is recoverable either way, but
        # leaving litter for the next process to clean up is not a design.
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(number, self.stop)
        self.publish()
        while self.running:
            if self.device is None and not self.hopeless and time.monotonic() >= self.next_attempt:
                try:
                    self.connect()
                except UnsupportedDevice as error:
                    self.hopeless = True
                    self.drop(str(error))
                except HeadsetError as error:
                    self.drop(str(error))

            watch = [self.listener] if self.listener is not None else []
            if self.stdin is not None and self.stdin.open:
                watch.append(self.stdin.fd)
            if self.device is not None:
                watch.append(self.device.fileno())
            writable = [c.sock for c in self.clients if c.outbox and c.alive]
            watch += [c.sock for c in self.clients if c.alive]

            readable, ready_write, _ = select.select(watch, writable, [], self._timeout())

            for sock in ready_write:
                for client in self.clients:
                    if client.sock is sock:
                        client.flush()

            if self.listener is not None and self.listener in readable:
                self.accept()

            lines = []
            if self.stdin is not None and self.stdin.fd in readable:
                lines += self.stdin.read_lines()
            for client in list(self.clients):
                if client.sock in readable:
                    lines += client.read_lines()
            self._reap()
            for line in lines:
                self.handle(line)
                if not self.running:
                    break

            if self.device is not None:
                try:
                    self.poll_records()
                    self.device.pump()
                except HeadsetError as error:
                    self.drop(str(error))
                    continue

            # The last watcher left and the widget that started us is gone.
            if self.stdin is not None and not self.stdin.open and not self.clients:
                self.running = False

        if self.device is not None:
            self.device.close()
        for client in self.clients:
            client.close()
        if self.listener is not None:
            with contextlib.suppress(OSError):
                self.listener.close()
            with contextlib.suppress(OSError):
                ipc.socket_path(self.address).unlink()
        return 0


def follow(sock: socket.socket, stdin: Stdin | None) -> int:
    """Relay the owner's state to our stdout and our stdin to the owner."""
    version = ipc.code_version()
    with contextlib.suppress(OSError):
        sock.sendall((json.dumps({"replace": True, "version": version}) + "\n").encode())
    buffer = b""
    while True:
        watch = [sock]
        if stdin is not None and stdin.open:
            watch.append(stdin.fd)
        readable, _, _ = select.select(watch, [], [], IDLE_TICK)
        if sock in readable:
            try:
                chunk = sock.recv(8192)
            except BlockingIOError:
                # Readable and yet nothing to read. Treating that as end of input
                # spent a reconnect attempt on a spurious wakeup.
                continue
            except OSError:
                return 0
            if chunk == b"":
                return 0  # the owner finished; the caller decides whether to retry
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    sys.stdout.buffer.write(line + b"\n")
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    return 0
        if stdin is not None and stdin.fd in readable:
            for line in stdin.read_lines():
                with contextlib.suppress(OSError):
                    sock.sendall((line + "\n").encode())
            if not stdin.open:
                return 0


def watch(address: str, name: str, driver_id: str = "", attempts: int = 4) -> int:
    """Own the headset, or follow whoever does. One verb, either role."""
    stdin = Stdin()
    for attempt in range(attempts):
        election = ipc.Election(address)
        if election.win():
            try:
                # Inside the try: a bind that raises used to leave the election
                # lock held, so nothing else could become the owner either.
                listener = ipc.bind_listener(address)
                return Owner(address, name, driver_id, listener, stdin).loop()
            finally:
                election.release()
        sock = ipc.connect_to_owner(address)
        if sock is None:
            # The owner died between our failed lock and our connect. Go round.
            time.sleep(0.2)
            continue
        try:
            follow(sock, stdin)
        finally:
            with contextlib.suppress(OSError):
                sock.close()
        if not stdin.open:
            return 0
        time.sleep(0.2)
    raise HeadsetError("could not reach or become the headset helper")
