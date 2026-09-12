#!/usr/bin/env python3
"""Find out which controls the hardware actually honours.

The device acknowledges writes it intends to discard, and then reports the old value
to the session that wrote it for up to a minute. Neither the acknowledgement nor a
read-back on the same session proves anything. So for each control this:

    reads the value, writes a different one, drops the session,
    reconnects, reads again, and restores the original

A control whose value moved is honoured. One that came back unchanged after a fresh
session read it is acknowledged and ignored, and the panel must offer it read-only
or not at all.

    tools/verify.py <address> [control ...]

Nothing here powers the headset off; that control is verified by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from headset import drivers  # noqa: E402
from headset.device import Device  # noqa: E402
from headset.errors import HeadsetError  # noqa: E402

SETTLE = 1.5

# value to write, and the state keys to compare afterwards
TRIALS = {
    "noise": ({"noise": "anc"}, ("noise",)),
    "ambient_level": ({"noise": "ambient", "ambient_level": 8}, ("ambient_level",)),
    "focus_on_voice": ({"noise": "ambient", "focus_on_voice": True}, ("focus_on_voice",)),
    "eq_bands": ({"eq_bands": [2, -3, 4, -1, 6]}, ("eq_bands",)),
    "eq_clear_bass": ({"eq_clear_bass": 5}, ("eq_clear_bass",)),
    "eq_preset": ("vocal", ("eq_preset",)),
    "speak_to_chat": (True, ("speak_to_chat",)),
    "speak_to_chat_sensitivity": ({"speak_to_chat_sensitivity": "high"}, ("speak_to_chat_sensitivity",)),
    "speak_to_chat_timeout": ({"speak_to_chat_timeout": "long"}, ("speak_to_chat_timeout",)),
    "pause_on_removal": (False, ("pause_on_removal",)),
    "voice_guidance": (False, ("voice_guidance",)),
    "dsee": (True, ("dsee",)),
    "touch_sensor": (False, ("touch_sensor",)),
}

# Trials whose write is only accepted while the device is in another mode, and the
# control that gets it there.
NEEDS_AMBIENT = ("ambient_level", "focus_on_voice")


def connect(address: str, name: str) -> Device:
    driver = drivers.for_device(name) or drivers.by_id("sony-mdr")
    device = Device(address=address, driver=driver, name=name,
                    on_log=lambda text: print(f"      . {text}"))
    device.open()
    device.read()
    return device


def restore_steps(control: str, baseline: dict) -> list:
    """The writes that put the original reading back, in order.

    Ambient level and focus-on-voice are discarded unless the device is in ambient
    mode, so restoring them means passing through ambient mode first and only then
    setting the original mode. One combined write silently loses two of the three.
    """
    if control in ("noise", "ambient_level", "focus_on_voice"):
        target = {key: baseline[key] for key in ("noise", "ambient_level", "focus_on_voice")
                  if key in baseline}
        if target.get("noise") == "ambient":
            return [("noise", target)]
        return [("noise", dict(target, noise="ambient")), ("noise", target)]
    return [(control, _restore_value(control, baseline))]


def _restore_value(control: str, baseline: dict):
    if control in ("eq_bands", "eq_clear_bass"):
        return {key: baseline[key] for key in ("eq_bands", "eq_clear_bass") if key in baseline}
    if control.startswith("speak_to_chat_"):
        return {key: baseline[key] for key in
                ("speak_to_chat_sensitivity", "speak_to_chat_timeout") if key in baseline}
    return baseline.get(control)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("controls", nargs="*")
    parser.add_argument("--name", default="WH-1000XM5")
    parser.add_argument("--json", dest="as_json", action="store_true")
    args = parser.parse_args()

    device = connect(args.address, args.name)
    baseline = dict(device.state)
    print(f"connected to {args.name} on channel {device.channel}, firmware "
          f"{baseline.get('firmware', '?')}")
    print(f"records present: "
          f"{', '.join(sorted(r.id for r in device.driver.records if device.support.get(r.id)))}")
    missing = sorted(r.id for r in device.driver.records if not device.support.get(r.id))
    if missing:
        print(f"records absent : {', '.join(missing)}")
    print()

    wanted = args.controls or [c for c in TRIALS if device.supports(c)]
    results = {}

    for control in wanted:
        if control not in TRIALS:
            print(f"{control:26} SKIP  no trial defined")
            continue
        if not device.supports(control):
            results[control] = "unsupported"
            print(f"{control:26} ABSENT the record it belongs to never answered")
            continue

        value, keys = TRIALS[control]
        before = {key: device.state.get(key) for key in keys}
        record = device.driver.control(control).record

        try:
            status = device.write_sync(control, value)
        except HeadsetError as error:
            results[control] = f"refused: {error}"
            print(f"{control:26} ERROR {error}")
            continue

        device.close()
        time.sleep(SETTLE)
        device = connect(args.address, args.name)
        after = {key: device.state.get(key) for key in keys}

        moved = any(after.get(key) != before.get(key) for key in keys)
        results[control] = "honoured" if moved else "ignored"
        arrow = "->" if moved else "  "
        print(f"{control:26} {'HONOURED' if moved else 'IGNORED ':8} "
              f"ack={status:8} {before} {arrow} {after}")

        # Put it back, whatever happened.
        for step_control, target in restore_steps(control, baseline):
            if target is None:
                continue
            try:
                device.write_sync(step_control, target)
            except HeadsetError as error:
                print(f"      ! could not restore {control}: {error}")
        device.close()
        time.sleep(SETTLE)
        device = connect(args.address, args.name)

    print()
    final = {key: device.state.get(key) for key in sorted(baseline) if key in baseline}
    drifted = {key: (baseline[key], final.get(key)) for key in baseline
               if final.get(key) != baseline[key]}
    print("restored to the original reading" if not drifted else f"still different: {drifted}")
    device.close()

    if args.as_json:
        print(json.dumps({"results": results, "baseline": baseline, "final": final}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except HeadsetError as error:
        print(f"verify: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
