"""Where an external program is found, and what it is allowed to inherit.

The panel starts this helper by itself, whenever a headset connects, so nothing
it runs is run by a person who could look at it first. That makes `PATH` the
wrong way to find `pactl`, `pipewire` or `bluetoothctl`: an entry ahead of
`/usr/bin` in whatever environment the bar was started with would decide what
this helper executes. The same goes for the environment handed to a child.
`LD_PRELOAD` and `LD_LIBRARY_PATH` choose what any program loads before its own
first line runs, and `SPA_PLUGIN_DIR` and `PIPEWIRE_MODULE_DIR` choose what
PipeWire loads after it, so passing this process's environment on wholesale
hands those choices to whoever set them.

So a program is taken from a fixed list of directories only root can write, and
a child is given an environment built up from nothing rather than copied down
from here.

None of this is a privilege boundary. The helper, the panel and the shell are
one user, and anyone who can set that user's `PATH` can already run code as
them. What it is, is the difference between this plugin running the programs it
names and this plugin running whatever answers to those names.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

# Only root writes these, and everything this plugin runs is packaged into the
# first. `/bin` is the same directory on a merged system and a separate root-owned
# one where it is not; either way what is found there is checked below.
SEARCH_PATH = "/usr/bin:/bin"

# What every child gets before its caller names anything. A fixed `PATH` rather
# than none at all: a program that looks something up finds the same trusted
# directories this module does, and one that inherits no `PATH` falls back to a
# built-in default that is not ours to choose.
BASE_ENVIRONMENT = {"PATH": SEARCH_PATH}


def owned_by_root(info: os.stat_result) -> bool:
    """Whether a stat reading describes something only root can rewrite."""
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def trusted(path: Path) -> bool:
    """Whether `path` and every directory above it are root's alone to change.

    The walk upwards is the half that is easy to leave out. A root-owned binary
    inside a directory somebody else can write is not a binary somebody else
    cannot replace: they rename it aside and put their own file in its place,
    and the permissions on the original never came into it.
    """
    for entry in (path, *path.parents):
        try:
            # Following symlinks, deliberately: `/bin` is a link with the
            # meaningless 0777 every symlink has, and what matters is the
            # directory it lands in.
            info = entry.stat()
        except OSError:
            return False
        if not owned_by_root(info):
            return False
    return True


def find(name: str) -> str | None:
    """The absolute path of an external program, or None if there is no trusted one.

    `shutil.which` with the search path spelled out, so `PATH` cannot reach this
    decision, and so the suite that takes the machine away has one thing to take.
    """
    found = shutil.which(name, path=SEARCH_PATH)
    if not found:
        return None
    # Resolved before it is judged: a link is only as trustworthy as the file at
    # the end of it, and the parents that count are the real one's.
    real = Path(found).resolve()
    if not trusted(real):
        return None
    return str(real)


def environment(carry: tuple[str, ...] = (), **values: str) -> dict[str, str]:
    """An environment for one child, built from nothing.

    `carry` names the variables taken from this process where they are set, and
    `values` the ones this caller decides outright. Everything else is left
    behind. Callers name what their program needs rather than what it must not
    have: a deny list is a list of the loader variables somebody has thought of.
    """
    built = dict(BASE_ENVIRONMENT)
    for name in carry:
        # Never over the base. A caller naming `PATH` in `carry` would be asking
        # for this process's own, which is the one thing here that is certain
        # not to be trusted, and it would read as a caller being careful.
        if name in BASE_ENVIRONMENT:
            continue
        value = os.environ.get(name)
        if value:
            built[name] = value
    built.update(values)
    return built
