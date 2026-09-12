"""The one exception the CLI turns into an exit code and a message."""


class HeadsetError(Exception):
    """Something the user can act on: no device, no permission, a refused write."""


class UnsupportedDevice(HeadsetError):
    """This headset will never work with this driver, so retrying is pointless."""
