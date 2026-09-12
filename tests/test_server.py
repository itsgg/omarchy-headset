"""Commands in, and how a group of features becomes one message."""
import json
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset import server  # noqa: E402
from headset.errors import HeadsetError  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_words_from_a_terminal(self):
        self.assertEqual(server.parse_command("set noise anc"), {"set": {"noise": "anc"}})
        self.assertEqual(server.parse_command("toggle speak_to_chat"), {"toggle": "speak_to_chat"})
        self.assertEqual(server.parse_command("refresh"), {"refresh": True})
        self.assertEqual(server.parse_command("  "), {})

    def test_a_typed_value_keeps_its_type(self):
        self.assertEqual(server.parse_command("set ambient_level 6"), {"set": {"ambient_level": 6}})
        self.assertEqual(server.parse_command("set dsee true"), {"set": {"dsee": True}})
        self.assertEqual(server.parse_command("set eq_bands [1,2,3,4,5]"),
                         {"set": {"eq_bands": [1, 2, 3, 4, 5]}})

    def test_json_from_the_panel(self):
        self.assertEqual(server.parse_command('{"set": {"noise": "ambient", "ambient_level": 6}}'),
                         {"set": {"noise": "ambient", "ambient_level": 6}})

    def test_nonsense_is_refused_with_something_readable(self):
        for text in ("dance", "set", "set only-one-word", "{not json"):
            with self.assertRaises(HeadsetError, msg=text):
                server.parse_command(text)


class FakeSession:
    def __init__(self):
        self.sent = []

    def set(self, payload, message_type, key="", on_done=None, label=""):
        self.sent.append({"payload": bytes(payload), "type": message_type, "key": key})


class FakeDevice:
    def __init__(self, support):
        self.support = support
        self.session = FakeSession()
        self.channel = 9
        self.connected = True
        self.ready = True

    def supports(self, feature):
        from headset.drivers import sony_mdr
        control = sony_mdr.DRIVER.control(feature)
        return bool(control and self.support.get(control.record))


def owner(state_values, support=None):
    """A real Owner with a fake headset behind it.

    Built through the constructor rather than assembled field by field: the
    hand-built version silently went stale every time Owner gained a field, and
    the tests then failed for a reason that had nothing to do with the tests.
    """
    from headset.drivers import sony_mdr
    made = server.Owner("AA:BB:CC:DD:EE:FF", "WH-1000XM5", listener=None, stdin=None)
    made.stdout_open = False
    made.state.observe(state_values)
    made.device = FakeDevice(support or {record.id: True for record in sony_mdr.DRIVER.records})
    return made


BASE = {"noise": "off", "ambient_level": 20, "focus_on_voice": False,
        "eq_bands": [0, 5, 7, 7, 9], "eq_clear_bass": -1, "speak_to_chat": False}


class IdleTests(unittest.TestCase):
    def test_an_owner_that_has_given_up_waits_rather_than_spinning(self):
        # Once the retry deadline is in the past and will never be acted on, the
        # loop woke every 50ms for the rest of the session.
        import time

        own = owner(BASE)
        own.device = None
        own.hopeless = True
        own.next_attempt = time.monotonic() - 60
        self.assertGreaterEqual(own._timeout(), server.IDLE_TICK)

    def test_an_owner_still_trying_wakes_for_its_next_attempt(self):
        import time

        own = owner(BASE)
        own.device = None
        own.hopeless = False
        own.next_attempt = time.monotonic() + 0.1
        self.assertLess(own._timeout(), server.IDLE_TICK)


class ApplyTests(unittest.TestCase):
    def test_features_sharing_a_message_are_written_once(self):
        # Mode, level and focus travel in one frame. Three frames would have each
        # overwritten the last two.
        own = owner(BASE)
        own.apply({"noise": "ambient", "ambient_level": 6, "focus_on_voice": True})
        self.assertEqual(len(own.device.session.sent), 1)
        self.assertEqual(own.device.session.sent[0]["payload"].hex(" "), "68 17 01 01 01 01 06")

    def test_features_in_different_messages_are_written_separately(self):
        own = owner(BASE)
        own.apply({"noise": "anc", "speak_to_chat": True})
        self.assertEqual(len(own.device.session.sent), 2)
        self.assertEqual({frame["key"] for frame in own.device.session.sent},
                         {"noise", "speak_to_chat"})

    def test_a_write_is_shown_at_once_and_marked_unconfirmed(self):
        own = owner(BASE)
        own.apply({"noise": "anc"})
        self.assertEqual(own.state.snapshot()["noise"], "anc")
        self.assertEqual(own.state.unconfirmed(), ["noise"])

    def test_a_control_this_headset_ignores_is_refused_rather_than_sent(self):
        own = owner(BASE)
        own.apply({"touch_sensor": False})
        self.assertEqual(own.device.session.sent, [])
        self.assertIn("read-only", own.error)

    def test_a_control_the_headset_does_not_have_is_refused(self):
        own = owner(BASE, support={"noise": True})
        own.apply({"speak_to_chat": True})
        self.assertEqual(own.device.session.sent, [])
        self.assertIn("no speak to chat", own.error)

    def test_an_ambient_setting_outside_ambient_mode_is_refused(self):
        # The headset discards it, so sending it would be a write into a void.
        own = owner(BASE)
        own.apply({"ambient_level": 6})
        self.assertEqual(own.device.session.sent, [])
        self.assertIn("ambient mode", own.error)

    def test_the_same_command_may_enter_ambient_mode_and_set_the_level(self):
        own = owner(BASE)
        own.apply({"noise": "ambient", "ambient_level": 6})
        self.assertEqual(len(own.device.session.sent), 1)
        self.assertEqual(own.error, "")

    def test_a_bad_value_is_reported_and_nothing_is_written(self):
        own = owner(BASE)
        own.apply({"noise": "loud"})
        self.assertEqual(own.device.session.sent, [])
        self.assertNotEqual(own.error, "")

    def test_a_preset_and_a_band_curve_are_both_sent(self):
        # They used to share a coalescing key, so whichever encoder was reached
        # first ran with both sets of values and wrote only its own.
        own = owner(BASE)
        own.apply({"eq_preset": "vocal", "eq_bands": [1, 1, 1, 1, 1]})
        sent = [frame["payload"] for frame in own.device.session.sent]
        self.assertEqual(len(sent), 2)
        self.assertTrue(any(frame[2] == 0x14 for frame in sent), "the preset")
        self.assertTrue(any(frame[2] == 0xA0 for frame in sent), "the band curve")

    def test_the_published_payload_says_what_is_writable_and_what_is_available(self):
        own = owner(BASE)
        payload = own.payload()
        self.assertTrue(payload["controls"]["noise"]["writable"])
        self.assertFalse(payload["controls"]["touch_sensor"]["writable"])
        self.assertFalse(payload["controls"]["ambient_level"]["available"])
        self.assertEqual(payload["device"]["name"], "WH-1000XM5")
        json.dumps(payload)  # it has to survive the socket


if __name__ == "__main__":
    unittest.main()
