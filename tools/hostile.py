#!/usr/bin/env python3
"""Run the Python suite on a machine that has none of what this plugin uses.

CI has broken twice on tests that quietly depended on the machine they were
written on: once on `socket.AF_BLUETOOTH`, which a Python built without
Bluetooth does not have, and once on `/usr/share/pipewire/filter-chain.conf`,
which a runner with no PipeWire does not have. Both passed here and failed
there, which is the worst shape a test can have.

So the suite is run twice: once as itself, and once with every one of those
taken away. A test that needs a real PipeWire, a real `pipewire` binary, a real
`pactl` or a real Bluetooth socket fails here rather than on a push.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True

from headset import equaliser, session  # noqa: E402


def no_binary(name, *args, **kwargs):
    return None


def no_process(*args, **kwargs):
    raise FileNotFoundError(2, "No such file or directory", (args[0] or [""])[0])


def main() -> int:
    suite = unittest.TestLoader().discover(str(ROOT / "tests"))
    with patch.object(equaliser, "BASE_CONFIG", "/nonexistent/filter-chain.conf"), \
         patch.object(session, "AF_BLUETOOTH", None), \
         patch.object(session, "BTPROTO_RFCOMM", None), \
         patch("shutil.which", no_binary), \
         patch("subprocess.Popen", no_process), \
         patch("subprocess.run", no_process):
        result = unittest.TextTestRunner(verbosity=0).run(suite)
    if result.wasSuccessful():
        print("hostile environment: the suite needs nothing from this machine")
        return 0
    for test, _ in result.errors + result.failures:
        print(f"depends on this machine: {test.id()}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
