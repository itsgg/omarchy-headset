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

from . import audio, drivers, equaliser, ipc
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
        # Why a particular setting was refused, keyed by the setting. A reason
        # that belongs to one row is shown on that row: a single line at the foot
        # of the panel cannot say which of nine settings it is about, and it
        # outlives the action that caused it.
        self.refused: dict = {}
        self.backoff = BACKOFF_START
        self.next_attempt = 0.0
        self.next_poll: dict[str, float] = {}
        self.stdout_open = True
        self.running = True
        # Set when the headset is one this driver can never talk to. Retrying that
        # every minute for the life of the session helps nobody.
        self.hopeless = False
        # What the panel was last told had gone unanswered, so a write giving up
        # is published even though no reading arrived to prompt it.
        self.reported_ignored: list = []
        # An equaliser this machine runs, for a headset whose own hardware has
        # none. Offered only where the driver has no equaliser of its own: two in
        # one panel is a question nobody should have to answer.
        self.equaliser = equaliser.Equaliser(address, name, on_log=_log)
        self.host_equaliser = not any(c.id == "eq_bands" for c in self.driver.controls)
        # Not before this, so a sink that is a moment behind bluez is waited for.
        self.restore_after = 0.0
        self.restored = False
        # How many checks in a row have found no sink for this headset.
        self.sink_missing = 0
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
        sound = {}
        if self.host_equaliser and self.equaliser.available():
            sound = self.equaliser.state()
            controls["eq_enabled"] = {"supported": True, "writable": True, "available": True}
            controls["eq_gains"] = {"supported": True, "writable": True,
                                    "available": bool(sound.get("eq_enabled"))}
            controls["eq_preset_host"] = {"supported": True, "writable": True,
                                          "available": bool(sound.get("eq_enabled"))}
            sound = dict(sound)
            sound["eq_preset_host"] = sound.pop("eq_preset", "")
            snapshot = dict(snapshot)
            snapshot.update(sound)
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
            "equaliser": sound,
            "pending": self.state.unconfirmed(),
            "ignored": self.state.ignored(),
            "unsupported": self.hopeless,
            "error": self.error,
            "refused": dict(self.refused),
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

    def apply_equaliser(self, values: dict) -> bool:
        """The equaliser this machine runs. Nothing here reaches the headset."""
        wanted = {k: v for k, v in values.items()
                  if k in ("eq_enabled", "eq_gains", "eq_preset_host")}
        if not wanted:
            return False
        if not self.host_equaliser:
            self._refuse(wanted, "This headset has an equaliser of its own")
            self.publish()
            return True
        if not self.equaliser.available():
            # The opposite of the message above, and it used to send that one.
            self._refuse(wanted, "PipeWire here cannot run an equaliser")
            self.publish()
            return True
        # Into the headset itself, never into the equaliser's own sink. A card
        # name cannot be turned into a sink name by string surgery, so the sink
        # is looked up, and if it is not there yet the equaliser says so.
        # A preset is a curve, so it arrives as one. Nothing stores which preset
        # is selected: the name is read back off the gains, and the two cannot
        # drift apart the way a remembered name drifts from a hand-moved band.
        gains = wanted.get("eq_gains")
        if "eq_preset_host" in wanted:
            curve = equaliser.curve_for(str(wanted["eq_preset_host"]))
            if curve is None:
                # Only the preset. One command can carry the switch as well, and
                # answering "there is no such preset" to an on/off switch is both
                # nonsense and a refusal of something that was perfectly valid.
                self._refuse({"eq_preset_host": None}, "There is no such preset")
            else:
                gains = list(curve)
        try:
            self.equaliser.apply(self.sink_name(),
                                 enabled=wanted.get("eq_enabled"),
                                 gains=gains)
        except HeadsetError as error:
            self._refuse(wanted, str(error))
        self.publish()
        return True

    def _refuse(self, wanted: dict, reason: str) -> None:
        """Say why, on the rows it is about rather than at the foot of the panel."""
        for feature in wanted:
            self.refused[feature] = reason

    def tend_equaliser(self) -> None:
        """Keep the equaliser matching what is actually there, once a loop pass.

        Three things, none of which the user should have to do by hand: clear a
        chain that outlived the helper that started it, switch a saved equaliser
        back on when the headset comes back, and shut one down when the headset
        it was playing into has gone.
        """
        if not self.host_equaliser or not self.equaliser.available():
            return
        if not self.restored:
            # A chain is a separate process and survives a helper that was killed
            # outright, so the audio can still be going through a curve nothing
            # owns while the panel reports the equaliser off. Clear it once at
            # startup, whether or not one is wanted now.
            self.restored = True
            self.equaliser.reap_strays()
        # Cheap and every pass: the routing waits for the chain's sink here
        # rather than in a sleep, so that nothing else has to wait for it.
        self.equaliser.settle()
        self.equaliser.bury_the_dead()
        if not self.equaliser.enabled:
            return
        now = time.monotonic()
        if now < self.restore_after:
            return
        self.restore_after = now + 2.0
        sink = self.sink_name()
        if self.equaliser.running:
            # The headset went away underneath a running chain. Audio is going
            # into a sink whose own output points at nothing, which is silence
            # with no obvious way back, so take the chain out of the path.
            #
            # Twice in a row, though, never once: a codec change tears the
            # transport down and builds it again, so the sink is missing for a
            # moment in the middle of something the panel itself offers. Acting
            # on the first look would stop the equaliser during a codec change
            # and start it again four seconds later.
            self.sink_missing = self.sink_missing + 1 if not sink else 0
            if self.sink_missing >= 2:
                self.sink_missing = 0
                self.equaliser.stop()
                _log("the headset's audio went away, so the equaliser stopped")
                self.publish()
            return
        if not sink:
            return
        try:
            self.equaliser.apply(sink)
            # The reason it could not start is not true any more. A message that
            # outlives the thing it described is the fault this whole map exists
            # to avoid, and nothing else clears it: this is not a command.
            self.refused.pop("eq_enabled", None)
            self.refused.pop("eq_gains", None)
            _log("equaliser switched back on, as it was left")
        except HeadsetError as error:
            # Say it once. A sink that never arrives should not fill the journal.
            self.equaliser.enabled = False
            _log(f"could not restore the equaliser: {error}")
        self.publish()

    def sink_name(self) -> str:
        """The headset's own audio sink, which is where the equaliser sends."""
        with contextlib.suppress(HeadsetError):
            for line in audio.run_pactl(["list", "sinks", "short"]).splitlines():
                fields = line.split()
                if len(fields) > 1 and self.address.replace(":", "_").upper() in fields[1].upper():
                    return fields[1]
        return ""

    def apply(self, values: dict) -> None:
        """Write features, one frame per group of features that share a message."""
        if self.apply_equaliser(values):
            values = {k: v for k, v in values.items() if k not in ("eq_enabled", "eq_gains")}
            if not values:
                return
        if self.device is None:
            self.error = "the headset is not connected"
            self.publish()
            return
        snapshot = dict(self.state.snapshot())
        snapshot.update(values)
        groups: dict[str, dict] = {}
        problems: dict = {}
        for feature, value in values.items():
            control = self.driver.control(feature)
            if control is None or not self.device.supports(feature):
                problems[feature] = "This headset does not have this setting"
                continue
            if not control.honoured:
                problems[feature] = "This headset reports this and does not accept changes"
                continue
            if control.available is not None and not control.available(snapshot):
                # The device takes these and discards them outside the right mode,
                # so refusing plainly beats writing into a void.
                problems[feature] = "Only applies in ambient mode"
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
                for feature in group["values"]:
                    problems[feature] = str(error)
                continue
            self.device.session.set(frame, message_type, key=control.key,
                                    label=f"set {control.id}")
            self.state.expect(group["values"])
        # Merged, not replaced: one command can carry both an equaliser setting
        # and a headset setting, and assigning here threw away the reason the
        # equaliser half was refused a moment earlier.
        self.refused.update(problems)
        self.publish()

    def handle(self, text: str) -> None:
        # A message is about the action that produced it and must not outlive it.
        # Left to persist, one failed write puts a line on the panel that stays
        # there through every unrelated thing the user does next.
        stale = bool(self.error or self.refused)
        self.error = ""
        self.refused = {}
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
            # Clearing a message is itself a change the panel has to be told
            # about, or it keeps showing one this command has already retracted.
            if stale:
                self.publish()
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
        giving_up = self.state.next_deadline()
        if giving_up is not None:
            waits.append(max(0.05, giving_up))
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

            self.tend_equaliser()

            if self.device is not None:
                try:
                    self.poll_records()
                    self.device.pump()
                except HeadsetError as error:
                    self.drop(str(error))
                    continue

            # A write that has just given up changes the panel with nothing else
            # to announce it.
            gave_up = self.state.ignored()
            if gave_up != self.reported_ignored:
                self.reported_ignored = gave_up
                self.publish()

            # The last watcher left and the widget that started us is gone.
            if self.stdin is not None and not self.stdin.open and not self.clients:
                self.running = False

        if self.device is not None:
            self.device.close()
        self.equaliser.stop()
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
