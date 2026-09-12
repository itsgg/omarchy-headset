"""The Sony driver, against replies captured from a WH-1000XM5 on firmware 2.5.1.

Every hex string below came off the wire. If a decoder drifts from the hardware,
these fail rather than the panel quietly showing the wrong thing.
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset import framing  # noqa: E402
from headset.drivers import sony_mdr as sony  # noqa: E402

CAPTURED = {
    "firmware": "05 02 05 32 2e 35 2e 31",
    "battery": "23 00 23 00",
    "codec": "13 02 10",
    "noise": "67 17 01 00 00 00 14",
    "equalizer": "57 00 10 06 09 0a 0f 11 11 13",
    "speak_to_chat": "f7 0c 01 01",
    "speak_to_chat_config": "fb 0c 00 01",
    "pause_on_removal": "f7 01 00",
    "voice_guidance": "47 01 00 01",
    "dsee": "e7 02 00",
    "touch_sensor": "d7 d2 00 00",
}

# The state that headset was actually in when those were read.
BASELINE = {
    "firmware": "2.5.1", "battery": 35, "charging": False, "codec": "LDAC",
    "noise": "off", "focus_on_voice": False, "ambient_level": 20, "noise_variant": 0x17,
    "eq_preset": "bright", "eq_clear_bass": -1, "eq_bands": [0, 5, 7, 7, 9],
    "speak_to_chat": False, "speak_to_chat_sensitivity": "auto",
    "speak_to_chat_timeout": "standard", "pause_on_removal": True,
    "voice_guidance": True, "dsee": False, "touch_sensor": True,
}


def decoded():
    out = {}
    for record in sony.RECORDS:
        out.update(record.decode(bytes.fromhex(CAPTURED[record.id])) or {})
    return out


class DecodeTests(unittest.TestCase):
    def test_every_captured_reply_decodes_to_what_the_headset_was_set_to(self):
        self.assertEqual(decoded(), BASELINE)

    def test_zero_means_on_for_the_shared_switch_and_one_means_charging(self):
        # Getting this backwards is the usual bug in this protocol, in both
        # directions: the switches are inverted and the charging flag is not.
        self.assertTrue(sony.decode_pause_on_removal(bytes.fromhex("f7 01 00"))["pause_on_removal"])
        self.assertFalse(sony.decode_pause_on_removal(bytes.fromhex("f7 01 01"))["pause_on_removal"])
        self.assertFalse(sony.decode_battery(bytes.fromhex("23 00 23 00"))["charging"])
        self.assertTrue(sony.decode_battery(bytes.fromhex("23 00 23 01"))["charging"])

    def test_noise_modes_come_from_two_separate_bytes(self):
        at = lambda hexes: sony.decode_noise(bytes.fromhex(hexes))["noise"]
        self.assertEqual(at("67 17 01 00 00 00 14"), "off")
        self.assertEqual(at("67 17 01 01 00 00 14"), "anc")
        self.assertEqual(at("67 17 01 01 01 00 14"), "ambient")

    def test_an_eight_byte_noise_record_carries_wind_noise_reduction(self):
        self.assertEqual(sony.decode_noise(bytes.fromhex("67 17 01 01 00 03 00 14"))["noise"], "wind")

    def test_the_equaliser_reply_is_clear_bass_then_five_bands(self):
        out = sony.decode_equalizer(bytes.fromhex(CAPTURED["equalizer"]))
        self.assertEqual(out["eq_clear_bass"], -1)
        self.assertEqual(out["eq_bands"], [0, 5, 7, 7, 9])
        self.assertEqual(out["eq_preset"], "bright")

    def test_a_ten_band_equaliser_has_no_clear_bass_and_a_different_offset(self):
        out = sony.decode_equalizer(bytes.fromhex("57 00 a0 0a") + bytes([6] * 10))
        self.assertIsNone(out["eq_clear_bass"])
        self.assertEqual(out["eq_bands"], [0] * 10)

    def test_short_and_mismatched_records_decode_to_nothing_rather_than_raising(self):
        for record in sony.RECORDS:
            full = bytes.fromhex(CAPTURED[record.id])
            for length in range(len(full)):
                self.assertIsNone(record.decode(full[:length]), f"{record.id} at {length}")
            # A reply whose sub-byte belongs to another record must be declined.
            wrong = bytearray(full)
            wrong[1] ^= 0xFF
            self.assertIsNone(record.decode(bytes(wrong)), record.id)

    def test_an_out_of_range_reading_is_refused(self):
        self.assertIsNone(sony.decode_battery(bytes.fromhex("23 00 65 00")))
        self.assertIsNone(sony.decode_noise(bytes.fromhex("67 17 01 01 00 00 15")))

    def test_the_touch_panel_state_is_the_last_byte_not_the_constant(self):
        # Index 2 is a constant 0x00, so reading it reported the touch panel as
        # on whatever the headset said.
        self.assertTrue(sony.decode_touch_sensor(bytes.fromhex("d7 d2 00 00"))["touch_sensor"])
        self.assertFalse(sony.decode_touch_sensor(bytes.fromhex("d7 d2 00 01"))["touch_sensor"])

    def test_an_equaliser_frame_with_impossible_gains_is_refused(self):
        # This was the one decoder that trusted its arithmetic: a corrupt frame
        # decoded to gains of hundreds of decibels and reached the panel.
        self.assertIsNone(sony.decode_equalizer(bytes.fromhex("57 00 10 06 ff ff ff ff ff ff")))
        self.assertIsNone(sony.decode_equalizer(bytes.fromhex("57 00 10 06 09 0a 0f 11 11 ff")))
        self.assertIsNotNone(sony.decode_equalizer(bytes.fromhex(CAPTURED["equalizer"])))

    def test_the_init_reply_length_names_the_protocol(self):
        self.assertEqual(sony.identify(bytes(8))["protocol"], "v2")
        self.assertEqual(sony.identify(bytes(4))["protocol"], "v1")
        self.assertEqual(sony.identify(bytes(5))["protocol"], "unknown")


class RecordMatchingTests(unittest.TestCase):
    def message(self, hexes, message_type=framing.COMMAND_1):
        return framing.Message(message_type, 0, bytes.fromhex(hexes))

    def matches(self, message):
        return [r.id for r in sony.RECORDS if r.matches(message)]

    def test_records_sharing_a_payload_type_are_told_apart_by_their_sub_byte(self):
        # Speak-to-chat and pause-on-removal both answer as 0xf7. Matching on the
        # type alone sent one record's reply to the other's decoder, which declined
        # it, and the real feature then looked like one the headset does not have.
        self.assertEqual(self.matches(self.message("f7 0c 01 01")), ["speak_to_chat"])
        self.assertEqual(self.matches(self.message("f7 01 00")), ["pause_on_removal"])

    def test_a_reply_on_the_other_command_channel_does_not_match(self):
        self.assertEqual(self.matches(self.message("47 01 00 01")), [])
        self.assertEqual(self.matches(self.message("47 01 00 01", framing.COMMAND_2)),
                         ["voice_guidance"])

    def test_an_unprompted_notification_matches_its_record(self):
        # A press of the button on the earcup arrives as the request code plus
        # three. Without this the panel never notices a change made on the headset.
        self.assertEqual(self.matches(self.message("69 17 01 01 00 00 14")), ["noise"])

    def test_every_record_matches_its_own_captured_reply_and_nothing_else(self):
        for record in sony.RECORDS:
            message = framing.Message(record.message_type, 0, bytes.fromhex(CAPTURED[record.id]))
            self.assertEqual(self.matches(message), [record.id])


class EncodeTests(unittest.TestCase):
    def test_noise_writes_restate_the_mode_level_and_focus_together(self):
        # One message carries all three. A partial write zeroes what it omits.
        state = dict(BASELINE)
        self.assertEqual(sony.encode_noise({"noise": "anc"}, state)[1].hex(" "),
                         "68 17 01 01 00 00 14")
        self.assertEqual(sony.encode_noise({"noise": "ambient", "ambient_level": 6}, state)[1].hex(" "),
                         "68 17 01 01 01 00 06")
        self.assertEqual(sony.encode_noise({"focus_on_voice": True}, state)[1].hex(" "),
                         "68 17 01 00 00 01 14")

    def test_the_equaliser_always_restates_clear_bass_with_the_bands(self):
        # Sending bands alone re-sends a stale clear bass, and it drifts one step
        # every time a band is touched.
        state = dict(BASELINE)
        bands_only = sony.encode_equalizer({"eq_bands": [1, 2, 3, 4, 5]}, state)[1]
        self.assertEqual(bands_only.hex(" "), "58 00 a0 06 09 0b 0c 0d 0e 0f")
        bass_only = sony.encode_equalizer({"eq_clear_bass": 5}, state)[1]
        self.assertEqual(bass_only.hex(" "), "58 00 a0 06 0f 0a 0f 11 11 13")

    def test_writing_a_band_selects_manual(self):
        self.assertEqual(sony.encode_equalizer({"eq_bands": [0] * 5}, {})[1][2], sony.EQ_MANUAL_PRESET)

    def test_values_beyond_the_range_are_clamped_rather_than_sent(self):
        report = sony.encode_equalizer({"eq_bands": [99, -99, 0, 0, 0], "eq_clear_bass": 99}, {})[1]
        self.assertEqual(list(report[4:10]), [20, 20, 0, 10, 10, 10])
        self.assertEqual(sony.encode_noise({"noise": "ambient", "ambient_level": 99}, {})[1][6], 20)

    def test_a_grouped_write_reads_its_own_feature_rather_than_the_group(self):
        # Writes are grouped by the message they share, so an encoder is handed a
        # dict even for a single feature. Reading that dict as the value made every
        # switch send "on", because a non-empty dict is truthy.
        state = dict(BASELINE)
        for feature, encoder in (("speak_to_chat", sony.encode_speak_to_chat),
                                 ("pause_on_removal", sony.encode_pause_on_removal),
                                 ("voice_guidance", sony.encode_voice_guidance),
                                 ("dsee", sony.encode_dsee),
                                 ("touch_sensor", sony.encode_touch_sensor)):
            off = encoder({feature: False}, state)[1]
            on = encoder({feature: True}, state)[1]
            self.assertNotEqual(off, on, feature)
            self.assertEqual(encoder(False, state)[1], off, feature)
            self.assertEqual(encoder(True, state)[1], on, feature)

    def test_a_grouped_write_falls_back_to_the_current_reading(self):
        state = {"speak_to_chat": True}
        self.assertEqual(sony.encode_speak_to_chat({"something_else": 1}, state)[1],
                         sony.encode_speak_to_chat(True, state)[1])

    def test_speak_to_chat_settings_travel_together(self):
        state = dict(BASELINE)
        self.assertEqual(
            sony.encode_speak_to_chat_config({"speak_to_chat_sensitivity": "high"}, state)[1].hex(" "),
            "fc 0c 01 01")
        self.assertEqual(
            sony.encode_speak_to_chat_config({"speak_to_chat_timeout": "off"}, state)[1].hex(" "),
            "fc 0c 00 03")

    def test_voice_guidance_uses_the_second_command_channel(self):
        self.assertEqual(sony.encode_voice_guidance(True, {})[0], sony.COMMAND_2)
        self.assertEqual(sony.encode_noise({"noise": "off"}, {})[0], sony.COMMAND_1)

    def test_unknown_values_are_refused_before_anything_is_written(self):
        with self.assertRaises(ValueError):
            sony.encode_noise({"noise": "loud"}, {})
        with self.assertRaises(ValueError):
            sony.encode_eq_preset({"eq_preset": "gaming"}, {})
        with self.assertRaises(ValueError):
            sony.encode_equalizer({"eq_bands": [0, 0]}, {})
        with self.assertRaises(ValueError):
            sony.encode_speak_to_chat_config({"speak_to_chat_timeout": "soon"}, {})

    def test_an_encoded_write_decodes_back_to_what_was_asked_for(self):
        # The write and the read are different opcodes with the same body, so a
        # round trip catches a layout that drifts on one side only.
        state = dict(BASELINE)
        for wanted in ({"noise": "anc"}, {"noise": "ambient", "ambient_level": 3},
                       {"noise": "ambient", "focus_on_voice": True}):
            written = bytearray(sony.encode_noise(wanted, state)[1])
            written[0] = 0x67  # the reply to the write we just built
            read_back = sony.decode_noise(bytes(written))
            for key, value in wanted.items():
                self.assertEqual(read_back[key], value, wanted)


class DriverTests(unittest.TestCase):
    def test_the_driver_claims_the_models_it_knows_and_nothing_else(self):
        self.assertTrue(sony.claims("WH-1000XM5"))
        self.assertTrue(sony.claims("Ganesh's WH-1000XM5"))
        self.assertTrue(sony.claims("wh_1000xm5"))
        self.assertFalse(sony.claims("Arctis Nova Pro"))
        self.assertFalse(sony.claims(""))
        self.assertFalse(sony.claims(None))

    def test_the_older_over_ear_models_are_not_claimed(self):
        # They speak protocol v1, which none of these records are written for.
        # The letter is the whole difference: WF-1000XM4 is v2, WH-1000XM4 is not,
        # so a looser match would claim a headset this cannot talk to.
        for model in ("WH-1000XM4", "WH-1000XM3", "WH-1000XM2", "WF-SP800N", "WI-SP600N"):
            self.assertFalse(sony.claims(model), model)
        for model in ("WF-1000XM4", "WF-1000XM5", "WH-1000XM5", "WH-1000XM6",
                      "WH-CH720N", "LinkBuds", "LinkBuds S"):
            self.assertTrue(sony.claims(model), model)

    def test_a_product_that_merely_contains_a_model_name_is_not_claimed(self):
        # The LinkBuds Speaker is a speaker. Claiming it opens a control session
        # against a device that has no such service.
        self.assertFalse(sony.claims("LinkBuds Speaker"))
        self.assertFalse(sony.claims("Sony LinkBuds Speaker"))
        self.assertTrue(sony.claims("LinkBuds"))
        self.assertTrue(sony.claims("LinkBuds S"))

    def test_the_driver_says_which_protocol_its_records_are_written_for(self):
        self.assertEqual(sony.DRIVER.protocols, ("v2",))

    def test_the_touch_panel_is_offered_read_only(self):
        # Verified by writing it and reading back from a fresh session: the headset
        # acknowledges the change and does not make it.
        self.assertFalse(sony.DRIVER.control("touch_sensor").honoured)
        self.assertTrue(sony.DRIVER.control("noise").honoured)

    def test_ambient_settings_are_gated_on_ambient_mode(self):
        # The headset discards them in any other mode, so offering them there
        # would be offering a control that does nothing.
        available = sony.DRIVER.control("ambient_level").available
        self.assertTrue(available({"noise": "ambient"}))
        self.assertFalse(available({"noise": "anc"}))
        self.assertIsNone(sony.DRIVER.control("noise").available)

    def test_every_control_names_a_record_that_exists(self):
        ids = {record.id for record in sony.DRIVER.records}
        for control in sony.DRIVER.controls:
            self.assertIn(control.record, ids, control.id)

    def test_controls_that_share_a_message_share_a_coalescing_key(self):
        keys = {c.id: c.key for c in sony.DRIVER.controls}
        self.assertEqual(keys["noise"], keys["ambient_level"])
        self.assertEqual(keys["noise"], keys["focus_on_voice"])
        self.assertEqual(keys["eq_bands"], keys["eq_clear_bass"])
        self.assertNotEqual(keys["noise"], keys["eq_bands"])
        # A preset and a band curve are different messages, so sharing a key let
        # one encoder run with the other's values and silently drop them.
        self.assertNotEqual(keys["eq_preset"], keys["eq_bands"])

    def test_controls_sharing_a_key_share_an_encoder(self):
        by_key = {}
        for control in sony.DRIVER.controls:
            by_key.setdefault(control.key or control.id, set()).add(control.encode)
        for key, encoders in by_key.items():
            self.assertEqual(len(encoders), 1, key)


if __name__ == "__main__":
    unittest.main()
