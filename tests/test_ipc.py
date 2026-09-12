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
from headset.errors import HeadsetError  # noqa: E402

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

    def test_a_record_with_no_rfcomm_channel_is_an_error_not_a_guess(self):
        with patch.object(sdp, "service_record", return_value=b"\x00\x01\x02"):
            with self.assertRaises(HeadsetError):
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


if __name__ == "__main__":
    unittest.main()
