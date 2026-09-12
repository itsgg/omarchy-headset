"""Talk to the process that already owns the headset, rather than fighting it.

A headset allows one control session. While the bar widget is running it holds it,
so a second process opening its own would be refused with EBUSY, which is what the
command line used to do. Anything one-shot therefore asks the owner first and only
opens its own session when there is nobody to ask.
"""
from __future__ import annotations

import json
import select
import socket
import time

from . import ipc


def _read_state(sock: socket.socket, deadline: float, buffer: bytes = b"") -> tuple[dict | None, bytes]:
    while time.monotonic() < deadline:
        readable, _, _ = select.select([sock], [], [], max(0.05, deadline - time.monotonic()))
        if not readable:
            continue
        try:
            chunk = sock.recv(8192)
        except BlockingIOError:
            continue
        except OSError:
            return None, buffer
        if not chunk:
            return None, buffer
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            text = line.decode("utf-8", "replace").strip()
            if not text.startswith("{"):
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if payload.get("type") == "state":
                return payload, buffer
    return None, buffer


def state(address: str, timeout: float = 5.0) -> dict | None:
    """The owner's current view, or None when there is no owner."""
    sock = ipc.connect_to_owner(address)
    if sock is None:
        return None
    try:
        payload, _ = _read_state(sock, time.monotonic() + timeout)
        return payload
    finally:
        sock.close()


def send(address: str, command: dict, timeout: float = 6.0) -> dict | None:
    """Hand a command to the owner and return the state it publishes next."""
    sock = ipc.connect_to_owner(address)
    if sock is None:
        return None
    try:
        deadline = time.monotonic() + timeout
        first, buffer = _read_state(sock, deadline)
        sock.sendall((json.dumps(command) + "\n").encode())
        # The owner publishes on every change, so the next state is the answer.
        payload, _ = _read_state(sock, deadline, buffer)
        return payload or first
    except OSError:
        return None
    finally:
        sock.close()
