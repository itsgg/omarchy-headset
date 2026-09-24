"""MDR framing, against bytes captured from a real WH-1000XM5."""
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset import framing  # noqa: E402

# The init reply this headset actually sent, in full, header and checksum included.
REAL_INIT_REPLY = bytes.fromhex("3e 0c 01 00 00 00 08 01 00 03 00 20 16 00 00 4f 3c")


class FramingTests(unittest.TestCase):
    def test_a_real_reply_decodes_to_its_payload(self):
        message = framing.decode(REAL_INIT_REPLY)
        self.assertIsNotNone(message)
        self.assertEqual(message.type, framing.COMMAND_1)
        self.assertEqual(message.seq, 1)
        self.assertEqual(message.payload, bytes.fromhex("01 00 03 00 20 16 00 00"))
        self.assertEqual(message.kind, 0x01)

    def test_encode_and_decode_round_trip(self):
        for payload in (b"", b"\x00\x00", bytes(range(64)), b"\x3e\x3d\x3c"):
            message = framing.Message(framing.COMMAND_1, 1, payload)
            self.assertEqual(framing.decode(framing.encode(message)), message)

    def test_header_trailer_and_escape_bytes_are_escaped(self):
        # Nothing between the header and the trailer may look like either.
        raw = framing.encode(framing.Message(framing.COMMAND_1, 0, b"\x3e\x3c\x3d"))
        self.assertNotIn(framing.HEADER, raw[1:-1])
        self.assertNotIn(framing.TRAILER, raw[1:-1])
        self.assertEqual(framing.unescape(framing.escape(b"\x3e\x3c\x3d")), b"\x3e\x3c\x3d")

    def test_an_escape_with_nothing_after_it_is_refused(self):
        self.assertIsNone(framing.unescape(b"\x01\x3d"))

    def test_a_wrong_checksum_is_refused(self):
        corrupt = bytearray(REAL_INIT_REPLY)
        corrupt[-2] ^= 0xFF
        self.assertIsNone(framing.decode(bytes(corrupt)))

    def test_a_wrong_length_is_refused(self):
        body = bytes.fromhex("0c 01 00 00 00 09 01 00 03 00 20 16 00 00")
        raw = (bytes((framing.HEADER,)) + framing.escape(body)
               + framing.escape(bytes((framing.checksum(body),))) + bytes((framing.TRAILER,)))
        self.assertIsNone(framing.decode(raw))

    def test_truncated_and_headerless_frames_return_nothing_rather_than_raising(self):
        for bad in (b"", b"\x3e", b"\x3e\x3c", REAL_INIT_REPLY[:6], REAL_INIT_REPLY[1:]):
            self.assertIsNone(framing.decode(bad))

    def test_an_ack_answers_with_the_complement_of_the_sequence(self):
        self.assertEqual(framing.decode(framing.ack(0)).seq, 1)
        self.assertEqual(framing.decode(framing.ack(1)).seq, 0)
        self.assertEqual(framing.decode(framing.ack(0)).payload, b"")

    def test_split_finds_whole_frames_and_keeps_the_remainder(self):
        stream = REAL_INIT_REPLY + REAL_INIT_REPLY[:5]
        frames, rest = framing.split(stream)
        self.assertEqual(frames, [REAL_INIT_REPLY])
        self.assertEqual(rest, REAL_INIT_REPLY[:5])

    def test_split_drops_noise_before_a_header(self):
        frames, rest = framing.split(b"\xff\xff" + REAL_INIT_REPLY)
        self.assertEqual(frames, [REAL_INIT_REPLY])
        self.assertEqual(rest, b"")

    def test_a_stray_header_does_not_swallow_the_frame_behind_it(self):
        # The radio delivers a lone 3e now and then. Taking the span from it to
        # the next trailer made the candidate fail its checksum and took the good
        # frame with it.
        frames, rest = framing.split(b"\x3e" + REAL_INIT_REPLY)
        self.assertEqual(frames, [REAL_INIT_REPLY])
        self.assertEqual(rest, b"")
        self.assertIsNotNone(framing.decode(frames[0]))

    def test_split_of_a_buffer_with_no_header_keeps_nothing(self):
        self.assertEqual(framing.split(b"\x00\x01\x02"), ([], b""))


class BoundTests(unittest.TestCase):
    """What is kept when a headset opens a frame and never closes it.

    The link is long-lived and the helper outlives the panel, so a buffer that
    grows with the bytes is a helper that is eventually killed for its memory.
    A paired headset is not a trusted peer: it is a radio that anything within
    range can be.
    """

    def test_an_unfinished_frame_cannot_grow_the_buffer_without_end(self):
        rest = b""
        # Sixteen times the cap, in the chunks the socket actually delivers.
        for _ in range(16 * framing.MAX_FRAME // 4096):
            frames, rest = framing.split(rest + bytes((framing.HEADER,)) + b"\x00" * 4095)
            self.assertEqual(frames, [])
        self.assertLessEqual(len(rest), framing.MAX_FRAME)

    def test_a_real_frame_behind_a_wedged_one_is_still_read(self):
        # The point of resynchronising rather than dropping everything: the
        # headset that wedged the stream is the one we still need to hear from.
        #
        # Both halves are asserted. Delivering the frame on its own proves
        # nothing, because the code before the bound delivered it too, by
        # keeping every byte since the wedge began; what is new is delivering it
        # while what is kept stays small.
        rest = b""
        for _ in range(4):
            frames, rest = framing.split(rest + bytes((framing.HEADER,)) + b"\x00" * 4095)
            self.assertLessEqual(len(rest), framing.MAX_FRAME)
        frames, rest = framing.split(rest + REAL_INIT_REPLY)
        self.assertIn(REAL_INIT_REPLY, frames)
        self.assertEqual(framing.decode(frames[-1]).payload,
                         bytes.fromhex("01 00 03 00 20 16 00 00"))

    def test_a_frame_split_across_chunks_still_completes(self):
        # The bound must not cost the ordinary case, which is a frame arriving
        # in two pieces because that is what a radio does.
        first, second = REAL_INIT_REPLY[:9], REAL_INIT_REPLY[9:]
        frames, rest = framing.split(first)
        self.assertEqual(frames, [])
        self.assertEqual(rest, first)
        frames, rest = framing.split(rest + second)
        self.assertEqual(frames, [REAL_INIT_REPLY])
        self.assertEqual(rest, b"")

    def test_the_largest_frame_the_protocol_can_express_survives(self):
        # 255 bytes of firmware string behind a one-byte length is the longest
        # payload there is, and every byte of it escaped is the worst the wire
        # can carry. The cap has to sit above that or it would eat real traffic.
        payload = bytes([0x05, 0x02, 0xFF]) + bytes((framing.ESCAPE,)) * 255
        whole = framing.encode(framing.Message(framing.COMMAND_1, 1, payload))
        self.assertLess(len(whole), framing.MAX_FRAME)
        frames, rest = framing.split(whole[:-1])
        self.assertEqual(frames, [])
        frames, rest = framing.split(rest + whole[-1:])
        self.assertEqual(framing.decode(frames[0]).payload, payload)
        self.assertEqual(rest, b"")


if __name__ == "__main__":
    unittest.main()
