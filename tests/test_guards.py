"""Run guards: PAUSED kill switch, usage attribution, daily cost caps."""
import datetime
import io
import json
import os
import sys
import tempfile
import time
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
        self.assertEqual(log.call_count, 1)
        message = log.call_args.args[0]
        self.assertRegex(message, r"^tick\[[0-9a-f]{12}\]: paused: cost spike$")
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
            "analyze": llm.analyze, "read_thread": gmail.read_thread,
        }

        def restore():
            gmail.search = saved["search"]
            gmail.read_message = saved["read"]
            gmail.user_labels = saved["user_labels"]
            labels.gmail = saved["lab_gmail"]
            llm.analyze = saved["analyze"]
            gmail.read_thread = saved["read_thread"]

        self.addCleanup(restore)

        def search(query, max_results=20):
            self.searched.append(query)
            return [{"message_id": "m1", "thread_id": "m1", "subject": "S"}]

        def analyze(r, prompt):
            self.scopes.append(("analyze", claude_bridge.current_tag()))
            return "a summary"

        gmail.search = search
        gmail.read_message = lambda mid: {
            "headers": {"subject": "S", "from": "Sender",
                        "date": "Sun, 19 Jul 2026 15:46:29 +0000"},
            "body": "body",
        }
        gmail.read_thread = lambda tid: {"messages": [{
            "id": tid, "labels": ["INBOX"], "body": "body",
            "headers": {"subject": "S", "from": "Sender",
                        "date": "Sun, 19 Jul 2026 15:46:29 +0000"},
        }]}
        gmail.user_labels = lambda: []
        labels.gmail = gmail
        llm.analyze = analyze

    def ledger(self):
        path = self.base / "state" / "processed.json"
        return json.loads(path.read_text()) if path.exists() else {}


class UsageScopeTest(StubbedRunnerBase):
    def test_runner_tags_analysis_with_the_routine(self):
        totals = runner.run(self.base, [routine(self.vault)])
        self.assertEqual(totals["processed"], 1)
        self.assertIn(("analyze", "guarded"), self.scopes)
        self.assertIsNone(claude_bridge.current_tag())

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
                claude_bridge.current_tag()),
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
        # 20:00Z is 09:00 on Mar 11 in Auckland (+13): local midnight is
        # 11:00Z on Mar 10, long after UTC midnight.
        write_usage(self.base, [
            usage_entry("2026-03-10T10:59:59Z", 1.0),  # local Mar 10, UTC Mar 10
            usage_entry("2026-03-10T11:00:00Z", 2.0),  # local Mar 11
        ])
        saved = os.environ.get("TZ")

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Pacific/Auckland"
        time.tzset()
        self.assertAlmostEqual(
            claude_bridge.spent_today(self.base, now=self.NOW), 2.0)
        self.assertAlmostEqual(claude_bridge.spent_today(
            self.base, tz=UTC, now=self.NOW), 3.0)

    def test_cost_estimate_counts_when_no_cost_was_reported(self):
        write_usage(self.base, [
            usage_entry("2026-03-10T19:00:00Z", None, cost_estimate_usd=0.25),
        ])
        self.assertAlmostEqual(
            claude_bridge.spent_today(self.base, tz=UTC, now=self.NOW), 0.25)

    def test_unreadable_log_fails_closed(self):
        (self.base / "state" / "claude-usage.jsonl").mkdir()
        with self.assertRaises(claude_bridge.BridgeError):
            claude_bridge.spent_today(self.base, now=self.NOW)
        (self.base / "state" / "budget.yaml").write_text("daily_usd: 5\n")
        self.assertIn("unreadable", claude_bridge.budget_exhausted(
            self.base, now=self.NOW))

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
    def setUp(self):
        super().setUp()
        self.analyzed = []
        inner = llm.analyze
        llm.analyze = lambda r, p: (self.analyzed.append(r["id"]), inner(r, p))[1]

    def forbid_spawn(self):
        """A regression must fail the test, never launch the real CLI."""
        for target, name in (
            (claude_bridge, "claude_bin"), (claude_bridge.subprocess, "Popen"),
        ):
            patcher = mock.patch.object(
                target, name, side_effect=AssertionError("claude was spawned"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def spend(self, cost, rid="guarded"):
        ts = datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        write_usage(self.base, [usage_entry(ts, cost, rid)])

    def bridge_routine(self, **extra):
        r = routine(self.vault, **extra)
        r["analyze"] = dict(r["analyze"], provider="claude", model="haiku")
        return r

    def run_with(self, routines=None, **budget):
        routines = routines or [routine(self.vault, budget=budget)]
        with mock.patch.object(runner, "log") as log:
            totals = runner.run(self.base, routines)
        return totals, [c.args[0] for c in log.call_args_list]

    def test_capped_routine_is_held_without_side_effects(self):
        self.spend(2.0)
        totals, logged = self.run_with(daily_usd=1.5)
        self.assertEqual(self.analyzed, [])
        self.assertEqual(self.ledger(), {})
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(totals["held_ids"], ["guarded"])
        self.assertTrue(any("budget exhausted" in m for m in logged))
        self.assertFalse(any("ERROR" in m or "FATAL" in m for m in logged))
        self.assertFalse((self.base / "state" / "cursors.json").exists())
        self.assertEqual(list(self.vault.glob("*.md")) if self.vault.exists()
                         else [], [])

    def test_fallback_cannot_take_a_held_routines_items(self):
        self.spend(2.0, rid="domain")
        domain = routine(self.vault, rid="domain", budget={"daily_usd": 1})
        fallback = routine(self.vault, rid="general",
                           routing={"fallback": True})
        totals, _ = self.run_with([domain, fallback])
        self.assertTrue(self.searched)  # listed, so ownership is known
        self.assertEqual(self.analyzed, [])
        self.assertEqual(self.ledger(), {})
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["held_ids"], ["domain"])
        # Once the domain routine is allowed again it, not the fallback, wins.
        domain["budget"]["daily_usd"] = 100
        totals, _ = self.run_with([domain, fallback])
        self.assertEqual(self.analyzed, ["domain"])
        self.assertEqual(self.ledger()["m1"]["rule_id"], "domain")

    def test_held_catch_up_routine_keeps_its_cursor(self):
        source = {
            "kind": "gmail", "query": "in:inbox", "max_results": 0,
            "catch_up": True, "catch_up_after": "2026-07-01T00:00:00Z",
        }
        r = routine(self.vault, budget={"daily_usd": 1})
        del r["source"]
        r["sources"] = [source]
        cursor_id = runner._catch_up_cursor_id(source)
        state.CursorStore(self.base).mark_successful_at(
            [("guarded", cursor_id, "gmail", "2026-07-20T00:00:00Z")])
        self.spend(2.0)
        self.run_with([r])
        self.assertEqual(
            state.CursorStore(self.base).checkpoint(
                "guarded", cursor_id, "gmail"),
            "2026-07-20T00:00:00Z",
        )
        self.assertEqual(self.ledger(), {})

    def test_hold_is_logged_once_per_day(self):
        self.spend(2.0)
        _, first = self.run_with(daily_usd=1.5)
        _, second = self.run_with(daily_usd=1.5)
        self.assertEqual(sum("budget exhausted" in m for m in first), 1)
        self.assertEqual(sum("budget exhausted" in m for m in second), 0)

    def test_guard_stop_during_memory_capture_is_held_not_failed(self):
        r = routine(self.vault, memory={"store": str(self.base / "store")})
        self.forbid_spawn()
        with mock.patch.object(
            runner.memory_sink, "capture",
            side_effect=claude_bridge.BudgetExhausted("routine cap $1 reached"),
        ):
            with mock.patch.object(runner, "log"):
                totals = runner.run(self.base, [r])
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(totals["processed"], 0)
        record = self.ledger()["m1"]
        self.assertTrue(record["memory_held"])
        self.assertNotIn("memory_error", record)
        # The next run retries it.
        with mock.patch.object(
            runner.memory_sink, "capture", return_value={"memory": "ok"}
        ), mock.patch.object(runner, "log"):
            totals = runner.run(self.base, [r])
        self.assertEqual(totals["processed"], 1)
        self.assertNotIn("memory_held", self.ledger()["m1"])

    def test_routine_under_its_cap_runs(self):
        self.spend(1.0)
        totals, logged = self.run_with(daily_usd=1.5)
        self.assertEqual(totals["processed"], 1)
        self.assertNotIn("held_ids", totals)
        self.assertFalse(any("budget exhausted" in m for m in logged))

    def test_other_routines_spend_does_not_block(self):
        self.spend(9.0, rid="someone-else")
        totals, _ = self.run_with(daily_usd=1.5)
        self.assertEqual(totals["processed"], 1)

    def test_held_routine_catches_up_once_the_cap_lifts(self):
        self.spend(2.0)
        self.run_with(daily_usd=1.5)
        totals, _ = self.run_with(daily_usd=10)
        self.assertEqual(totals["processed"], 1)

    def test_global_cap_holds_every_routine_quietly(self):
        (self.base / "state" / "budget.yaml").write_text(
            "daily_usd: 1\nday_timezone: UTC\n")
        self.spend(5.0, rid="someone-else")
        totals, logged = self.run_with([self.bridge_routine()])
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(totals["held_ids"], ["guarded"])
        self.assertFalse(any("ERROR" in m or "FATAL" in m for m in logged))

    def test_global_cap_leaves_routines_that_never_call_claude_alone(self):
        (self.base / "state" / "budget.yaml").write_text(
            "daily_usd: 1\nday_timezone: UTC\n")
        self.spend(5.0, rid="someone-else")
        totals, _ = self.run_with([routine(self.vault)])  # gemini provider
        self.assertEqual(totals["processed"], 1)
        self.assertNotIn("held_ids", totals)

    def test_own_cap_still_holds_a_routine_that_never_calls_claude(self):
        (self.base / "state" / "budget.yaml").write_text("daily_usd: 100\n")
        self.spend(2.0)
        totals, _ = self.run_with(daily_usd=1.5)
        self.assertEqual(totals["held_ids"], ["guarded"])

    def test_malformed_budget_file_holds_instead_of_erroring(self):
        (self.base / "state" / "budget.yaml").write_text("daily_usd: nope\n")
        totals, logged = self.run_with([self.bridge_routine()])
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(totals["held_ids"], ["guarded"])
        totals, _ = self.run_with([routine(self.vault)])  # no bridge: runs
        self.assertEqual(totals["processed"], 1)

    def test_cap_reached_mid_run_stops_the_routine_without_errors(self):
        self.spend(0.9)
        gmail.search = lambda q, m=20: [
            {"message_id": f"m{i}", "thread_id": f"m{i}", "subject": "S"}
            for i in (1, 2)
        ]

        def analyze(r, prompt):
            self.analyzed.append(r["id"])
            # This item's call lands the routine over its cap...
            ts = datetime.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(self.base / "state" / "claude-usage.jsonl", "a") as f:
                f.write(json.dumps(usage_entry(ts, 0.3, "guarded")) + "\n")
            # ...so the next bridge call is refused.
            return claude_bridge.complete(
                prompt, model="haiku", base_dir=self.base)

        llm.analyze = analyze
        self.forbid_spawn()
        totals, logged = self.run_with(daily_usd=1.0)
        self.assertEqual(self.analyzed, ["guarded"])  # item 2 never started
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(totals["skipped"], 2)
        self.assertEqual(self.ledger(), {})
        self.assertEqual(totals["held_ids"], ["guarded"])
        self.assertFalse(any("ERROR" in m or "FATAL" in m for m in logged))

    def test_pause_mid_run_stops_spending(self):
        gmail.search = lambda q, m=20: [
            {"message_id": f"m{i}", "thread_id": f"m{i}", "subject": "S"}
            for i in (1, 2)
        ]

        def analyze(r, prompt):
            self.analyzed.append(r["id"])
            (self.base / "state" / "PAUSED").write_text("flipped\n")
            return claude_bridge.complete(
                prompt, model="haiku", base_dir=self.base)

        llm.analyze = analyze
        self.forbid_spawn()
        with mock.patch.object(state, "paused", side_effect=[None, "flipped"]):
            totals, _ = self.run_with([routine(self.vault)])
        self.assertEqual(totals["errors"], 0)
        self.assertEqual(self.analyzed, ["guarded"])
        self.assertEqual(self.ledger(), {})

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
            totals, _ = self.run_with([census])
        run.assert_not_called()
        self.assertEqual(totals["held_ids"], ["sweeper"])


class TickHeldAndMidRunPauseTest(TempBase):
    def setUp(self):
        super().setUp()
        self.routine = routine(self.vault, rid="domain", schedule={"every": "1h"})
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
        self.args = SimpleNamespace(
            dry_run=False, refresh_labels=False, group="all")

    def test_held_routines_are_not_marked_attempted(self):
        totals = dict(EMPTY_TOTALS, held_ids=["domain"])
        with mock.patch.object(daemon_cli.runner, "run", return_value=totals), \
             mock.patch.object(daemon_cli, "log"):
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)
        self.assertNotIn("domain", state.ScheduleStore(self.base).entries)

    def test_pause_appearing_mid_tick_stops_but_closes_the_tick(self):
        with mock.patch.object(
                daemon_cli.runner, "run",
                side_effect=state.Paused("paused: late")), \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_tick(self.args), 0)
        messages = [c.args[0] for c in log.call_args_list]
        self.assertRegex(messages[-1], r"^tick\[[0-9a-f]+\] done: .* \(paused\)$")
        self.assertTrue(any("stopped early" in m for m in messages))
        self.assertNotIn("domain", state.ScheduleStore(self.base).entries)

    def test_pause_appearing_mid_run_is_a_clean_stop(self):
        args = SimpleNamespace(
            dry_run=False, refresh_labels=False, routine=None,
            include_disabled=False, ignore_pause=False)
        with mock.patch.object(
                daemon_cli.runner, "run",
                side_effect=state.Paused("paused: late")), \
             mock.patch.object(daemon_cli, "log") as log:
            self.assertEqual(daemon_cli.cmd_run(args), 0)
        self.assertIn(mock.call("run stopped: paused: late"),
                      log.call_args_list)


class UsesClaudeBridgeTest(unittest.TestCase):
    def test_detection(self):
        uses = config.uses_claude_bridge
        self.assertFalse(uses(routine("/v")))
        claude = routine("/v")
        claude["analyze"]["provider"] = "claude"
        self.assertTrue(uses(claude))
        handler = routine("/v", handlers={"h": {"analyze": {"provider": "claude"}}})
        self.assertTrue(uses(handler))
        identity = routine("/v", memory={"identity": {"provider": "slack_mcp"}})
        self.assertTrue(uses(identity))
        self.assertTrue(uses({"id": "c", "maintenance": {"kind": "calendar_cache"}}))
        self.assertFalse(uses({"id": "c", "maintenance": {
            "kind": "slack_conversation_census"}}))
        mcp = routine("/v")
        mcp["source"] = {"kind": "slack_mcp"}
        self.assertTrue(uses(mcp))


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
