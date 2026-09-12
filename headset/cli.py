"""The command line: what a terminal and the panel both go through.

    headsetctl devices                  every connected headset a driver claims
    headsetctl status [address]         one JSON object and exit
    headsetctl watch [address]          JSON lines, commands on stdin
    headsetctl get <feature>            one value
    headsetctl set <feature> <value>    write one feature and report the outcome
    headsetctl toggle <feature>

With no address, the first connected headset a driver claims is used.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

from . import audio, client, drivers, server
from .device import Device
from .errors import HeadsetError


def connected_devices() -> list[dict]:
    """Connected Bluetooth devices, from bluez, that some driver claims.

    The panel does not use this: Quickshell already knows every bluez device and
    hands the helper an address. It exists so the CLI can be used without one.
    """
    try:
        listing = subprocess.run(["bluetoothctl", "devices", "Connected"],
                                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise HeadsetError(f"could not ask bluez which devices are connected: {error}") from error
    found = []
    for line in listing.stdout.splitlines():
        match = re.match(r"Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+(.*)", line.strip())
        if not match:
            continue
        address, name = match.group(1), match.group(2).strip()
        driver = drivers.for_device(name, address)
        if driver is not None:
            found.append({"address": address, "name": name, "driver": driver.id})
    return found


def resolve(address: str | None, name: str | None) -> tuple[str, str]:
    if address:
        return address, name or ""
    found = connected_devices()
    if not found:
        raise HeadsetError("no connected headset that this helper knows how to talk to")
    return found[0]["address"], found[0]["name"]


def one_shot(address: str, name: str, driver_id: str = "") -> Device:
    driver = drivers.by_id(driver_id) if driver_id else drivers.for_device(name, address)
    if driver is None:
        raise HeadsetError(f"no driver claims {name or address!r}")
    device = Device(address=address, driver=driver, name=name)
    device.open()
    device.read()
    return device


def cmd_status(args) -> int:
    address, name = resolve(args.address, args.name)
    # Ask the running helper before opening a session of our own, which the
    # headset would refuse while the bar widget holds the only one it allows.
    published = client.state(address)
    if published is not None:
        print(json.dumps(published, indent=2 if args.pretty else None, sort_keys=True))
        return 0
    try:
        device = one_shot(address, name, args.driver)
    except HeadsetError as error:
        print(json.dumps({"connected": False, "device": {"address": address, "name": name},
                          "error": str(error)}, indent=2 if args.pretty else None))
        return 0
    try:
        payload = {
            "connected": True,
            "device": {"address": address, "name": name,
                       "driver": device.driver.id, "channel": device.channel},
            "support": device.support,
            "state": device.state,
        }
    finally:
        device.close()
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))
    return 0


def cmd_devices(args) -> int:
    found = connected_devices()
    if args.pretty or not found:
        print(json.dumps(found, indent=2))
        return 0
    for entry in found:
        print(f"{entry['address']}  {entry['name']}  ({entry['driver']})")
    return 0


def cmd_watch(args) -> int:
    address, name = resolve(args.address, args.name)
    if drivers.by_id(args.driver) is None if args.driver else drivers.for_device(name, address) is None:
        # Say it on stdout and exit cleanly. Raising here exited with a message on
        # stderr that the panel could not read, so it respawned this for ever
        # against a headset nothing here knows how to talk to.
        print(json.dumps({
            "type": "state", "unsupported": True, "connected": False,
            "device": {"address": address, "name": name, "driver": "", "channel": 0},
            "state": {}, "controls": {}, "support": {}, "pending": [], "ignored": [],
            "error": f"no driver here knows how to talk to {name or address}",
        }), flush=True)
        return 0
    return server.watch(address, name, args.driver)


def _coerce(raw: str):
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def cmd_set(args) -> int:
    address, name = resolve(args.address, args.name)
    published = client.state(address)
    if published is not None:
        current = (published.get("state") or {}).get(args.feature)
        value = _coerce(args.value) if args.value is not None else not bool(current)
        answer = client.send(address, {"set": {args.feature: value}})
        if answer is None:
            raise HeadsetError("the running helper stopped responding")
        if answer.get("error"):
            raise HeadsetError(answer["error"])
        print(json.dumps({"feature": args.feature, "value": value, "via": "running helper"}))
        return 0
    device = one_shot(address, name, args.driver)
    try:
        value = _coerce(args.value) if args.value is not None else not bool(device.state.get(args.feature))
        control = device.driver.control(args.feature)
        if control is None:
            raise HeadsetError(f"unknown feature {args.feature!r}")
        if not control.honoured:
            raise HeadsetError(f"{args.feature.replace('_', ' ')} is read-only on this headset")
        if control.available is not None:
            merged = dict(device.state)
            merged[args.feature] = value
            if not control.available(merged):
                raise HeadsetError(f"{args.feature.replace('_', ' ')} only applies in ambient mode")
        status = device.write_sync(args.feature, {args.feature: value})
        print(json.dumps({"feature": args.feature, "value": value, "acknowledged": status}))
        if status != "acked":
            return 1
        # Saying so is the point. An acknowledgement means the frame arrived, and
        # this session will keep reporting the old value for a while yet.
        sys.stderr.write("acknowledged; confirm with tools/verify.py, not by reading back\n")
    finally:
        device.close()
    return 0


def cmd_get(args) -> int:
    address, name = resolve(args.address, args.name)
    published = client.state(address)
    if published is not None:
        values = published.get("state") or {}
        if args.feature not in values:
            raise HeadsetError(f"this headset did not report {args.feature.replace('_', ' ')}")
        print(json.dumps(values[args.feature]))
        return 0
    device = one_shot(address, name, args.driver)
    try:
        if args.feature not in device.state:
            raise HeadsetError(f"this headset did not report {args.feature.replace('_', ' ')}")
        print(json.dumps(device.state[args.feature]))
    finally:
        device.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    # The shared options go on a parent parser as well as the top level, so
    # `headsetctl status --pretty` works as readily as `headsetctl --pretty status`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--address", help="Bluetooth address; the first connected headset by default")
    common.add_argument("--name", help="the device name, which decides the driver")
    common.add_argument("--driver", default="", help="force a driver by id")
    common.add_argument("--pretty", action="store_true", help="indent JSON output")

    parser = argparse.ArgumentParser(prog="headsetctl", description=__doc__, parents=[common],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser("devices", parents=[common],
                    help="connected headsets a driver claims").set_defaults(run=cmd_devices)
    status = subs.add_parser("status", parents=[common], help="read everything once and exit")
    status.add_argument("positional_address", nargs="?", help=argparse.SUPPRESS)
    status.set_defaults(run=cmd_status)
    watch = subs.add_parser("watch", parents=[common],
                            help="stream state as JSON lines, take commands on stdin")
    watch.add_argument("positional_address", nargs="?", help=argparse.SUPPRESS)
    watch.set_defaults(run=cmd_watch)
    setter = subs.add_parser("set", parents=[common], help="write one feature")
    setter.add_argument("feature")
    setter.add_argument("value", nargs="?")
    setter.set_defaults(run=cmd_set)
    toggler = subs.add_parser("toggle", parents=[common], help="flip one on/off feature")
    toggler.add_argument("feature")
    toggler.set_defaults(run=cmd_set, value=None)
    getter = subs.add_parser("get", parents=[common], help="read one feature")
    getter.add_argument("feature")
    getter.set_defaults(run=cmd_get)
    # The tier every headset has, driver or no driver.
    sound = subs.add_parser("audio", parents=[common],
                            help="codec and connection mode, for any headset")
    sound.add_argument("profile", nargs="?", help="switch to this profile first")
    sound.set_defaults(run=audio.cmd_audio)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    if getattr(args, "positional_address", None) and not args.address:
        args.address = args.positional_address
    try:
        return args.run(args)
    except HeadsetError as error:
        sys.stderr.write(f"headsetctl: {error}\n")
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
