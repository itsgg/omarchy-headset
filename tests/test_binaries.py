"""Which program each name resolves to, and what a child is allowed to inherit.

The bar starts the helper by itself, so every program named here is run with
nobody watching. That makes this the one place where getting it wrong is
invisible, and the tests below are written so they cannot pass by accident: the
trust check is exercised on directories this file creates rather than on
whatever the machine running it happens to look like, and each of the three call
sites is checked for the argument vector and the environment it actually builds
rather than for calling something named correctly.
"""
import ast
import importlib.util
import os
import socket
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from headset import audio, binaries, cli, equaliser  # noqa: E402
from headset.errors import HeadsetError  # noqa: E402

# A dangerous environment, of the shape this whole module exists to stop. The
# first two decide what any program loads before its own code runs, the next two
# what PipeWire loads after it, and PATH decides which program runs at all.
HOSTILE = {
    "LD_PRELOAD": "/home/someone/evil.so",
    "LD_LIBRARY_PATH": "/home/someone/lib",
    "SPA_PLUGIN_DIR": "/home/someone/spa",
    "PIPEWIRE_MODULE_DIR": "/home/someone/modules",
    "PYTHONPATH": "/home/someone/python",
    "PATH": "/home/someone/bin",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "HOME": "/home/someone",
}


def reading(uid: int, mode: int) -> os.stat_result:
    """A stat result with only the two fields the trust check reads."""
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


class OwnershipTests(unittest.TestCase):
    def test_root_owned_and_unwritable_by_anybody_else_is_what_counts(self):
        self.assertTrue(binaries.owned_by_root(reading(0, 0o100755)))

    def test_a_program_somebody_else_owns_is_refused(self):
        self.assertFalse(binaries.owned_by_root(reading(1000, 0o100755)))

    def test_a_program_root_owns_and_anybody_can_write_is_refused(self):
        # Ownership on its own proves nothing: the whole point of a writable file
        # is that its contents are not its owner's decision.
        self.assertFalse(binaries.owned_by_root(reading(0, 0o100775)))
        self.assertFalse(binaries.owned_by_root(reading(0, 0o100777)))


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.folder / "bin"
        self.directory.mkdir()
        # Writable by anybody, so the refusal below is a fact about the fixture
        # rather than a fact about whoever is running the suite. Left to
        # ownership alone this passes here and fails for a runner that is root
        # with a temporary directory somewhere root owns.
        self.directory.chmod(0o777)
        self.program = self.directory / "pactl"
        self.program.write_text("#!/usr/bin/bash\n")
        self.program.chmod(0o755)

    def test_a_program_under_a_directory_somebody_else_can_write_is_refused(self):
        # Nothing here can give a file to root, so it is the reading that is
        # faked and not the file. What is under test is the walk upwards: a
        # program root owns, inside a directory root does not, is a program that
        # can be renamed aside and replaced without its own permissions changing.
        loose = self.directory.stat().st_ino
        with patch.object(binaries, "owned_by_root", lambda info: info.st_ino != loose):
            self.assertFalse(binaries.trusted(self.program))
        # And with nothing loose it passes, so the refusal above is the directory
        # rather than the walk failing for a reason of its own.
        with patch.object(binaries, "owned_by_root", lambda info: True):
            self.assertTrue(binaries.trusted(self.program))

    def test_a_directory_anybody_can_write_is_not_trusted(self):
        # The plain case, with nothing patched.
        self.assertFalse(binaries.trusted(self.program))

    def test_a_program_that_is_not_there_is_not_trusted(self):
        self.assertFalse(binaries.trusted(self.directory / "absent"))

    def test_path_is_not_consulted(self):
        # A `pactl` on PATH ahead of /usr/bin is exactly the attack. The trust
        # check is switched off for the length of it, deliberately: with it on,
        # this passes for a lookup that searched PATH and was saved afterwards by
        # the ownership walk, and then it is testing the other half of the module.
        # Off, the only thing that can keep this program out is the fixed path.
        with patch.object(binaries, "trusted", return_value=True), \
             patch.dict(os.environ, {"PATH": str(self.directory)}):
            self.assertNotEqual(binaries.find("pactl"), str(self.program))

    def test_a_program_the_trust_check_refuses_is_not_returned(self):
        # The other half of the pair above: with the lookup succeeding and the
        # trust check saying no, `find` must still come back empty. Without this
        # the whole trust check can be deleted and every other test here passes,
        # because each of them exercises one side or stands the other one down.
        with patch.object(binaries, "trusted", return_value=False), \
             patch("shutil.which", return_value="/usr/bin/pactl"):
            self.assertIsNone(binaries.find("pactl"))

    def test_a_name_nothing_answers_to_is_absent_rather_than_a_crash(self):
        # Named so that no machine can be holding one, rather than assumed.
        self.assertIsNone(binaries.find(f"no-such-program-{uuid.uuid4().hex}"))


class EnvironmentTests(unittest.TestCase):
    def test_nothing_is_carried_unless_it_is_named(self):
        with patch.dict(os.environ, HOSTILE, clear=True):
            built = binaries.environment(("XDG_RUNTIME_DIR",))
        self.assertEqual(built, {"PATH": binaries.SEARCH_PATH,
                                 "XDG_RUNTIME_DIR": "/run/user/1000"})

    def test_the_loader_variables_are_left_behind_at_every_call_site(self):
        # Named separately from the test above because this is the sentence the
        # whole module is for, and it should fail by name when it stops holding.
        with patch.dict(os.environ, HOSTILE, clear=True):
            built = binaries.environment(("XDG_RUNTIME_DIR", "HOME"), PIPEWIRE_CONFIG_DIR="/run/x")
        for dangerous in ("LD_PRELOAD", "LD_LIBRARY_PATH", "SPA_PLUGIN_DIR",
                          "PIPEWIRE_MODULE_DIR", "PYTHONPATH"):
            self.assertNotIn(dangerous, built)

    def test_the_path_a_child_gets_is_the_trusted_one_and_never_the_caller_s(self):
        with patch.dict(os.environ, HOSTILE, clear=True):
            self.assertEqual(binaries.environment(("PATH",))["PATH"], binaries.SEARCH_PATH)

    def test_a_named_variable_that_is_unset_is_not_invented(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertNotIn("XDG_RUNTIME_DIR", binaries.environment(("XDG_RUNTIME_DIR",)))

    def test_what_the_caller_sets_outright_wins(self):
        with patch.dict(os.environ, {"LC_ALL": "fr_FR.UTF-8"}):
            self.assertEqual(binaries.environment(("LC_ALL",), LC_ALL="C")["LC_ALL"], "C")


class CallSiteTests(unittest.TestCase):
    """Every external program this plugin runs, as it is actually run.

    One test per call site, because this is the list that grows: a fourth
    program added without going through `binaries` is the regression, and it
    shows up as a call site with no test beside it rather than as a failure.
    """

    def run_and_capture(self, call, program: str, environment: dict | None = None) -> dict:
        """Run `call` with `program` resolved and nothing executed, and report the call.

        `environment` is laid over the hostile one above; a value of None there
        means the variable is unset for the length of the call, which is how a
        test says "this machine has no PULSE_SERVER".
        """
        seen = {}

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def record(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return Done()

        merged = dict(HOSTILE, **(environment or {}))
        unset = [name for name, value in merged.items() if value is None]
        for name in unset:
            merged.pop(name)
        with patch.object(binaries, "find", return_value=f"/usr/bin/{program}"), \
             patch.dict(os.environ, merged, clear=False), \
             patch("headset.binaries.run", side_effect=record):
            for name in unset:
                os.environ.pop(name, None)
            call()
        return seen

    def assert_safely_run(self, seen: dict, program: str) -> None:
        self.assertEqual(seen["argv"][0], f"/usr/bin/{program}")
        self.assertEqual(seen["env"]["PATH"], binaries.SEARCH_PATH)
        for dangerous in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONPATH"):
            self.assertNotIn(dangerous, seen["env"])

    def test_pactl_is_run_from_usr_bin_with_an_environment_of_its_own(self):
        seen = self.run_and_capture(lambda: audio.run_pactl(["list", "cards"]), "pactl")
        self.assert_safely_run(seen, "pactl")
        # Still in English, which is the other thing this environment is for.
        self.assertEqual(seen["env"]["LC_ALL"], "C")
        self.assertEqual(seen["env"]["LANG"], "C")

    def test_pactl_is_told_which_server_to_use_so_it_cannot_autospawn(self):
        # Naming the server is what turns autospawn off, which is the last way a
        # configuration file could name a program for libpulse to run. Measured
        # against a real pactl: with `autospawn = yes` and a `daemon-binary` in
        # a client.conf, an unreachable server ran that binary without this
        # argument and did not run it with it.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "pulse").mkdir()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(str(folder / "pulse" / "native"))
        seen = self.run_and_capture(lambda: audio.run_pactl(["list", "cards"]), "pactl",
                                    {"XDG_RUNTIME_DIR": str(folder), "PULSE_SERVER": None})
        self.assertIn("--server", seen["argv"])
        self.assertEqual(seen["argv"][seen["argv"].index("--server") + 1].split()[0],
                         f"unix:{folder / 'pulse' / 'native'}")

    def test_a_server_the_user_named_is_the_one_pactl_is_given(self):
        seen = self.run_and_capture(lambda: audio.run_pactl(["list", "cards"]), "pactl",
                                    {"PULSE_SERVER": "tcp:box:4713"})
        self.assertEqual(seen["argv"][seen["argv"].index("--server") + 1], "tcp:box:4713")

    def test_a_server_is_named_even_when_the_socket_is_not_there(self):
        # The missing socket is the case that matters: a connection that fails is
        # what autospawn is for, so leaving the server unnamed there would put
        # the hole back exactly where it bites. Measured against a real pactl
        # before this was changed, and it did run the daemon-binary.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        seen = self.run_and_capture(lambda: audio.run_pactl(["list", "cards"]), "pactl",
                                    {"XDG_RUNTIME_DIR": str(folder), "PULSE_SERVER": None})
        self.assertEqual(seen["argv"][seen["argv"].index("--server") + 1].split()[0],
                         f"unix:{folder / 'pulse' / 'native'}")

    def test_with_nothing_to_name_pactl_is_not_run_at_all(self):
        # Not run, rather than run and failing. A HeadsetError alone proves
        # nothing here: a pactl that ran without --server and could not connect
        # raises the same thing, and that is the run this refuses to make.
        with patch.object(binaries, "find", return_value="/usr/bin/pactl"), \
             patch.dict(os.environ, {}, clear=True), \
             patch("headset.binaries.run") as ran:
            with self.assertRaises(HeadsetError):
                audio.run_pactl(["list", "cards"])
        ran.assert_not_called()

    def test_a_runtime_directory_with_a_space_is_refused_rather_than_split(self):
        # The argument is a whitespace-separated list with no quoting, so a
        # crafted runtime directory is a second server address rather than a
        # path. Confirmed against a real pactl: given two addresses it fails
        # over from the first to the second and connects.
        with patch.object(binaries, "find", return_value="/usr/bin/pactl"), \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/tmp/x tcp:10.0.0.1"}, clear=True), \
             patch("headset.binaries.run") as ran:
            with self.assertRaises(HeadsetError):
                audio.run_pactl(["list", "cards"])
        ran.assert_not_called()

    def test_the_system_wide_socket_is_still_offered_after_the_user_one(self):
        # libpulse tries the per-user socket and then the system-wide one, so a
        # machine running PulseAudio in system mode keeps working.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        seen = self.run_and_capture(lambda: audio.run_pactl(["list", "cards"]), "pactl",
                                    {"XDG_RUNTIME_DIR": str(folder), "PULSE_SERVER": None})
        named = seen["argv"][seen["argv"].index("--server") + 1].split()
        self.assertEqual(named, [f"unix:{folder / 'pulse' / 'native'}",
                                 f"unix:{audio.SYSTEM_SOCKET}"])

    def test_a_pactl_that_is_not_under_usr_bin_is_a_message_and_not_a_fallback(self):
        with patch.object(binaries, "find", return_value=None):
            with self.assertRaises(HeadsetError):
                audio.run_pactl(["list", "cards"])

    def test_bluetoothctl_is_run_from_usr_bin_with_an_environment_of_its_own(self):
        with patch.object(audio, "headset_cards", return_value=[]):
            seen = self.run_and_capture(cli.connected_devices, "bluetoothctl")
        self.assert_safely_run(seen, "bluetoothctl")

    def test_a_bluetoothctl_that_is_not_under_usr_bin_is_a_message_and_not_a_fallback(self):
        with patch.object(binaries, "find", return_value=None):
            with self.assertRaises(HeadsetError):
                cli.connected_devices()

    def test_pipewire_is_run_from_usr_bin_with_an_environment_of_its_own(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        base = folder / "filter-chain.conf"
        base.write_text("context.modules = [ ]\n")
        seen = {}

        class Started:
            def poll(self):
                return None

        def record(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env")
            return Started()

        with patch.dict(os.environ, dict(HOSTILE, XDG_RUNTIME_DIR=str(folder)), clear=False), \
             patch.object(equaliser, "BASE_CONFIG", str(base)), \
             patch.object(binaries, "find", return_value="/usr/bin/pipewire"), \
             patch("subprocess.Popen", side_effect=record):
            unit = equaliser.Equaliser("3C:B0:ED:50:BC:9C", "Nothing Ear")
            unit.start("bluez_output.X.1")
        self.assertEqual(seen["argv"][0], "/usr/bin/pipewire")
        self.assertEqual(seen["env"]["PATH"], binaries.SEARCH_PATH)
        # The chain is PipeWire loading modules and plugins, so these two decide
        # what runs inside the process the user's audio is sent through.
        for dangerous in ("LD_PRELOAD", "LD_LIBRARY_PATH", "SPA_PLUGIN_DIR",
                          "PIPEWIRE_MODULE_DIR"):
            self.assertNotIn(dangerous, seen["env"])
        self.assertTrue(seen["env"]["PIPEWIRE_CONFIG_DIR"].startswith(str(folder)))

    def test_every_call_site_in_the_helper_goes_through_this_module(self):
        """A backstop for the three tests above, with its scope written down.

        What it covers: the `headset` package, which is everything the bar runs
        unattended, and the two ways a program gets started there, which are the
        subprocess family and the os exec family. What it refuses: a program
        named as a literal, and a call with no environment of its own or one
        built out of this process's.

        It also covers the shipped scripts under `tools/` that carry the
        executable bit. Those are run by hand rather than by the bar, so they are
        not the boundary this module defends, but the marketplace baseline treats
        executable files as scanned source wherever they sit, and one rule about
        starting a program is easier to keep than two.

        What it does not cover: a subprocess call that reaches the interpreter
        through an alias or an indirection. It is a net for the obvious way of
        reintroducing this, not a proof that nobody can.
        """
        starters = {"run", "Popen", "call", "check_call", "check_output"}
        shipped = [path for path in sorted((ROOT / "tools").glob("*.py"))
                   if os.access(path, os.X_OK)]
        offenders = []
        for source in sorted((ROOT / "headset").rglob("*.py")) + shipped:
            for node in ast.walk(ast.parse(source.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                called = node.func
                if not isinstance(called, ast.Attribute) or not isinstance(called.value, ast.Name):
                    continue
                where = f"{source.relative_to(ROOT)}:{node.lineno}"
                if called.value.id == "os" and (called.attr.startswith(("exec", "spawn"))
                                                or called.attr == "system"):
                    offenders.append(f"{where} starts a program around this module")
                    continue
                if called.value.id != "subprocess" or called.attr not in starters:
                    continue
                argv = node.args[0] if node.args else None
                if isinstance(argv, ast.Constant):
                    offenders.append(f"{where} starts a program named as a bare string")
                elif isinstance(argv, ast.List) and argv.elts and isinstance(argv.elts[0], ast.Constant):
                    offenders.append(f"{where} starts a program by bare name")
                given = [keyword for keyword in node.keywords if keyword.arg == "env"]
                if not given:
                    offenders.append(f"{where} hands on this process's environment")
                    continue
                value = given[0].value
                if isinstance(value, ast.Constant) and value.value is None:
                    offenders.append(f"{where} hands on this process's environment")
                elif any(isinstance(inner, ast.Attribute) and inner.attr == "environ"
                         for inner in ast.walk(value)):
                    offenders.append(f"{where} builds its environment out of this one")
        self.assertEqual(offenders, [])


def load_shots():
    """tools/shots.py as a module, the way tests/test_shots.py loads it.

    By path rather than by import, because `tools` is not a package, and behind
    the same Pillow guard: the screenshot tool needs Pillow and a runner that
    takes screenshots does not exist, so importing it unguarded is a test that
    only passes on the machine it was written on.
    """
    spec = importlib.util.spec_from_file_location("shots", ROOT / "tools" / "shots.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(importlib.util.find_spec("PIL") is None,
                 "Pillow is only needed to take screenshots")
class ShippedToolTests(unittest.TestCase):
    """The scripts that ship with the executable bit, checked by behaviour.

    The tree-parsing guard above cannot see these protections: a `program()`
    that returned its argument would satisfy it, and a shell script has no AST
    it reads. So each one is exercised.

    The two that load the screenshot tool are skipped where Pillow is absent.
    The linter's two below are not: they read a file and need nothing.
    """

    def test_the_screenshot_tool_resolves_its_programs_under_usr_bin(self):
        shots = load_shots()
        with patch.object(binaries, "find", return_value="/usr/bin/grim"):
            self.assertEqual(shots.program("grim"), "/usr/bin/grim")
        # And a name with no trusted program behind it stops the run rather than
        # falling back to whatever PATH would have answered with.
        with patch.object(binaries, "find", return_value=None):
            with self.assertRaises(SystemExit):
                shots.program("grim")

    def test_the_screenshot_tool_does_not_photograph_a_panel_it_failed_to_open(self):
        shots = load_shots()

        class Done:
            returncode = 1
            stdout = ""
            stderr = "OMARCHY_PATH is not set\n"

        with patch.object(binaries, "find", return_value="/usr/bin/omarchy-shell"), \
             patch("subprocess.run", return_value=Done()):
            with self.assertRaises(SystemExit):
                shots.shell("omarchy-shell", "plugin", "open")


class ServerTests(unittest.TestCase):
    """Which server pactl is pointed at, and how the path to it is built."""

    def test_a_long_alias_to_the_runtime_directory_is_shortened(self):
        # libpulse shortens this before building a socket address, because the
        # address holds about 108 bytes. Passing the alias through unshortened
        # is a connection that fails where libpulse's own lookup succeeds.
        #
        # Through `pulse_server` rather than through `shortest` on its own: a
        # test that calls the helper directly stays green when the call to it is
        # deleted, which is the only way this gets lost.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "run" / "user" / "1000" / "pulse").mkdir(parents=True)
        (folder / "x").mkdir()
        alias = f"{folder}" + "/x/.." * 20 + "/run/user/1000"
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": alias}, clear=True):
            named = audio.pulse_server().split()
        self.assertEqual(named[0], f"unix:{folder}/run/user/1000/pulse/native")

    def test_a_path_that_is_already_shortest_is_left_alone(self):
        # Only when canonicalising actually shortens it, the way libpulse has
        # it. The fixture is the shape that catches an unconditional resolve: a
        # short name pointing at a long one, where following the link is the
        # wrong answer.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target = folder / ("d" * 80)
        target.mkdir()
        link = folder / "s"
        link.symlink_to(target)
        self.assertEqual(audio.shortest(str(link)), str(link))

    def test_shorter_is_counted_in_bytes_and_not_in_characters(self):
        # A socket address holds bytes, so "shorter" has to mean bytes. This
        # target is fewer characters than the alias and nearly twice as many
        # bytes: counted as characters it wins and the address built from it is
        # over the limit the shortening exists to stay under.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        target = folder / ("\u00e9" * 50)
        target.mkdir()
        link = folder / ("a" * 60)
        link.symlink_to(target)
        self.assertLess(len(str(target)), len(str(link)))
        self.assertGreater(len(os.fsencode(str(target))), len(os.fsencode(str(link))))
        self.assertEqual(audio.shortest(str(link)), str(link))

    def test_a_path_that_is_not_all_there_is_left_alone(self):
        # `resolve` without strict folds `..` across a component that does not
        # exist, so an unreachable path becomes a reachable one somewhere else.
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        missing = f"{folder}/absent/../also-absent"
        self.assertEqual(audio.shortest(missing), missing)

class LinterScriptTests(unittest.TestCase):
    """The shipped shell script, which has no AST to parse and needs nothing."""

    def test_the_linter_ignores_shell_startup_code_from_the_environment(self):
        # bash sources $BASH_ENV before the first line of a non-interactive
        # script, so pinning PATH inside the script is already too late: the
        # environment has chosen what runs before the script gets a turn.
        # Privileged mode is what stops it. Verified by hand both ways, and with
        # the exact script codex used to demonstrate it.
        self.assertEqual((ROOT / "tools" / "qmllint.sh").read_bytes().split(b"\n")[0],
                         b"#!/usr/bin/bash -p")

    def test_the_linter_pins_its_own_path(self):
        # A shell script's every command goes through PATH, so one assignment
        # covers dirname, mktemp, ln, grep, rm and the linter itself. There is
        # no AST to check it with, so the text is.
        lines = (ROOT / "tools" / "qmllint.sh").read_text().splitlines()
        commands = [number for number, line in enumerate(lines)
                    if line.strip() and not line.startswith("#!")
                    and not line.lstrip().startswith("#")
                    and not line.startswith("set ")]
        self.assertIn("PATH=/usr/bin:/bin", lines)
        # Before the first command rather than merely present, and counted over
        # commands rather than over the file's text: the comment above the
        # assignment names the programs it covers, and matching on those names
        # finds the prose instead of the code.
        self.assertEqual(lines.index("PATH=/usr/bin:/bin"), commands[0])


class InterpreterTests(unittest.TestCase):
    def test_nothing_shipped_resolves_its_interpreter_through_the_environment(self):
        # `#!/usr/bin/env python3` asks PATH which python runs this, and the bar
        # starts the helper with whatever environment the shell had. Every
        # executable in the tree names its interpreter outright instead.
        loose = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            with open(path, "rb") as handle:
                first = handle.readline(128)
            if first.startswith(b"#!/usr/bin/env"):
                loose.append(str(path.relative_to(ROOT)))
        self.assertEqual(loose, [])

    def test_the_helper_runs_isolated_from_the_user_site_directory(self):
        # `-I`. HOME reaches the helper, and HOME is what Python derives
        # ~/.local/lib/pythonX/site-packages from, where a .pth file runs its
        # own contents at startup before the first line of this plugin. Nothing
        # here wants anything from there.
        self.assertEqual((ROOT / "headsetctl").read_bytes().split(b"\n")[0],
                         b"#!/usr/bin/python3 -I")


if __name__ == "__main__":
    unittest.main()
