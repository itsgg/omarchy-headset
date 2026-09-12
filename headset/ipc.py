"""Where the socket lives, and how exactly one process comes to own it.

Election runs through an exclusive lock, not through the socket file. Two processes
racing on the socket can both decide it is stale and both replace it, and then both
believe they own the headset. A lock has one holder by construction, and the winner
is the only process that ever unlinks anything.

The socket is a file under `$XDG_RUNTIME_DIR` with the directory at 0700, rather
than an abstract socket. An abstract socket has no permissions at all: every process
on the machine could read the state and send commands.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import socket
import stat
from pathlib import Path

from .errors import HeadsetError


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise HeadsetError("XDG_RUNTIME_DIR is not set, so there is nowhere private to put the socket")
    try:
        info = os.stat(base)
    except OSError as error:
        raise HeadsetError(f"cannot use {base}: {error.strerror or error}") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise HeadsetError(f"{base} is not a directory you own")
    folder = Path(base) / "omarchy-headset"
    try:
        folder.mkdir(mode=0o700, exist_ok=True)
        folder.chmod(0o700)
        if folder.stat().st_uid != os.getuid():
            raise HeadsetError(f"{folder} is not owned by you")
    except OSError as error:
        raise HeadsetError(f"cannot prepare {folder}: {error.strerror or error}") from error
    return folder


def _slug(address: str) -> str:
    return address.replace(":", "").lower() or "default"


def socket_path(address: str) -> Path:
    return runtime_dir() / f"{_slug(address)}.sock"


def lock_path(address: str) -> Path:
    return runtime_dir() / f"{_slug(address)}.lock"


def code_version() -> str:
    """A digest of this package's files, so a follower can spot a stale owner.

    After an edit or a plugin update the running owner is the old code. The version
    is how the new process notices and asks it to stand down.
    """
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        with contextlib.suppress(OSError):
            info = path.stat()
            digest.update(f"{path.name}:{info.st_size}:{info.st_mtime_ns}".encode())
    return digest.hexdigest()[:16]


class Election:
    """Hold this to be the one process talking to the headset."""

    def __init__(self, address: str):
        self.address = address
        self.handle: int | None = None

    def win(self) -> bool:
        path = lock_path(self.address)
        handle = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(handle)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return False
            raise
        os.ftruncate(handle, 0)
        os.write(handle, f"{os.getpid()}\n".encode())
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.handle, fcntl.LOCK_UN)
                os.close(self.handle)
            self.handle = None


def bind_listener(address: str) -> socket.socket:
    """Bind the state socket. Only the election winner may call this."""
    path = socket_path(address)
    # Safe now: holding the lock means no other owner exists, so anything here is
    # a leftover from a process that died.
    # lexists, not exists: a dangling symlink here is invisible to exists() and
    # bind then fails with EADDRINUSE while we hold the lock that should have
    # made us the owner.
    if os.path.lexists(path):
        if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):
            raise HeadsetError(f"{path} exists and is not a socket we can replace")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Bind under a umask that leaves no window in which the socket is reachable by
    # anyone else, rather than creating it and fixing the mode afterwards.
    previous = os.umask(0o177)
    try:
        listener.bind(str(path))
    finally:
        os.umask(previous)
    listener.listen(8)
    listener.setblocking(False)
    return listener


def connect_to_owner(address: str, timeout: float = 2.0) -> socket.socket | None:
    path = socket_path(address)
    if not path.exists():
        return None
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
    except OSError:
        with contextlib.suppress(OSError):
            client.close()
        return None
    client.setblocking(False)
    return client
