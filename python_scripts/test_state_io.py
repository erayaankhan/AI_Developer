"""state_io: atomic write, locked read-modify-write, stale and held locks."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import state_io


class StateIoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_write_state_replaces_file_and_leaves_no_temp(self):
        self.path.write_text('{"old": 1}', encoding="utf-8")
        state_io.write_state(self.path, {"new": 2})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"new": 2})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_failed_write_keeps_the_old_file(self):
        self.path.write_text('{"old": 1}', encoding="utf-8")
        with mock.patch("state_io.os.replace", side_effect=PermissionError), mock.patch("state_io.time.sleep"):
            with self.assertRaises(PermissionError):
                state_io.write_state(self.path, {"new": 2})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": 1})
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["state.json"])

    def test_update_state_changes_the_file_as_it_is_now(self):
        self.path.write_text('{"a": 1}', encoding="utf-8")
        stale_copy = json.loads(self.path.read_text(encoding="utf-8"))
        state_io.write_state(self.path, {"a": 1, "b": 2})  # another command wrote meanwhile
        state_io.update_state(self.path, lambda state: state.update(c=3))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"a": 1, "b": 2, "c": 3})
        self.assertEqual(stale_copy, {"a": 1})
        self.assertFalse(self.path.with_name("state.json.lock").exists())

    def test_update_state_without_a_file_does_nothing(self):
        self.assertEqual(state_io.update_state(self.path, lambda state: state.update(c=3)), {})
        self.assertFalse(self.path.exists())

    def test_lock_is_released_when_the_change_raises(self):
        self.path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            state_io.update_state(self.path, lambda state: (_ for _ in ()).throw(ValueError("boom")))
        self.assertFalse(self.path.with_name("state.json.lock").exists())

    def test_stale_lock_is_taken_over(self):
        self.path.write_text("{}", encoding="utf-8")
        lock = self.path.with_name("state.json.lock")
        lock.write_text("", encoding="utf-8")
        old = time.time() - state_io.LOCK_STALE_SECONDS - 5
        os.utime(lock, (old, old))
        state_io.update_state(self.path, lambda state: state.update(ok=True))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"ok": True})

    def test_held_lock_times_out(self):
        self.path.write_text("{}", encoding="utf-8")
        self.path.with_name("state.json.lock").write_text("", encoding="utf-8")
        with mock.patch.object(state_io, "LOCK_WAIT_SECONDS", 0.2):
            with self.assertRaises(TimeoutError):
                state_io.update_state(self.path, lambda state: state.update(ok=True))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {})


if __name__ == "__main__":
    unittest.main()
