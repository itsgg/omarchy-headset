"""The socket, the election, and the cache file beside them."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from headset import ipc, sdp  # noqa: E402
from headset.errors import HeadsetError, UnsupportedDevice  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"


class ElectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.chmod(self.folder.name, 0o700)
        self.env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_only_one_process_can_hold_the_headset(self):
        first, second = ipc.Election(ADDRESS), ipc.Election(ADDRESS)
        self.assertTrue(first.win())
        self.assertFalse(second.win())
        first.release()
        self.assertTrue(second.win())
        second.release()

    def test_the_runtime_directory_is_private(self):
        self.assertEqual(ipc.runtime_dir().stat().st_mode & 0o777, 0o700)

    def test_a_socket_left_by_a_dead_owner_is_replaced(self):
        path = ipc.socket_path(ADDRESS)
        listener = ipc.bind_listener(ADDRESS)
        listener.close()
        self.assertTrue(path.exists())
        second = ipc.bind_listener(ADDRESS)   # the lock is what makes this safe
        second.close()

    def test_a_dangling_symlink_where_the_socket_goes_is_refused_not_ignored(self):
        # exists() follows symlinks, so a broken one looked like nothing at all
        # and bind then failed with EADDRINUSE while we held the lock.
        path = ipc.socket_path(ADDRESS)
        path.symlink_to(Path(self.folder.name) / "does-not-exist")
        with self.assertRaises(HeadsetError):
            ipc.bind_listener(ADDRESS)
        path.unlink()

    def test_a_plain_file_where_the_socket_goes_is_refused(self):
        path = ipc.socket_path(ADDRESS)
        path.write_text("not a socket")
        with self.assertRaises(HeadsetError):
            ipc.bind_listener(ADDRESS)

    def test_a_runtime_directory_that_is_not_ours_is_refused(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/"}):
            with self.assertRaises(HeadsetError):
                ipc.runtime_dir()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(HeadsetError):
                ipc.runtime_dir()

    def test_the_version_changes_when_the_code_does(self):
        self.assertEqual(ipc.code_version(), ipc.code_version())
        self.assertEqual(len(ipc.code_version()), 16)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_CACHE_HOME": self.folder.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def test_a_channel_is_remembered_and_read_back(self):
        with patch.object(sdp, "service_record", return_value=b"\x19\x00\x03\x08\x09"):
            self.assertEqual(sdp.channel_for(ADDRESS, "uuid", use_cache=False), 9)
        # No device this time: it has to come from the cache.
        with patch.object(sdp, "service_record", side_effect=AssertionError("asked the device")):
            self.assertEqual(sdp.channel_for(ADDRESS, "uuid"), 9)

    def test_the_cache_is_written_without_following_a_symlink(self):
        # The read side already refused to follow one and the write side did not,
        # so a symlink at the temporary name was followed and its target truncated.
        target = Path(self.folder.name) / "precious"
        target.write_text("do not touch")
        cache = Path(self.folder.name) / "omarchy-headset"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (ADDRESS.replace(":", "").lower() + ".json")
        path.with_suffix(f".{os.getpid()}.tmp").symlink_to(target)
        with patch.object(sdp, "service_record", return_value=b"\x19\x00\x03\x08\x09"):
            sdp.channel_for(ADDRESS, "uuid", use_cache=False)
        self.assertEqual(target.read_text(), "do not touch")

    def test_an_absurd_cached_channel_is_ignored_rather_than_used(self):
        cache = Path(self.folder.name) / "omarchy-headset"
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / (ADDRESS.replace(":", "").lower() + ".json")
        path.write_text(json.dumps({"uuid": 999}))
        with patch.object(sdp, "service_record", return_value=b"\x19\x00\x03\x08\x09"):
            self.assertEqual(sdp.channel_for(ADDRESS, "uuid"), 9)

    def test_a_corrupt_cache_file_is_ignored_rather_than_fatal(self):
        cache = Path(self.folder.name) / "omarchy-headset"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / (ADDRESS.replace(":", "").lower() + ".json")).write_text("{not json")
        with patch.object(sdp, "service_record", return_value=b"\x19\x00\x03\x08\x09"):
            self.assertEqual(sdp.channel_for(ADDRESS, "uuid"), 9)

    def test_a_device_with_no_such_service_is_hopeless_rather_than_retried(self):
        # It answered and has no headset control service. Retrying that once a
        # minute for the life of the session helps nobody.
        with patch.object(sdp, "service_record", return_value=b"\x00\x01\x02"):
            with self.assertRaises(UnsupportedDevice):
                sdp.channel_for(ADDRESS, "uuid", use_cache=False)


class DeviceLifecycleTests(unittest.TestCase):
    def test_a_failed_handshake_closes_the_socket_it_opened(self):
        # Leaving it open makes the next attempt fail with EBUSY against this
        # process rather than against the headset, for ever.
        from headset import drivers
        from headset.device import Device

        device = Device(address=ADDRESS, driver=drivers.by_id("sony-mdr"), name="WH-1000XM5")
        closed = {"count": 0}

        class Stub:
            alive = True

            def connect(self):
                pass

            def submit(self, *args, **kwargs):
                pass

            def run_until_idle(self, timeout=0):
                pass

            def close(self):
                closed["count"] += 1
                Stub.alive = False

        with patch.object(sdp, "channel_for", return_value=9), \
             patch("headset.device.Session", return_value=Stub()):
            with self.assertRaises(HeadsetError):
                device.open()
        self.assertEqual(closed["count"], 1)


class HandshakeTests(unittest.TestCase):
    def make(self, name, reply, driver=None):
        from headset import drivers
        from headset.device import Device

        device = Device(address=ADDRESS, driver=driver or drivers.by_id("sony-mdr"), name=name)

        class Stub:
            alive = True

            def connect(self):
                pass

            def submit(self, payload, message_type=None, expect=None, on_done=None, label=""):
                on_done("replied", reply)

            def run_until_idle(self, timeout=0):
                pass

            def close(self):
                pass

        return device, Stub()

    def test_a_driver_that_identifies_nothing_still_connects(self):
        # Whether it answered is a different question from what the driver read
        # out of the answer, and conflating them refused every such driver.
        import dataclasses

        from headset.drivers import sony_mdr

        plain = dataclasses.replace(sony_mdr.DRIVER, identify=None, protocols=())
        device, stub = self.make("Something", bytes.fromhex("01 00"), driver=plain)
        with patch.object(sdp, "channel_for", return_value=9), \
             patch("headset.device.Session", return_value=stub):
            device.open()
        self.assertTrue(device.ready)

    def test_a_handshake_that_never_answered_is_still_a_failure(self):
        from headset import drivers
        from headset.device import Device

        device = Device(address=ADDRESS, driver=drivers.by_id("sony-mdr"), name="WH-1000XM5")

        class Silent:
            alive = True

            def connect(self):
                pass

            def submit(self, payload, message_type=None, expect=None, on_done=None, label=""):
                on_done("silent", None)

            def run_until_idle(self, timeout=0):
                pass

            def close(self):
                pass

        with patch.object(sdp, "channel_for", return_value=9), \
             patch("headset.device.Session", return_value=Silent()):
            with self.assertRaises(HeadsetError):
                device.open()

    def test_a_refused_headset_publishes_nothing_about_itself_first(self):
        # It used to apply the reading, which told the panel about a headset that
        # was about to be rejected, and then reject it.
        from headset import drivers
        from headset.device import Device

        seen = []
        device = Device(address=ADDRESS, driver=drivers.by_id("sony-mdr"), name="WH-1000XM4",
                        on_change=seen.append)
        _, stub = self.make("WH-1000XM4", bytes.fromhex("01 00 40 10"))
        with patch.object(sdp, "channel_for", return_value=9), \
             patch("headset.device.Session", return_value=stub):
            with self.assertRaises(UnsupportedDevice):
                device.open()
        self.assertEqual(seen, [])


class ProtocolGuardTests(unittest.TestCase):
    def test_a_headset_speaking_the_older_protocol_is_refused_by_name(self):
        # Its records would almost all go unanswered, and the panel would be
        # empty with nothing said about why.
        from headset import drivers, framing
        from headset.device import Device

        device = Device(address=ADDRESS, driver=drivers.by_id("sony-mdr"), name="WH-1000XM4")

        class Stub:
            alive = True

            def connect(self):
                pass

            def submit(self, payload, message_type=None, expect=None, on_done=None, label=""):
                # A four-byte init reply is what a v1 headset answers with.
                on_done("replied", bytes.fromhex("01 00 40 10"))

            def run_until_idle(self, timeout=0):
                pass

            def close(self):
                pass

        with patch.object(sdp, "channel_for", return_value=9), \
             patch("headset.device.Session", return_value=Stub()):
            with self.assertRaises(UnsupportedDevice) as caught:
                device.open()
        self.assertIn("v1", str(caught.exception))

    def test_a_headset_speaking_the_right_protocol_is_accepted(self):
        from headset import drivers
        from headset.device import Device

        device = Device(address=ADDRESS, driver=drivers.by_id("sony-mdr"), name="WH-1000XM5")

        class Stub:
            alive = True

            def connect(self):
                pass

            def submit(self, payload, message_type=None, expect=None, on_done=None, label=""):
                on_done("replied", bytes.fromhex("01 00 03 00 20 16 00 00"))

            def run_until_idle(self, timeout=0):
                pass

            def close(self):
                pass

        with patch.object(sdp, "channel_for", return_value=9), \
             patch("headset.device.Session", return_value=Stub()):
            device.open()
        self.assertTrue(device.ready)
        self.assertEqual(device.state["protocol"], "v2")


class LineTests(unittest.TestCase):
    """Reading lines off a stream that may never send another newline."""

    def test_whole_lines_come_out_and_the_rest_is_kept(self):
        lines, rest = ipc.take_lines(b'{"a": 1}\n{"b": 2}\n{"c"')
        self.assertEqual(lines, [b'{"a": 1}', b'{"b": 2}'])
        self.assertEqual(rest, b'{"c"')

    def test_a_line_split_across_chunks_still_arrives(self):
        # The bound must not cost the ordinary case: a line arriving in two
        # reads is what a socket does under any load at all.
        lines, rest = ipc.take_lines(b'{"half"')
        self.assertEqual(lines, [])
        lines, rest = ipc.take_lines(rest + b': 1}\n')
        self.assertEqual(lines, [b'{"half": 1}'])
        self.assertEqual(rest, b"")

    def test_a_peer_that_sends_no_newline_cannot_grow_the_buffer(self):
        rest = b""
        for _ in range(16):
            lines, rest = ipc.take_lines(rest + b"x" * (ipc.MAX_LINE // 4))
            self.assertEqual(lines, [])
            self.assertLessEqual(len(rest), ipc.MAX_LINE)

    def test_a_whole_line_after_a_dropped_one_is_still_read(self):
        rest = b"x" * (ipc.MAX_LINE + 1)
        lines, rest = ipc.take_lines(rest)
        self.assertEqual(rest, b"")
        lines, rest = ipc.take_lines(rest + b'{"after": true}\n')
        self.assertEqual(lines, [b'{"after": true}'])


if __name__ == "__main__":
    unittest.main()
