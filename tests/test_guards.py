"""Run guards: PAUSED kill switch, usage attribution, daily cost caps."""
import datetime
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daemon as daemon_cli  # noqa: E402
from workspace_daemon import (  # noqa: E402
    claude_bridge, config, gmail, labels, llm, runner, state,
)

TOKYO = ZoneInfo("Asia/Tokyo")  # fixed +09:00, no DST
UTC = datetime.timezone.utc
EMPTY_TOTALS = {
    "processed": 0, "skipped": 0, "errors": 0, "matched": 0,
    "fallbacks": 0, "pending_actions": 0, "ambiguous": 0,
}


def epoch(text):
    return datetime.datetime.strptime(
        text, "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC).timestamp()


def usage_entry(ts, cost, routine=None, **extra):
    entry = {"ts": ts, "model": "m", "tools": [], "attempt": 1,
             "outcome": "ok", "total_cost_usd": cost}
    if routine:
        entry["routine"] = routine
    entry.update(extra)
    return entry


def write_usage(base, entries):
    path = Path(base) / "state" / "claude-usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n")
    return path


def routine(vault, rid="guarded", **extra):
    r = {
        "id": rid, "enabled": True,
        "source": {"kind": "gmail", "query": "in:inbox", "max_results": 5},
        "analyze": {"provider": "gemini", "model": "m", "instruction": "go"},
        "output": {"vault_dir": str(vault), "slug_prefix": "note"},
        "actions": [],
    }
    r.update(extra)
    return r


class TempBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base / "state").mkdir()
        self.vault = self.base / "vault"


class PausedStateTest(TempBase):
    def pause(self, text):
        (self.base / "state" / "PAUSED").write_text(text)

    def test_absent_is_none(self):
        self.assertIsNone(state.paused(self.base))

    def test_first_line_is_the_reason(self):
        self.pause("maintenance window\nsecond line\n")
        self.assertEqual(state.paused(self.base), "maintenance window")

    def test_empty_file_still_pauses(self):
        self.pause("")
        self.assertEqual(state.paused(self.base), "paused")
        self.pause("\n\nlater text")
        self.assertEqual(state.paused(self.base), "paused")


class PausedCliTest(TempBase):
    def setUp(self):
        super().setUp()
        (self.base / "state" / "PAUSED").write_text("cost spike\n")
        self.routine = routine(self.vault)
        patches = [
            mock.patch.object(daemon_cli, "BASE_DIR", self.base),
            mock.patch.object(daemon_cli, "set_log_file"),
            mock.patch.object(daemon_cli.config, "discover",
                              return_value=[self.routine]),
            mock.patch.object(daemon_cli.config, "secure_routine_files"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tick_args(self):
        return SimpleNamespace(
            dry_run=False, refresh_labels=False, group="all",
        )

    def run_args(self, **kw):
        values = dict(
            dry_run=False, refresh_labels=False, routine=None,
            include_disabled=False, ignore_pause=False,
        )
        values.update(kw)
        return SimpleNamespace(**values)

    def test_tick_logs_and_runs_nothing(self):
        with mock.patch.object(daemon_cli.runner, "run") as run, \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_tick(self.tick_args()), 0)
        run.assert_not_called()
        log.assert_called_once_with("paused: cost spike")
        self.assertFalse((self.base / "state" / "schedule.json").exists())

    def test_run_logs_and_runs_nothing(self):
        with mock.patch.object(daemon_cli.runner, "run") as run, \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_run(self.run_args()), 0)
        run.assert_not_called()
        log.assert_called_once_with("paused: cost spike")

    def test_ignore_pause_lets_a_manual_run_proceed(self):
        with mock.patch.object(
                daemon_cli.runner, "run", return_value=dict(EMPTY_TOTALS)
             ) as run, \
             mock.patch.object(daemon_cli, "log") as log:
            code = daemon_cli.cmd_run(self.run_args(ignore_pause=True))
        self.assertEqual(code, 0)
        self.assertTrue(run.called)
        self.assertTrue(all(c.kwargs["ignore_pause"] for c in run.call_args_list))
        self.assertNotIn(mock.call("paused: cost spike"), log.call_args_list)

    def test_flag_is_parsed_for_run_only(self):
        with mock.patch.object(daemon_cli, "cmd_run", return_value=0) as cmd:
            daemon_cli.main(["run", "--ignore-pause"])
        self.assertTrue(cmd.call_args.args[0].ignore_pause)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            daemon_cli.main(["tick", "--ignore-pause"])


class RunnerPauseBackstopTest(TempBase):
    def test_runner_refuses_while_paused(self):
        (self.base / "state" / "PAUSED").write_text("stop\n")
        with self.assertRaises(state.Paused) as caught:
            runner.run(self.base, [routine(self.vault)], dry_run=True)
        self.assertIn("paused: stop", str(caught.exception))

    def test_ignore_pause_runs(self):
        (self.base / "state" / "PAUSED").write_text("stop\n")
        with mock.patch.object(runner, "_run_locked",
                               return_value=dict(EMPTY_TOTALS)) as locked:
            runner.run(self.base, [], dry_run=True, ignore_pause=True)
        self.assertTrue(locked.called)


class StubbedRunnerBase(TempBase):
    """Drives runner.run with Gmail and the LLM stubbed out."""

    def setUp(self):
        super().setUp()
        self.searched = []
        self.scopes = []
        saved = {
            "search": gmail.search, "read": gmail.read_message,
            "user_labels": gmail.user_labels, "lab_gmail": labels.gmail,
            "analyze": llm.analyze,
        }

        def restore():
            gmail.search = saved["search"]
            gmail.read_message = saved["read"]
            gmail.user_labels = saved["user_labels"]
            labels.gmail = saved["lab_gmail"]
            llm.analyze = saved["analyze"]

        self.addCleanup(restore)

        def search(query, max_results=20):
            self.searched.append(query)
            self.scopes.append(("search", claude_bridge._USAGE_SCOPE.get()))
            return [{"message_id": "m1", "thread_id": "m1", "subject": "S"}]

        def analyze(r, prompt):
            self.scopes.append(("analyze", claude_bridge._USAGE_SCOPE.get()))
            return "a summary"

        gmail.search = search
        gmail.read_message = lambda mid: {
            "headers": {"subject": "S", "from": "Sender",
                        "date": "Sun, 19 Jul 2026 15:46:29 +0000"},
            "body": "body",
        }
        gmail.user_labels = lambda: []
        labels.gmail = gmail
        llm.analyze = analyze

    def ledger(self):
        path = self.base / "state" / "processed.json"
        return json.loads(path.read_text()) if path.exists() else {}


class UsageScopeTest(StubbedRunnerBase):
    def test_runner_tags_listing_and_analysis_with_the_routine(self):
        totals = runner.run(self.base, [routine(self.vault)])
        self.assertEqual(totals["processed"], 1)
        self.assertIn(("search", "guarded"), self.scopes)
        self.assertIn(("analyze", "guarded"), self.scopes)
        self.assertIsNone(claude_bridge._USAGE_SCOPE.get())

    def test_maintenance_run_is_tagged(self):
        seen = []
        census = {
            "id": "sweeper", "enabled": True, "role": "maintenance",
            "schedule": {"every": "1d"},
            "maintenance": {
                "kind": "slack_conversation_census",
                "checkpoint": "state/slack-census.json",
            },
        }
        with mock.patch.object(
            runner.maintenance, "run",
            side_effect=lambda *a, **k: seen.append(
                claude_bridge._USAGE_SCOPE.get()),
        ):
            runner.run(self.base, [census], dry_run=True)
        self.assertEqual(seen, ["sweeper"])


class SpentTodayTest(TempBase):
    NOW = epoch("2026-03-10T20:00:00Z")  # 05:00 on Mar 11 in Tokyo

    def test_sums_costs_and_filters_by_routine(self):
        write_usage(self.base, [
            usage_entry("2026-03-10T19:00:00Z", 0.5, "alpha"),
            usage_entry("2026-03-10T19:30:00Z", 0.25, "beta"),
            usage_entry("2026-03-10T19:45:00Z", 0.125),
            usage_entry("2026-03-10T19:50:00Z", None, "alpha"),
        ])
        spent = lambda **kw: claude_bridge.spent_today(
            self.base, tz=UTC, now=self.NOW, **kw)
        self.assertAlmostEqual(spent(), 0.875)
        self.assertAlmostEqual(spent(routine="alpha"), 0.5)
        self.assertAlmostEqual(spent(routine="beta"), 0.25)

    def test_failed_attempts_count(self):
        write_usage(self.base, [
            usage_entry("2026-03-10T19:00:00Z", 0.4,
                        outcome="error: claude run failed: result subtype x"),
        ])
        self.assertAlmostEqual(
            claude_bridge.spent_today(self.base, tz=UTC, now=self.NOW), 0.4)

    def test_day_boundary_follows_the_given_timezone(self):
        write_usage(self.base, [
            usage_entry("2026-03-10T14:59:59Z", 1.0),  # Mar 10 23:59:59 Tokyo
            usage_entry("2026-03-10T15:00:00Z", 2.0),  # Mar 11 00:00:00 Tokyo
        ])
        self.assertAlmostEqual(claude_bridge.spent_today(
            self.base, tz=TOKYO, now=self.NOW), 2.0)
        self.assertAlmostEqual(claude_bridge.spent_today(
            self.base, tz=UTC, now=self.NOW), 3.0)

    def test_default_timezone_is_local_time(self):
        now = 1_800_000_900.0
        ts = datetime.datetime.fromtimestamp(now - 5, UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        write_usage(self.base, [usage_entry(ts, 0.3)])
        self.assertAlmostEqual(
            claude_bridge.spent_today(self.base, now=now), 0.3)

    def test_missing_or_garbled_log_is_zero_spend(self):
        self.assertEqual(claude_bridge.spent_today(self.base, now=self.NOW), 0)
        path = write_usage(self.base, [usage_entry("2026-03-10T19:00:00Z", 1)])
        path.write_text("not json\n[1]\n" + path.read_text() + '{"ts": 5}\n')
        self.assertAlmostEqual(
            claude_bridge.spent_today(self.base, tz=UTC, now=self.NOW), 1)

    def test_only_the_recent_tail_is_read(self):
        old = [usage_entry("2026-01-01T00:00:00Z", 100.0, "x" * 50)
               for _ in range(3000)]  # well over one 64 KiB block
        path = write_usage(self.base, old)
        with open(path, "a") as handle:
            handle.write(json.dumps(
                usage_entry("2026-03-10T19:00:00Z", 0.5)) + "\n")
        self.assertGreater(path.stat().st_size, 65536 * 2)
        reads = []
        real_open = open

        def spy(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            if str(file) == str(path):
                inner = handle.read
                handle.read = lambda n=-1: (
                    reads.append(n), inner(n))[1]
            return handle

        with mock.patch("builtins.open", spy):
            spent = claude_bridge.spent_today(
                self.base, tz=UTC, now=self.NOW)
        self.assertAlmostEqual(spent, 0.5)
        self.assertLessEqual(len(reads), 2)
        self.assertTrue(all(n <= 65536 for n in reads))

    def test_window_never_exceeds_seven_days(self):
        write_usage(self.base, [
            usage_entry("2026-03-01T12:00:00Z", 9.0),
            usage_entry("2026-03-09T12:00:00Z", 1.0),
        ])
        found = claude_bridge.recent_usage(self.base, now=self.NOW)
        self.assertEqual([e["total_cost_usd"] for _, e in found], [1.0])


class BudgetFileTest(TempBase):
    def write(self, text):
        (self.base / "state" / "budget.yaml").write_text(text)

    def test_missing_file_means_no_cap(self):
        self.assertEqual(claude_bridge.load_budget(self.base), (None, None))
        self.assertIsNone(claude_bridge.budget_exhausted(self.base))

    def test_reads_cap_and_timezone(self):
        self.write("daily_usd: 5\nday_timezone: Asia/Tokyo\n")
        cap, tz = claude_bridge.load_budget(self.base)
        self.assertEqual(cap, 5)
        self.assertEqual(tz, TOKYO)

    def test_bad_files_are_rejected_not_ignored(self):
        for text in (
            "daily_usd: 0\n", "daily_usd: -1\n", "daily_usd: true\n",
            "daily_usd: lots\n", "daily_usd: 1\nextra: 1\n", "- 1\n",
            "daily_usd: 1\nday_timezone: Mars/Base\n", "a: [unclosed\n",
        ):
            self.write(text)
            with self.assertRaises(claude_bridge.BridgeError, msg=text):
                claude_bridge.load_budget(self.base)

    def test_bad_file_fails_closed(self):
        self.write("daily_usd: -1\n")
        self.assertIn("daily_usd",
                      claude_bridge.budget_exhausted(self.base))

    def test_global_cap(self):
        now = epoch("2026-03-10T20:00:00Z")
        self.write("daily_usd: 1.0\nday_timezone: UTC\n")
        write_usage(self.base, [usage_entry("2026-03-10T10:00:00Z", 0.6)])
        self.assertIsNone(claude_bridge.budget_exhausted(self.base, now=now))
        write_usage(self.base, [
            usage_entry("2026-03-10T10:00:00Z", 0.6),
            usage_entry("2026-03-10T11:00:00Z", 0.4, "other"),
        ])
        self.assertIn("global cap", claude_bridge.budget_exhausted(
            self.base, now=now))

    def test_routine_cap_counts_only_that_routine(self):
        now = epoch("2026-03-10T20:00:00Z")
        write_usage(self.base, [
            usage_entry("2026-03-10T10:00:00Z", 0.6, "alpha"),
            usage_entry("2026-03-10T11:00:00Z", 5.0, "beta"),
        ])
        hit = lambda rid: claude_bridge.budget_exhausted(
            self.base, rid, routine_cap=0.5, now=now)
        self.assertIn("routine cap", hit("alpha"))
        self.assertIsNone(claude_bridge.budget_exhausted(
            self.base, "gamma", routine_cap=0.5, now=now))


class RoutineBudgetRunTest(StubbedRunnerBase):
    def spend(self, cost, rid="guarded"):
        ts = datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_usage(self.base, [usage_entry(ts, cost, rid)])

    def run_with(self, **budget):
        r = routine(self.vault, budget=budget)
        with mock.patch.object(runner, "log") as log:
            totals = runner.run(self.base, [r])
        return totals, [c.args[0] for c in log.call_args_list]

    def test_capped_routine_is_skipped_without_side_effects(self):
        self.spend(2.0)
        totals, logged = self.run_with(daily_usd=1.5)
        self.assertEqual(self.searched, [])
        self.assertEqual(self.ledger(), {})
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["errors"], 0)
        self.assertTrue(any("budget exhausted" in m for m in logged))
        self.assertFalse((self.base / "state" / "cursors.json").exists())

    def test_routine_under_its_cap_runs(self):
        self.spend(1.0)
        totals, logged = self.run_with(daily_usd=1.5)
        self.assertEqual(totals["processed"], 1)
        self.assertFalse(any("budget exhausted" in m for m in logged))

    def test_other_routines_spend_does_not_block(self):
        self.spend(9.0, rid="someone-else")
        totals, _ = self.run_with(daily_usd=1.5)
        self.assertEqual(totals["processed"], 1)

    def test_skipped_routine_catches_up_once_the_cap_lifts(self):
        self.spend(2.0)
        self.run_with(daily_usd=1.5)
        totals, _ = self.run_with(daily_usd=10)
        self.assertEqual(totals["processed"], 1)

    def test_capped_maintenance_routine_does_not_run(self):
        self.spend(2.0, rid="sweeper")
        census = {
            "id": "sweeper", "enabled": True, "role": "maintenance",
            "schedule": {"every": "1d"}, "budget": {"daily_usd": 1},
            "maintenance": {
                "kind": "slack_conversation_census",
                "checkpoint": "state/slack-census.json",
            },
        }
        with mock.patch.object(runner.maintenance, "run") as run:
            runner.run(self.base, [census])
        run.assert_not_called()


class BudgetKeyValidationTest(unittest.TestCase):
    def problems(self, budget):
        r = routine("/tmp/vault", budget=budget)
        return [p for p in config.validate(r) if "budget" in p]

    def test_valid_values(self):
        self.assertEqual(self.problems({"daily_usd": 2}), [])
        self.assertEqual(self.problems({"daily_usd": 0.5}), [])

    def test_key_is_allowed_at_top_level(self):
        self.assertIn("budget", config.TOP_LEVEL_KEYS)
        self.assertEqual(config.validate(routine("/tmp/vault", budget={
            "daily_usd": 1})), [])

    def test_rejects_bad_shapes(self):
        for budget in (
            {"daily_usd": 0}, {"daily_usd": -3}, {"daily_usd": "5"},
            {"daily_usd": True}, {"daily_usd": float("inf")},
            {"daily_usd": float("nan")}, {}, {"daily_usd": 1, "weekly": 2},
            5, "cheap", None, [],
        ):
            self.assertTrue(self.problems(budget), msg=repr(budget))


if __name__ == "__main__":
    unittest.main()
