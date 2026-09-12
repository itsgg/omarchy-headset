"""The published state, and the one rule that keeps it honest after a write.

The device will not read back its own change. Write a new noise mode and this
session keeps being told the old one for up to a minute, while the hardware has
already audibly changed. A press of the button on the earcup, by contrast, is
announced within a second or two.

So a written value is shown at once and held as pending. A later reading is believed
only if it moved: equal to what was written means it converged, equal to what was
there before means it is the stale echo, and anything else means something changed
the setting behind our back and the device wins.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

# How long a written value may disagree with the device before the panel is told
# that this control looks like one the hardware ignores.
CONVERGE_SECONDS = 25.0


@dataclass
class Pending:
    value: object
    was: object
    since: float


@dataclass
class State:
    values: dict = field(default_factory=dict)
    pending: dict = field(default_factory=dict)
    now: object = time.monotonic

    def snapshot(self) -> dict:
        """What the panel should show: readings, with unconfirmed writes over them.

        A write that has gone unconfirmed for long enough stops being shown. The
        panel's job is to report the headset, and a requested value that the
        hardware never took is a misreport however it is labelled.
        """
        out = dict(self.values)
        moment = self.now()
        for key, entry in self.pending.items():
            if moment - entry.since <= CONVERGE_SECONDS:
                out[key] = entry.value
        return out

    def unconfirmed(self) -> list:
        """Controls whose written value the device has not caught up with yet."""
        return sorted(self.pending)

    def next_deadline(self) -> float | None:
        """Seconds until the earliest unconfirmed write gives up, or None.

        The panel changes at that moment with no reading to prompt it, so the
        loop has to wake for it or the reverted value sits on screen unseen.
        """
        moment = self.now()
        waits = [entry.since + CONVERGE_SECONDS - moment
                 for entry in self.pending.values()
                 if entry.since + CONVERGE_SECONDS > moment]
        # Entries already past their deadline have been reported. Continuing to
        # return zero for them wakes the loop twenty times a second for ever.
        return min(waits) if waits else None

    def ignored(self) -> list:
        """Written, acknowledged, and still not reflected long afterwards."""
        moment = self.now()
        return sorted(key for key, entry in self.pending.items()
                      if moment - entry.since > CONVERGE_SECONDS)

    def expect(self, values: dict) -> dict:
        """Record what was just written. Returns what the panel should now show."""
        changed = {}
        for key, value in values.items():
            entry = self.pending.get(key)
            if entry is not None:
                # Writing the same value again is not a reason to forget it is
                # still unconfirmed. Dropping the entry here reverted the panel to
                # the value being replaced, which is the opposite of what happened.
                entry.value = value
                entry.since = self.now()
                if value != self.values.get(key):
                    changed[key] = value
                continue
            if self.values.get(key) == value:
                continue
            self.pending[key] = Pending(value=value, was=self.values.get(key), since=self.now())
            changed[key] = value
        return changed

    def observe(self, values: dict) -> dict:
        """Fold in a reading from the device. Returns what changed on screen.

        Reported against the snapshot rather than against the stored reading,
        because the panel shows the snapshot. A pending write that expires puts
        the device's value back on screen without the stored reading moving at
        all, and reporting nothing there left the panel showing a value the
        headset had never accepted.
        """
        changed = {}
        moment = self.now()
        for key, value in values.items():
            before = self.snapshot().get(key)
            entry = self.pending.get(key)
            if entry is not None and moment - entry.since > CONVERGE_SECONDS:
                # Long enough. The headset was never going to apply this, so stop
                # showing it and stop reading every real change as our own echo.
                del self.pending[key]
                entry = None
            if entry is None:
                self.values[key] = value
            elif value == entry.value:
                # Converged. The device now agrees with what was asked for.
                del self.pending[key]
                self.values[key] = value
            elif value == entry.was:
                # The stale echo of the value being replaced. Not news.
                self.values[key] = value
            else:
                # Neither: something else changed this setting, and it is the truth.
                del self.pending[key]
                self.values[key] = value
            after = self.snapshot().get(key)
            if after != before:
                changed[key] = after
        return changed

    def forget(self, keys=None) -> None:
        """Drop everything, or one feature, on a reconnect."""
        if keys is None:
            self.values.clear()
            self.pending.clear()
            return
        for key in keys:
            self.values.pop(key, None)
            self.pending.pop(key, None)
