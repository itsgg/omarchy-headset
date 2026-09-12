"""The acknowledgement discipline, against a socket that is not there."""
import sys
import time
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset import framing  # noqa: E402
from headset.errors import HeadsetError  # noqa: E402
from headset.session import ACKED, FAILED, REPLIED, SILENT, Session, reply_code  # noqa: E402


class FakeSocket:
    """Records what was written and replays what the test queues."""

    def __init__(self):
        self.written = []
        self.inbox = b""
        self.closed = False

    def sendall(self, data):
        self.written.append(bytes(data))

    def send(self, data):
        self.sendall(data)
        return len(data)

    def recv(self, size):
        if not self.inbox:
            raise BlockingIOError()
        chunk, self.inbox = self.inbox[:size], self.inbox[size:]
        return chunk

    def close(self):
        self.closed = True

    def fileno(self):
        return -1

    # helpers
    def deliver(self, message):
        self.inbox += framing.encode(message)

    def frames(self):
        return [framing.decode(raw) for raw in self.written]


def session(**kwargs):
    made = Session(address="AA:BB:CC:DD:EE:FF", channel=9, **kwargs)
    made.sock = FakeSocket()
    made.alive = True
    return made


class DisciplineTests(unittest.TestCase):
    def test_only_one_request_is_on_the_wire_at_a_time(self):
        s = session()
        s.get(bytes((0x22, 0x00)), label="battery")
        s.get(bytes((0x12, 0x02)), label="codec")
        s.pump()
        self.assertEqual(len(s.sock.written), 1)
        self.assertEqual(s.sock.frames()[0].payload, bytes((0x22, 0x00)))
        # The second one waits for the first to finish.
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.sock.deliver(framing.Message(framing.COMMAND_1, 1, bytes((0x23, 0x00, 0x20, 0x00))))
        s.pump()
        self.assertEqual(len(s.sock.written), 3)  # request, our ack, next request
        self.assertEqual(s.sock.frames()[2].payload, bytes((0x12, 0x02)))

    def test_the_next_request_uses_the_sequence_the_ack_carried(self):
        s = session()
        s.get(bytes((0x22, 0x00)))
        s.pump()
        self.assertEqual(s.sock.frames()[0].seq, 0)
        # The reply has to arrive too: a GET stays outstanding until it does,
        # which is the whole point of allowing one request on the wire.
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.sock.deliver(framing.Message(framing.COMMAND_1, 1, bytes((0x23, 0x00, 0x20, 0x00))))
        s.pump()
        s.get(bytes((0x12, 0x02)))
        s.pump()
        self.assertEqual(s.sock.frames()[-1].payload, bytes((0x12, 0x02)))
        self.assertEqual(s.sock.frames()[-1].seq, 1)

    def test_a_command_from_the_device_is_acknowledged_with_the_complement(self):
        seen = []
        s = session(on_message=seen.append)
        s.sock.deliver(framing.Message(framing.COMMAND_1, 1, bytes((0x69, 0x17, 0x01, 0x01, 0x00, 0x00, 0x14))))
        s.pump()
        self.assertEqual(len(seen), 1)
        ack = framing.decode(s.sock.written[0])
        self.assertEqual((ack.type, ack.seq), (framing.ACK, 0))

    def test_a_reply_completes_its_request(self):
        outcomes = []
        s = session()
        s.get(bytes((0x66, 0x17)), on_done=lambda status, payload: outcomes.append((status, payload)))
        s.pump()
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.sock.deliver(framing.Message(framing.COMMAND_1, 1, bytes((0x67, 0x17, 0x01, 0x00, 0x00, 0x00, 0x14))))
        s.pump()
        self.assertEqual(outcomes[0][0], REPLIED)
        self.assertEqual(outcomes[0][1][:2], bytes((0x67, 0x17)))

    def test_a_write_completes_on_the_acknowledgement_alone(self):
        outcomes = []
        s = session()
        s.set(bytes((0x68, 0x17, 0x01, 0x01, 0x00, 0x00, 0x14)),
              on_done=lambda status, payload: outcomes.append(status))
        s.pump()
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.pump()
        self.assertEqual(outcomes, [ACKED])

    def test_acknowledged_and_then_silence_is_reported_as_silence(self):
        # This is how an unsupported feature announces itself: the device takes
        # the request politely and never answers it.
        outcomes = []
        s = session(reply_timeout=0.01)
        s.get(bytes((0x96, 0x06)), on_done=lambda status, payload: outcomes.append(status))
        s.pump()
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.pump()
        time.sleep(0.02)
        s.pump()
        self.assertEqual(outcomes, [SILENT])

    def test_an_unacknowledged_request_is_retransmitted_then_gives_up(self):
        s = session(ack_timeout=0.01, max_attempts=2)
        s.get(bytes((0x22, 0x00)))
        s.pump()
        self.assertEqual(len(s.sock.written), 1)
        time.sleep(0.02)
        s.pump()
        self.assertEqual(len(s.sock.written), 2)  # same request again
        self.assertEqual(s.sock.frames()[0].payload, s.sock.frames()[1].payload)
        time.sleep(0.02)
        with self.assertRaises(HeadsetError):
            s.pump()
        self.assertFalse(s.alive)

    def test_a_superseded_write_never_reaches_the_wire(self):
        # A drag queues one value per pixel and only the last is a fact.
        s = session()
        s.get(bytes((0x22, 0x00)))  # something to keep the queue busy
        s.pump()
        outcomes = []
        for level in range(5):
            s.set(bytes((0x68, 0x17, 0x01, 0x01, 0x01, 0x00, level)), key="noise",
                  on_done=lambda status, payload: outcomes.append(status))
        self.assertEqual(len(s.queue), 1)
        self.assertEqual(outcomes, [FAILED] * 4)
        self.assertEqual(s.queue[0].payload[-1], 4)

    def test_writes_with_different_keys_do_not_supersede_each_other(self):
        s = session()
        s.get(bytes((0x22, 0x00)))
        s.pump()
        s.set(bytes((0x68, 0x17)), key="noise")
        s.set(bytes((0x58, 0x00)), key="equalizer")
        self.assertEqual(len(s.queue), 2)

    def test_a_dropped_link_fails_everything_outstanding_rather_than_hanging(self):
        outcomes = []
        s = session()
        s.get(bytes((0x22, 0x00)), on_done=lambda status, payload: outcomes.append(status))
        s.set(bytes((0x68, 0x17)), on_done=lambda status, payload: outcomes.append(status))
        s.pump()
        s.close()
        self.assertEqual(outcomes, [FAILED, FAILED])

    def test_a_malformed_frame_is_logged_and_skipped_rather_than_fatal(self):
        logs = []
        s = session(on_log=logs.append)
        s.sock.inbox = b"\x3e\x0c\x01\x00\x00\x00\x08\xff\x3c"
        s.pump()
        self.assertTrue(any("malformed" in line for line in logs))
        self.assertTrue(s.alive)

    def test_a_duplicate_acknowledgement_does_not_complete_the_next_request(self):
        # After a retransmission the headset answers twice. The second
        # acknowledgement used to land on whatever was outstanding by then and
        # complete it, putting two commands on the wire at once.
        outcomes = []
        s = session(ack_timeout=0.01)
        s.set(bytes((0x68, 0x17)), on_done=lambda status, payload: outcomes.append(("first", status)))
        s.set(bytes((0xF8, 0x0C)), on_done=lambda status, payload: outcomes.append(("second", status)))
        s.pump()
        time.sleep(0.02)
        s.pump()  # retransmits the first
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.pump()
        self.assertEqual(outcomes, [("first", ACKED)])
        # The second is still held: one acknowledgement is outstanding, so the
        # queue does not move.
        payloads = [f.payload for f in s.sock.frames() if f.type != framing.ACK]
        self.assertEqual(payloads, [bytes((0x68, 0x17))] * 2)
        # The duplicate arrives. It belongs to the retransmission, not to the
        # request that would otherwise be on the wire by now.
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.pump()
        self.assertEqual(outcomes, [("first", ACKED)])
        payloads = [f.payload for f in s.sock.frames() if f.type != framing.ACK]
        self.assertEqual(payloads[-1], bytes((0xF8, 0x0C)))
        # And it completes only on an acknowledgement of its own.
        s.sock.deliver(framing.Message(framing.ACK, 0, b""))
        s.pump()
        self.assertEqual(outcomes, [("first", ACKED), ("second", ACKED)])

    def test_a_reply_before_its_acknowledgement_holds_the_queue(self):
        # Sending the next request then would use a sequence number the headset
        # has not handed back yet.
        s = session()
        s.get(bytes((0x66, 0x17)))
        s.get(bytes((0x22, 0x00)))
        s.pump()
        s.sock.deliver(framing.Message(framing.COMMAND_1, 1,
                                       bytes((0x67, 0x17, 0x01, 0x00, 0x00, 0x00, 0x14))))
        s.pump()
        payloads = [frame.payload for frame in s.sock.frames() if frame.type != framing.ACK]
        self.assertEqual(payloads, [bytes((0x66, 0x17))])
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.pump()
        payloads = [frame.payload for frame in s.sock.frames() if frame.type != framing.ACK]
        self.assertEqual(payloads, [bytes((0x66, 0x17)), bytes((0x22, 0x00))])
        self.assertEqual(s.sock.frames()[-1].seq, 1)

    def test_an_acknowledgement_the_socket_refuses_is_kept_and_sent_later(self):
        # The socket is non-blocking. Dropping an acknowledgement because the
        # buffer was momentarily full stops the headset announcing anything ever
        # again, because it waits for one before sending the next.
        s = session()
        full = {"count": 0}
        real_send = s.sock.send

        def refuse(data):
            full["count"] += 1
            if full["count"] <= 2:
                raise BlockingIOError()
            return real_send(data)

        s.sock.send = refuse
        s.sock.deliver(framing.Message(framing.COMMAND_1, 1, bytes((0x69, 0x17, 0x01))))
        s.pump()
        self.assertEqual(s.sock.written, [])   # refused, and kept
        self.assertNotEqual(s.outbox, b"")
        s.pump()
        self.assertEqual(framing.decode(s.sock.written[0]).type, framing.ACK)
        self.assertEqual(s.outbox, b"")

    def test_the_wait_for_a_reply_starts_when_the_request_was_taken(self):
        # Measuring it from the first send made a retransmitted request look
        # unsupported moments after it was finally acknowledged.
        outcomes = []
        s = session(ack_timeout=0.01, reply_timeout=0.2)
        s.get(bytes((0x66, 0x17)), on_done=lambda status, payload: outcomes.append(status))
        s.pump()
        time.sleep(0.02)
        s.pump()               # retransmit
        time.sleep(0.15)       # well past the reply timeout measured from the send
        s.sock.deliver(framing.Message(framing.ACK, 1, b""))
        s.pump()
        self.assertEqual(outcomes, [])
        time.sleep(0.25)
        s.pump()
        self.assertEqual(outcomes, [SILENT])

    def test_a_reply_code_is_the_request_plus_one(self):
        self.assertEqual(reply_code(bytes((0x66, 0x17))), 0x67)
        with self.assertRaises(HeadsetError):
            reply_code(b"")


if __name__ == "__main__":
    unittest.main()
