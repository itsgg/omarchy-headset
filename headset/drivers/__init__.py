"""Driver registry.

A driver claims a device by name, says which records to read from it, and turns a
feature and a value into bytes. Everything above this package is device-agnostic:
the session speaks a framing, the server speaks JSON, and the panel draws whatever
features the driver reported. Adding a headset is a module here.
"""
from __future__ import annotations

from . import fastpair, sony_mdr

DRIVERS = (sony_mdr.DRIVER, fastpair.DRIVER)


def for_device(name: str, address: str = ""):
    """The driver for this device, or None.

    A named driver first, because it knows the most about the model it names.
    Failing that, the fallback, which is identified by a service the device
    advertises rather than by any name and so proves itself by opening. That is
    what lets a headset nobody has written a driver for still be talked to.
    """
    for driver in DRIVERS:
        if driver.claims(name):
            return driver
    for driver in DRIVERS:
        if driver.fallback:
            return driver
    return None


def by_id(driver_id: str):
    for driver in DRIVERS:
        if driver.id == driver_id:
            return driver
    return None
