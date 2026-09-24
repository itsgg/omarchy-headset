"""The tier every headset has, parsed from real `pactl list cards` output.

The fixture is three real cards: this laptop's own, a Nothing Ear (open) and a
WH-1000XM5, captured on the machine this was written on.
"""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from headset import audio, binaries  # noqa: E402
from headset.errors import HeadsetError  # noqa: E402

CARDS = (ROOT / "tests" / "fixtures" / "pactl-cards.txt").read_text()
NOTHING = "3C:B0:ED:50:BC:9C"
SONY = "AC:80:0A:44:B3:93"


class ParseTests(unittest.TestCase):
    def test_every_card_is_found_including_the_one_that_is_not_bluetooth(self):
        cards = audio.parse_cards(CARDS)
        self.assertEqual(len(cards), 3)
        self.assertEqual([c["name"] for c in cards][0], "alsa_card.pci-0000_00_1f.3")

    def test_a_card_is_found_by_its_bluetooth_address(self):
        card = audio.card_for(NOTHING, CARDS)
        self.assertEqual(card["description"], "Nothing Ear (open)")
        self.assertEqual(card["form_factor"], "headset")
        self.assertEqual(audio.card_for(SONY, CARDS)["description"], "WH-1000XM5")

    def test_an_address_with_no_card_is_absent_rather_than_an_error(self):
        self.assertIsNone(audio.card_for("00:11:22:33:44:55", CARDS))

    def test_the_address_match_ignores_case(self):
        self.assertIsNotNone(audio.card_for(NOTHING.lower(), CARDS))

    def test_the_codec_comes_from_the_label_not_the_profile_name(self):
        # The best profile is plain "a2dp-sink" with its codec only in the text,
        # so reading the codec off the id would lose exactly the one that matters.
        card = audio.card_for(SONY, CARDS)
        best = next(p for p in card["profiles"] if p["id"] == "a2dp-sink")
        self.assertEqual(best["codec"], "LDAC")
        self.assertEqual(best["kind"], audio.HIGH_FIDELITY)

    def test_profiles_are_sorted_into_listening_talking_and_off(self):
        kinds = {p["id"]: p["kind"] for p in audio.card_for(NOTHING, CARDS)["profiles"]}
        self.assertEqual(kinds["off"], audio.OFF)
        self.assertEqual(kinds["a2dp-sink-sbc"], audio.HIGH_FIDELITY)
        self.assertEqual(kinds["headset-head-unit"], audio.HEADSET)


class SummaryTests(unittest.TestCase):
    def test_a_headset_with_no_driver_still_has_everything_this_tier_needs(self):
        out = audio.summary(NOTHING, CARDS)
        self.assertEqual(out["active_codec"], "AAC")
        self.assertEqual(out["mode"], audio.HIGH_FIDELITY)
        self.assertEqual([c["label"] for c in out["codecs"]], ["SBC", "SBC-XQ", "AAC"])
        self.assertTrue(out["has_microphone"])
        self.assertEqual(out["headset_profile"], "headset-head-unit")

    def test_the_sony_offers_its_own_codecs(self):
        out = audio.summary(SONY, CARDS)
        self.assertEqual(out["active_codec"], "LDAC")
        self.assertIn("LDAC", [c["label"] for c in out["codecs"]])

    def test_the_best_talking_profile_is_the_one_offered(self):
        # Several differ only by a codec nobody picks a phone call by.
        self.assertEqual(audio.summary(SONY, CARDS)["headset_profile"], "headset-head-unit")

    def test_a_device_with_no_card_summarises_to_nothing_rather_than_failing(self):
        out = audio.summary("00:11:22:33:44:55", CARDS)
        self.assertEqual(out["card"], "")
        self.assertEqual(out["codecs"], [])
        self.assertFalse(out["has_microphone"])

    def test_an_unavailable_profile_is_not_offered(self):
        text = CARDS.replace(
            "a2dp-sink-sbc_xq: High Fidelity Playback (A2DP Sink, codec SBC-XQ) "
            "(sinks: 1, sources: 0, priority: 131, available: yes)",
            "a2dp-sink-sbc_xq: High Fidelity Playback (A2DP Sink, codec SBC-XQ) "
            "(sinks: 1, sources: 0, priority: 131, available: no)")
        self.assertNotIn("SBC-XQ", [c["label"] for c in audio.summary(NOTHING, text)["codecs"]])


class LocaleTests(unittest.TestCase):
    def test_pactl_is_asked_in_english(self):
        # Every heading and field below is matched in English. Under a translated
        # locale the parser finds no card at all and a headset that has audio
        # controls loses them.
        seen = {}

        class Done:
            returncode = 0
            stdout = ""
            stderr = ""

        def record(argv, **kwargs):
            seen.update(kwargs.get("env") or {})
            return Done()

        # The lookup is stood in for because this runs where there may be no
        # pactl to find; what it is run with is tested in tests/test_binaries.py.
        with patch.object(binaries, "find", return_value="/usr/bin/pactl"), \
             patch("headset.binaries.run", side_effect=record):
            audio.run_pactl(["list", "cards"])
        self.assertEqual(seen.get("LC_ALL"), "C")
        self.assertEqual(seen.get("LANG"), "C")


class DiscoveryTests(unittest.TestCase):
    def test_a_headset_with_no_driver_is_still_found(self):
        # This tier exists for headsets nothing claims, so resolving through the
        # driver list refused the very devices it is for.
        with patch.object(audio, "run_pactl", return_value=CARDS):
            self.assertEqual(audio.any_connected_audio_device(), NOTHING)

    def test_a_speaker_or_a_keyboard_is_not_a_headset(self):
        # Every device now resolves to a driver, because the fallback is found by
        # an advertised service rather than a name. What makes something a headset
        # is that its card says it is worn.
        speaker = CARDS.replace('device.form_factor = "headset"',
                                'device.form_factor = "speaker"', 1)
        found = [c["address"] for c in audio.headset_cards(speaker)]
        self.assertNotIn(NOTHING, found)
        self.assertIn(SONY, found)
        self.assertEqual(len(audio.headset_cards(CARDS)), 2)

    def test_no_bluetooth_audio_at_all_is_an_error(self):
        with patch.object(audio, "run_pactl", return_value="Card #0\n\tName: alsa_card.x\n"):
            with self.assertRaises(HeadsetError):
                audio.any_connected_audio_device()


class SwitchTests(unittest.TestCase):
    def test_switching_a_profile_calls_pactl_with_the_card_and_profile(self):
        with patch.object(audio, "run_pactl", return_value="") as ran:
            audio.set_profile("bluez_card.X", "a2dp-sink")
        ran.assert_called_once_with(["set-card-profile", "bluez_card.X", "a2dp-sink"])

    def test_switching_without_a_card_or_profile_is_refused(self):
        for card, profile in (("", "a2dp-sink"), ("bluez_card.X", "")):
            with self.assertRaises(HeadsetError):
                audio.set_profile(card, profile)

    def test_a_missing_pactl_is_reported_rather_than_crashing(self):
        with patch.object(binaries, "find", return_value="/usr/bin/pactl"), \
             patch("headset.binaries.run", side_effect=FileNotFoundError()):
            with self.assertRaises(HeadsetError):
                audio.run_pactl(["list", "cards"])

    def test_a_switch_that_did_not_take_is_reported_rather_than_returning_zero(self):
        # PipeWire can hold or restore a profile against us, and exiting zero
        # there reports a change that did not happen.
        class Args:
            address = NOTHING
            name = None
            profile = "a2dp-sink-sbc"
            pretty = False

        with patch.object(audio, "card_for", return_value={"name": "bluez_card.X"}), \
             patch.object(audio, "set_profile"), \
             patch.object(audio, "wait_for_profile",
                          return_value={"active_profile": "headset-head-unit"}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(HeadsetError):
                audio.cmd_audio(Args())

    def test_a_switch_that_took_reports_the_settled_reading(self):
        class Args:
            address = NOTHING
            name = None
            profile = "a2dp-sink"
            pretty = False

        with patch.object(audio, "card_for", return_value={"name": "bluez_card.X"}), \
             patch.object(audio, "set_profile"), \
             patch.object(audio, "wait_for_profile",
                          return_value={"active_profile": "a2dp-sink"}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(audio.cmd_audio(Args()), 0)

    def test_a_failing_pactl_reports_its_own_first_line(self):
        class Done:
            returncode = 1
            stdout = ""
            stderr = "Failure: No such entity\nmore noise\n"

        with patch.object(binaries, "find", return_value="/usr/bin/pactl"), \
             patch("headset.binaries.run", return_value=Done()):
            with self.assertRaises(HeadsetError) as caught:
                audio.run_pactl(["set-card-profile", "x", "y"])
        self.assertEqual(str(caught.exception), "Failure: No such entity")


if __name__ == "__main__":
    unittest.main()
