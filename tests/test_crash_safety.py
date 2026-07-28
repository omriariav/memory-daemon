"""Crash-safety regression tests.

Every case here is a bug that actually shipped and was found in review. They use
fault injection rather than real interruptions, since the point is to pin the
recovery behaviour, not to exercise the kernel.

Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace_daemon import actions, notes, state  # noqa: E402


def routine(vault, **output):
    cfg = {"vault_dir": str(vault), "slug_prefix": "note"}
    cfg.update(output)
    return {
        "id": "t", "source": {"kind": "gmail"},
        "analyze": {"model": "m"}, "output": cfg,
    }


def item(item_id="abc123def456", title="Weekly sync", date="2026-07-26"):
    return {"id": item_id, "title": title, "date": date,
            "body": "body", "frontmatter": {}}


class TestNoteCollision(unittest.TestCase):
    """A crash between writing a note and recording the ledger must not duplicate."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_item_rewritten_in_place(self):
        r, i = routine(self.vault), item()
        first = notes.write(r, i, "summary v1", None)
        second = notes.write(r, i, "summary v2", None)
        self.assertEqual(first, second, "a retry of the same item must overwrite its own note")
        self.assertEqual(len(list(self.vault.glob("*.md"))), 1)
        self.assertIn("summary v2", second.read_text())

    def test_different_items_do_not_overwrite(self):
        r = routine(self.vault)
        a = notes.write(r, item("aaaaaaaa1111", "Sync"), "from A", None)
        b = notes.write(r, item("bbbbbbbb2222", "Sync"), "from B", None)
        self.assertNotEqual(a, b, "genuinely different items must not share a note")
        self.assertIn("from A", a.read_text())
        self.assertIn("from B", b.read_text())

    def test_owner_survives_a_legacy_note_without_item_id(self):
        """Notes written before item_id existed still carry gmail_message_id."""
        path = self.vault / "note-2026-07-26.md"
        path.write_text("---\nkind: x\ngmail_message_id: abc123def456\n---\n\nold\n")
        self.assertEqual(notes.note_owner(path), "abc123def456")
        self.assertEqual(notes.write(routine(self.vault), item(), "new", None), path)

    def test_owner_of_a_non_note_file_is_none(self):
        path = self.vault / "note-2026-07-26.md"
        path.write_text("no frontmatter here")
        self.assertIsNone(notes.note_owner(path))


class TestLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def ledger(self):
        return json.loads((self.base / "state" / "processed.json").read_text())

    def test_failed_write_is_not_committed_by_a_later_one(self):
        store = state.Store(self.base)
        original = state.write_atomic
        state.write_atomic = lambda p, t: (_ for _ in ()).throw(OSError("disk full"))
        try:
            with self.assertRaises(OSError):
                store.record("A", {"rule_id": "r"})
        finally:
            state.write_atomic = original
        store.record("B", {"rule_id": "r"})
        self.assertEqual(list(self.ledger()), ["B"], "A's failed write must not ride along with B")

    def test_each_record_is_flushed_immediately(self):
        store = state.Store(self.base)
        store.record("A", {"rule_id": "r"})
        self.assertEqual(list(self.ledger()), ["A"])
        store.record("B", {"rule_id": "r"})
        self.assertEqual(sorted(self.ledger()), ["A", "B"])

    def test_dry_run_writes_nothing(self):
        state.Store(self.base, dry_run=True).record("A", {"rule_id": "r"})
        self.assertFalse((self.base / "state" / "processed.json").exists())

    def test_wrong_shaped_json_raises_state_error(self):
        path = self.base / "state" / "processed.json"
        for bad in ("[]", "null", '"hello"', '{"k": 5}', "{oops"):
            path.write_text(bad)
            with self.assertRaises(state.StateError, msg=f"{bad!r} should be rejected"):
                state.load(self.base)

    def test_permissions_are_preserved_across_replace(self):
        path = self.base / "state" / "processed.json"
        path.write_text("{}")
        os.chmod(path, 0o600)
        state.Store(self.base).record("A", {"rule_id": "r"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_no_temp_file_is_left_behind(self):
        state.Store(self.base).record("A", {"rule_id": "r"})
        self.assertEqual(list((self.base / "state").glob(".*.tmp")), [])

    def test_sweep_removes_only_stale_temps(self):
        d = self.base / "state"
        fresh, stale = d / ".processed.json.1.tmp", d / ".processed.json.2.tmp"
        fresh.write_text("x")
        stale.write_text("x")
        os.utime(stale, (0, 0))
        state.sweep_temp_files(d)
        self.assertTrue(fresh.exists(), "an in-flight write must not be swept")
        self.assertFalse(stale.exists())


class TestRunLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_run_is_refused_and_lock_is_reusable(self):
        with state.RunLock(self.base):
            with self.assertRaises(state.AlreadyRunning):
                state.RunLock(self.base).__enter__()
        with state.RunLock(self.base):
            pass  # released on exit

    def test_holder_pid_survives_a_losing_contender(self):
        """open(path,'w') would truncate the pid before even trying to lock."""
        lock = state.RunLock(self.base)
        with lock:
            with self.assertRaises(state.AlreadyRunning):
                state.RunLock(self.base).__enter__()
            self.assertEqual(lock.path.read_text().strip(), str(os.getpid()))

    def test_holder_detects_a_deleted_lock_file(self):
        """flock survives on an orphan inode; the holder must notice and stop.

        Once the path is gone there is no rendezvous left, so another run can
        legitimately lock a freshly created file. The guarantee we can keep is
        that the holder refuses to keep processing.
        """
        with state.RunLock(self.base) as held:
            self.assertTrue(held.still_held())
            held.check()  # no raise while healthy
            held.path.unlink()
            self.assertFalse(held.still_held())
            with self.assertRaises(state.AlreadyRunning):
                held.check()

    def test_holder_detects_a_replaced_lock_file(self):
        with state.RunLock(self.base) as held:
            held.path.unlink()
            held.path.write_text("someone else\n")
            with self.assertRaises(state.AlreadyRunning):
                held.check()


class TestActionRetry(unittest.TestCase):
    def setUp(self):
        self.saved = dict(actions._HANDLERS)
        self.calls = []

    def tearDown(self):
        actions._HANDLERS.clear()
        actions._HANDLERS.update(self.saved)

    def test_a_failing_action_does_not_abort_the_others(self):
        actions._HANDLERS["mark_read"] = lambda m: self.calls.append("mark_read")
        actions._HANDLERS["archive"] = lambda m: (_ for _ in ()).throw(RuntimeError("boom"))
        actions._HANDLERS["unstar"] = lambda m: self.calls.append("unstar")
        applied, pending = actions.apply("id", ["mark_read", "archive", "unstar"], None)
        self.assertEqual(applied, ["mark_read", "unstar"])
        self.assertEqual(pending, ["archive"], "the failure must come back for retry")
        self.assertIn("unstar", self.calls, "an early failure must not skip later actions")

    def test_pending_order_follows_the_declared_sequence(self):
        for name in ("mark_read", "archive"):
            actions._HANDLERS[name] = lambda m: (_ for _ in ()).throw(RuntimeError("boom"))
        _, pending = actions.apply("id", ["mark_read", "archive"], None)
        self.assertEqual(pending, ["mark_read", "archive"])

    def test_apply_label_is_skipped_without_a_validated_label(self):
        applied, pending = actions.apply("id", ["apply_label"], None)
        self.assertEqual((applied, pending), ([], []))


class TestFsyncDirFailure(unittest.TestCase):
    """A dir-fsync failure after a successful replace must not look like a failed write."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()
        self.saved = state._fsync_dir
        state._fsync_dir = lambda p: (_ for _ in ()).throw(OSError("EIO"))

    def tearDown(self):
        state._fsync_dir = self.saved
        self.tmp.cleanup()

    def test_write_still_succeeds_and_commits(self):
        store = state.Store(self.base)
        store.record("A", {"rule_id": "r"})  # must not raise
        on_disk = json.loads((self.base / "state" / "processed.json").read_text())
        self.assertEqual(list(on_disk), ["A"])
        self.assertIn("A", store, "in-memory state must match what is on disk")

    def test_a_later_write_does_not_lose_the_earlier_one(self):
        """The bug this guards: treating a committed write as failed, then
        rebuilding from stale memory and clobbering it."""
        store = state.Store(self.base)
        store.record("A", {"rule_id": "r"})
        store.record("B", {"rule_id": "r"})
        self.assertEqual(sorted(json.loads((self.base / "state" / "processed.json").read_text())),
                         ["A", "B"])


class TestStreams(unittest.TestCase):
    """`streams` gives one routine several report streams. Never reviewed until late."""

    @staticmethod
    def make(streams=None, **extra):
        r = {"id": "t", "enabled": True,
             "source": {"kind": "gmail", "query": "in:inbox"},
             "analyze": {"provider": "gemini", "model": "m", "instruction": "go"},
             "output": {"vault_dir": "/tmp/x", "slug_prefix": "p"},
             "actions": ["apply_label"]}
        if streams is not None:
            r["streams"] = streams
        r.update(extra)
        return r

    @staticmethod
    def msg(subject="", sender=""):
        return {"id": "1", "frontmatter": {"email_subject": subject, "email_from": sender}}

    def test_matches_on_subject_including_reply_prefixes(self):
        r = self.make({"Weekly Report DACH": {"title": "Weekly - DACH", "label": "EMEA"}})
        from workspace_daemon import runner
        for subject in ("Weekly Report DACH TEAM - week29",
                        "Re: Weekly Report DACH TEAM - week29",
                        "FW: Weekly Report DACH TEAM"):
            self.assertEqual(runner._stream_for(r, self.msg(subject))["label"], "EMEA", subject)

    def test_subject_match_survives_a_reply_from_someone_else(self):
        """The real case: the Turkey report arrives as a reply from a colleague."""
        from workspace_daemon import runner
        r = self.make({"Turkey, Africa, Baltics Weekly": {"title": "Weekly - TAB", "label": "EMEA"}})
        item = self.msg("Re: Turkey, Africa, Baltics Weekly Updates | 4/7/2026",
                        '"Someone Else" <someone.else@example.com>')
        self.assertEqual(runner._stream_for(r, item)["title"], "Weekly - TAB")

    def test_from_prefix_matches_the_sender(self):
        from workspace_daemon import runner
        r = self.make({"from:boss@example.com": {"title": "Boss", "label": "ECN"}})
        self.assertEqual(runner._stream_for(r, self.msg("anything", "Boss <boss@example.com>"))["title"],
                         "Boss")
        self.assertEqual(runner._stream_for(r, self.msg("boss@example.com", "other@example.com")), {})

    def test_no_match_returns_empty_and_no_label(self):
        from workspace_daemon import runner
        r = self.make({"Nope": {"title": "N", "label": "L"}})
        self.assertEqual(runner._stream_for(r, self.msg("unrelated")), {})
        self.assertIsNone(runner._static_label(r, self.msg("unrelated")))

    def test_routine_label_is_the_fallback(self):
        from workspace_daemon import runner
        r = self.make({"Nope": {"title": "N"}}, label="FALLBACK")
        self.assertEqual(runner._static_label(r, self.msg("unrelated")), "FALLBACK")

    def test_configured_labels_sees_stream_labels(self):
        """The bug: validation counted stream labels, the runner did not, so a
        streams-only routine ran with an empty catalog and rejected its own."""
        from workspace_daemon import config, runner
        r = self.make({"A": {"label": "EMEA"}, "B": {"label": "CHANNELS"}})
        self.assertEqual(sorted(config.configured_labels(r)), ["CHANNELS", "EMEA"])
        self.assertTrue(runner._needs_label_catalog([r]),
                        "a streams-only routine still needs the label catalog")

    def test_apply_label_is_satisfied_by_a_stream_label(self):
        from workspace_daemon import config
        self.assertEqual(config.validate(self.make({"A": {"label": "EMEA"}})), [])

    def test_apply_label_without_any_label_source_is_rejected(self):
        from workspace_daemon import config
        problems = config.validate(self.make())
        self.assertTrue(any("apply_label" in p for p in problems), problems)

    def test_configured_label_and_pick_label_are_mutually_exclusive(self):
        from workspace_daemon import config
        r = self.make({"A": {"label": "EMEA"}})
        r["analyze"]["pick_label"] = True
        self.assertTrue(any("not both" in p for p in config.validate(r)), config.validate(r))

    def test_malformed_streams_are_rejected(self):
        from workspace_daemon import config
        for streams in ({}, [], {"A": "just a string"}, {"A": {"bogus": 1}}):
            self.assertTrue(config.validate(self.make(streams)),
                            f"{streams!r} should be rejected")

    def test_message_updates_must_be_boolean(self):
        from workspace_daemon import config
        valid = self.make({"A": {"label": "EMEA", "message_updates": True}})
        invalid = self.make({"A": {"label": "EMEA", "message_updates": "yes"}})
        self.assertEqual(config.validate(valid), [])
        self.assertTrue(
            any("message_updates must be a boolean" in p for p in config.validate(invalid))
        )

    def test_message_updates_requires_a_gmail_source(self):
        from workspace_daemon import config
        r = self.make(
            {"A": {"message_updates": True}},
            source={"kind": "slack", "channels": ["CEXAMPLE"]},
            actions=[],
        )
        self.assertTrue(
            any("message_updates requires a Gmail source" in p for p in config.validate(r))
        )

    def test_validated_label_is_case_insensitive_and_rejects_unknown(self):
        from workspace_daemon import runner
        self.assertEqual(runner._validated_label("emea", ["EMEA", "CHANNELS"], "t"), "EMEA")
        with self.assertRaises(RuntimeError):
            runner._validated_label("NOPE", ["EMEA"], "t")


class TestTempSweepScope(unittest.TestCase):
    """The sweep runs inside the user's vault, which the daemon does not own."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_our_own_temp_shape_is_swept(self):
        ours = self.dir / ".processed.json.123.tmp"
        theirs = [self.dir / ".obsidian-sync.tmp", self.dir / ".note.md.swp.tmp"]
        for f in [ours, *theirs]:
            f.write_text("x")
            os.utime(f, (0, 0))  # all stale
        state.sweep_temp_files(self.dir)
        self.assertFalse(ours.exists())
        for f in theirs:
            self.assertTrue(f.exists(), f"{f.name} is not ours and must survive")


if __name__ == "__main__":
    unittest.main()
