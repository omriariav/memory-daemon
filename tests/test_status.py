"""Health-status command regression tests."""
import datetime
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from workspace_daemon import config, state, status


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
            {
                "id": "alpha", "role": "specialized",
                "enabled": True, "schedule": {"every": "1h"},
            },
            {
                "id": "beta", "role": "specialized",
                "enabled": True, "schedule": {"every": "4h"},
            },
            {
                "id": "off", "role": "specialized",
                "enabled": False, "schedule": {"every": "1d"},
            },
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
        self.assertEqual(by_id["beta"]["status"], "waiting")
        self.assertEqual(by_id["beta"]["armed"], "yes")
        self.assertEqual(by_id["off"]["status"], "disabled")
        self.assertEqual(by_id["off"]["armed"], "no")
        self.assertEqual(by_id["alpha"]["role"], "specialized")
        self.assertEqual(by_id["alpha"]["sources"], "-")

    def test_reports_transcriptions_that_need_manual_calendar_matching(self):
        state.save(self.base, {
            "mila:ID@hash": {
                "rule_id": "meeting-transcriptions",
                "processed_at": "2026-07-30T10:00:00Z",
                "calendar_match_rejected": True,
            },
        })
        routines = [{
            "id": "meeting-transcriptions",
            "enabled": True,
            "schedule": {"every": "1h"},
            "source": {
                "kind": "mila",
                "recordings_file": "/tmp/recordings.json",
                "max_results": 0,
            },
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "summarize",
            },
            "memory": {"store": "/tmp/store", "type": "note"},
        }]

        rows = status.routine_rows(
            self.base, routines, {"routines": {}}, now=5000
        )

        self.assertEqual(rows[0]["status"], "attention")
        self.assertEqual(rows[0]["issues"], "1 meeting match")

    def test_reports_general_domain_and_source_roles(self):
        routines = [
            {
                "id": "general",
                "role": "general",
                "source": {"kind": "gchat", "all_spaces": True},
                "analyze": {"connector_sweep": True},
            },
            {
                "id": "domain",
                "role": "domain",
                "sources": [
                    {"kind": "gmail"},
                    {"kind": "gchat"},
                ],
            },
            {
                "id": "partial",
                "role": "partial",
                "source": {
                    "kind": "slack",
                    "direct_channels": ["C1"],
                },
                "analyze": {
                    "connector_sweep": True,
                    "instruction_from_connector": "slack",
                },
            },
        ]

        rows = status.routine_rows(
            self.base, routines, {"routines": {}}, now=5000
        )
        by_id = {row["routine"]: row for row in rows}

        self.assertEqual(by_id["general"]["role"], "general")
        self.assertEqual(by_id["general"]["sources"], "gchat")
        self.assertEqual(by_id["domain"]["role"], "domain")
        self.assertEqual(by_id["domain"]["sources"], "gmail+gchat")
        self.assertEqual(by_id["partial"]["role"], "partial")
        self.assertEqual(by_id["partial"]["sources"], "slack")

    def test_reports_work_hours_cadence_and_next_transition(self):
        routine = {
            "id": "gchat",
            "enabled": True,
            "role": "general",
            "schedule": {
                "every": "1h",
                "work_hours": {
                    "every": "15m",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "start": "08:00",
                    "end": "20:00",
                    "timezone": "Europe/London",
                },
            },
            "source": {"kind": "gchat", "all_spaces": True},
        }
        now = datetime.datetime(
            2026, 8, 3, 7, 55, tzinfo=ZoneInfo("Europe/London")
        ).timestamp()
        last = datetime.datetime(
            2026, 8, 3, 7, 30, tzinfo=ZoneInfo("Europe/London")
        ).timestamp()
        schedule_path = state.schedule_file(self.base)
        schedule_path.write_text(json.dumps({
            "gchat": {
                "last_attempted_epoch": last,
                "last_attempted_at": "2026-08-02T04:30:00Z",
            },
        }))

        row = status.routine_rows(
            self.base, [routine], {"routines": {}}, now=now
        )[0]

        self.assertEqual(row["every"], "15m work / 1h off")
        self.assertEqual(row["next"], "in 5m")

    def test_role_is_explicit_not_inferred_from_source_count(self):
        routines = [
            {
                "id": "single-domain",
                "role": "domain",
                "source": {"kind": "gmail"},
            },
            {
                "id": "multi-utility",
                "role": "specialized",
                "sources": [{"kind": "gmail"}, {"kind": "gchat"}],
            },
            {
                "id": "legacy",
                "source": {"kind": "gchat", "all_spaces": True},
                "analyze": {"connector_sweep": True},
            },
            {
                "id": "invalid",
                "role": ["domain"],
                "source": {"kind": "gmail"},
            },
        ]
        rows = status.routine_rows(
            self.base, routines, {"routines": {}}, now=5000
        )
        by_id = {row["routine"]: row for row in rows}

        self.assertEqual(by_id["single-domain"]["role"], "domain")
        self.assertEqual(by_id["multi-utility"]["role"], "specialized")
        self.assertEqual(by_id["legacy"]["role"], "-")
        self.assertEqual(by_id["invalid"]["role"], "-")

    def test_invalid_declared_role_is_rejected(self):
        for invalid in ("guessed", ["domain"]):
            with self.subTest(role=invalid):
                problems = config.validate({
                    "id": "bad-role",
                    "role": invalid,
                    "source": {"kind": "gmail", "query": "in:inbox"},
                    "analyze": {
                        "provider": "gemini",
                        "instruction": "Summarize.",
                    },
                })

                self.assertTrue(
                    any("role must be one of" in item for item in problems)
                )

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
        self.assertIn("Scheduler: armed · idle", text)
        self.assertIn("Next coordinator run: within 15m", text)
        self.assertIn("ROUTINE", text)
        self.assertIn("ROLE", text)
        self.assertIn("SOURCES", text)
        self.assertIn("ARMED", text)
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
        self.assertIn(
            "Next coordinator run: legacy scheduler: within 15m",
            text,
        )

    def test_running_scheduler_reports_next_eligible_interval(self):
        launchd = {
            "loaded": True,
            "label": "com.memory-daemon",
            "state": "running",
            "pid": 4321,
            "runs": 1,
            "last_exit": None,
            "interval_seconds": 900,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(launchd),
            ):
                text, _ = status.render(Path(tmp), [], now=1100)

        self.assertIn(
            "Next coordinator run: after current tick "
            "(then within 15m)",
            text,
        )

    def test_arbitrary_interval_bound_is_never_rounded_down(self):
        for state_name, expected in (
            ("not running", "Next coordinator run: within 1m1s"),
            (
                "running",
                "Next coordinator run: after current tick (then within 1m1s)",
            ),
        ):
            with self.subTest(state=state_name), tempfile.TemporaryDirectory() as tmp:
                launchd = {
                    "loaded": True,
                    "label": "com.memory-daemon",
                    "state": state_name,
                    "pid": 4321 if state_name == "running" else None,
                    "runs": 1,
                    "last_exit": None,
                    "interval_seconds": 61,
                }
                with mock.patch.object(
                    status,
                    "probe_launchd",
                    side_effect=self.probe(launchd),
                ):
                    text, _ = status.render(Path(tmp), [], now=1100)

            self.assertIn(expected, text)

    def test_loaded_scheduler_without_interval_reports_unknown_schedule(self):
        launchd = {
            "loaded": True,
            "label": "com.memory-daemon",
            "state": "not running",
            "pid": None,
            "runs": 0,
            "last_exit": None,
            "interval_seconds": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(launchd),
            ):
                text, _ = status.render(Path(tmp), [], now=1100)

        self.assertIn("Next coordinator run: schedule unavailable", text)

    def test_running_scheduler_without_interval_limits_claim_to_current_tick(self):
        launchd = {
            "loaded": True,
            "label": "com.memory-daemon",
            "state": "running",
            "pid": 4321,
            "runs": 1,
            "last_exit": None,
            "interval_seconds": None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                status,
                "probe_launchd",
                side_effect=self.probe(launchd),
            ):
                text, _ = status.render(Path(tmp), [], now=1100)

        self.assertIn(
            "Next coordinator run: current tick running; "
            "future schedule unavailable",
            text,
        )

    def test_launchctl_probe_failure_does_not_claim_job_is_unscheduled(self):
        for detail in ("launchctl unavailable", "launchctl timed out"):
            with self.subTest(detail=detail), tempfile.TemporaryDirectory() as tmp:
                unavailable = {
                    "loaded": False,
                    "label": "com.memory-daemon",
                    "detail": detail,
                }
                with mock.patch.object(
                    status,
                    "probe_launchd",
                    side_effect=[unavailable, unavailable],
                ):
                    text, _ = status.render(Path(tmp), [], now=1100)

            self.assertIn("Next coordinator run: schedule unavailable", text)

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
        self.assertIn(
            "Next coordinator run: multiple schedulers loaded; "
            "schedule ambiguous",
            text,
        )


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
