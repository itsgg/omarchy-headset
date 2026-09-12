"""The rule that keeps the panel honest after a write."""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset.state import CONVERGE_SECONDS, State  # noqa: E402


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        self.state = State(now=lambda: self.clock[0])
        self.state.observe({"noise": "off", "battery": 35})

    def test_a_reading_is_shown_as_it_arrives(self):
        self.assertEqual(self.state.snapshot()["noise"], "off")
        self.assertEqual(self.state.observe({"battery": 34}), {"battery": 34})
        self.assertEqual(self.state.observe({"battery": 34}), {})

    def test_a_written_value_is_shown_at_once_and_marked_unconfirmed(self):
        self.assertEqual(self.state.expect({"noise": "anc"}), {"noise": "anc"})
        self.assertEqual(self.state.snapshot()["noise"], "anc")
        self.assertEqual(self.state.unconfirmed(), ["noise"])

    def test_the_stale_echo_of_the_replaced_value_is_ignored(self):
        # The device keeps reporting the old value to the session that wrote it,
        # for up to a minute, while the hardware has already changed.
        self.state.expect({"noise": "anc"})
        self.assertEqual(self.state.observe({"noise": "off"}), {})
        self.assertEqual(self.state.snapshot()["noise"], "anc")

    def test_the_device_catching_up_clears_the_mark(self):
        self.state.expect({"noise": "anc"})
        self.state.observe({"noise": "anc"})
        self.assertEqual(self.state.unconfirmed(), [])
        self.assertEqual(self.state.snapshot()["noise"], "anc")

    def test_a_third_value_means_something_else_changed_it_and_wins(self):
        # A press of the button on the earcup, which the device announces.
        self.state.expect({"noise": "anc"})
        self.assertEqual(self.state.observe({"noise": "ambient"}), {"noise": "ambient"})
        self.assertEqual(self.state.unconfirmed(), [])

    def test_a_write_that_never_lands_is_reported_rather_than_hidden(self):
        self.state.expect({"touch_sensor": False})
        self.assertEqual(self.state.ignored(), [])
        self.clock[0] += CONVERGE_SECONDS + 1
        self.assertEqual(self.state.ignored(), ["touch_sensor"])

    def test_writing_the_value_it_already_has_marks_nothing(self):
        self.assertEqual(self.state.expect({"noise": "off"}), {})
        self.assertEqual(self.state.unconfirmed(), [])

    def test_writing_twice_keeps_the_original_value_as_the_stale_one(self):
        self.state.expect({"noise": "anc"})
        self.state.expect({"noise": "ambient"})
        # "off" is still the echo to ignore, not "anc".
        self.assertEqual(self.state.observe({"noise": "off"}), {})
        self.assertEqual(self.state.snapshot()["noise"], "ambient")

    def test_zero_and_false_are_values_rather_than_absences(self):
        self.state.observe({"ambient_level": 0, "focus_on_voice": False})
        self.assertEqual(self.state.snapshot()["ambient_level"], 0)
        self.assertIs(self.state.snapshot()["focus_on_voice"], False)
        self.state.expect({"ambient_level": 0})
        self.assertEqual(self.state.unconfirmed(), [])

    def test_writing_the_same_pending_value_twice_does_not_revert_the_panel(self):
        # Clicking ANC twice before the headset caught up used to drop the mark
        # and show "off" again, which is the value being replaced.
        self.state.expect({"noise": "anc"})
        self.state.expect({"noise": "anc"})
        self.assertEqual(self.state.snapshot()["noise"], "anc")
        self.assertEqual(self.state.unconfirmed(), ["noise"])
        self.assertEqual(self.state.observe({"noise": "off"}), {})
        self.assertEqual(self.state.snapshot()["noise"], "anc")

    def test_a_write_the_headset_never_applied_stops_suppressing_real_readings(self):
        # Otherwise the panel shows the unapplied value for ever, and a change
        # made on the headset itself is read as our own stale echo and ignored.
        self.state.expect({"noise": "anc"})
        self.clock[0] += CONVERGE_SECONDS + 1
        self.assertEqual(self.state.ignored(), ["noise"])
        self.assertEqual(self.state.observe({"noise": "off"}), {"noise": "off"})
        self.assertEqual(self.state.snapshot()["noise"], "off")
        self.assertEqual(self.state.unconfirmed(), [])
        self.assertEqual(self.state.ignored(), [])

    def test_forgetting_drops_the_readings_and_the_marks(self):
        self.state.expect({"noise": "anc"})
        self.state.forget()
        self.assertEqual(self.state.snapshot(), {})
        self.assertEqual(self.state.unconfirmed(), [])


if __name__ == "__main__":
    unittest.main()
