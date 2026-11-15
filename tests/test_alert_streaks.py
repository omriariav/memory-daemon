"""Routine-health alerts: interrupted ticks, error streaks, awake time, retry."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_claude_bridge import (
    TOOL_A, BridgeBase, init, result, tool_result, tool_use,
)
from workspace_daemon import alerts, claude_bridge, events, runner, sleepclock, status

START = datetime.datetime(
    2026, 3, 10, 8, 0, tzinfo=datetime.timezone.utc).timestamp()
ROUTINE = {"id": "slack-direct"}
OUTAGE = f"tool returned an error: mcp__x__slack_search ({sleepclock.OUTAGE_NOTE})"
PLAIN_ERROR = "tool returned an error: mcp__x__slack_search"


def stamp(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LogBuilder:
    """Writes tick blocks for one routine into a run log."""

    def __init__(self, path):
        self.path, self.t, self.n, self.lines = path, START, 0, []

    def tick(self, *errors, after=300, held=()):
        """One tick after `after` seconds; each error is a FATAL line text."""
        self.t += after
        self.n += 1
        tid = f"t{self.n}"
        self.lines.append(f"{stamp(self.t)} tick[{tid}](capture): due=slack-direct")
        for text in errors:
            self.lines.append(
                f"{stamp(self.t)} routine=slack-direct source=slack_mcp "
                f"FATAL: {text}")
        self.lines.append(
            f"{stamp(self.t + 1)} tick[{tid}](capture) done: 0 processed, "
            f"0 already-seen, {len(errors)} error(s)"
            + (f" held={','.join(held)}" if held else ""))
        self.flush()

    def preflight_skips(self, count, gap, why="offline"):
        for _ in range(count):
            self.t += gap
            self.n += 1
            self.lines.append(
                f"{stamp(self.t)} tick[p{self.n}](capture) skipped: {why}")
        self.flush()

    def other_ticks(self, count, gap):
        """Ticks of another group; they also move the awake clock."""
        for _ in range(count):
            self.t += gap
            self.n += 1
            self.lines.append(
                f"{stamp(self.t)} tick[o{self.n}](calendar): no routines due")
        self.flush()

    def flush(self):
        self.path.write_text("\n".join(self.lines) + "\n")


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "logs").mkdir()
        (self.base / "state").mkdir()
        self.log = LogBuilder(self.base / "logs" / "run.log")

    def history(self):
        return status.read_tick_history(self.base / "logs" / "run.log")

    def state(self):
        return self.history()["routines"]["slack-direct"]["state"]

    def check(self):
        return alerts.check(self.base, [ROUTINE], now=self.log.t + 10)

    def issues(self):
        path = self.base / "state" / events.FILE_NAME
        return json.loads(path.read_text().splitlines()[0])["details"]["issues"]

    # -- interrupted vs error ------------------------------------------------

    def test_tagged_fatal_is_interrupted_not_error(self):
        self.log.tick(OUTAGE)
        self.assertEqual(self.state(), "interrupted")
        row = status.routine_rows(
            self.base, [ROUTINE], self.history(), now=self.log.t + 10)[0]
        self.assertEqual(row["issues"], "interrupted")

    def test_untagged_fatal_is_error(self):
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.state(), "error")

    def test_plain_offline_marker_is_interrupted(self):
        self.log.tick("could not resolve host")
        self.assertEqual(self.state(), "interrupted")

    def test_rate_limit_stays_an_error_even_with_a_dead_network_word(self):
        self.log.tick("rate limit hit, could not resolve host")
        self.assertEqual(self.state(), "error")

    def test_only_measured_sleep_and_dead_network_are_interrupted(self):
        for text in ("name or service not known", "nodename nor servname "
                     "provided", "temporary failure in name resolution",
                     "network is unreachable"):
            self.log.tick(text)
            self.assertEqual(self.state(), "interrupted", text)

    def test_timeouts_and_refused_connections_stay_errors(self):
        for text in ("claude run timed out", "command timed out",
                     "connection refused", "connection reset by peer"):
            self.log.tick(text)
            self.assertEqual(self.state(), "error", text)

    def test_three_interrupted_ticks_and_two_awake_hours_alert(self):
        for _ in range(20):
            self.log.tick(OUTAGE)
        self.assertEqual(self.check(), [])
        for _ in range(8):
            self.log.tick(OUTAGE)
        self.assertEqual(self.check(), ["interrupted:slack-direct"])
        path = self.base / "state" / events.FILE_NAME
        details = json.loads(path.read_text().splitlines()[0])["details"]
        self.assertEqual(details["key"], "interrupted:slack-direct")
        self.assertIn("interrupted runs in a row", details["issues"][0])

    def test_one_interrupted_tick_and_three_awake_hours_do_not_alert(self):
        self.log.tick(OUTAGE)
        self.log.other_ticks(18, 600)  # 3 h of other ticks
        self.assertEqual(self.check(), [])

    def test_three_interrupted_ticks_within_forty_minutes_do_not_alert(self):
        for _ in range(3):
            self.log.tick(OUTAGE, after=600)
        self.assertEqual(self.check(), [])

    def test_both_keys_can_be_active_and_dedupe_separately(self):
        for _ in range(28):
            self.log.tick(OUTAGE)
        self.assertEqual(self.check(), ["interrupted:slack-direct"])
        for _ in range(3):
            self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), ["attention:slack-direct"])
        self.assertEqual(self.check(), [])
        self.log.tick(OUTAGE)
        self.assertEqual(self.check(), [])

    def test_interrupted_safety_net_resets_on_a_clean_tick(self):
        for _ in range(26):
            self.log.tick(OUTAGE)
        self.log.tick()
        self.assertEqual(self.check(), [])
        self.assertEqual(self.history()["interrupted"], {})

    def test_interrupted_asleep_gaps_do_not_trip_the_safety_net(self):
        for _ in range(30):
            self.log.tick(OUTAGE, after=3600)  # hourly: every gap is sleep
        self.assertEqual(self.check(), [])

    def test_mixed_real_error_and_outage_line_is_error(self):
        self.log.tick(OUTAGE, PLAIN_ERROR)
        self.assertEqual(self.state(), "error")
        self.assertEqual(self.history()["routines"]["slack-direct"]["errors"], 1)

    def test_interrupted_never_alerts(self):
        for _ in range(5):
            self.log.tick(OUTAGE)
        self.assertEqual(self.check(), [])

    # -- streak rule -----------------------------------------------------------

    def test_two_errors_do_not_alert_three_do(self):
        self.log.tick(PLAIN_ERROR)
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), [])
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), ["attention:slack-direct"])
        self.assertEqual(self.check(), [])  # deduped
        self.assertEqual(self.issues(), ["3 failed runs in a row"])

    def test_interrupted_tick_neither_counts_nor_breaks_the_streak(self):
        self.log.tick(PLAIN_ERROR)
        self.log.tick(OUTAGE)
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), [])
        self.log.tick(OUTAGE)
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), ["attention:slack-direct"])

    def test_clean_tick_resets_the_streak_and_clears_the_alert(self):
        for _ in range(3):
            self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), ["attention:slack-direct"])
        self.log.tick()
        self.assertEqual(self.check(), [])
        self.log.tick(PLAIN_ERROR)
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), [])
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), ["attention:slack-direct"])

    # -- awake-time rule ---------------------------------------------------------

    def test_two_awake_hours_alert_after_a_single_error(self):
        self.log.tick(PLAIN_ERROR)
        self.log.other_ticks(11, 600)  # 110 min awake
        self.assertEqual(self.check(), [])
        self.log.other_ticks(1, 660)   # past 120 min
        self.assertEqual(self.check(), ["attention:slack-direct"])

    def test_sleep_gaps_add_no_awake_time(self):
        self.log.tick(PLAIN_ERROR)
        self.log.other_ticks(1, 8 * 3600)   # slept overnight
        self.log.other_ticks(5, 600)
        self.log.other_ticks(1, 31 * 60)    # over the cap: sleep
        self.log.other_ticks(1, 3 * 3600)
        self.assertEqual(self.history()["streaks"]["slack-direct"]["awake"], 3000)
        self.assertEqual(self.check(), [])

    def test_gap_of_exactly_the_cap_counts(self):
        self.log.tick(PLAIN_ERROR)
        self.log.other_ticks(5, 30 * 60)
        self.assertEqual(self.check(), ["attention:slack-direct"])

    def test_interrupted_ticks_do_not_start_the_error_clock(self):
        self.log.tick(OUTAGE)
        self.log.other_ticks(11, 600)
        self.assertEqual(self.history()["streaks"], {})
        self.assertEqual(self.check(), [])

    def test_budget_held_tick_neither_counts_nor_resets(self):
        self.log.tick(PLAIN_ERROR)
        self.log.tick(PLAIN_ERROR)
        self.log.tick(held=["slack-direct"])
        self.assertEqual(self.history()["streaks"]["slack-direct"]["errors"], 2)
        self.assertEqual(self.check(), [])
        self.log.tick(PLAIN_ERROR)
        self.assertEqual(self.check(), ["attention:slack-direct"])

    def test_held_routine_with_a_real_error_still_counts_it(self):
        self.log.tick(PLAIN_ERROR, held=["slack-direct"])
        self.assertEqual(self.state(), "error")
        self.assertEqual(self.history()["streaks"]["slack-direct"]["errors"], 1)

    def test_held_tick_keeps_the_last_result(self):
        self.log.tick(PLAIN_ERROR)
        self.log.tick(held=["slack-direct"])
        self.assertEqual(self.state(), "error")

    def test_preflight_skipped_ticks_add_no_awake_time(self):
        self.log.tick(PLAIN_ERROR)
        self.log.preflight_skips(13, 600)
        self.log.preflight_skips(2, 600, why="lid closed")
        self.assertEqual(self.history()["streaks"]["slack-direct"]["awake"], 0)
        self.assertEqual(self.check(), [])

    def test_out_of_order_stamp_cannot_overcount_a_gap(self):
        self.log.tick(PLAIN_ERROR)
        t = self.log.t
        for offset in (601, 301, 901):
            self.log.lines.append(
                f"{stamp(t + offset)} tick[z{offset}](calendar): "
                "no routines due")
        self.log.flush()
        self.assertEqual(self.history()["streaks"]["slack-direct"]["awake"], 900)

    def test_other_issue_kinds_keep_alerting_at_once(self):
        rows = [{"routine": "slack-direct", "armed": "yes",
                 "issues": "1 memory sink, 1 last-run error"}]
        with mock.patch.object(alerts.status, "routine_rows",
                               return_value=rows):
            self.assertEqual(self.check(), ["attention:slack-direct"])
        self.assertEqual(self.issues(), ["1 memory sink"])


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        self.logs = []
        for name, value in (("_retry_sleep", self.sleeps.append),
                            ("log", self.logs.append)):
            patch = mock.patch.object(runner, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_listing(self, *outcomes, kind="slack_mcp", **flags):
        calls = iter(outcomes)

        def candidates(_source):
            outcome = next(calls)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return runner._list_with_retry(
            candidates, dict(kind=kind, **flags), "slack-direct", kind)

    def test_untagged_tool_error_is_retried_once_after_the_delay(self):
        result = self.run_listing(claude_bridge.BridgeError(PLAIN_ERROR), ["a"])
        self.assertEqual(result, ["a"])
        self.assertEqual(self.sleeps, [10])
        self.assertTrue(any("retry succeeded" in line for line in self.logs))

    def test_gmail_connector_is_retried_too(self):
        result = self.run_listing(
            claude_bridge.BridgeError(PLAIN_ERROR), ["a"], kind="gmail_mcp")
        self.assertEqual(result, ["a"])

    def test_tagged_outage_is_not_retried(self):
        with self.assertRaises(claude_bridge.BridgeError):
            self.run_listing(claude_bridge.BridgeOutage(OUTAGE), ["a"])
        self.assertEqual(self.sleeps, [])

    def test_count_only_is_not_retried(self):
        with self.assertRaises(claude_bridge.BridgeError):
            self.run_listing(
                claude_bridge.BridgeError(PLAIN_ERROR), ["a"],
                _count_only=True)
        self.assertEqual(self.sleeps, [])

    def test_retry_held_by_a_cap_raises_the_first_failure(self):
        first = claude_bridge.BridgeError(PLAIN_ERROR)
        with self.assertRaises(claude_bridge.BridgeError) as caught:
            self.run_listing(first, claude_bridge.BudgetExhausted("cap"))
        self.assertIs(caught.exception, first)
        self.assertNotIsInstance(caught.exception, claude_bridge.GUARD_STOPS)

    def test_second_failure_is_raised_for_the_fatal_line(self):
        second = claude_bridge.BridgeError(PLAIN_ERROR)
        with self.assertRaises(claude_bridge.BridgeError) as caught:
            self.run_listing(claude_bridge.BridgeError(PLAIN_ERROR), second)
        self.assertIs(caught.exception, second)
        self.assertEqual(len(self.sleeps), 1)
        self.assertTrue(any("retry failed" in line for line in self.logs))

    def test_dry_run_is_not_retried(self):
        with self.assertRaises(claude_bridge.BridgeError):
            self.run_listing(
                claude_bridge.BridgeError(PLAIN_ERROR), ["a"], _dry_run=True)
        self.assertEqual(self.sleeps, [])

    def test_other_exceptions_and_sources_are_not_retried(self):
        with self.assertRaises(ValueError):
            self.run_listing(ValueError("boom"), ["a"])
        with self.assertRaises(claude_bridge.BridgeError):
            self.run_listing(
                claude_bridge.BridgeError(PLAIN_ERROR), ["a"], kind="gchat")
        self.assertEqual(self.sleeps, [])

    def test_success_is_not_retried(self):
        self.assertEqual(self.run_listing(["a"]), ["a"])
        self.assertEqual(self.sleeps, [])


class ToolErrorCategoryTest(BridgeBase):
    """The real `_attempt` path: a failed tool call's body is never logged."""

    def failure(self, body):
        self.stream(
            init()
            + tool_use("t1", "ToolSearch", {"query": f"select:{TOOL_A}"})
            + tool_result("t1", [{"type": "tool_reference",
                                  "tool_name": TOOL_A}])
            + tool_use("t2", TOOL_A, {"q": "x"})
            + tool_result("t2", body, is_error=True)
            + result())
        with self.assertRaises(claude_bridge.BridgeError) as caught:
            claude_bridge.call_tools(
                "prompt", {TOOL_A: {}}, base_dir=self.base, connect_delay=0,
                attempts=1)
        return caught.exception

    def test_rate_limit_body_is_tagged_and_not_retried(self):
        exc = self.failure("Error: Rate limit exceeded for user U123")
        self.assertTrue(str(exc).endswith("(rate limited)"))
        self.assertNotIn("U123", str(exc))
        self.assertFalse(runner._retryable_tool_error(exc, {"kind": "slack_mcp"}))
        self.assertTrue(runner._is_shared_dependency_failure(exc))

    def test_quota_body_is_tagged_rate_limited(self):
        for body in ("quota exceeded", "ratelimited", "error: rate_limited"):
            exc = self.failure(body)
            self.assertTrue(str(exc).endswith("(rate limited)"), body)

    def test_oauth_error_codes_are_tagged_auth_failed(self):
        for body in ("invalid_auth", "not_authed", "token_expired",
                     "token_revoked", "invalid_grant"):
            exc = self.failure(body)
            self.assertTrue(str(exc).endswith("(auth failed)"), body)

    def test_auth_body_is_tagged_and_not_retried(self):
        exc = self.failure("Unauthenticated: token expired")
        self.assertTrue(str(exc).endswith("(auth failed)"))
        self.assertNotIn("token", str(exc))
        self.assertFalse(runner._retryable_tool_error(exc, {"kind": "slack_mcp"}))
        self.assertTrue(runner._is_shared_dependency_failure(exc))
        self.assertFalse(sleepclock.text_is_outage(exc))

    def test_other_body_stays_untagged_and_is_retried(self):
        exc = self.failure("internal server error")
        self.assertEqual(str(exc), f"tool returned an error: {TOOL_A}")
        self.assertTrue(runner._retryable_tool_error(exc, {"kind": "slack_mcp"}))


if __name__ == "__main__":
    unittest.main()
