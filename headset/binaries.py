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

And a program that is asked a question is bounded while it answers. `run` gives
it a deadline, reads what it says as the bytes arrive and stops it the moment
it has said more than any real answer could be, and stopping means the whole
process group, then a reap: a child the program started that kept the pipe
open would otherwise keep the helper waiting on it for ever.

None of this is a privilege boundary. The helper, the panel and the shell are
one user, and anyone who can set that user's `PATH` can already run code as
them. What it is, is the difference between this plugin running the programs it
names and this plugin running whatever answers to those names.
"""
from __future__ import annotations

import ctypes
import os
import select
import shutil
import signal
import stat
import subprocess
import threading
import time
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


# How long a program asked a question has to answer when the caller names no
# deadline of its own, and how much it may say. The cap is far above any real
# answer: `pactl list cards` on a machine with a dozen cards is tens of
# kilobytes. It exists so that a program that never stops talking cannot grow
# the helper without bound.
DEFAULT_DEADLINE = 30.0
OUTPUT_CAP = 8 * 1024 * 1024


class OutputTooLarge(subprocess.SubprocessError):
    """A program was stopped for saying more than the cap allows."""

    def __init__(self, argv: list, cap: int):
        super().__init__(f"{argv[0]} produced more than {cap} bytes and was stopped")
        self.cmd = argv
        self.cap = cap


def _drain(stream, sink: list, cap: int, overflow: threading.Event,
           stop: threading.Event) -> None:
    """Read a pipe as the bytes arrive, and stop reading the moment the cap is passed.

    `os.read` on the descriptor rather than `stream.read(n)`: the latter waits
    for n bytes or the end, which is not live. Past the cap this returns; the
    pipe fills, the program blocks on its next write, and the caller kills it.
    Nothing past the cap is kept.

    Polled with `select`, so the caller can ask this thread to give up when a
    writer that escaped the group keeps the pipe open. The thread closes the
    descriptor itself on its way out, whichever way it leaves: closing it from
    another thread while a read was blocked would free the number for the next
    program, whose output that read would then take.
    """
    fd = stream.fileno()
    total = 0
    try:
        while not stop.is_set():
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            total += len(chunk)
            if total > cap:
                overflow.set()
                return
            sink.append(chunk)
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _die_with(parent: int):
    """A function for the child to run before exec: die when the helper does.

    The bar can kill the helper outright (its own deadline sends SIGKILL), and
    a program the helper was waiting on would otherwise outlive the one thing
    that was going to stop it. PR_SET_PDEATHSIG (1) with SIGKILL is that
    promise, made by the child about itself, and it covers the program the
    helper started and not what that program starts in turn. Linux only,
    which this plugin is.

    The parent is checked after the promise is made, not before: a helper
    that died between the fork and the prctl has already handed this child
    to init, and the signal would then wait for init to die.
    """
    def register() -> None:
        try:
            ctypes.CDLL(None, use_errno=True).prctl(1, 9, 0, 0, 0)
        except Exception:
            pass
        if os.getppid() != parent:
            os._exit(1)
    return register


def kill_group(process: subprocess.Popen) -> None:
    """Kill everything a program started along with it, then reap it.

    The program was started in a session of its own, so its pid is its process
    group, and SIGKILL to the group reaches whatever it spawned that did not
    leave the group on purpose. Killing only the program would leave a child of
    its holding the pipe open, and the caller waiting on that pipe.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def run(argv: list, *, env: dict, timeout: float | None = None,
        cap: int = OUTPUT_CAP, **_ignored) -> subprocess.CompletedProcess:
    """Ask a program a question, bounded in time and in what it may answer.

    The shape of `subprocess.run(argv, capture_output=True, text=True, ...)`,
    which is what every caller here wants, so a caller reads the same
    CompletedProcess and catches the same TimeoutExpired. Past the deadline or
    the cap the whole process group is killed and reaped, and the caller gets
    TimeoutExpired or OutputTooLarge, both SubprocessError. `env` is required
    rather than defaulted: a child built from this process's environment is
    what this module exists to prevent, and a caller has to say what it built.
    """
    deadline = DEFAULT_DEADLINE if timeout is None else float(timeout)
    # preexec_fn is documented as unsafe with other threads holding locks at
    # fork time. The helper is single-threaded when this runs: the readers
    # below are started after the fork and are gone before the next one.
    process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, preexec_fn=_die_with(os.getpid()))
    sinks: dict[str, list] = {"out": [], "err": []}
    overflow = threading.Event()
    stop = threading.Event()
    readers = []
    for name, stream in (("out", process.stdout), ("err", process.stderr)):
        reader = threading.Thread(target=_drain, args=(stream, sinks[name], cap, overflow, stop),
                                  daemon=True)
        reader.start()
        readers.append(reader)
    started = time.monotonic()
    timed_out = False
    while process.poll() is None:
        if overflow.is_set():
            kill_group(process)
            break
        if time.monotonic() - started > deadline:
            timed_out = True
            kill_group(process)
            break
        time.sleep(0.02)
    # The program has been reaped. Its pipes may still be open: a chunk that
    # crossed the cap in the last moment, or a child it started and left in
    # its group (`sleep 60 &` from a shell) that still holds the write end. In
    # either case the group has to go, and the group outlives its leader for
    # as long as any member does.
    def join_all(budget: float) -> None:
        # One budget for both readers, not one each, so the worst case here is
        # a few seconds and not a few seconds per pipe.
        until = time.monotonic() + budget
        for reader in readers:
            reader.join(timeout=max(0.0, until - time.monotonic()))

    join_all(2.0)
    if overflow.is_set() or any(reader.is_alive() for reader in readers):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass
        join_all(2.0)
    # Whatever is still holding a pipe now left the group on purpose. The
    # readers are told to give up and close their own descriptors, and are
    # waited for without a budget: a reader checks the stop flag before every
    # 0.2 s select and a read after a readable select does not block, so this
    # wait is bounded by scheduling alone, and returning with a reader alive
    # would leave a descriptor open and a thread running into the next call.
    stop.set()
    for reader in readers:
        reader.join()
    out = b"".join(sinks["out"]).decode("utf-8", "replace")
    err = b"".join(sinks["err"]).decode("utf-8", "replace")
    if overflow.is_set():
        raise OutputTooLarge(argv, cap)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, deadline, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, process.returncode, out, err)
