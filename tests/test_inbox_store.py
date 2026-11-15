"""Local inbox state: done, snooze, ack; atomic, private, capped."""
import json
import os
import stat
import tempfile
import threading
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

    def files(self):
        return sorted(p.name for p in self.path.parent.iterdir())

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
        self.assertEqual(self.files(), ["inbox-state.json", "inbox-state.lock"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent / "inbox-state.lock").st_mode), 0o600)

    def test_write_is_atomic(self):
        s = self.store()
        s.done("a")
        before = self.path.read_text()
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                s.done("b")
        self.assertEqual(self.path.read_text(), before)  # old file intact
        self.assertEqual(self.files(), ["inbox-state.json", "inbox-state.lock"])  # no temp left

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

    def test_marks_of_unlisted_items_expire_after_seven_days(self):
        s = self.store()
        s.done("gone")
        s.snooze("sleeper", self.clock.t + 90 * 86400)
        self.clock.t += store_mod.RETENTION_S - 10
        s.note_listed([], prune=True)
        self.assertIn("gone", s.marks)            # not yet seven days
        self.clock.t += 20
        s.note_listed([], prune=True)
        self.assertNotIn("gone", s.marks)
        self.assertIn("sleeper", s.marks)          # an active snooze stays
        self.assertNotIn("gone", self.store().marks)

    def test_age_pruning_needs_an_explicit_successful_poll(self):
        s = self.store()
        s.done("gone")
        self.clock.t += 30 * 86400
        s.note_listed([])                          # a poll with a failed source
        s.done("other")                            # and plain writes
        self.assertIn("gone", s.marks)
        s.note_listed([], prune=True)
        self.assertNotIn("gone", s.marks)

    def test_writes_never_prune_by_age(self):
        s = self.store()
        s.done("old")
        self.clock.t += 60 * 86400
        for n in range(3):
            s.done(f"w{n}")
            s.ack(f"a{n}")
            s.snooze(f"s{n}", self.clock.t + 60)
            s.restore("nothing")
        self.assertIn("old", self.store().marks)

    def test_done_mark_written_before_the_first_poll_after_8_days_away(self):
        """Reproduction: the app reopens after 8 days, the user marks another
        item before the first poll lands; the still-listed marks must survive
        (and the long-gone one is pruned only by the poll)."""
        s = self.store()
        s.done("listed")
        s.done("gone")
        self.clock.t += 8 * 86400
        s = self.store()                           # reopened
        s.done("new-before-poll")                  # a write before any poll
        self.assertTrue(s.is_hidden("listed"))
        self.assertIn("gone", s.marks)
        s.note_listed(["listed", "new-before-poll"], prune=True)  # first poll
        fresh = self.store()
        self.assertTrue(fresh.is_hidden("listed"))
        self.assertTrue(fresh.is_hidden("new-before-poll"))
        self.assertNotIn("gone", fresh.marks)

    def test_a_twenty_day_old_done_item_stays_hidden_while_listed(self):
        s = self.store()
        s.done("old")
        for _ in range(20):                        # a poll a day lists it
            self.clock.t += 86400
            s.note_listed(["old"])
        s.note_listed(["old"], prune=True)
        s.done("other")
        self.assertTrue(self.store().is_hidden("old"))
        self.assertIn("last_listed", s.marks["old"])

    def test_first_poll_after_a_long_absence_keeps_listed_marks(self):
        s = self.store()
        s.done("old")
        self.clock.t += 20 * 86400                 # app not run for 20 days
        s = self.store()
        s.done("new")                              # a write before the poll
        s.note_listed(["old"], prune=True)         # then the poll lists it
        self.assertTrue(self.store().is_hidden("old"))

    def test_note_listed_does_not_rewrite_the_file_every_poll(self):
        s = self.store()
        s.done("a")
        s.note_listed(["a"])
        before = os.stat(self.path).st_mtime_ns
        self.clock.t += 60
        s.note_listed(["a", "unmarked"])
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        self.assertNotIn("unmarked", s.marks)

    def test_load_picks_up_another_panes_marks(self):
        a, b = self.store(), self.store()
        b.done("x")
        self.assertFalse(a.is_hidden("x"))
        a.load()
        self.assertTrue(a.is_hidden("x"))

    def test_entry_count_is_capped(self):
        with mock.patch.object(store_mod, "MAX_ENTRIES", 5):
            s = self.store()
            for n in range(9):
                self.clock.t += 1
                s.done(f"i{n}")
            self.assertEqual(len(s.marks), 5)
            self.assertIn("i8", s.marks)
            self.assertNotIn("i0", s.marks)

    def test_concurrent_writers_lose_no_marks(self):
        """4 threads, each with its own store object (like 4 panes), 20
        trials of 20 marks: no exception, no lost mark."""
        for trial in range(20):
            base = tempfile.mkdtemp(dir=self.base)
            errors, barrier = [], threading.Barrier(4)

            def worker(n):
                try:
                    st = InboxStore(base, clock=self.clock)
                    barrier.wait()
                    for i in range(5):
                        st.done(f"t{n}-{i}")
                except Exception as exc:
                    errors.append(repr(exc))

            threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [], f"trial {trial}")
            marks = InboxStore(base, clock=self.clock).marks
            self.assertEqual(len(marks), 20, f"trial {trial}: lost marks")
            names = [p.name for p in (Path(base) / "state").iterdir()]
            self.assertEqual(sorted(names), ["inbox-state.json", "inbox-state.lock"])

    def test_one_shared_store_is_thread_safe_too(self):
        s = self.store()
        errors = []

        def worker(n):
            try:
                for i in range(20):
                    s.done(f"s{n}-{i}")
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store().marks), 80)

    def test_in_memory_store_touches_no_files(self):
        s = InboxStore(None, clock=self.clock)
        s.done("a")
        self.assertTrue(s.is_hidden("a"))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
