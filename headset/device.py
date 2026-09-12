"""A headset: a session, a driver, and what the two of them established together.

`support` is the point of this module. A feature is supported only if the device
answered its record. Anything that was acknowledged and then met with silence is
recorded as absent, so the panel never offers a control the hardware discards.
"""
from __future__ import annotations

import select
import time
from dataclasses import dataclass, field
from typing import Callable

from . import framing, sdp
from .drivers import spec
from .errors import HeadsetError, UnsupportedDevice
from .session import ACKED, FAILED, REPLIED, SILENT, Session


@dataclass
class Device:
    address: str
    driver: spec.Driver
    name: str = ""
    on_log: Callable[[str], None] | None = None
    on_change: Callable[[dict], None] | None = None

    session: Session | None = None
    state: dict = field(default_factory=dict)
    support: dict = field(default_factory=dict)
    channel: int = 0
    ready: bool = False
    last_read: dict = field(default_factory=dict)

    # -------------------------------------------------------------------- opening

    def open(self, use_cache: bool = True) -> None:
        self.channel = sdp.channel_for(self.address, self.driver.service_uuid, use_cache=use_cache)
        factory = self.driver.session or Session
        self.session = factory(
            address=self.address,
            channel=self.channel,
            on_message=self._absorb,
            on_log=self._log,
        )
        self.session.connect()
        self.ready = False
        try:
            self._handshake()
        except Exception:
            # Leaving the socket open here is not untidiness. The next attempt
            # then fails with EBUSY against this process's own leftovers rather
            # than against the headset, and one bad handshake becomes permanent.
            self.close()
            raise

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        self.ready = False

    @property
    def connected(self) -> bool:
        return self.session is not None and self.session.alive

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def _handshake(self) -> None:
        """The init exchange decides the protocol generation before anything else.

        A protocol with no handshake skips it: the channel being open is the whole
        of the negotiation, and the device starts talking on its own.
        """
        if self.driver.init is None:
            self.ready = True
            return

        answer: dict = {}
        # Whether it answered at all is a different question from what the driver
        # read out of the answer. A driver with no `identify` returns nothing and
        # was being told its headset had not replied.
        replied = False

        def done(status: str, payload: bytes | None) -> None:
            nonlocal replied
            if status != REPLIED:
                return
            replied = True
            if payload is not None and self.driver.identify:
                answer.update(self.driver.identify(payload))

        self.session.submit(self.driver.init, expect=0x01, on_done=done, label="init")
        self.session.run_until_idle(timeout=6.0)
        if not replied:
            raise HeadsetError("the headset did not answer the control handshake")
        protocol = answer.get("protocol")
        if self.driver.protocols and protocol not in self.driver.protocols:
            # Refused before anything is applied, so the panel is never briefly
            # told about a headset that is about to be rejected.
            raise UnsupportedDevice(
                f"this headset speaks the older Sony protocol ({protocol}), which this "
                "driver does not implement"
            )
        self._apply(answer)
        self.ready = True

    # ------------------------------------------------------------------ absorbing

    def _absorb(self, message: framing.Message) -> None:
        """Decode any inbound command, whether we asked for it or the device offered.

        A press of the button on the earcup arrives here unprompted, which is the
        only reason the panel can show a change made on the headset itself.
        """
        for record in self.driver.records:
            if not record.matches(message):
                continue
            decoded = record.decode(message.payload)
            if decoded is None:
                # Keep looking. Stopping at the first record whose payload type
                # matched is what hid a whole feature behind another one.
                continue
            self.last_read.update(decoded)
            if record.volunteered and not self.support.get(record.id):
                # It just arrived, so it exists. Waiting for the next explicit
                # read left a feature that had announced itself unsupported.
                self.support[record.id] = True
                for feature in record.provides:
                    self.support[feature] = True
            self._apply(decoded)
            return

    def _apply(self, values: dict) -> None:
        changed = {}
        for key, value in values.items():
            # A battery reading of zero arrives spuriously from some protocols
            # just after a multipoint attach and corrects on the next read, so the
            # last good value stands. Only where the driver says so: on a protocol
            # that reports a real zero, holding the old number is the lie.
            if (key == "battery" and value == 0 and self.state.get("battery")
                    and self.driver.battery_zero_is_noise):
                continue
            if self.state.get(key) != value:
                self.state[key] = value
                changed[key] = value
        if changed and self.on_change:
            self.on_change(changed)

    # -------------------------------------------------------------------- reading

    def read(self, record_ids: tuple[str, ...] | None = None, timeout: float = 12.0) -> dict:
        """Ask for records and record which ones the device actually has."""
        records = [r for r in self.driver.records
                   if record_ids is None or r.id in record_ids]
        outcomes: dict[str, str] = {}

        for record in records:
            if record.volunteered:
                continue

            def done(status: str, payload: bytes | None, record=record) -> None:
                outcomes[record.id] = status

            self.session.get(record.request, record.message_type,
                             on_done=done, label=record.id)
        self.session.run_until_idle(timeout=timeout)

        for record in records:
            if record.volunteered:
                # Nothing was asked, so support is whatever turned up.
                present = any(feature in self.last_read for feature in record.provides)
                self.support[record.id] = present
                for feature in record.provides:
                    self.support[feature] = present
                continue
            status = outcomes.get(record.id, FAILED)
            present = status == REPLIED
            self.support[record.id] = present
            for feature in record.provides:
                self.support[feature] = present
            if not present:
                for feature in record.provides:
                    self.state.pop(feature, None)
        return dict(self.support)

    def supports(self, control_id: str) -> bool:
        control = self.driver.control(control_id)
        if control is None:
            return False
        return bool(self.support.get(control.record))

    def available(self, control_id: str) -> bool:
        control = self.driver.control(control_id)
        if control is None or not self.supports(control_id):
            return False
        return control.available is None or control.available(self.state)

    # -------------------------------------------------------------------- writing

    def write(self, control_id: str, value, on_done=None) -> None:
        control = self.driver.control(control_id)
        if control is None:
            raise HeadsetError(f"unknown control {control_id!r}")
        if not self.supports(control_id):
            raise HeadsetError(f"this headset has no {control_id.replace('_', ' ')}")
        message_type, payload = control.encode(value, self.state)
        self.session.set(payload, message_type, key=control.key,
                         on_done=on_done, label=f"set {control_id}")

    def write_sync(self, control_id: str, value, timeout: float = 6.0) -> str:
        """Write and wait for the acknowledgement only.

        An acknowledgement is not agreement. The device takes some writes and
        discards them, and it will keep reporting the old value to this session for
        up to a minute afterwards. The only way to know is `tools/verify.py`, which
        reconnects before reading back.
        """
        outcome = {"status": FAILED}

        def done(status: str, payload: bytes | None) -> None:
            outcome["status"] = status

        self.write(control_id, value, on_done=done)
        self.session.run_until_idle(timeout=timeout)
        return outcome["status"]

    # ---------------------------------------------------------------- event loop

    def fileno(self) -> int:
        if self.session is None:
            raise HeadsetError("the session is not open")
        return self.session.fileno()

    def deadline(self) -> float | None:
        return self.session.deadline() if self.session else None

    def pump(self) -> None:
        if self.session is None:
            raise HeadsetError("the session is not open")
        self.session.pump()


def open_device(address: str, name: str = "", driver_id: str = "") -> Device:
    from . import drivers

    driver = drivers.by_id(driver_id) if driver_id else drivers.for_device(name, address)
    if driver is None:
        raise HeadsetError(f"no driver claims {name or address!r}")
    device = Device(address=address, driver=driver, name=name)
    device.open()
    return device
