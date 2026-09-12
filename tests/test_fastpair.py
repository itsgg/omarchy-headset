"""Fast Pair Message Stream, against Google's own example and a real headset.

The battery example is the one printed in the specification; the model id and
battery frames beside it came off a Nothing Ear (open).
"""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset.drivers import fastpair as fp  # noqa: E402

GOOGLES_EXAMPLE = bytes.fromhex("0303000357417f")
REAL_MODEL = bytes.fromhex("03010003fc3aaf")
REAL_BATTERY = bytes.fromhex("030300036464 7f".replace(" ", ""))


class FramingTests(unittest.TestCase):
    def test_googles_own_example_splits_and_decodes(self):
        frames, rest = fp.split(GOOGLES_EXAMPLE)
        self.assertEqual(rest, b"")
        self.assertEqual((frames[0].group, frames[0].code), (0x03, 0x03))
        out = fp.decode_battery(frames[0].payload)
        self.assertEqual(out["battery_parts"]["left"], {"level": 87, "charging": False})
        self.assertEqual(out["battery_parts"]["right"], {"level": 65, "charging": False})
        self.assertNotIn("case", out["battery_parts"])
        self.assertEqual(out["battery"], 65)

    def test_a_partial_frame_is_kept_for_the_next_read(self):
        frames, rest = fp.split(GOOGLES_EXAMPLE + REAL_MODEL[:4])
        self.assertEqual(len(frames), 1)
        self.assertEqual(rest, REAL_MODEL[:4])

    def test_several_frames_in_one_read(self):
        frames, rest = fp.split(REAL_MODEL + REAL_BATTERY)
        self.assertEqual([(f.group, f.code) for f in frames], [(3, 1), (3, 3)])
        self.assertEqual(rest, b"")

    def test_a_frame_claiming_more_than_it_has_is_held_not_dropped(self):
        self.assertEqual(fp.split(bytes.fromhex("030300ff41")), ([], bytes.fromhex("030300ff41")))

    def test_an_impossible_length_resynchronises_instead_of_stalling(self):
        # The stream has no sync byte, so a lost boundary would otherwise hold
        # every real frame behind it waiting for 65,535 bytes that never come.
        frames, rest = fp.split(bytes.fromhex("0303ffff") + REAL_BATTERY)
        self.assertEqual(len(frames), 1)
        self.assertEqual(fp.decode_battery(frames[0].payload)["battery"], 100)
        self.assertEqual(rest, b"")

    def test_leading_rubbish_does_not_swallow_the_frame_behind_it(self):
        frames, _ = fp.split(b"\x00" + REAL_BATTERY)
        self.assertTrue(any(f.group == 3 and f.code == 3 for f in frames))

    def test_encode_round_trips(self):
        frames, _ = fp.split(fp.encode(0x08, 0x12, b"\x02\x02\xa8\xa8\x08"))
        self.assertEqual((frames[0].group, frames[0].code), (0x08, 0x12))
        self.assertEqual(frames[0].payload, b"\x02\x02\xa8\xa8\x08")


class BatteryTests(unittest.TestCase):
    def test_a_real_reading_from_the_headset(self):
        frames, _ = fp.split(REAL_BATTERY)
        out = fp.decode_battery(frames[0].payload)
        self.assertEqual(out["battery"], 100)
        self.assertEqual(sorted(out["battery_parts"]), ["left", "right"])

    def test_charging_is_the_top_bit_and_the_level_is_the_rest(self):
        out = fp.decode_battery(bytes((0x80 | 55, 60, 0x7F)))
        self.assertEqual(out["battery_parts"]["left"], {"level": 55, "charging": True})
        self.assertFalse(out["battery_parts"]["right"]["charging"])
        self.assertTrue(out["charging"])

    def test_the_single_number_is_the_bud_that_runs_out_first(self):
        out = fp.decode_battery(bytes((95, 62, 0x7F)))
        self.assertEqual(out["battery"], 62)

    def test_an_unknown_part_and_an_absent_case_are_left_out(self):
        out = fp.decode_battery(bytes((0x7F, 40, 0xFF)))
        self.assertEqual(sorted(out["battery_parts"]), ["right"])
        self.assertEqual(out["battery"], 40)
        out = fp.decode_battery(bytes((50, 50, 0x2A)))
        self.assertEqual(out["battery_parts"]["case"], {"level": 42, "charging": False})

    def test_a_reading_that_says_unknown_is_still_a_reading(self):
        # Returning nothing here left the last known levels on screen as though
        # they were current.
        out = fp.decode_battery(bytes((0x7F, 0x7F, 0xFF)))
        self.assertIsNone(out["battery_parts"])
        self.assertIsNone(out["battery"])
        self.assertIsNone(out["charging"])

    def test_a_level_above_a_hundred_is_not_a_percentage(self):
        out = fp.decode_battery(bytes((0x65, 0x66, 0xFF)))
        self.assertIsNone(out["battery_parts"])
        self.assertIsNone(out["battery"])

    def test_a_frame_too_short_to_read_decodes_to_nothing(self):
        self.assertIsNone(fp.decode_battery(b"\x40"))


class NoiseTests(unittest.TestCase):
    # Google numbers the bits from the most significant, so bit 0 is 0x80.
    # Reading them the other way round offers modes the headset does not have.
    def test_the_headset_says_which_modes_it_has(self):
        out = fp.decode_noise(bytes((0x02, fp.ANC_OFF | fp.ANC_ON | fp.ANC_TRANSPARENT,
                                     fp.ANC_OFF | fp.ANC_ON, fp.ANC_ON)))
        self.assertEqual(sorted(out["noise_modes"]), ["anc", "off", "transparent"])
        self.assertEqual(sorted(out["noise_settable"]), ["anc", "off"])
        self.assertEqual(out["noise"], "anc")

    def test_a_headset_offering_nothing_is_not_a_headset_with_noise_control(self):
        self.assertIsNone(fp.decode_noise(bytes((0x02, 0x00, 0x00, 0x00))))
        self.assertIsNone(fp.decode_noise(b"\x02\x88"))

    def test_setting_a_mode_is_not_offered_until_it_can_be_tried(self):
        # The specification does not say what the capability bytes carry on a Set,
        # and no headset here has noise cancelling to find out against. Reading is
        # checked and shipped; writing waits for hardware rather than a guess.
        self.assertEqual(fp.DRIVER.controls, ())
        self.assertFalse(hasattr(fp, "encode_noise"))


class DriverTests(unittest.TestCase):
    def test_it_claims_nothing_by_name_and_is_the_fallback(self):
        # It is identified by a service the device advertises, not by any name,
        # so it proves itself by opening rather than by matching a table.
        self.assertFalse(fp.DRIVER.claims("Nothing Ear (open)"))
        self.assertTrue(fp.DRIVER.fallback)
        self.assertIsNone(fp.DRIVER.init)

    def test_every_record_is_volunteered_rather_than_asked_for(self):
        for record in fp.DRIVER.records:
            self.assertTrue(record.volunteered, record.id)
            self.assertIsNotNone(record.match, record.id)

    def test_a_record_matches_only_its_own_frames(self):
        frames, _ = fp.split(REAL_BATTERY)
        hit = [r.id for r in fp.DRIVER.records if r.match(frames[0])]
        self.assertEqual(hit, ["battery"])

    def test_the_registry_falls_back_to_it_for_an_unknown_headset(self):
        from headset import drivers
        self.assertEqual(drivers.for_device("Nothing Ear (open)").id, "fast-pair")
        self.assertEqual(drivers.for_device("WH-1000XM5").id, "sony-mdr")


if __name__ == "__main__":
    unittest.main()
