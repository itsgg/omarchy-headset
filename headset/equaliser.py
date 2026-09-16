"""An equaliser for headsets whose own hardware has none.

Bluetooth standardises no equaliser, so a headset without a vendor protocol has
none this machine can reach. It can have one anyway: PipeWire will run a filter
chain, and the audio can be sent through it on the way to the headset. The
processing happens here rather than in the earcups, which is the only difference
that matters, and it works on anything.

The chain runs as its own short-lived PipeWire client, configured from a private
directory under `$XDG_RUNTIME_DIR`. Nothing of the user's configuration is
written, read or replaced, and everything this creates is gone at logout whether
or not anything shut down cleanly.

Where a headset has its own equaliser, this is not offered: two equalisers in one
panel is a question nobody should have to answer.
"""
from __future__ import annotations

import cmath
import contextlib
import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import binaries
from .errors import HeadsetError

# The ten the ear expects to see, an octave apart.
FREQUENCIES = (32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
BAND_MIN, BAND_MAX = -10, 10
# Somewhere to start. Ten bands is more knobs than anyone wants to turn before
# hearing anything, and every equaliser people actually use opens on a named
# curve rather than on a flat line and a shrug.
#
# Shelves rather than bumps: a boost at one band and nothing either side is a
# resonance, not a tone control, so each curve tapers across neighbours. Cuts
# are preferred to boosts where a cut will do, because a cut costs no headroom
# and the trim gives back every decibel of boost anyway.
PRESETS = (
    ("Flat", (0, 0, 0, 0, 0, 0, 0, 0, 0, 0)),
    ("Bass", (6, 5, 3.5, 1.5, 0, 0, 0, 0, 0, 0)),
    ("Vocal", (-3, -2.5, -1, 1, 3, 3.5, 2.5, 1, 0, 0)),
    ("Treble", (0, 0, 0, 0, 0, 0.5, 1.5, 3, 4.5, 5)),
    ("Podcast", (-6, -4, -1.5, 1, 2.5, 3, 2, 0.5, -1, -2)),
)
CUSTOM = "Custom"
BASE_CONFIG = "/usr/share/pipewire/filter-chain.conf"
QUALITY = 1.1
# The rates the response is measured at, and how many points across 20 Hz to
# 20 kHz. Neither reaches the audio: they only decide the trim.
#
# Both common rates, because PipeWire recomputes the filter coefficients at
# whatever the graph negotiated and the bilinear warping differs between them,
# most at the 16 kHz band, which sits at 0.73 of Nyquist on 44.1 kHz against
# 0.67 on 48 kHz. Measuring both and taking the louder is a bound that holds
# either way, and costs a few hundred multiplications once per curve change.
RATES = (44100, 48000)
RATE = 48000
PROBE_POINTS = 240


def clamp(value) -> float:
    """A gain from anywhere, held to the range and the step the panel offers.

    Infinity and not-a-number are checked for rather than assumed away: JSON
    carries both, `float("nan")` accepts the word, and rounding either one
    raises. This value arrives from a panel and from a file a user can edit.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return max(BAND_MIN, min(BAND_MAX, round(number * 2) / 2))


def quoted(text: str) -> str:
    """A string safe to put between double quotes in PipeWire's config.

    The headset's name comes from BlueZ, and a user can rename a headset to
    anything at all. A double quote in it ends the string early and the rest
    becomes configuration, which at best refuses to start.
    """
    clean = "".join(character for character in str(text) if character.isprintable())
    return clean.replace("\\", "\\\\").replace('"', '\\"')


def slug(address: str) -> str:
    return address.replace(":", "").lower() or "headset"


def response(levels: list, frequency: float, rate: int = RATE) -> float:
    """How loud the whole chain is at one frequency, as a plain ratio.

    The Audio EQ Cookbook peaking filter, which is the one PipeWire's
    `bq_peaking` implements, evaluated on the unit circle and multiplied down
    the chain. Written out rather than approximated because the point of it is
    to be right about the one number that decides whether the output clips.
    """
    total = 1.0
    omega = 2.0 * math.pi * frequency / rate
    z1 = cmath.exp(-1j * omega)
    z2 = z1 * z1
    for centre, gain in zip(FREQUENCIES, levels):
        if not gain:
            continue
        amplitude = 10.0 ** (gain / 40.0)
        w0 = 2.0 * math.pi * centre / rate
        alpha = math.sin(w0) / (2.0 * QUALITY)
        cos0 = math.cos(w0)
        numerator = (1 + alpha * amplitude) + (-2 * cos0) * z1 + (1 - alpha * amplitude) * z2
        denominator = (1 + alpha / amplitude) + (-2 * cos0) * z1 + (1 - alpha / amplitude) * z2
        if denominator == 0:
            continue
        total *= abs(numerator / denominator)
    return total


def headroom(levels: list) -> float:
    """The loudest the chain gets anywhere, in decibels, never below zero.

    Taking back only the tallest band is not enough: two neighbouring bands both
    boosted overlap, and the sum between them is louder than either. So the
    response is measured across the audible range rather than guessed from the
    largest number in the list.
    """
    if not any(level > 0 for level in levels):
        return 0.0
    peak = 1.0
    for rate in RATES:
        for step in range(PROBE_POINTS + 1):
            frequency = 20.0 * (1000.0 ** (step / PROBE_POINTS))
            if frequency * 2 >= rate:
                break
            peak = max(peak, response(levels, frequency, rate))
    return 20.0 * math.log10(peak)


def graph(gains: list) -> str:
    """One peaking filter per band, in series, with a trim for the boosts.

    Boosting ten bands and sending the result on unchanged is how a filter chain
    clips. The trim takes back the chain's loudest point, measured rather than
    assumed, so the loudest thing through it is no louder than it went in.
    """
    levels = [clamp(g) for g in gains][:len(FREQUENCIES)]
    levels += [0.0] * (len(FREQUENCIES) - len(levels))
    nodes = []
    for index, (frequency, gain) in enumerate(zip(FREQUENCIES, levels)):
        nodes.append(f'        {{ type = builtin name = b{index} label = bq_peaking '
                     f'control = {{ "Freq" = {frequency} "Q" = {QUALITY} "Gain" = {gain} }} }}')
    # Rounded down, never to nearest: rounding up puts the trim back above the
    # value that made the guarantee true, by a hair, and the guarantee is the
    # only reason the trim is here.
    trim = math.floor(10 ** (-headroom(levels) / 20.0) * 10000) / 10000
    nodes.append(f'        {{ type = builtin name = trim label = linear '
                 f'control = {{ "Gain" = {trim} "Offset" = 0.0 }} }}')
    links = []
    names = [f"b{i}" for i in range(len(FREQUENCIES))] + ["trim"]
    for source, target in zip(names, names[1:]):
        links.append(f'        {{ output = "{source}:Out" input = "{target}:In" }}')
    return ("      nodes = [\n" + "\n".join(nodes) + "\n      ]\n"
            + "      links = [\n" + "\n".join(links) + "\n      ]")


def fragment(name: str, address: str, sink: str, gains: list) -> str:
    key = slug(address)
    name = quoted(name)
    sink = quoted(sink)
    return f'''context.modules = [
{{ name = libpipewire-module-filter-chain
  args = {{
    node.description = "{name} equaliser"
    media.name       = "{name} equaliser"
    filter.graph = {{
{graph(gains)}
    }}
    capture.props = {{
      node.name        = "headset_eq.{key}"
      node.description = "{name} (equalised)"
      media.class      = Audio/Sink
      audio.channels   = 2
      audio.position   = [ FL FR ]
    }}
    playback.props = {{
      node.name      = "headset_eq_out.{key}"
      node.passive   = true
      target.object  = "{sink}"
      audio.channels = 2
      audio.position = [ FL FR ]
    }}
  }}
}}
]
'''


def runtime_root() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise HeadsetError("XDG_RUNTIME_DIR is not set, so the equaliser has nowhere to live")
    return Path(base) / "omarchy-headset-eq"


def state_path(address: str) -> Path | None:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    folder = Path(base) / "omarchy-headset"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return folder / f"eq-{slug(address)}.json"


def load(address: str) -> dict:
    path = state_path(address)
    if path is None:
        return {}
    try:
        saved = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    gains = saved.get("eq_gains")
    return {
        "eq_enabled": bool(saved.get("eq_enabled")),
        "eq_gains": [clamp(g) for g in gains] if isinstance(gains, list) else [0.0] * len(FREQUENCIES),
    }


def save(address: str, enabled: bool, gains: list) -> None:
    path = state_path(address)
    if path is None:
        return
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with open(temporary, "w") as stream:
            json.dump({"eq_enabled": bool(enabled), "eq_gains": [clamp(g) for g in gains]}, stream)
        os.replace(temporary, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def curve_for(name: str) -> tuple | None:
    for preset, gains in PRESETS:
        if preset == name:
            return gains
    return None


def preset_for(gains: list) -> str:
    """Which named curve this is, or Custom once a band has been moved.

    Derived from the gains rather than stored beside them, so the two can never
    disagree: a preset remembered separately says Bass over a curve the user has
    since flattened by hand.
    """
    levels = [clamp(g) for g in gains]
    for preset, curve in PRESETS:
        if levels == [clamp(g) for g in curve]:
            return preset
    return CUSTOM


class Equaliser:
    """The filter chain for one headset, and the process running it."""

    def __init__(self, address: str, name: str, on_log=None):
        self.address = address
        self.name = name or "Headset"
        self.on_log = on_log
        self.process: subprocess.Popen | None = None
        # Chains asked to stop, with the moment they stop being asked nicely.
        self.dying: list = []
        # Where the audio was going before the chain took it.
        self.previous_sink = ""
        self.gains = [0.0] * len(FREQUENCIES)
        self.enabled = False
        # A chain that has been started and has not registered its sink yet. The
        # audio is routed into it when it appears, on a later pass of the loop,
        # because waiting here would stop the helper answering anything.
        self.pending_sink = ""
        self.settle_after = 0.0
        self.settle_until = 0.0
        saved = load(address)
        if saved:
            self.gains = saved["eq_gains"]
            self.enabled = saved["eq_enabled"]

    @property
    def sink_name(self) -> str:
        return f"headset_eq.{slug(self.address)}"

    @property
    def directory(self) -> Path:
        return runtime_root() / slug(self.address)

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _log(self, text: str) -> None:
        if self.on_log:
            self.on_log(text)

    def available(self) -> bool:
        return Path(BASE_CONFIG).is_file() and binaries.find("pipewire") is not None

    def start(self, sink: str) -> None:
        """Run the chain, replacing any chain already running for this headset."""
        if not sink:
            raise HeadsetError("the headset has no audio sink to send the equaliser to")
        if not self.available():
            raise HeadsetError("PipeWire's filter chain is not installed, so there is no equaliser")
        self.stop()
        folder = self.directory
        with contextlib.suppress(OSError):
            shutil.rmtree(folder)
        try:
            (folder / "filter-chain.conf.d").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BASE_CONFIG, folder / "filter-chain.conf")
            (folder / "filter-chain.conf.d" / "10-headset.conf").write_text(
                fragment(self.name, self.address, sink, self.gains))
        except OSError as error:
            # `available` checked a moment ago, so this is the file going away
            # underneath us, or a runtime directory that cannot be written.
            raise HeadsetError(f"could not lay out the equaliser: {error}") from error
        program = binaries.find("pipewire")
        if program is None:
            # `available` said otherwise a moment ago, so the binary went away or
            # its directory stopped being root's between then and now.
            raise HeadsetError("no trusted pipewire to run the equaliser with")
        # Built from nothing rather than copied from here. A filter chain is
        # PipeWire loading modules and SPA plugins, and which ones it loads is
        # `PIPEWIRE_MODULE_DIR` and `SPA_PLUGIN_DIR`'s to say: inheriting them
        # would let whatever started the bar choose the code running inside the
        # process this plugin puts the user's audio through.
        #
        # What is passed is how the chain reaches the server it is joining, and
        # nothing about what it loads once it is there: `XDG_RUNTIME_DIR` and
        # `PIPEWIRE_RUNTIME_DIR` are where the socket lives, `PIPEWIRE_REMOTE` is
        # its name, and the config directory is the private one laid out above.
        environment = binaries.environment(("XDG_RUNTIME_DIR", "PIPEWIRE_RUNTIME_DIR",
                                            "PIPEWIRE_REMOTE", "HOME"),
                                           PIPEWIRE_CONFIG_DIR=str(folder))
        # Not in a new session: it belongs to this process and should not outlive
        # it any longer than it takes to notice.
        try:
            self.process = subprocess.Popen(
                [program, "-c", "filter-chain.conf"], env=environment,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            raise HeadsetError(f"could not start the equaliser: {error}") from error
        self._log(f"equaliser running as {self.sink_name}")

    def stop(self) -> None:
        """Ask the chain to go, and do not wait for it.

        Waiting was five seconds in the worst case, inside the helper's one event
        loop, on every curve change: the headset session is not read, panel
        clients see nothing, and new connections queue. The chain is asked to
        stop here and buried on later passes by `bury_the_dead`.
        """
        self.route_back()
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(OSError):
                self.process.terminate()
            self.dying.append((self.process, time.monotonic() + 3.0))
        self.process = None
        self.pending_sink = ""
        self.reap_strays()
        with contextlib.suppress(OSError):
            shutil.rmtree(self.directory)

    def bury_the_dead(self) -> None:
        """Collect chains asked to stop, escalating to a kill once time is up.

        Nothing here blocks. A zombie for a few hundred milliseconds costs one
        process table entry; a blocking wait costs the whole helper.
        """
        still = []
        for process, deadline in self.dying:
            if process.poll() is not None:
                continue
            if time.monotonic() >= deadline:
                with contextlib.suppress(OSError):
                    process.kill()
                # One more pass to collect it, then it is the kernel's problem.
                still.append((process, time.monotonic() + 1.0))
            else:
                still.append((process, deadline))
        self.dying = still

    def reap_strays(self) -> None:
        """Kill a chain left behind by a helper that was killed outright.

        Matched on this headset's own private configuration directory and on the
        executable being PipeWire, never on a substring of a command line: that
        is how a process nobody meant to touch gets killed.
        """
        marker = str(self.directory)
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if (entry / "exe").resolve().name != "pipewire":
                    continue
                environ = (entry / "environ").read_bytes()
            except (OSError, RuntimeError):
                continue
            if f"PIPEWIRE_CONFIG_DIR={marker}".encode() not in environ:
                continue
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(entry.name), 15)
                self._log(f"stopped an equaliser left behind by a previous run ({entry.name})")

    # ------------------------------------------------------------------ routing
    #
    # A filter chain nothing plays into is a process doing nothing. Audio has to
    # be sent to its sink, which means moving the default and whatever is already
    # playing, and putting both back afterwards.

    def _pactl(self, arguments: list) -> str:
        from . import audio
        return audio.run_pactl(arguments)

    def default_sink(self) -> str:
        with contextlib.suppress(HeadsetError):
            return self._pactl(["get-default-sink"]).strip()
        return ""

    def streams_on(self, sink: str) -> list:
        """The ids of what is currently playing into one sink."""
        found = []
        # By index rather than by node name: the name is not printed the same way
        # across PipeWire versions, and the index always is.
        wanted = self._sink_index(sink)
        if not wanted:
            return found
        with contextlib.suppress(HeadsetError):
            current = None
            for line in self._pactl(["list", "sink-inputs"]).splitlines():
                stripped = line.strip()
                if stripped.startswith("Sink Input #"):
                    current = stripped.split("#", 1)[1].strip()
                elif current and stripped.startswith("Sink: "):
                    if stripped.split("Sink: ", 1)[1].strip() == wanted:
                        found.append(current)
                    current = None
        return found

    def _sink_index(self, name: str) -> str:
        with contextlib.suppress(HeadsetError):
            for line in self._pactl(["list", "sinks", "short"]).splitlines():
                fields = line.split()
                if len(fields) > 1 and fields[1] == name:
                    return fields[0]
        return ""

    def route_into(self, headset_sink: str) -> None:
        """Send the default and everything playing through the chain."""
        if self.previous_sink == "":
            current = self.default_sink()
            # Never remember our own sink as the thing to go back to.
            self.previous_sink = current if current != self.sink_name else headset_sink
        moving = self.streams_on(headset_sink)
        with contextlib.suppress(HeadsetError):
            self._pactl(["set-default-sink", self.sink_name])
        for stream in moving:
            with contextlib.suppress(HeadsetError):
                self._pactl(["move-sink-input", stream, self.sink_name])

    def route_back(self) -> None:
        """Put the default and anything we moved back where it was."""
        target = self.previous_sink
        self.previous_sink = ""
        if not target:
            return
        moving = self.streams_on(self.sink_name)
        with contextlib.suppress(HeadsetError):
            if self.default_sink() == self.sink_name:
                self._pactl(["set-default-sink", target])
        for stream in moving:
            with contextlib.suppress(HeadsetError):
                self._pactl(["move-sink-input", stream, target])

    def apply(self, sink: str, enabled: bool | None = None, gains: list | None = None) -> None:
        if gains is not None and not isinstance(gains, (list, tuple)):
            # It arrives as JSON. A string would be read one character per band,
            # and a number is not iterable at all.
            raise HeadsetError("the equaliser needs a list of gains")
        if gains is not None:
            self.gains = [clamp(g) for g in gains][:len(FREQUENCIES)]
            self.gains += [0.0] * (len(FREQUENCIES) - len(self.gains))
        if enabled is not None:
            self.enabled = bool(enabled)
        save(self.address, self.enabled, self.gains)
        # The chain's graph is fixed at startup, so a changed curve is a restart.
        # It is one small client process and the restart is not audible.
        if self.enabled:
            self.start(sink)
            self.pending_sink = sink
            self.settle_after = 0.0
            self.settle_until = time.monotonic() + 5.0
            self.settle()
        else:
            self.pending_sink = ""
            self.route_back()
            self.stop()

    def settle(self) -> None:
        """Send the audio into the chain, once the chain has a sink to send to.

        A filter chain takes a moment to register with PipeWire. Waiting for it
        in place is the obvious thing and the wrong one: this runs inside the
        helper's one event loop, so a wait here is a wait for everything, and a
        curve changed on a slider would stall the headset for seconds at a time.
        So the routing is retried on later passes instead, and nothing blocks.
        """
        if not self.pending_sink:
            return
        now = time.monotonic()
        if now < self.settle_after:
            return
        self.settle_after = now + 0.2
        if not self.running or now > self.settle_until:
            # The chain died, or never came up. Leave the audio where it is
            # rather than sending it into something that is not there.
            self.pending_sink = ""
            return
        if not self._sink_index(self.sink_name):
            return
        target, self.pending_sink = self.pending_sink, ""
        self.route_into(target)

    def state(self) -> dict:
        return {"eq_enabled": self.enabled and self.running,
                "eq_gains": list(self.gains),
                "eq_preset": preset_for(self.gains),
                "eq_presets": [name for name, _ in PRESETS],
                "eq_sink": self.sink_name if self.running else ""}
