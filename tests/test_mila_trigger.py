"""Trigger-driven Mila runs: RunLock wait, `run --wait-lock`, the pending pre-check."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daemon as daemon_cli  # noqa: E402
from workspace_daemon import mila_source, state  # noqa: E402
from test_mila_source import record  # noqa: E402

SEGMENTS = [{"start": 0, "end": 2, "text": "A durable decision."}]


class RunLockWaitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_wait_succeeds_when_holder_releases_in_time(self):
        holder = state.RunLock(self.base)
        holder.__enter__()
        threading.Timer(0.3, holder.__exit__, (None, None, None)).start()
        started = time.monotonic()
        with mock.patch.object(state, "log") as log:
            with state.RunLock(self.base, wait=5, poll=0.05):
                waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 4)
        messages = [c.args[0] for c in log.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertIn("waiting up to", messages[0])
        self.assertIn("acquired after", messages[1])

    def test_wait_times_out_with_already_running(self):
        with state.RunLock(self.base):
            started = time.monotonic()
            with mock.patch.object(state, "log"), \
                 self.assertRaises(state.AlreadyRunning):
                state.RunLock(self.base, wait=0.3, poll=0.05).__enter__()
            self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_default_does_not_wait(self):
        with state.RunLock(self.base):
            started = time.monotonic()
            with self.assertRaises(state.AlreadyRunning):
                state.RunLock(self.base).__enter__()
            self.assertLess(time.monotonic() - started, 1)


class CmdRunTriggerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()
        self.routine = {
            "id": "mila-meetings", "enabled": True,
            "source": {"kind": "mila",
                       "recordings_file": str(self.base / "recordings.json")},
        }
        for patch in (
            mock.patch.object(daemon_cli, "BASE_DIR", self.base),
            mock.patch.object(daemon_cli, "set_log_file"),
            mock.patch.object(daemon_cli, "log"),
            mock.patch.object(daemon_cli.config, "discover",
                              return_value=[self.routine]),
            mock.patch.object(daemon_cli.config, "secure_routine_files"),
            mock.patch.object(daemon_cli.config, "validate", return_value=[]),
            mock.patch.object(daemon_cli.config, "cross_problems",
                              return_value={}),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def args(self, **kw):
        values = dict(
            dry_run=False, refresh_labels=False, routine="mila-meetings",
            include_disabled=False, ignore_pause=False, wait_lock=600,
            if_pending=False,
        )
        values.update(kw)
        return SimpleNamespace(**values)

    def test_wait_lock_timeout_falls_back_to_exit_locked(self):
        with mock.patch.object(
                daemon_cli.runner, "run",
                side_effect=state.AlreadyRunning("busy")) as run:
            code = daemon_cli.cmd_run(self.args())
        self.assertEqual(code, state.EXIT_LOCKED)
        self.assertTrue(all(c.kwargs["wait_lock"] == 600
                            for c in run.call_args_list))

    def test_pause_still_blocks_a_trigger(self):
        (self.base / "state" / "PAUSED").write_text("hold\n")
        with mock.patch.object(daemon_cli.runner, "run") as run:
            self.assertEqual(daemon_cli.cmd_run(self.args()), 0)
        run.assert_not_called()

    def test_if_pending_skips_without_taking_the_lock(self):
        with mock.patch.object(daemon_cli.mila_source, "has_pending",
                               return_value=False), \
             mock.patch.object(daemon_cli.runner, "run") as run:
            self.assertEqual(daemon_cli.cmd_run(self.args(if_pending=True)), 0)
        run.assert_not_called()

    def test_if_pending_runs_when_work_exists(self):
        totals = {"processed": 0, "skipped": 0, "errors": 0}
        with mock.patch.object(daemon_cli.mila_source, "has_pending",
                               return_value=True), \
             mock.patch.object(daemon_cli.runner, "run",
                               return_value=totals) as run:
            self.assertEqual(daemon_cli.cmd_run(self.args(if_pending=True)), 0)
        run.assert_called_once()

    def test_if_pending_runs_when_precheck_fails(self):
        totals = {"processed": 0, "skipped": 0, "errors": 0}
        with mock.patch.object(daemon_cli.mila_source, "has_pending",
                               side_effect=RuntimeError("unreadable")), \
             mock.patch.object(daemon_cli.runner, "run",
                               return_value=totals) as run:
            daemon_cli.cmd_run(self.args(if_pending=True))
        run.assert_called_once()


class HasPendingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "recordings.json"
        self.source = {"kind": "mila", "recordings_file": str(self.file)}

    def write(self, *records):
        self.file.write_text(json.dumps(list(records)))

    def done(self, **kw):
        return record("R1", segments=SEGMENTS, **kw)

    def ledger_for_all(self, **extra):
        return {c["id"]: dict(extra)
                for c in mila_source.candidates(self.source)}

    def test_new_completed_recording_is_pending(self):
        self.write(self.done())
        self.assertTrue(mila_source.has_pending(self.source, {}))

    def test_processed_recording_is_not_pending(self):
        self.write(self.done())
        ledger = self.ledger_for_all(memory="saved")
        self.assertFalse(mila_source.has_pending(self.source, ledger))

    def test_ledgered_with_memory_error_is_still_pending(self):
        self.write(self.done())
        ledger = self.ledger_for_all(memory_error="sink down")
        self.assertTrue(mila_source.has_pending(self.source, ledger))

    def test_corrected_transcript_is_pending_again(self):
        self.write(self.done())
        ledger = self.ledger_for_all(memory="saved")
        self.write(record("R1", segments=[
            {"start": 0, "end": 2, "text": "A corrected decision."}]))
        self.assertTrue(mila_source.has_pending(self.source, ledger))

    def test_recording_in_progress_is_not_pending(self):
        self.write(record("R1", status="recording", segments=SEGMENTS))
        self.assertFalse(mila_source.has_pending(self.source, {}))

    def test_deleted_recording_is_not_pending(self):
        self.write(dict(self.done(), deletedAt="2026-07-28T10:00:00Z"))
        self.assertFalse(mila_source.has_pending(self.source, {}))

    def test_excluded_recording_is_not_pending(self):
        self.write(self.done())
        source = dict(self.source, exclude_recording_ids=["R1"])
        self.assertFalse(mila_source.has_pending(source, {}))

    def test_held_for_app_summary_counts_as_pending(self):
        self.write(self.done())
        source = dict(self.source, app_summary={"mode": "hints"})
        self.assertTrue(mila_source.has_pending(source, {}))

    def test_empty_transcript_error_is_pending_until_ledgered(self):
        self.write(record("R1"))
        self.assertTrue(mila_source.has_pending(self.source, {}))
        ledger = {c["id"]: {"status": "failed"}
                  for c in mila_source.candidates(self.source)}
        self.assertFalse(mila_source.has_pending(self.source, ledger))


if __name__ == "__main__":
    unittest.main()
