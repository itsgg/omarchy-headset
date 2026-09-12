"""The whole Sony stack, driven on the bytes a WH-1000XM5 actually sent.

Everything below the panel runs for real here: framing, the session's
acknowledgement and sequence discipline, record matching, every decoder, the
capability probe and the owner's write grouping. Only the socket is fake, and it
replays captured frames rather than invented ones.

This exists because the driver seam was generalised for a second protocol and the
headset was not available to re-test against. A unit test per decoder would not
have caught a seam that quietly stopped asking for records.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset import drivers, framing, sdp, session  # noqa: E402
from headset.device import Device  # noqa: E402
from headset.drivers import sony_mdr as sony  # noqa: E402
from headset.server import Owner  # noqa: E402

# Request sub-byte -> the reply this headset gave, captured on firmware 2.5.1.
REPLIES = {
    (framing.COMMAND_1, 0x00): "01 00 03 00 20 16 00 00",   # init
    (framing.COMMAND_1, 0x04): "05 02 05 32 2e 35 2e 31",
    (framing.COMMAND_1, 0x22): "23 00 23 00",
    (framing.COMMAND_1, 0x12): "13 02 10",
    (framing.COMMAND_1, 0x66): "67 17 01 00 00 00 14",
    (framing.COMMAND_1, 0x56): "57 00 10 06 09 0a 0f 11 11 13",
    (framing.COMMAND_1, 0xFA): "fb 0c 00 01",
    (framing.COMMAND_1, 0xE6): "e7 02 00",
    (framing.COMMAND_1, 0xD6): "d7 d2 00 00",
    (framing.COMMAND_2, 0x46): "47 01 00 01",
}
# Two records share the 0xf6 request code and differ only in their sub-byte.
BY_SUB = {0x0C: "f7 0c 01 01", 0x01: "f7 01 00"}

BASELINE = {
    "protocol": "v2", "firmware": "2.5.1", "battery": 35, "charging": False,
    "codec": "LDAC", "noise": "off", "focus_on_voice": False, "ambient_level": 20,
    "noise_variant": 0x17, "eq_preset": "bright", "eq_clear_bass": -1,
    "eq_bands": [0, 5, 7, 7, 9], "speak_to_chat": False,
    "speak_to_chat_sensitivity": "auto", "speak_to_chat_timeout": "standard",
    "pause_on_removal": True, "voice_guidance": True, "dsee": False,
    "touch_sensor": True,
}


class Headset:
    """A WH-1000XM5 made of its own captured replies.

    Acknowledges every command the way the real one does, answers what it
    answered, and stays silent for what it ignored.
    """

    def __init__(self, silent=()):
        self.silent = set(silent)
        self.inbox = b""
        self.written = []
        self.seq = 0
        self.closed = False

    # socket surface
    def setsockopt(self, *args):
        pass

    def settimeout(self, value):
        pass

    def setblocking(self, value):
        pass

    def connect(self, where):
        pass

    def close(self):
        self.closed = True

    def fileno(self):
        return -1

    def send(self, data):
        self.sendall(data)
        return len(data)

    def recv(self, size):
        if not self.inbox:
            raise BlockingIOError()
        chunk, self.inbox = self.inbox[:size], self.inbox[size:]
        return chunk

    def sendall(self, data):
        message = framing.decode(bytes(data))
        if message is None or message.type == framing.ACK:
            return
        self.written.append(message)
        # Every command is acknowledged, and the ack carries the next sequence.
        self.seq ^= 1
        self.inbox += framing.encode(framing.Message(framing.ACK, self.seq, b""))
        code, sub = message.payload[0], (message.payload[1] if len(message.payload) > 1 else -1)
        if code in self.silent:
            return
        if code == 0xF6 and sub in BY_SUB:
            reply = BY_SUB[sub]
        else:
            reply = REPLIES.get((message.type, code))
        if reply is None:
            return
        self.inbox += framing.encode(
            framing.Message(message.type, self.seq, bytes.fromhex(reply)))


# The fake socket has no file descriptor to wait on, and there is nothing to wait
# for: its replies are already queued by the time the request returns.
def no_wait(read, write, error, timeout=0):
    return list(read), list(write), []


def open_headset(silent=()):
    fake = Headset(silent)
    device = Device(address="AC:80:0A:44:B3:93", driver=drivers.by_id("sony-mdr"),
                    name="WH-1000XM5")
    # The address family is patched as well as the constructor. A Python built
    # without Bluetooth has no AF_BLUETOOTH at all, which is every GitHub runner,
    # and this harness is the one test that opens a session.
    with patch.object(sdp, "channel_for", return_value=9), \
         patch.object(session, "AF_BLUETOOTH", 31), \
         patch.object(session, "BTPROTO_RFCOMM", 3), \
         patch("socket.socket", return_value=fake), \
         patch("select.select", no_wait):
        device.open()
        device.read(timeout=8.0)
    return device, fake


class EndToEndTests(unittest.TestCase):
    def test_the_whole_headset_reads_back_exactly_as_captured(self):
        device, _ = open_headset()
        self.assertEqual(device.state, BASELINE)

    def test_every_record_is_reported_as_supported(self):
        device, _ = open_headset()
        missing = [r.id for r in sony.DRIVER.records if not device.support.get(r.id)]
        self.assertEqual(missing, [])

    def test_every_record_was_actually_asked_for(self):
        # A seam change that quietly stopped sending requests would still leave
        # decoders passing their own unit tests.
        device, fake = open_headset()
        asked = {(m.type, m.payload[0], m.payload[1]) for m in fake.written if len(m.payload) > 1}
        for record in sony.DRIVER.records:
            self.assertIn((record.message_type, record.request[0], record.request[1]),
                          asked, record.id)

    def test_a_record_the_headset_ignores_is_reported_absent_and_leaves_no_state(self):
        device, _ = open_headset(silent=(0xE6,))
        self.assertFalse(device.support["dsee"])
        self.assertNotIn("dsee", device.state)
        # And nothing else was disturbed by it.
        self.assertEqual(device.state["noise"], "off")
        self.assertTrue(device.support["noise"])

    def test_the_sequence_number_follows_the_acknowledgements(self):
        _, fake = open_headset()
        # The headset alternates; every command after the first must follow it.
        self.assertGreater(len(fake.written), 5)
        self.assertEqual(sorted({m.seq for m in fake.written}), [0, 1])

    def test_writes_group_by_message_and_carry_the_captured_shape(self):
        device, fake = open_headset()
        own = Owner("AC:80:0A:44:B3:93", "WH-1000XM5", listener=None, stdin=None)
        own.stdout_open = False
        own.device = device
        own.state.observe(dict(device.state))
        before = len(fake.written)
        own.apply({"noise": "ambient", "ambient_level": 6, "focus_on_voice": True})
        with patch("select.select", no_wait):
            device.session.run_until_idle(timeout=4.0)
        sent = [m for m in fake.written[before:]]
        self.assertEqual(len(sent), 1, "three features, one message")
        self.assertEqual(sent[0].payload.hex(" "), "68 17 01 01 01 01 06")

    def test_an_equaliser_write_restates_clear_bass_with_the_bands(self):
        device, fake = open_headset()
        own = Owner("AC:80:0A:44:B3:93", "WH-1000XM5", listener=None, stdin=None)
        own.stdout_open = False
        own.device = device
        own.state.observe(dict(device.state))
        before = len(fake.written)
        own.apply({"eq_bands": [1, 2, 3, 4, 5]})
        with patch("select.select", no_wait):
            device.session.run_until_idle(timeout=4.0)
        sent = fake.written[before:]
        self.assertEqual(len(sent), 1)
        # Clear bass keeps the value the headset reported, rather than drifting.
        self.assertEqual(sent[0].payload.hex(" "), "58 00 a0 06 09 0b 0c 0d 0e 0f")

    def test_a_notification_from_the_headset_updates_the_panel(self):
        # A press of the button on the earcup, unprompted.
        device, fake = open_headset()
        seen = {}
        device.on_change = seen.update
        fake.inbox += framing.encode(framing.Message(
            framing.COMMAND_1, 1, bytes.fromhex("69 17 01 01 00 00 14")))
        device.pump()
        self.assertEqual(seen.get("noise"), "anc")
        self.assertEqual(device.state["noise"], "anc")

    def test_the_seam_did_not_turn_sony_into_a_volunteered_driver(self):
        for record in sony.DRIVER.records:
            self.assertFalse(record.volunteered, record.id)
        self.assertIsNotNone(sony.DRIVER.init)
        self.assertIsNone(sony.DRIVER.session)
        self.assertFalse(sony.DRIVER.fallback)
        self.assertTrue(sony.DRIVER.battery_zero_is_noise)


if __name__ == "__main__":
    unittest.main()
