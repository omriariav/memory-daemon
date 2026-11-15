"""Local inbox state: done, snooze, ack; atomic, private, capped."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon.inbox import store as store_mod
from workspace_daemon.inbox.store import InboxStore


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def row(i):
    return {"id": i}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.clock = Clock()
        self.path = Path(self.base) / "state" / "inbox-state.json"

    def store(self):
        return InboxStore(self.base, clock=self.clock)

    def test_done_hides_and_persists(self):
        s = self.store()
        s.done("a")
        self.assertTrue(s.is_hidden("a"))
        self.assertEqual([r["id"] for r in s.visible([row("a"), row("b")])], ["b"])
        self.assertTrue(self.store().is_hidden("a"))  # a fresh process agrees

    def test_ack_hides(self):
        s = self.store()
        s.ack("a")
        self.assertTrue(s.is_hidden("a"))

    def test_snooze_hides_then_reappears_when_due(self):
        s = self.store()
        s.snooze("a", self.clock.t + 3600)
        self.assertTrue(s.is_hidden("a"))
        self.assertEqual(s.snoozed_until("a"), self.clock.t + 3600)
        self.clock.t += 3599
        self.assertTrue(s.is_hidden("a"))
        self.clock.t += 2
        self.assertFalse(s.is_hidden("a"))
        self.assertEqual([r["id"] for r in s.visible([row("a")])], ["a"])
        self.assertIsNone(s.snoozed_until("a"))

    def test_restore_undoes_a_mark(self):
        s = self.store()
        s.done("a")
        s.restore("a")
        self.assertFalse(s.is_hidden("a"))
        self.assertFalse(self.store().is_hidden("a"))

    def test_file_is_private_json_and_leaves_no_temp_files(self):
        self.store().done("a")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode), 0o700)
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["v"], 1)
        self.assertIn("done", doc["items"]["a"])
        self.assertEqual([p.name for p in self.path.parent.iterdir()],
                         ["inbox-state.json"])

    def test_write_is_atomic(self):
        s = self.store()
        s.done("a")
        before = self.path.read_text()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                s.done("b")
        self.assertEqual(self.path.read_text(), before)  # old file intact
        self.assertEqual([p.name for p in self.path.parent.iterdir()],
                         ["inbox-state.json"])  # temp file cleaned up

    def test_second_window_marks_are_not_lost(self):
        a, b = self.store(), self.store()
        a.done("x")
        b.done("y")
        fresh = self.store()
        self.assertTrue(fresh.is_hidden("x") and fresh.is_hidden("y"))

    def test_corrupt_file_reads_empty_and_is_kept_aside(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{ not json")
        s = self.store()
        self.assertFalse(s.is_hidden("a"))
        s.done("a")
        self.assertTrue(Path(str(self.path) + ".bad").exists())
        self.assertTrue(self.store().is_hidden("a"))

    def test_wrong_shapes_are_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"v": 1, "items": {
            "a": "str", "b": {"done": "yes"}, "c": {"done": 5.0}}}))
        s = self.store()
        self.assertFalse(s.is_hidden("a"))
        self.assertFalse(s.is_hidden("b"))
        self.assertTrue(s.is_hidden("c"))

    def test_old_marks_are_pruned_but_active_snoozes_stay(self):
        s = self.store()
        s.done("old")
        s.snooze("sleeper", self.clock.t + 90 * 86400)
        self.clock.t += store_mod.RETENTION_S + 10
        s.done("fresh")
        self.assertNotIn("old", s.marks)
        self.assertIn("sleeper", s.marks)
        self.assertIn("fresh", s.marks)

    def test_entry_count_is_capped(self):
        with mock.patch.object(store_mod, "MAX_ENTRIES", 5):
            s = self.store()
            for n in range(9):
                self.clock.t += 1
                s.done(f"i{n}")
            self.assertEqual(len(s.marks), 5)
            self.assertIn("i8", s.marks)
            self.assertNotIn("i0", s.marks)

    def test_in_memory_store_touches_no_files(self):
        s = InboxStore(None, clock=self.clock)
        s.done("a")
        self.assertTrue(s.is_hidden("a"))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
