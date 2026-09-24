"""What a program asked a question may do: answer within a deadline, say no
more than the cap, and take every process it started with it when stopped.

Against real shells, because a runner that fakes the process cannot show a
grandchild dying with its group. Skipped, not failed, where there is no shell
to start: tools/hostile.py runs this suite with every process taken away.
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from headset import binaries  # noqa: E402

SHELL = "/bin/sh"
ENV = {"PATH": binaries.SEARCH_PATH}
WIDGET = (ROOT / "BarWidget.qml").read_text()


def gone(pid: int) -> bool:
    """Whether a process is dead, counting a zombie as dead.

    `os.kill(pid, 0)` says a zombie is alive until whoever adopted it reaps
    it, which on a runner with no reaper may be never. The state in /proc is
    what the kill actually changed.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True
    for line in status.splitlines():
        if line.startswith("State:"):
            return line.split()[1] == "Z"
    return True


def wait_gone(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if gone(pid):
            return True
        time.sleep(0.05)
    return False


class RunTests(unittest.TestCase):
    def setUp(self):
        try:
            subprocess.Popen([SHELL, "-c", "true"], env=ENV).wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("no shell to start on this machine")

    def test_the_answer_comes_back_as_a_completed_process(self):
        done = binaries.run([SHELL, "-c", "printf out; printf err >&2; exit 3"], env=ENV, timeout=10)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (3, "out", "err"))
        self.assertEqual(done.args[0], SHELL)

    def test_only_the_environment_the_caller_built_reaches_the_program(self):
        with patch.dict(os.environ, {"LD_PRELOAD": "/x", "HOME": "/h"}, clear=True):
            done = binaries.run([SHELL, "-c", "/usr/bin/env"], env={"ONLY": "this", **ENV}, timeout=10)
        names = {line.split("=")[0] for line in done.stdout.splitlines()}
        self.assertIn("ONLY", names)
        self.assertNotIn("LD_PRELOAD", names)
        self.assertNotIn("HOME", names)

    def test_a_program_that_says_too_much_is_stopped_while_it_is_talking(self):
        started = time.monotonic()
        with self.assertRaises(binaries.OutputTooLarge):
            binaries.run([SHELL, "-c", "yes | head -c 50000000"], env=ENV, timeout=30, cap=100000)
        # At the cap, not after 50 MB and not at the deadline. Generous for a
        # loaded runner; the kill itself lands within milliseconds.
        self.assertLess(time.monotonic() - started, 20)

    def test_the_deadline_takes_the_whole_process_group_and_reaps(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            started = time.monotonic()
            # Four seconds, not half of one: the shell has to have written the
            # pid before the deadline lands, and a loaded runner is slow to
            # start it.
            with self.assertRaises(subprocess.TimeoutExpired):
                binaries.run([SHELL, "-c", f"sleep 30 & echo $! > {pidfile}; sleep 30"], env=ENV, timeout=4.0)
            self.assertLess(time.monotonic() - started, 15)
            child = int(pidfile.read_text().strip())
        # The grandchild sleep went with the group, not only the shell.
        if not wait_gone(child):
            os.kill(child, 9)
            self.fail("the grandchild survived the group kill")

    def test_a_child_left_in_the_group_by_a_program_that_exited_goes_too(self):
        # `sleep 60 &` then exit: the program is gone, its child holds the
        # pipe. The reading must not wait on that pipe, and the child must not
        # be left running.
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            started = time.monotonic()
            done = binaries.run([SHELL, "-c", f"sleep 60 & echo $! > {pidfile}; echo answered; exit 0"],
                                env=ENV, timeout=30)
            self.assertLess(time.monotonic() - started, 10)
            self.assertEqual(done.stdout, "answered\n")
            child = int(pidfile.read_text().strip())
        if not wait_gone(child):
            os.kill(child, 9)
            self.fail("the child left in the group survived")

    def test_no_reader_thread_or_descriptor_outlives_a_call(self):
        # A grandchild in a session of its own keeps the pipe open past the
        # group kill, and keeps it for longer than the call: the readers have
        # to give up on it and close their own descriptors. It is killed by
        # hand at the end, since nothing in run() can reach it, by design.
        import threading
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            before = threading.active_count()
            fds = len(os.listdir("/proc/self/fd"))
            started = time.monotonic()
            with self.assertRaises(subprocess.TimeoutExpired):
                binaries.run([SHELL, "-c", f"setsid sh -c 'echo $$ > {pidfile}; exec sleep 60' & sleep 30"],
                             env=ENV, timeout=4.0)
            elapsed = time.monotonic() - started
            escaped = int(pidfile.read_text().strip())
        try:
            self.assertFalse(gone(escaped), "the escaped writer should still be alive")
            # The deadline, then at most four seconds of shared join budgets.
            self.assertLess(elapsed, 25)
            self.assertEqual(threading.active_count(), before)
            self.assertEqual(len(os.listdir("/proc/self/fd")), fds)
        finally:
            with self.subTest("cleanup"):
                try:
                    os.kill(escaped, 9)
                except ProcessLookupError:
                    pass

    def test_the_program_dies_with_the_helper(self):
        # PR_SET_PDEATHSIG, measured: a helper that starts a program through
        # run() and is then killed takes the program with it. `exec`, so the
        # pid recorded is the program itself and not a shell in front of it:
        # the promise covers the program the helper started, not what that
        # program starts, and a test that recorded a shell would pass while
        # the sleep behind it lived on.
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / "pid"
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "from headset import binaries\n"
                "binaries.run(['/bin/sh', '-c', 'echo $$ > %s; exec sleep 60'], env=%r, timeout=60)\n"
                % (str(ROOT), pidfile, ENV)
            )
            helper = subprocess.Popen([sys.executable, "-c", code], env=ENV)
            for _ in range(100):
                if pidfile.exists() and pidfile.read_text().strip():
                    break
                time.sleep(0.05)
            else:
                helper.kill()
                self.fail("the program never started")
            program = int(pidfile.read_text().strip())
            helper.kill()
            helper.wait(timeout=10)
        if not wait_gone(program):
            os.kill(program, 9)
            self.fail("the program outlived the helper that started it")

    def test_a_program_born_to_a_helper_that_had_already_died_does_not_run(self):
        # The registration races the helper's own death: a child forked just
        # before the helper died is init's by the time it asks for the signal.
        # It checks its parent after asking, and leaves.
        # A pid that is not this process's, whatever this process's is: a
        # runner inside a container can be pid 1 itself.
        register = binaries._die_with(next(p for p in (2, 3) if p != os.getpid()))
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "ran"
            process = subprocess.Popen([SHELL, "-c", f"touch {marker}"], env=ENV,
                                       preexec_fn=register)
            code = process.wait(timeout=10)
            # Checked inside the directory's lifetime; outside it the marker is
            # gone whether or not the command ran.
            self.assertEqual(code, 1)
            self.assertFalse(marker.exists())
            # And the same registration with the real parent lets it run.
            process = subprocess.Popen([SHELL, "-c", f"touch {marker}"], env=ENV,
                                       preexec_fn=binaries._die_with(os.getpid()))
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertTrue(marker.exists())

    def test_a_deadline_applies_when_the_caller_names_none(self):
        with patch.object(binaries, "DEFAULT_DEADLINE", 0.3):
            with self.assertRaises(subprocess.TimeoutExpired):
                binaries.run([SHELL, "-c", "sleep 30"], env=ENV)

    def test_a_missing_program_raises_what_subprocess_run_raised(self):
        with self.assertRaises(FileNotFoundError):
            binaries.run(["/nonexistent/program"], env=ENV, timeout=10)


class CallSiteTests(unittest.TestCase):
    """The two questions the helper asks go through run(), with a deadline."""

    def test_pactl_and_bluetoothctl_are_asked_through_the_bounded_runner(self):
        for rel in ("headset/audio.py", "headset/cli.py"):
            source = (ROOT / rel).read_text()
            self.assertNotIn("subprocess.run(", source, rel)
            for call in re.findall(r"binaries\.run\((.*?)\)\n", source, re.S):
                self.assertIn("timeout=", call, rel)


class WidgetTests(unittest.TestCase):
    """The bar's own half, read as text: the audio reading is capped and deadlined."""

    def test_the_audio_reading_is_collected_by_a_capped_collector(self):
        self.assertIn("component CappedCollector: StdioCollector {", WIDGET)
        self.assertIn("stdout: CappedCollector {\n      proc: audioProcess", WIDGET)
        block = WIDGET[WIDGET.index("component CappedCollector"):][:1600]
        self.assertIn("waitForEnd: false", block)
        self.assertIn("collector.proc.signal(9)", block)
        self.assertNotIn("waitForEnd: true", WIDGET)
        # The one StdioCollector is the component; a stopped answer is not parsed.
        self.assertEqual(WIDGET.count("StdioCollector {"), 1)
        self.assertIn("if (stopped) return", WIDGET)
        # stderr is capped the same way, and read line by line as it arrives.
        self.assertIn("stderr: CappedCollector {\n      proc: audioProcess", WIDGET)
        self.assertNotIn("stderr: SplitParser {\n      onRead: function(line) {\n        var text = String(line || \"\").trim()\n        if (text.length > 0) root.helperError", WIDGET)

    def test_the_audio_reading_has_a_deadline_that_is_reset_per_run(self):
        self.assertIn("id: audioDeadline", WIDGET)
        self.assertIn("audioProcess.stdout.stopped = true", WIDGET)
        self.assertIn("audioProcess.stderr.stopped = true", WIDGET)
        self.assertIn("audioProcess.signal(9)", WIDGET)
        self.assertIn("audioDeadline.restart()", WIDGET)
        self.assertIn("audioProcess.stdout.stopped = false", WIDGET)


if __name__ == "__main__":
    unittest.main()
