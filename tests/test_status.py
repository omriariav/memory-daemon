"""Health-status command regression tests."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import state, status


class LaunchdStatusTest(unittest.TestCase):
    def test_parses_only_safe_health_fields(self):
        output = """
gui/501/com.memory-daemon = {
    state = running
    environment = {
        SECRET => never-return-this
    }
    runs = 12
    pid = 4321
    last exit code = 0
    run interval = 900 seconds
}
"""
        result = mock.Mock(returncode=0, stdout=output)
        with mock.patch.object(status.subprocess, "run", return_value=result):
            health = status.probe_launchd(uid=501)

        self.assertEqual(
            health,
            {
                "loaded": True,
                "label": "com.memory-daemon",
                "state": "running",
                "pid": 4321,
                "runs": 12,
                "last_exit": 0,
                "interval_seconds": 900,
            },
        )
        self.assertNotIn("SECRET", repr(health))

    def test_missing_job_is_reported_without_raw_stderr(self):
        result = mock.Mock(returncode=113, stdout="", stderr="private path")
        with mock.patch.object(status.subprocess, "run", return_value=result):
            health = status.probe_launchd()
        self.assertEqual(health["detail"], "not loaded")
        self.assertNotIn("private path", repr(health))


class TickHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / "run.log"

    def write(self, text):
        self.log.write_text(text)

    def test_failed_tick_conservatively_marks_every_due_routine(self):
        self.write(
            "2026-07-29T10:00:00Z tick: due=alpha, beta\n"
            "2026-07-29T10:00:01Z routine=beta source ERROR: unavailable\n"
            "2026-07-29T10:00:02Z tick done: 1 processed, 0 already-seen, "
            "1 error(s)\n"
            "2026-07-29T10:15:00Z tick: no routines due\n"
        )
        history = status.read_tick_history(self.log)
        self.assertEqual(history["routines"]["alpha"]["state"], "error")
        self.assertEqual(history["routines"]["beta"]["state"], "error")
        self.assertEqual(history["latest"]["state"], "idle")

    def test_unfinished_tick_marks_its_routines_incomplete(self):
        self.write("2026-07-29T10:00:00Z tick: due=alpha, beta\n")
        history = status.read_tick_history(self.log)
        self.assertEqual(history["latest"]["state"], "incomplete")
        self.assertEqual(history["routines"]["alpha"]["state"], "incomplete")

    def test_unattributed_error_fails_all_routines_in_that_tick(self):
        self.write(
            "2026-07-29T10:00:00Z tick: due=alpha, beta\n"
            "2026-07-29T10:00:02Z tick done: 0 processed, 0 already-seen, "
            "1 error(s)\n"
        )
        history = status.read_tick_history(self.log)
        self.assertEqual(history["routines"]["alpha"]["state"], "error")
        self.assertEqual(history["routines"]["beta"]["state"], "error")

    def test_mixed_attributed_and_unattributed_errors_fail_all_due_routines(self):
        self.write(
            "2026-07-29T10:00:00Z tick: due=alpha, beta\n"
            "2026-07-29T10:00:01Z routine=beta source ERROR: unavailable\n"
            "2026-07-29T10:00:02Z ownership ERROR source=gmail id=same\n"
            "2026-07-29T10:00:03Z tick done: 0 processed, 0 already-seen, "
            "2 error(s)\n"
        )
        history = status.read_tick_history(self.log)
        self.assertEqual(history["routines"]["alpha"]["state"], "error")
        self.assertEqual(history["routines"]["beta"]["state"], "error")
        self.assertEqual(history["latest"]["state"], "error")

    def test_dry_run_does_not_replace_real_health(self):
        self.write(
            "2026-07-29T09:00:00Z tick: due=alpha\n"
            "2026-07-29T09:00:02Z routine=alpha source ERROR: unavailable\n"
            "2026-07-29T09:00:03Z tick done: 0 processed, 0 already-seen, "
            "1 error(s)\n"
            "2026-07-29T10:00:00Z tick: due=alpha (dry-run)\n"
            "2026-07-29T10:00:03Z tick done: 1 processed, 0 already-seen, "
            "0 error(s)\n"
        )
        history = status.read_tick_history(self.log)
        self.assertEqual(history["latest"]["state"], "error")
        self.assertEqual(history["latest"]["at"], "2026-07-29T09:00:03Z")
        self.assertEqual(history["routines"]["alpha"]["state"], "error")

    def test_interleaved_dry_run_is_correlated_by_tick_id(self):
        self.write(
            "2026-07-29T09:00:00Z tick[real123]: due=alpha\n"
            "2026-07-29T09:00:01Z tick[dry456]: due=beta (dry-run)\n"
            "2026-07-29T09:00:02Z tick[real123] done: 0 processed, "
            "0 already-seen, 1 error(s)\n"
            "2026-07-29T09:00:03Z tick[dry456] done: 1 processed, "
            "0 already-seen, 0 error(s) (dry-run)\n"
        )
        history = status.read_tick_history(self.log)
        self.assertEqual(history["latest"]["state"], "error")
        self.assertEqual(history["latest"]["at"], "2026-07-29T09:00:02Z")
        self.assertEqual(history["routines"]["alpha"]["state"], "error")
        self.assertNotIn("beta", history["routines"])


class RoutineStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.routines = [
            {"id": "alpha", "enabled": True, "schedule": {"every": "1h"}},
            {"id": "beta", "enabled": True, "schedule": {"every": "4h"}},
            {"id": "off", "enabled": False, "schedule": {"every": "1d"}},
        ]
        schedule_path = state.schedule_file(self.base)
        schedule_path.parent.mkdir(parents=True)
        schedule_path.write_text(json.dumps({
            "alpha": {
                "last_attempted_at": "2026-07-29T10:00:00Z",
                "last_attempted_epoch": 1000,
            },
            "beta": {
                "last_attempted_at": "2026-07-29T10:00:00Z",
                "last_attempted_epoch": 1000,
            },
        }))

    def test_reports_due_waiting_disabled_and_unresolved_work(self):
        state.save(self.base, {
            "one": {
                "rule_id": "alpha",
                "processed_at": "1970-01-01T00:16:40Z",
                "memory_error": "sink failed",
            },
            "two": {
                "rule_id": "alpha",
                "processed_at": "1970-01-01T00:16:41Z",
                "actions_pending": ["archive"],
            },
        })
        rows = status.routine_rows(
            self.base,
            self.routines,
            {"routines": {}},
            now=5000,
        )
        by_id = {row["routine"]: row for row in rows}

        self.assertEqual(by_id["alpha"]["status"], "attention")
        self.assertEqual(
            by_id["alpha"]["issues"], "1 memory sink, 1 Gmail triage"
        )
        self.assertEqual(by_id["alpha"]["next"], "due")
        self.assertEqual(by_id["beta"]["status"], "ok")
        self.assertEqual(by_id["off"]["status"], "disabled")

    def test_running_tick_takes_precedence_over_due(self):
        rows = status.routine_rows(
            self.base,
            self.routines,
            {
                "latest": {"state": "incomplete", "at": "now"},
                "routines": {
                    "alpha": {"state": "incomplete", "at": "now"}
                },
            },
            now=5000,
            scheduler_running=True,
        )
        alpha = next(row for row in rows if row["routine"] == "alpha")
        self.assertEqual(alpha["status"], "running")

    def test_abandoned_tick_requires_attention(self):
        rows = status.routine_rows(
            self.base,
            self.routines,
            {"routines": {"alpha": {"state": "incomplete"}}},
            now=5000,
            scheduler_running=False,
        )
        alpha = next(row for row in rows if row["routine"] == "alpha")
        self.assertEqual(alpha["status"], "attention")
        self.assertEqual(alpha["issues"], "incomplete")


class RenderStatusTest(unittest.TestCase):
    @staticmethod
    def probe(current, legacy=None):
        legacy = legacy or {
            "loaded": False,
            "label": "com.workspace-daemon",
            "detail": "not loaded",
        }

        def resolve(label):
            return (
                current
                if label == status.DEFAULT_LAUNCHD_LABEL
                else legacy
            )

        return resolve

    def test_render_is_scannable_and_returns_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            routine = {
                "id": "example",
                "enabled": True,
                "schedule": {"every": "1h"},
            }
            state.ScheduleStore(base).mark_attempted({"example"}, now=1000)
            log = base / "logs" / "run.log"
            log.parent.mkdir()
            log.write_text(
                "1970-01-01T00:18:20Z tick: no routines due\n"
            )
            launchd = {
                "loaded": True,
                "label": "com.memory-daemon",
                "state": "not running",
                "pid": None,
                "runs": 4,
                "last_exit": 0,
                "interval_seconds": 900,
            }
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(launchd),
            ):
                text, healthy = status.render(
                    base, [routine], now=1100
                )

        self.assertTrue(healthy)
        self.assertIn("Memory Daemon", text)
        self.assertIn("Scheduler: loaded (idle)", text)
        self.assertIn("ROUTINE", text)
        self.assertIn("example", text)
        self.assertIn("Logs: logs/run.log", text)

    def test_launches_without_a_tick_log_are_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            launchd = {
                "loaded": True,
                "label": "com.memory-daemon",
                "state": "not running",
                "pid": None,
                "runs": 4,
                "last_exit": 0,
                "interval_seconds": 900,
            }
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(launchd),
            ):
                text, healthy = status.render(Path(tmp), [], now=1100)

        self.assertFalse(healthy)
        self.assertIn("ATTENTION: no tick log after 4 launch(es)", text)

    def test_stale_tick_log_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            log = base / "logs" / "run.log"
            log.parent.mkdir()
            log.write_text(
                "1970-01-01T00:16:40Z tick: no routines due\n"
            )
            launchd = {
                "loaded": True,
                "label": "com.memory-daemon",
                "state": "not running",
                "pid": None,
                "runs": 4,
                "last_exit": 0,
                "interval_seconds": 900,
            }
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(launchd),
            ):
                text, healthy = status.render(base, [], now=3000)

        self.assertFalse(healthy)
        self.assertIn("last tick is stale", text)

    def test_legacy_loaded_job_gets_a_migration_hint(self):
        current = {
            "loaded": False,
            "label": "com.memory-daemon",
            "detail": "not loaded",
        }
        legacy = {
            "loaded": True,
            "label": "com.workspace-daemon",
            "state": "not running",
            "runs": 1,
            "last_exit": 0,
            "interval_seconds": 900,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                status, "probe_launchd", side_effect=[current, legacy]
            ):
                text, healthy = status.render(Path(tmp), [], now=1100)

        self.assertFalse(healthy)
        self.assertIn("legacy com.workspace-daemon is still loaded", text)

    def test_both_loaded_jobs_are_unhealthy(self):
        current = {
            "loaded": True,
            "label": "com.memory-daemon",
            "state": "not running",
            "runs": 0,
            "last_exit": 0,
            "interval_seconds": 900,
        }
        legacy = {
            "loaded": True,
            "label": "com.workspace-daemon",
            "state": "not running",
            "runs": 1,
            "last_exit": 0,
            "interval_seconds": 900,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(current, legacy),
            ):
                text, healthy = status.render(Path(tmp), [], now=1100)

        self.assertFalse(healthy)
        self.assertIn("legacy com.workspace-daemon is also loaded", text)


class StatusWrapperTest(unittest.TestCase):
    def test_wrapper_is_executable_and_can_run_outside_the_repository(self):
        wrapper = Path(__file__).resolve().parents[1] / "memory-daemon-status.sh"
        self.assertTrue(os.access(wrapper, os.X_OK))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo with spaces"
            repo.mkdir()
            (repo / "daemon.py").write_text("# test target\n")
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\"\n"
            )
            fake_python.chmod(0o755)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            env = dict(os.environ)
            env["MEMORY_DAEMON_DIR"] = str(repo)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(
                [str(wrapper), "--label", "example.label"],
                cwd=elsewhere,
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                str(repo / "daemon.py"),
                "status",
                "--label",
                "example.label",
            ],
        )
