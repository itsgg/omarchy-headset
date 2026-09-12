"""The equaliser this machine runs, for a headset whose hardware has none.

Nothing here starts PipeWire. The graph is checked as text because the text is
the contract: a filter chain is a configuration file, and a wrong number in it
fails silently by sounding wrong rather than by raising.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from headset import equaliser  # noqa: E402
from headset.errors import HeadsetError  # noqa: E402

FLAT = [0.0] * 10
ADDRESS = "3C:B0:ED:50:BC:9C"


class ClampTests(unittest.TestCase):
    def test_a_gain_is_held_inside_the_range_the_panel_offers(self):
        self.assertEqual(equaliser.clamp(99), 10)
        self.assertEqual(equaliser.clamp(-99), -10)
        self.assertEqual(equaliser.clamp(0), 0)

    def test_a_gain_lands_on_a_half_decibel_because_that_is_the_step(self):
        self.assertEqual(equaliser.clamp(1.4), 1.5)
        self.assertEqual(equaliser.clamp(1.2), 1.0)
        self.assertEqual(equaliser.clamp(-1.4), -1.5)

    def test_nonsense_reads_as_flat_rather_than_raising(self):
        # It arrives as JSON from a panel, so it can be anything at all. JSON
        # carries infinity and not-a-number, and rounding either one raises, so
        # this is a crash in the helper rather than a wrong number in the ear.
        for value in (None, "loud", {}, [], True, float("nan"), float("inf"),
                      float("-inf"), "nan", "Infinity"):
            with self.subTest(value=value):
                self.assertIsInstance(equaliser.clamp(value), float)
        for value in (None, "loud", float("nan"), float("inf"), "nan"):
            with self.subTest(value=value):
                self.assertEqual(equaliser.clamp(value), 0.0)


class GraphTests(unittest.TestCase):
    def test_one_filter_per_band_and_a_trim_after_them(self):
        text = equaliser.graph(FLAT)
        self.assertEqual(text.count("bq_peaking"), len(equaliser.FREQUENCIES))
        self.assertEqual(text.count("label = linear"), 1)
        for frequency in equaliser.FREQUENCIES:
            self.assertIn(f'"Freq" = {frequency}', text)

    def test_every_filter_is_chained_to_the_next_with_nothing_left_dangling(self):
        text = equaliser.graph(FLAT)
        # Ten bands plus the trim is eleven nodes, so ten links.
        self.assertEqual(text.count("output = "), len(equaliser.FREQUENCIES))
        self.assertIn('{ output = "b0:Out" input = "b1:In" }', text)
        self.assertIn('{ output = "b9:Out" input = "trim:In" }', text)

    def test_a_flat_equaliser_does_not_turn_the_volume_down(self):
        self.assertIn('"Gain" = 1.0', equaliser.graph(FLAT))

    def test_the_trim_takes_back_the_boost_so_the_chain_cannot_clip(self):
        # Boosting and passing the result on unchanged is how a filter chain
        # clips, so the loudest thing through it is no louder than it went in.
        import math
        gains = [0.0] * 5 + [6.0] + [0.0] * 4
        # Rounded down, so the trim is never nudged back above the value that
        # makes the guarantee true.
        exact = math.floor(10 ** (-6 / 20.0) * 10000) / 10000
        self.assertIn(f'"Gain" = {exact}', equaliser.graph(gains))

    def test_the_trim_is_never_rounded_up_past_the_guarantee(self):
        import math
        for gains in ([0.0] * 5 + [10.0, 10.0] + [0.0] * 3, [10.0] * 10, [7.5] * 3 + [0.0] * 7):
            with self.subTest(gains=gains):
                trim = math.floor(10 ** (-equaliser.headroom(gains) / 20.0) * 10000) / 10000
                self.assertLessEqual(trim, 10 ** (-equaliser.headroom(gains) / 20.0))

    def test_two_neighbouring_boosts_are_trimmed_by_more_than_either_of_them(self):
        # Peaking filters overlap. Trimming by the tallest band alone leaves the
        # sum between two of them above unity, which is the clipping the trim
        # was there to prevent, so the response is measured rather than guessed.
        alone = equaliser.headroom([0.0] * 5 + [10.0] + [0.0] * 4)
        together = equaliser.headroom([0.0] * 5 + [10.0, 10.0] + [0.0] * 3)
        self.assertAlmostEqual(alone, 10.0, places=1)
        self.assertGreater(together, alone + 2)

    def test_the_measured_chain_never_comes_out_louder_than_it_went_in(self):
        for gains in ([10.0] * 10, [0.0] * 5 + [10.0] + [0.0] * 4,
                      [10.0, -10.0] * 5, [3.5, 4.0, 4.0, 0.0, 0.0, 0.0, 2.0, 6.0, 6.0, 1.0]):
            with self.subTest(gains=gains):
                levels = [equaliser.clamp(g) for g in gains]
                trim = 10 ** (-equaliser.headroom(levels) / 20.0)
                peak = max(equaliser.response(levels, 20.0 * (1000.0 ** (n / 240)))
                           for n in range(241))
                self.assertLessEqual(peak * trim, 1.001)

    def test_the_trim_holds_at_the_other_rate_pipewire_might_be_running(self):
        # PipeWire recomputes the coefficients at whatever the graph negotiated,
        # and the warping differs, most at 16 kHz. Measuring one rate and
        # running at the other leaves the output clipping.
        gains = [0.0] * 8 + [10.0, 10.0]
        trim = 10 ** (-equaliser.headroom(gains) / 20.0)
        for rate in equaliser.RATES:
            peak = max(equaliser.response(gains, 20.0 * (1000.0 ** (n / 240)), rate)
                       for n in range(241) if 20.0 * (1000.0 ** (n / 240)) * 2 < rate)
            with self.subTest(rate=rate):
                self.assertLessEqual(peak * trim, 1.0001)

    def test_a_curve_that_only_cuts_is_not_quietened_any_further(self):
        self.assertEqual(equaliser.headroom([-10.0] * 10), 0.0)

    def test_a_cut_only_curve_is_not_trimmed_further(self):
        self.assertIn('"Gain" = 1.0', equaliser.graph([-6.0] + [0.0] * 9))

    def test_a_short_or_long_list_still_produces_ten_bands(self):
        self.assertEqual(equaliser.graph([3.0]).count("bq_peaking"), 10)
        self.assertEqual(equaliser.graph([1.0] * 40).count("bq_peaking"), 10)


class QuotingTests(unittest.TestCase):
    """The headset's name comes from BlueZ, and a user can rename a headset."""

    def test_a_quote_in_the_name_cannot_end_the_string_early(self):
        text = equaliser.fragment('My "Best" Buds', ADDRESS, "sink", FLAT)
        self.assertIn(r'node.description = "My \"Best\" Buds equaliser"', text)
        # Every quote the name contributed is escaped, so none of them closes a
        # string: the three places the name appears carry two quotes each.
        self.assertEqual(text.count(r'\"'), 6)
        self.assertNotIn('"Best"', text)

    def test_a_newline_in_the_name_cannot_add_a_line_of_configuration(self):
        text = equaliser.fragment("Ear\nnode.name = evil", ADDRESS, "sink", FLAT)
        self.assertNotIn("\nnode.name = evil", text)

    def test_a_backslash_cannot_escape_the_closing_quote(self):
        self.assertIn(r'"Ear\\ equaliser"', equaliser.fragment("Ear\\", ADDRESS, "sink", FLAT))

    def test_the_sink_name_is_quoted_too(self):
        self.assertIn(r'target.object  = "a\"b"', equaliser.fragment("x", ADDRESS, 'a"b', FLAT))


class FragmentTests(unittest.TestCase):
    def test_the_chain_plays_into_the_headset_and_captures_under_its_own_name(self):
        text = equaliser.fragment("Nothing Ear", ADDRESS, "bluez_output.X.1", FLAT)
        self.assertIn('node.name        = "headset_eq.3cb0ed50bc9c"', text)
        self.assertIn('target.object  = "bluez_output.X.1"', text)
        self.assertIn("media.class      = Audio/Sink", text)

    def test_the_sink_is_named_for_the_address_so_two_headsets_cannot_collide(self):
        first = equaliser.fragment("A", "AA:BB:CC:DD:EE:FF", "sink", FLAT)
        second = equaliser.fragment("B", ADDRESS, "sink", FLAT)
        self.assertNotEqual(first, second)
        self.assertIn("headset_eq.aabbccddeeff", first)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(__import__("tempfile").TemporaryDirectory())
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": self.home})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_a_curve_survives_being_written_and_read_back(self):
        gains = [1.0, -2.5, 0.0, 3.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.5]
        equaliser.save(ADDRESS, True, gains)
        self.assertEqual(equaliser.load(ADDRESS), {"eq_enabled": True, "eq_gains": gains})

    def test_an_unwritten_headset_reads_as_nothing_rather_than_as_flat_and_on(self):
        self.assertEqual(equaliser.load("11:22:33:44:55:66"), {})

    def test_a_corrupt_file_is_not_a_crash_on_the_next_connect(self):
        path = equaliser.state_path(ADDRESS)
        path.write_text("{not json")
        self.assertEqual(equaliser.load(ADDRESS), {})
        path.write_text(json.dumps([1, 2, 3]))
        self.assertEqual(equaliser.load(ADDRESS), {})

    def test_gains_from_the_file_are_clamped_because_a_file_can_be_edited(self):
        equaliser.state_path(ADDRESS).write_text(
            json.dumps({"eq_enabled": True, "eq_gains": [99, -99, "x"]}))
        self.assertEqual(equaliser.load(ADDRESS)["eq_gains"], [10.0, -10.0, 0.0])

    def test_a_headset_remembers_its_equaliser_across_a_reconnect(self):
        # The reconnect is the normal case: it happens every time the headset is
        # put on. An equaliser that forgets it was on is worse than none.
        equaliser.save(ADDRESS, True, [4.0] + [0.0] * 9)
        fresh = equaliser.Equaliser(ADDRESS, "Nothing Ear")
        self.assertTrue(fresh.enabled)
        self.assertEqual(fresh.gains[0], 4.0)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.home = self.enterContext(__import__("tempfile").TemporaryDirectory())
        patcher = patch.dict(os.environ, {"XDG_STATE_HOME": self.home,
                                          "XDG_RUNTIME_DIR": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.unit = equaliser.Equaliser(ADDRESS, "Nothing Ear")

    def test_a_chain_with_nowhere_to_play_refuses_rather_than_running_into_nothing(self):
        with self.assertRaises(HeadsetError):
            self.unit.start("")

    def test_a_missing_pipewire_is_reported_and_not_a_traceback(self):
        with patch.object(self.unit, "available", return_value=False):
            with self.assertRaises(HeadsetError):
                self.unit.start("bluez_output.X.1")

    def test_switching_off_reports_off_even_though_the_curve_is_kept(self):
        self.unit.enabled = True
        self.unit.gains = [5.0] + [0.0] * 9
        with patch.object(self.unit, "stop"), patch.object(self.unit, "route_back"):
            self.unit.apply("sink", enabled=False)
        self.assertFalse(self.unit.enabled)
        self.assertEqual(equaliser.load(ADDRESS)["eq_gains"][0], 5.0)

    def test_a_gain_list_that_is_not_a_list_is_refused(self):
        for bad in ("loud", 42, {"a": 1}, 3.5):
            with self.subTest(bad=bad):
                with self.assertRaises(HeadsetError):
                    self.unit.apply("sink", gains=bad)

    def test_the_routing_waits_on_later_passes_rather_than_in_a_sleep(self):
        # A wait here is a wait for everything: this runs inside the helper's one
        # event loop, which also carries the headset session and the panel.
        self.unit.pending_sink = "bluez_output.X.1"
        self.unit.settle_until = __import__("time").monotonic() + 5
        with patch.object(type(self.unit), "running", property(lambda self: True)), \
             patch.object(self.unit, "_sink_index", return_value=""), \
             patch.object(self.unit, "route_into") as routed:
            self.unit.settle()
        routed.assert_not_called()
        self.assertEqual(self.unit.pending_sink, "bluez_output.X.1")

    def test_the_audio_moves_as_soon_as_the_chain_has_somewhere_to_put_it(self):
        self.unit.pending_sink = "bluez_output.X.1"
        self.unit.settle_until = __import__("time").monotonic() + 5
        with patch.object(type(self.unit), "running", property(lambda self: True)), \
             patch.object(self.unit, "_sink_index", return_value="7"), \
             patch.object(self.unit, "route_into") as routed:
            self.unit.settle()
        routed.assert_called_once_with("bluez_output.X.1")
        self.assertEqual(self.unit.pending_sink, "")

    def test_stopping_a_chain_does_not_wait_for_it_to_die(self):
        # The wait was five seconds in the worst case, inside the one event loop,
        # on every curve change. A slow child stalled the headset session too.
        import time as clock

        class Slow:
            def __init__(self): self.signalled = []
            def poll(self): return None
            def terminate(self): self.signalled.append("term")
            def kill(self): self.signalled.append("kill")
            def wait(self, timeout=None): raise AssertionError("stop() must not wait")

        slow = Slow()
        self.unit.process = slow
        began = clock.monotonic()
        self.unit.stop()
        self.assertLess(clock.monotonic() - began, 0.5)
        self.assertEqual(slow.signalled, ["term"])
        self.assertEqual(len(self.unit.dying), 1)

    def test_a_chain_that_ignores_the_ask_is_killed_on_a_later_pass(self):
        class Stubborn:
            def __init__(self): self.signalled = []
            def poll(self): return None
            def terminate(self): self.signalled.append("term")
            def kill(self): self.signalled.append("kill")

        stubborn = Stubborn()
        self.unit.process = stubborn
        self.unit.stop()
        self.unit.bury_the_dead()
        self.assertEqual(stubborn.signalled, ["term"])
        # Once its grace has run out, and not before.
        self.unit.dying = [(stubborn, 0.0)]
        self.unit.bury_the_dead()
        self.assertEqual(stubborn.signalled, ["term", "kill"])

    def test_a_chain_that_went_quietly_is_not_kept_on_the_list(self):
        class Gone:
            def poll(self): return 0
            def terminate(self): pass
            def kill(self): raise AssertionError("already gone")

        self.unit.dying = [(Gone(), 0.0)]
        self.unit.bury_the_dead()
        self.assertEqual(self.unit.dying, [])

    def test_a_chain_that_never_comes_up_stops_being_waited_for(self):
        self.unit.pending_sink = "bluez_output.X.1"
        self.unit.settle_until = __import__("time").monotonic() - 1
        with patch.object(self.unit, "route_into") as routed:
            self.unit.settle()
        routed.assert_not_called()
        self.assertEqual(self.unit.pending_sink, "")

    def test_a_curve_arriving_alone_does_not_switch_the_equaliser_on(self):
        with patch.object(self.unit, "stop"), patch.object(self.unit, "route_back"):
            self.unit.apply("sink", gains=[1.0] * 10)
        self.assertFalse(self.unit.enabled)

    def test_a_stopped_chain_reports_itself_as_off_however_it_was_left(self):
        self.unit.enabled = True
        self.assertEqual(self.unit.state()["eq_enabled"], False)
        self.assertEqual(self.unit.state()["eq_sink"], "")

    def test_the_equaliser_never_remembers_its_own_sink_as_the_way_back(self):
        # Routing into the chain twice must not leave the chain pointing at
        # itself, which is a loop with no audio and no way out of it.
        with patch.object(self.unit, "default_sink", return_value=self.unit.sink_name), \
             patch.object(self.unit, "streams_on", return_value=[]), \
             patch.object(self.unit, "_pactl", return_value=""):
            self.unit.route_into("bluez_output.X.1")
        self.assertEqual(self.unit.previous_sink, "bluez_output.X.1")

    def test_nothing_is_killed_unless_it_is_pipewire_in_our_own_directory(self):
        killed = []
        with patch("os.kill", side_effect=lambda pid, sig: killed.append(pid)):
            self.unit.reap_strays()
        # This machine is running the session's own PipeWire, and it must survive.
        self.assertEqual(killed, [])


class StreamTests(unittest.TestCase):
    LISTING = """Sink Input #12
	Driver: PipeWire
	Sink: 51
	Format: pcm
Sink Input #14
	Driver: PipeWire
	Sink: 60
"""

    def setUp(self):
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.unit = equaliser.Equaliser(ADDRESS, "Nothing Ear")

    def test_only_what_is_playing_into_that_one_sink_is_moved(self):
        def fake(arguments):
            if arguments[:3] == ["list", "sinks", "short"]:
                return "51\tbluez_output.X.1\tmodule\n60\talsa_output.pci\tmodule\n"
            return self.LISTING

        with patch.object(self.unit, "_pactl", side_effect=fake):
            self.assertEqual(self.unit.streams_on("bluez_output.X.1"), ["12"])

    def test_a_sink_that_is_not_there_moves_nothing_rather_than_everything(self):
        with patch.object(self.unit, "_pactl", return_value=""):
            self.assertEqual(self.unit.streams_on("bluez_output.X.1"), [])


if __name__ == "__main__":
    unittest.main()
