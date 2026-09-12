"""Driver registry.

A driver claims a device by name, says which records to read from it, and turns a
feature and a value into bytes. Everything above this package is device-agnostic:
the session speaks a framing, the server speaks JSON, and the panel draws whatever
features the driver reported. Adding a headset is a module here.
"""
from __future__ import annotations

from . import sony_mdr

DRIVERS = (sony_mdr.DRIVER,)


def for_device(name: str, address: str = ""):
    """The first driver that claims this device, or None."""
    for driver in DRIVERS:
        if driver.claims(name):
            return driver
    return None


def by_id(driver_id: str):
    for driver in DRIVERS:
        if driver.id == driver_id:
            return driver
    return None
