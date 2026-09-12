"""Commands in, and how a group of features becomes one message."""
import json
import sys
import unittest
from unittest.mock import patch
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

    def read(self):
        self.reads = getattr(self, "reads", 0) + 1

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
        self.assertIn("does not accept changes", own.refused["touch_sensor"])

    def test_a_control_the_headset_does_not_have_is_refused(self):
        own = owner(BASE, support={"noise": True})
        own.apply({"speak_to_chat": True})
        self.assertEqual(own.device.session.sent, [])
        self.assertIn("does not have", own.refused["speak_to_chat"])

    def test_an_ambient_setting_outside_ambient_mode_is_refused(self):
        # The headset discards it, so sending it would be a write into a void.
        own = owner(BASE)
        own.apply({"ambient_level": 6})
        self.assertEqual(own.device.session.sent, [])
        self.assertIn("ambient mode", own.refused["ambient_level"])

    def test_the_same_command_may_enter_ambient_mode_and_set_the_level(self):
        own = owner(BASE)
        own.apply({"noise": "ambient", "ambient_level": 6})
        self.assertEqual(len(own.device.session.sent), 1)
        self.assertEqual(own.refused, {})

    def test_a_bad_value_is_reported_and_nothing_is_written(self):
        own = owner(BASE)
        own.apply({"noise": "loud"})
        self.assertEqual(own.device.session.sent, [])
        # On the row it is about, so the panel can say which setting failed.
        self.assertIn("noise", own.refused)

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


class HostEqualiserTests(unittest.TestCase):
    """The equaliser this machine runs, and the headsets it must stay out of.

    A Sony has an equaliser in the earcups. Offering a second one in the same
    panel is a question nobody should have to answer, and worse, the two would
    disagree about what the headset sounds like.
    """

    def test_a_headset_with_its_own_equaliser_is_never_offered_this_one(self):
        own = owner(BASE)
        self.assertFalse(own.host_equaliser)
        payload = own.payload()
        self.assertNotIn("eq_enabled", payload["controls"])
        self.assertNotIn("eq_gains", payload["controls"])
        self.assertEqual(payload["equaliser"], {})

    def test_the_sony_equaliser_is_untouched_by_the_one_this_machine_runs(self):
        own = owner(BASE)
        payload = own.payload()
        self.assertTrue(payload["controls"]["eq_bands"]["supported"])
        self.assertTrue(payload["controls"]["eq_bands"]["writable"])
        self.assertEqual(payload["state"]["eq_bands"], [0, 5, 7, 7, 9])

    def test_a_band_sent_to_a_headset_with_its_own_equaliser_goes_to_the_headset(self):
        own = owner(BASE)
        own.apply({"eq_bands": [1, 2, 3, 4, 5]})
        self.assertEqual(len(own.device.session.sent), 1)
        self.assertFalse(own.equaliser.running)

    def test_a_headset_without_one_is_offered_the_equaliser_this_machine_runs(self):
        from headset.drivers import fastpair

        own = server.Owner("AA:BB:CC:DD:EE:FF", "Nothing Ear", listener=None, stdin=None)
        own.stdout_open = False
        own.driver = fastpair.DRIVER
        own.host_equaliser = not any(c.id == "eq_bands" for c in own.driver.controls)
        self.assertTrue(own.host_equaliser)

    def test_the_curve_row_is_unavailable_until_the_equaliser_is_switched_on(self):
        # Unavailable, not unwritable: a row that says "does not accept changes"
        # about a control that is merely switched off is a lie about the hardware.
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = False
        with patch.object(own.equaliser, "available", return_value=True):
            payload = own.payload()
        self.assertTrue(payload["controls"]["eq_gains"]["writable"])
        self.assertFalse(payload["controls"]["eq_gains"]["available"])

    def test_nothing_is_offered_when_pipewire_cannot_run_a_filter_chain(self):
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=False):
            payload = own.payload()
        self.assertNotIn("eq_gains", payload["controls"])

    def test_a_band_for_the_host_equaliser_never_reaches_the_headset(self):
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own.equaliser, "apply") as applied, \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.apply({"eq_gains": [1.0] * 10})
        applied.assert_called_once()
        self.assertEqual(own.device.session.sent, [])

    def test_one_command_carrying_both_is_split_and_both_halves_arrive(self):
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own.equaliser, "apply"), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.apply({"eq_gains": [1.0] * 10, "noise": "anc"})
        self.assertEqual(len(own.device.session.sent), 1)

    def test_an_equaliser_left_on_comes_back_when_the_headset_does(self):
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own.equaliser, "apply") as applied, \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.tend_equaliser()
        applied.assert_called_once_with("bluez_output.X.1")

    def test_a_sink_that_has_not_arrived_yet_is_waited_for_rather_than_failed(self):
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own.equaliser, "apply") as applied, \
             patch.object(own, "sink_name", return_value=""):
            own.tend_equaliser()
            own.restore_after = 0.0
            own.tend_equaliser()
        applied.assert_not_called()
        self.assertTrue(own.equaliser.enabled)

    def test_a_headset_with_its_own_equaliser_never_starts_this_one(self):
        own = owner(BASE)
        own.equaliser.enabled = True
        with patch.object(own.equaliser, "apply") as applied:
            own.tend_equaliser()
        applied.assert_not_called()

    def test_a_chain_outliving_its_helper_is_cleared_before_anything_else(self):
        # The chain is a separate process, so killing the helper outright leaves
        # audio going through a curve nothing owns while the panel says off.
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = False
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own.equaliser, "reap_strays") as reaped:
            own.tend_equaliser()
            own.tend_equaliser()
        reaped.assert_called_once()

    def test_a_chain_playing_into_a_headset_that_has_gone_is_shut_down(self):
        # The chain's own output points at the headset's sink. When that sink
        # goes, audio still goes into the chain and comes out nowhere, which is
        # silence with no obvious way back to the speakers.
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = True
        own.restored = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(type(own.equaliser), "running", property(lambda self: True)), \
             patch.object(own.equaliser, "stop") as stopped, \
             patch.object(own, "sink_name", return_value=""):
            own.tend_equaliser()
            stopped.assert_not_called()
            own.restore_after = 0.0
            own.tend_equaliser()
        stopped.assert_called_once()

    def test_a_sink_missing_for_a_moment_does_not_stop_the_equaliser(self):
        # A codec change tears the transport down and builds it again, and the
        # panel offers the codec, so this is a thing the user asks for. Acting on
        # the first look stopped the equaliser and started it four seconds later.
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = True
        own.restored = True
        sinks = iter(["", "bluez_output.X.1", "", "bluez_output.X.1"])
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(type(own.equaliser), "running", property(lambda self: True)), \
             patch.object(own.equaliser, "stop") as stopped, \
             patch.object(own, "sink_name", side_effect=lambda: next(sinks)):
            for _ in range(4):
                own.restore_after = 0.0
                own.tend_equaliser()
        stopped.assert_not_called()

    def test_a_running_chain_with_its_headset_still_there_is_left_alone(self):
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = True
        own.restored = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(type(own.equaliser), "running", property(lambda self: True)), \
             patch.object(own.equaliser, "stop") as stopped, \
             patch.object(own.equaliser, "apply") as applied, \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.tend_equaliser()
        stopped.assert_not_called()
        applied.assert_not_called()

    def test_a_gain_list_that_is_not_a_list_is_refused_and_not_a_crash(self):
        # It arrives as JSON from a panel. A string would be read one character
        # per band, and a number is not iterable at all.
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            for bad in ("loud", 42, {"a": 1}):
                with self.subTest(bad=bad):
                    own.apply({"eq_gains": bad})
                    self.assertIn("eq_gains", own.refused)


class MessageLifetimeTests(unittest.TestCase):
    """A message is about one action and must not outlive it.

    Left to persist, one refused write puts a line on the panel that stays there
    through everything the user does next, so it reads as a message appearing at
    random rather than as an answer to what they just did.
    """

    def test_a_refusal_is_gone_by_the_time_the_next_command_is_acted_on(self):
        own = owner(BASE)
        own.apply({"touch_sensor": False})
        self.assertIn("touch_sensor", own.refused)
        own.handle(json.dumps({"refresh": True}))
        self.assertEqual(own.refused, {})

    def test_an_error_does_not_survive_the_next_command_either(self):
        own = owner(BASE)
        own.device = None
        own.apply({"noise": "anc"})
        self.assertTrue(own.error)
        own.handle(json.dumps({"refresh": True}))
        self.assertEqual(own.error, "")

    def test_a_refusal_names_the_setting_it_is_about(self):
        own = owner(BASE, support={"noise": True})
        own.apply({"speak_to_chat": True, "touch_sensor": False})
        self.assertEqual(sorted(own.refused), ["speak_to_chat", "touch_sensor"])

    def test_what_succeeded_is_not_blamed_for_what_did_not(self):
        own = owner(BASE)
        own.apply({"noise": "anc", "touch_sensor": False})
        self.assertEqual(list(own.refused), ["touch_sensor"])
        self.assertEqual(len(own.device.session.sent), 1)

    def test_one_command_keeps_both_halves_reasons(self):
        # One command can carry an equaliser setting and a headset setting, and
        # the second half used to replace the first half's reason wholesale.
        own = owner(BASE, support={"noise": True})
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.apply({"eq_gains": "not a list", "speak_to_chat": True})
        self.assertIn("eq_gains", own.refused)
        self.assertIn("speak_to_chat", own.refused)

    def test_a_missing_filter_chain_is_not_reported_as_the_opposite(self):
        # It used to say the headset had an equaliser of its own, which is the
        # reason this one is offered at all.
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=False):
            own.apply({"eq_enabled": True})
        self.assertIn("PipeWire", own.refused["eq_enabled"])
        self.assertNotIn("of its own", own.refused["eq_enabled"])

    def test_a_headset_with_its_own_equaliser_is_told_that_and_not_something_else(self):
        own = owner(BASE)
        own.host_equaliser = False
        own.apply({"eq_enabled": True})
        self.assertIn("of its own", own.refused["eq_enabled"])

    def test_the_panel_is_told_which_setting_was_refused(self):
        own = owner(BASE)
        own.apply({"touch_sensor": False})
        self.assertIn("touch_sensor", own.payload()["refused"])


class MessageWidthTests(unittest.TestCase):
    """A reason is drawn under its row, and the panel is about 380 pixels wide.

    The budget is checked on both sides because the strings live on both sides:
    the panel's own copy is checked in the JavaScript tests, and these are the
    ones the helper writes. Every previous fix for a wrapping line was one string
    at a time, and the next person adding a refusal would not know there was a
    limit at all.
    """

    LIMIT = 54

    def _reasons(self) -> list:
        own = owner(BASE, support={"noise": True})
        own.apply({"speak_to_chat": True, "touch_sensor": False, "ambient_level": 6})
        found = list(own.refused.values())
        own.host_equaliser = False
        own.apply({"eq_enabled": True})
        found += list(own.refused.values())
        return found

    def test_every_reason_the_helper_writes_fits_on_one_line(self):
        reasons = self._reasons()
        self.assertGreaterEqual(len(reasons), 4)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertLessEqual(len(reason), self.LIMIT, reason)

    def test_a_reason_is_a_sentence_about_the_headset_not_a_field_name(self):
        # "speak to chat is read-only on this headset" put an identifier in front
        # of the user, in the panel, where the row already carries its own label.
        for reason in self._reasons():
            with self.subTest(reason=reason):
                self.assertEqual(reason[0], reason[0].upper())
                self.assertNotIn("_", reason)

    def test_a_reason_is_withdrawn_once_it_has_stopped_being_true(self):
        # Refused because the sink was not there yet, then the sink arrives and
        # the equaliser starts. Nothing else clears it: this is not a command.
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.enabled = True
        own.restored = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value=""):
            own.apply({"eq_enabled": True})
        self.assertIn("eq_enabled", own.refused)
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own.equaliser, "apply"), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.restore_after = 0.0
            own.tend_equaliser()
        self.assertNotIn("eq_enabled", own.refused)

    def test_a_preset_is_offered_and_arrives_as_a_curve(self):
        # The name is never stored. It is read back off the gains, so a preset
        # and a hand-moved band cannot disagree about what the sound is.
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"), \
             patch.object(own.equaliser, "start"), patch.object(own.equaliser, "settle"):
            own.apply({"eq_preset_host": "Bass"})
            payload = own.payload()
        self.assertEqual(own.equaliser.gains[0], 6.0)
        self.assertEqual(payload["state"]["eq_preset_host"], "Bass")
        self.assertIn("Bass", payload["equaliser"]["eq_presets"])

    def test_a_band_moved_by_hand_stops_claiming_to_be_a_preset(self):
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"), \
             patch.object(own.equaliser, "start"), patch.object(own.equaliser, "settle"):
            own.apply({"eq_preset_host": "Bass"})
            own.apply({"eq_gains": [6, 5, 3.5, 1.5, 0, 0, 0, 2, 0, 0]})
            payload = own.payload()
        self.assertEqual(payload["state"]["eq_preset_host"], "Custom")

    def test_a_preset_nobody_has_is_refused_rather_than_flattening_the_curve(self):
        own = owner(BASE)
        own.host_equaliser = True
        own.equaliser.gains = [4.0] * 10
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"):
            own.apply({"eq_preset_host": "Nonsense"})
        self.assertIn("eq_preset_host", own.refused)
        self.assertEqual(own.equaliser.gains, [4.0] * 10)

    def test_a_bad_preset_does_not_drag_down_what_it_was_sent_with(self):
        # The panel can batch the switch with a preset. Answering "there is no
        # such preset" to an on/off switch is nonsense, and it refused a
        # perfectly valid request at the same time.
        own = owner(BASE)
        own.host_equaliser = True
        with patch.object(own.equaliser, "available", return_value=True), \
             patch.object(own, "sink_name", return_value="bluez_output.X.1"), \
             patch.object(own.equaliser, "start"), patch.object(own.equaliser, "settle"):
            own.apply({"eq_enabled": True, "eq_preset_host": "Bass Boost"})
        self.assertIn("eq_preset_host", own.refused)
        self.assertNotIn("eq_enabled", own.refused)
        self.assertTrue(own.equaliser.enabled)
