"""What every headset has, whether or not a driver knows how to talk to it.

None of this is vendor protocol. PipeWire already knows which codec is carrying
the audio and which others the headset offered, and BlueZ already knows the
battery. A headset with no driver is not a headset nothing can be said about, so
this is the tier that is always available and the vendor driver is the extra.

The one thing PipeWire's own QML API does not expose is the card, and profiles
live on the card, so the listing and the switch go through `pactl`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import binaries
from .errors import HeadsetError

# "a2dp-sink-sbc: High Fidelity Playback (A2DP Sink, codec SBC) (sinks: 1, ...)"
PROFILE = re.compile(
    r"^\s+(?P<id>[A-Za-z0-9_+-]+): (?P<label>.+?)"
    r" \(sinks: \d+, sources: \d+, priority: (?P<priority>\d+), available: (?P<available>\w+)\)\s*$"
)
CODEC = re.compile(r"codec (?P<codec>[A-Za-z0-9_ +-]+?)\)")

# Where libpulse looks for a PulseAudio running in system mode, after the
# per-user socket and before anything on the network.
SYSTEM_SOCKET = "/var/run/pulse/native"

HIGH_FIDELITY = "a2dp"
HEADSET = "headset"
OFF = "off"


def profile_kind(profile_id: str, label: str) -> str:
    text = f"{profile_id} {label}".lower()
    if profile_id == "off":
        return OFF
    if "a2dp" in text:
        return HIGH_FIDELITY
    if "hsp" in text or "hfp" in text or "head-unit" in text or "headset" in text:
        return HEADSET
    return "other"


def parse_cards(text: str) -> list[dict]:
    """Every card in `pactl list cards` output, with its profiles."""
    cards = []
    card: dict | None = None
    section = ""
    for line in text.splitlines():
        if line.startswith("Card #"):
            card = {"name": "", "description": "", "address": "", "form_factor": "",
                    "active_profile": "", "profiles": []}
            cards.append(card)
            section = ""
            continue
        if card is None:
            continue
        stripped = line.strip()
        if stripped in ("Properties:", "Profiles:", "Ports:"):
            section = stripped.rstrip(":")
            continue
        if stripped.startswith("Name: "):
            card["name"] = stripped[6:].strip()
            continue
        if stripped.startswith("Active Profile: "):
            card["active_profile"] = stripped[16:].strip()
            section = ""
            continue
        if section == "Properties":
            key, _, value = stripped.partition(" = ")
            value = value.strip().strip('"')
            if key == "device.description":
                card["description"] = value
            elif key == "api.bluez5.address":
                card["address"] = value
            elif key == "device.form_factor":
                card["form_factor"] = value
            continue
        if section == "Profiles":
            match = PROFILE.match(line)
            if not match:
                continue
            label = match.group("label")
            codec = CODEC.search(label)
            card["profiles"].append({
                "id": match.group("id"),
                "label": label,
                "codec": codec.group("codec").strip() if codec else "",
                "kind": profile_kind(match.group("id"), label),
                "available": match.group("available") == "yes",
                "priority": int(match.group("priority")),
            })
    return cards


def shortest(directory: str) -> str:
    """A directory, or its canonical form where that is shorter.

    What libpulse does to the same path before it builds a socket address out of
    it, and for a reason worth copying rather than improving on: a Unix socket
    address holds about 108 bytes, so a long alias to a short directory is a
    connection that fails here while libpulse's own lookup would have made it.
    """
    try:
        # Strictly, so a path is only rewritten when every part of it is really
        # there. Without that, `resolve` folds `..` across a component that does
        # not exist, and `/run/absent/../user` becomes `/run/user`: an
        # unreachable socket quietly turned into a reachable one somewhere else.
        # libpulse keeps the original when realpath fails, and so does this.
        canonical = str(Path(directory).resolve(strict=True))
    except OSError:
        return directory
    # By what a socket address actually holds, which is bytes. Comparing
    # characters makes a short ASCII alias lose to a canonical path of fewer but
    # longer characters, and the address that gets built is the one over the
    # limit this is here to stay under.
    if len(os.fsencode(canonical)) < len(os.fsencode(directory)):
        return canonical
    return directory


def pulse_server() -> str | None:
    """The servers to name on pactl's own command line, or None if none can be named.

    Naming it is what turns autospawn off, and autospawn is the one path by which
    a configuration file can still name a program for libpulse to execute:
    `client.conf` carries `autospawn` and `daemon-binary`, and libpulse does
    `if (server) c->conf->autospawn = false` before it reads either of them.

    Measured rather than reasoned. With a `client.conf` carrying `autospawn =
    yes` and a `daemon-binary`, pactl pointed at an unreachable server ran that
    binary; pointed at the same unreachable server with `--server`, it did not.

    The other way to close it is to pin `PULSE_CLIENTCONFIG` at the system file,
    which works by throwing away every other setting the user has. That is a
    worse trade than the thing it fixes.

    This names a server every time rather than only when the local socket is
    there. An earlier version checked, so as not to override a `default-server`
    that a machine had set in its own client.conf, and that check put the hole
    back exactly where it mattered: a socket that is missing is the case where
    the connection fails, and a failed connection is what autospawn is for. The
    cost is that a machine whose only pointer to a non-local server lives in
    client.conf has to name it in `PULSE_SERVER` instead, which is one line and
    is carried through to here.
    """
    named = os.environ.get("PULSE_SERVER")
    if named:
        # The user's own, passed through whole: it was already read as a list by
        # libpulse before this, and it is theirs to write.
        return named
    runtime = os.environ.get("PULSE_RUNTIME_PATH")
    if not runtime:
        base = os.environ.get("XDG_RUNTIME_DIR")
        if not base:
            return None
        runtime = str(Path(base) / "pulse")
    runtime = shortest(runtime)
    # Both, in libpulse's own order. It tries the per-user socket and then the
    # system-wide one, so naming only the first would take PulseAudio in system
    # mode away from a machine that had it, which is a regression about
    # something else again.
    per_user = Path(runtime) / "native"
    if any(character.isspace() for character in str(per_user)):
        # This argument is a whitespace-separated list and libpulse's parser has
        # no quoting, so a space in the path is not a path with a space in it: it
        # is a second address. A runtime directory named `/tmp/x tcp:10.0.0.1`
        # would point pactl at a machine on the network.
        raise HeadsetError("the runtime directory has a space in it, which pactl "
                           "would read as the name of a second server")
    return f"unix:{per_user} unix:{SYSTEM_SOCKET}"


def run_pactl(arguments: list[str]) -> str:
    program = binaries.find("pactl")
    if program is None:
        raise HeadsetError("pactl is not installed, so the audio profile cannot be read")
    # In the C locale, because every heading and field name below is matched in
    # English. Under a translated locale the parser finds no card at all and the
    # audio rows vanish from a headset that has them. Said outright rather than
    # left to an environment that happens to carry no locale: this parser depends
    # on it, and depending on it by accident is how it comes back.
    #
    # `LANGUAGE` used to be emptied here, because gettext lets it override
    # `LC_ALL` for messages. It is not emptied now, it is simply never passed.
    #
    # `XDG_RUNTIME_DIR` is how pactl finds the server's socket, and `HOME` is
    # where it looks for the cookie if it is ever talking to a PulseAudio that
    # wants one rather than to PipeWire. `PULSE_COOKIE` is that cookie named
    # outright, for a machine pointed at a server that is not the local one;
    # it was inherited before this, and such a machine should not lose its audio
    # rows to a change about something else.
    #
    # `PULSE_CLIENTCONFIG` is deliberately not among them, though it is the
    # third variable in the same family. It names the client.conf to read, and a
    # client.conf carries `autospawn` and `daemon-binary`, so honouring it would
    # let one variable name a program for libpulse to run. That is the hole this
    # module is here to close, arriving by the other door.
    #
    # `~/.config/pulse/client.conf` carries those same two keys and is read
    # whatever this passes: dropping `HOME` does not stop it, measured, because
    # libpulse falls back to the passwd entry to find the home directory. What
    # stops it is naming the server on the command line; see `pulse_server`.
    environment = binaries.environment(("XDG_RUNTIME_DIR", "HOME", "PULSE_COOKIE"),
                                       LC_ALL="C", LANG="C")
    # `PULSE_SERVER` is read above and named on the command line instead of being
    # passed down, because on the command line it also turns autospawn off.
    server = pulse_server()
    if server is None:
        # Nothing can be named, so nothing is run: leaving the server for
        # libpulse to choose is the branch that lets a client.conf autospawn a
        # daemon-binary. Everything else in the helper needs this directory too.
        raise HeadsetError("XDG_RUNTIME_DIR is not set, so there is no audio server to ask")
    named = ["--server", server]
    try:
        done = binaries.run([program, *named, *arguments], timeout=10, env=environment)
    except FileNotFoundError as error:
        raise HeadsetError("pactl is not installed, so the audio profile cannot be read") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise HeadsetError(f"could not ask PipeWire about the audio profile: {error}") from error
    if done.returncode != 0:
        raise HeadsetError((done.stderr or "pactl failed").strip().splitlines()[0])
    return done.stdout


def card_for(address: str, text: str | None = None) -> dict | None:
    """The card belonging to one Bluetooth address, or None if it has no audio."""
    if text is None:
        text = run_pactl(["list", "cards"])
    wanted = address.strip().upper()
    for card in parse_cards(text):
        if card["address"].upper() == wanted:
            return card
    return None


def summary(address: str, text: str | None = None) -> dict:
    """What the panel needs to draw the rows every headset has."""
    card = card_for(address, text)
    if card is None:
        return {"card": "", "profiles": [], "codecs": [], "active_profile": "",
                "active_codec": "", "mode": "", "has_microphone": False,
                "best_listening": "", "headset_profile": "", "description": "",
                "form_factor": ""}
    active = next((p for p in card["profiles"] if p["id"] == card["active_profile"]), None)
    codecs = [p for p in card["profiles"] if p["kind"] == HIGH_FIDELITY and p["available"]]
    # Only the best headset profile is worth offering; the others differ by a
    # codec nobody chooses a call by.
    talking = sorted((p for p in card["profiles"] if p["kind"] == HEADSET and p["available"]),
                     key=lambda p: -p["priority"])
    listening = sorted((p for p in card["profiles"] if p["kind"] == HIGH_FIDELITY and p["available"]),
                       key=lambda p: -p["priority"])
    return {
        "card": card["name"],
        "best_listening": listening[0]["id"] if listening else "",
        "description": card["description"],
        "form_factor": card["form_factor"],
        "active_profile": card["active_profile"],
        "active_codec": active["codec"] if active else "",
        "mode": active["kind"] if active else "",
        "codecs": [{"value": p["id"], "label": p["codec"] or p["label"]} for p in codecs],
        "headset_profile": talking[0]["id"] if talking else "",
        "has_microphone": bool(talking),
        "profiles": card["profiles"],
    }


def set_profile(card: str, profile: str) -> None:
    if not card or not profile:
        raise HeadsetError("both a card and a profile are needed")
    run_pactl(["set-card-profile", card, profile])


# A profile switch tears the audio link down and brings it back, so a reading
# taken immediately afterwards is the old one, and reporting it as the new state
# is a lie the caller cannot catch. A fixed wait is no answer either: coming back
# from the headset profile takes longer than going to it, and a wait long enough
# for the slow case is dead time in the fast one. So: wait for the state.
SETTLE_POLL = 0.25
SETTLE_LIMIT = 6.0


def wait_for_profile(address: str, profile: str) -> dict:
    """Read until the card reports the profile that was asked for, or time out."""
    deadline = time.monotonic() + SETTLE_LIMIT
    latest = summary(address)
    while time.monotonic() < deadline:
        if latest.get("active_profile") == profile:
            return latest
        time.sleep(SETTLE_POLL)
        latest = summary(address)
    return latest


# What PipeWire calls a thing you wear. A speaker has a card too, and a keyboard
# has none at all.
HEADSET_FORM_FACTORS = ("headset", "headphone")


def headset_cards(text: str | None = None) -> list[dict]:
    """Connected Bluetooth cards that are headsets, not speakers or anything else."""
    if text is None:
        text = run_pactl(["list", "cards"])
    return [card for card in parse_cards(text)
            if card["address"] and card["form_factor"] in HEADSET_FORM_FACTORS]


def any_connected_audio_device() -> str:
    """The first connected device that has an audio card, driver or no driver.

    Deliberately not the driver-filtered lookup the vendor commands use: this
    tier exists for headsets nothing claims, and resolving through a driver list
    refused the very devices it is for.
    """
    for card in headset_cards():
        return card["address"]
    raise HeadsetError("no connected Bluetooth headset")


def cmd_audio(args) -> int:
    address = args.address or any_connected_audio_device()
    if args.profile:
        found = card_for(address)
        if found is None:
            raise HeadsetError("this device has no audio card")
        set_profile(found["name"], args.profile)
        settled = wait_for_profile(address, args.profile)
        print(json.dumps(settled, indent=2 if args.pretty else None, sort_keys=True))
        if settled.get("active_profile") != args.profile:
            # Exiting zero here reports a switch that did not happen. PipeWire
            # can hold or restore a profile against us, and the caller has no
            # other way to find out.
            raise HeadsetError(
                f"the profile did not change to {args.profile}; it is "
                f"{settled.get('active_profile') or 'unknown'}")
        return 0
    print(json.dumps(summary(address), indent=2 if args.pretty else None, sort_keys=True))
    return 0
