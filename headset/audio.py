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

from .errors import HeadsetError

# "a2dp-sink-sbc: High Fidelity Playback (A2DP Sink, codec SBC) (sinks: 1, ...)"
PROFILE = re.compile(
    r"^\s+(?P<id>[A-Za-z0-9_+-]+): (?P<label>.+?)"
    r" \(sinks: \d+, sources: \d+, priority: (?P<priority>\d+), available: (?P<available>\w+)\)\s*$"
)
CODEC = re.compile(r"codec (?P<codec>[A-Za-z0-9_ +-]+?)\)")

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


def run_pactl(arguments: list[str]) -> str:
    # In the C locale, because every heading and field name below is matched in
    # English. Under a translated locale the parser finds no card at all and the
    # audio rows vanish from a headset that has them.
    environment = dict(os.environ, LC_ALL="C", LANG="C", LANGUAGE="")
    try:
        done = subprocess.run(["pactl", *arguments], capture_output=True, text=True,
                              timeout=10, env=environment)
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


def any_connected_audio_device() -> str:
    """The first connected device that has an audio card, driver or no driver.

    Deliberately not the driver-filtered lookup the vendor commands use: this
    tier exists for headsets nothing claims, and resolving through a driver list
    refused the very devices it is for.
    """
    for card in parse_cards(run_pactl(["list", "cards"])):
        if card["address"]:
            return card["address"]
    raise HeadsetError("no connected Bluetooth audio device")


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
