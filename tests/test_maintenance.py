"""Scheduled source maintenance behavior."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import daemon as daemon_cli
from workspace_daemon import config, maintenance, runner


def census_routine():
    return {
        "id": "slack-census",
        "enabled": True,
        "description": "Refresh active conversation metadata.",
        "role": "maintenance",
        "schedule": {"every": "1d"},
        "maintenance": {
            "kind": "slack_conversation_census",
            "checkpoint": "state/slack-census.json",
            "hours": 48,
            "requests_per_minute": 40,
        },
    }


def tasks_routine():
    return {
        "id": "google-tasks-sync",
        "enabled": True,
        "description": "Sync operational tasks.",
        "role": "maintenance",
        "schedule": {
            "every": "1d",
            "work_hours": {
                "every": "1h",
                "days": ["sun", "mon", "tue", "wed", "thu"],
                "start": "08:00",
                "end": "20:00",
                "timezone": "Asia/Jerusalem",
            },
        },
        "maintenance": {
            "kind": "google_tasks_sync",
            "checkpoint": "state/google-tasks-sync.json",
            "store": "/tmp/personal-memory",
            "tasklists": "all",
            "outbound_tasklist": "incoming-list-id",
            "outbound_since": "2026-08-02",
            "exclude_tags": ["no-google-tasks"],
            "max_tasks": 10000,
        },
    }


class MaintenanceValidationTest(unittest.TestCase):
    def test_valid_census_routine_needs_no_prompt_or_sink(self):
        self.assertEqual(config.validate(census_routine()), [])

    def test_maintenance_rejects_capture_fields_and_invalid_rate(self):
        routine = census_routine()
        routine["source"] = {"kind": "slack"}
        routine["maintenance"]["requests_per_minute"] = 51
        problems = config.validate(routine)
        self.assertTrue(
            any("cannot set source" in problem for problem in problems),
            problems,
        )
        self.assertTrue(
            any("integer from 1 to 50" in problem for problem in problems),
            problems,
        )

    def test_capture_routine_cannot_claim_maintenance_role(self):
        routine = {
            "id": "capture",
            "role": "maintenance",
            "source": {"kind": "gmail", "query": "is:unread"},
            "analyze": {
                "provider": "gemini",
                "model": "m",
                "instruction": "Keep durable decisions.",
            },
            "output": {
                "vault_dir": "/tmp/vault",
                "slug_prefix": "capture",
            },
        }
        self.assertTrue(
            any(
                "requires a `maintenance` block" in problem
                for problem in config.validate(routine)
            )
        )

    def test_valid_google_tasks_sync_schedule_and_config(self):
        self.assertEqual(config.validate(tasks_routine()), [])

    def test_google_tasks_sync_rejects_relative_store_and_bad_cutover(self):
        routine = tasks_routine()
        routine["maintenance"]["store"] = "relative/store"
        routine["maintenance"]["outbound_since"] = "today"
        routine["maintenance"]["tasklists"] = ["another-list"]
        problems = config.validate(routine)

        self.assertTrue(any("absolute path" in problem for problem in problems))
        self.assertTrue(any("YYYY-MM-DD" in problem for problem in problems))
        self.assertTrue(any("must be included" in problem for problem in problems))


class MaintenanceRunTest(unittest.TestCase):
    REPORT = {
        "ok": True,
        "considered": 120,
        "active_count": 12,
        "error_count": 1,
        "fatal_error_count": 0,
        "errors": [{"id": "DSTALE", "error": "channel_not_found"}],
    }

    def test_dry_run_reads_but_passes_no_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            maintenance.slack_cli, "run_census", return_value=self.REPORT
        ) as census:
            result = maintenance.run(
                Path(tmp), census_routine(), dry_run=True
            )

        self.assertEqual(result["active_count"], 12)
        self.assertIsNone(census.call_args.kwargs["checkpoint"])

    def test_list_uses_schedule_attempt_for_maintenance_last_run(self):
        schedule = SimpleNamespace(entries={
            "slack-census": {
                "last_attempted_at": "2026-07-31T08:00:00Z",
            },
        })
        self.assertEqual(
            daemon_cli._routine_last_run(census_routine(), schedule),
            "2026-07-31T08:00:00Z",
        )

    def test_google_tasks_sync_dry_run_reads_the_real_checkpoint(self):
        report = {
            "ok": True,
            "tasklist_count": 2,
            "open_google_tasks": 3,
            "open_memory_todos": 4,
            "planned": [],
            "conflicts": 0,
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            maintenance.google_tasks_sync, "run", return_value=report
        ) as sync:
            result = maintenance.run(
                Path(tmp), tasks_routine(), dry_run=True
            )

        self.assertEqual(result, report)
        self.assertEqual(
            sync.call_args.kwargs["checkpoint_path"],
            Path(tmp) / "state" / "google-tasks-sync.json",
        )
        self.assertTrue(sync.call_args.kwargs["dry_run"])

    def test_maintenance_runs_before_capture_sources_in_same_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            order = []
            capture = {
                "id": "capture",
                "enabled": True,
                "source": {
                    "kind": "gmail",
                    "query": "is:unread",
                    "max_results": 0,
                },
                "analyze": {
                    "provider": "gemini",
                    "model": "m",
                    "instruction": "Keep durable decisions.",
                },
                "output": {
                    "vault_dir": str(base / "vault"),
                    "slug_prefix": "capture",
                },
            }
            saved = runner.SOURCES["gmail"]
            runner.SOURCES["gmail"] = (
                lambda _source: order.append("capture") or [],
                saved[1],
            )
            self.addCleanup(runner.SOURCES.__setitem__, "gmail", saved)

            with mock.patch.object(
                runner.maintenance,
                "run",
                side_effect=lambda *_args, **_kwargs: (
                    order.append("maintenance") or self.REPORT
                ),
            ):
                totals = runner.run(
                    base, [capture, census_routine()], dry_run=True
                )

        self.assertEqual(totals["errors"], 0)
        self.assertEqual(order, ["maintenance", "capture"])
