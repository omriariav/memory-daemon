"""Operational alerts: thresholds, dedupe until clear, and delivery."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import alerts, events

NOW = datetime.datetime(
    2026, 3, 10, 20, 0, tzinfo=datetime.timezone.utc).timestamp()
BRIDGED = {"id": "alpha", "analyze": {"provider": "claude"}}
OTHER = {"id": "beta", "analyze": {"provider": "claude"}}
PLAIN = {"id": "plain"}


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()
        self.usage = []

    def tearDown(self):
        self.tmp.cleanup()

    def cap(self, usd):
        (self.base / "state" / "budget.yaml").write_text(
            f"daily_usd: {usd}\nday_timezone: UTC\n")

    def spend(self, usd, routine="alpha", ts="2026-03-10T19:00:00Z",
              tools=(), outcome="ok"):
        self.usage.append({"ts": ts, "tools": list(tools), "outcome": outcome,
                           "total_cost_usd": usd, "routine": routine})
        (self.base / "state" / "claude-usage.jsonl").write_text(
            "".join(json.dumps(u) + "\n" for u in self.usage))

    def check(self, routines=(BRIDGED, OTHER, PLAIN), now=NOW):
        return alerts.check(self.base, list(routines), now=now)

    def emitted(self):
        path = self.base / "state" / events.FILE_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_below_80_percent_is_quiet(self):
        self.cap(10)
        self.spend(7.99)
        self.assertEqual(self.check(), [])
        self.assertEqual(self.emitted(), [])

    def test_80_percent_alerts_once_with_spend_by_routine(self):
        self.cap(10)
        self.spend(6.0)
        self.spend(2.0, routine="beta")
        self.assertEqual(self.check(), ["budget-80:2026-03-10"])
        self.assertEqual(self.check(), [])
        self.spend(0.5, routine="beta")
        self.assertEqual(self.check(), [])
        [event] = self.emitted()
        self.assertEqual((event["source"], event["kind"]), ("daemon", "alert"))
        self.assertIsNone(event["memory_id"])
        self.assertEqual(event["details"]["by_routine"],
                         {"alpha": 6.0, "beta": 2.0})
        self.assertIn("80%", event["title"])
        self.assertIn("alpha $6.00, beta $2.00", event["title"])

    def test_cap_hit_alerts_once_and_lists_held_routines(self):
        self.cap(5)
        self.spend(4.5)
        self.assertEqual(self.check(), ["budget-80:2026-03-10"])
        self.spend(0.51, routine="beta")
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(self.check(), [])
        hit = self.emitted()[-1]
        self.assertEqual(hit["details"]["held"], ["alpha", "beta"])
        self.assertEqual(hit["details"]["spent_usd"], 5.01)
        self.assertIn("holding alpha, beta", hit["title"])

    def test_jump_past_cap_sends_only_the_hit(self):
        self.cap(5)
        self.spend(6)
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(len(self.emitted()), 1)

    def test_global_hit_does_not_add_one_alert_per_routine(self):
        self.cap(5)
        self.spend(6)
        new = self.check()
        self.assertFalse([key for key in new if key.startswith("hold:")])

    def test_raised_cap_clears_and_can_alert_again(self):
        self.cap(5)
        self.spend(5.01)
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.cap(20)
        self.assertEqual(self.check(), [])
        self.assertEqual(
            json.loads((self.base / "state" / "alerts.json").read_text()), {})
        self.spend(11)
        self.assertEqual(self.check(), ["budget-80:2026-03-10"])

    def test_next_day_alerts_again(self):
        self.cap(5)
        self.spend(5.01)
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.spend(5.01, ts="2026-03-11T08:00:00Z")
        later = NOW + 13 * 3600
        self.assertEqual(self.check(now=later), ["budget-hit:2026-03-11"])

    def test_routine_cap_hold_alerts_once_per_day(self):
        capped = dict(BRIDGED, budget={"daily_usd": 1})
        self.spend(1.2)
        self.assertEqual(self.check([capped, OTHER]), ["hold:alpha:2026-03-10"])
        self.assertEqual(self.check([capped, OTHER]), [])
        [event] = self.emitted()
        self.assertEqual(event["details"]["routine"], "alpha")

    def test_disabled_routine_is_not_alerted(self):
        capped = dict(BRIDGED, budget={"daily_usd": 1}, enabled=False)
        self.spend(1.2)
        self.assertEqual(self.check([capped]), [])

    def test_invalid_budget_file_alerts_until_fixed(self):
        (self.base / "state" / "budget.yaml").write_text("daily_usd: -1\n")
        self.assertEqual(self.check(), ["budget-invalid"])
        self.assertEqual(self.check(), [])
        self.assertEqual(self.emitted()[0]["details"]["held"], ["alpha", "beta"])
        self.cap(10)
        self.assertEqual(self.check(), [])
        (self.base / "state" / "budget.yaml").write_text("daily_usd: -1\n")
        self.assertEqual(self.check(), ["budget-invalid"])

    def test_attention_alerts_once_until_cleared(self):
        rows = [{"routine": "alpha", "armed": "yes",
                 "issues": "1 memory sink, incomplete"}]
        with mock.patch.object(alerts.status, "routine_rows",
                               return_value=rows):
            self.assertEqual(self.check(), ["attention:alpha"])
            self.assertEqual(self.check(), [])
        event = self.emitted()[0]
        self.assertEqual(event["details"]["issues"], ["1 memory sink"])
        clean = [{"routine": "alpha", "armed": "yes", "issues": "-"}]
        with mock.patch.object(alerts.status, "routine_rows",
                               return_value=clean):
            self.assertEqual(self.check(), [])
        with mock.patch.object(alerts.status, "routine_rows",
                               return_value=rows):
            self.assertEqual(self.check(), ["attention:alpha"])

    def test_transient_tick_states_and_disarmed_routines_are_ignored(self):
        rows = [
            {"routine": "alpha", "armed": "yes", "issues": "incomplete"},
            {"routine": "beta", "armed": "yes", "issues": "overlap"},
            {"routine": "plain", "armed": "no", "issues": "1 memory sink"},
        ]
        with mock.patch.object(alerts.status, "routine_rows",
                               return_value=rows):
            self.assertEqual(self.check(), [])

    def test_connector_failing_over_an_hour_alerts_once(self):
        tool = ["mcp__srv_a__list"]
        self.spend(0.01, ts="2026-03-10T17:00:00Z", tools=tool)
        self.spend(0.01, ts="2026-03-10T18:30:00Z", tools=tool,
                   outcome="error: mcp__srv_a__list failed")
        self.spend(0.01, ts="2026-03-10T19:50:00Z", tools=tool,
                   outcome="error: mcp__srv_a__list failed")
        self.assertEqual(self.check(), ["connector:srv_a"])
        self.assertEqual(self.check(), [])
        self.spend(0.01, ts="2026-03-10T19:55:00Z", tools=tool)
        self.assertEqual(self.check(), [])
        self.assertEqual(
            json.loads((self.base / "state" / "alerts.json").read_text()), {})

    def test_recent_connector_error_waits_for_the_grace_period(self):
        tool = ["mcp__srv_a__list"]
        self.spend(0.01, ts="2026-03-10T17:00:00Z", tools=tool)
        self.spend(0.01, ts="2026-03-10T19:30:00Z", tools=tool,
                   outcome="error: mcp__srv_a__list failed")
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(now=NOW + 1800), ["connector:srv_a"])

    def test_error_only_in_a_failed_call_is_not_the_connectors_fault(self):
        self.spend(0.01, ts="2026-03-10T17:00:00Z",
                   tools=["mcp__srv_a__list", "mcp__srv_b__get"],
                   outcome="error: mcp__srv_b__get failed")
        self.assertEqual(self.check(), ["connector:srv_b"])

    def test_concurrent_check_is_skipped(self):
        self.cap(5)
        self.spend(6)
        import fcntl
        import os
        fd = os.open(str(self.base / "state" / alerts.LOCK_FILE),
                     os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.check(), [])
        finally:
            os.close(fd)
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])

    def test_failure_never_raises(self):
        with mock.patch.object(alerts, "budget_conditions",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self.check(), [])

    def test_failing_save_sends_nothing_until_it_works(self):
        self.cap(5)
        self.spend(6)
        with mock.patch.object(alerts.state, "write_atomic",
                               side_effect=OSError("disk full")):
            self.assertEqual(self.check(), [])
            self.assertEqual(self.check(), [])
        self.assertEqual(self.emitted(), [])
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(self.check(), [])
        self.assertEqual(len(self.emitted()), 1)

    def test_unconfirmed_send_is_retried(self):
        self.cap(5)
        self.spend(6)
        real = alerts.state.write_atomic
        calls = []

        def second_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(*args, **kwargs)

        with mock.patch.object(alerts.state, "write_atomic",
                               side_effect=second_fails):
            self.assertEqual(self.check(), [])
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(self.check(), [])
        self.assertEqual(len(self.emitted()), 2)

    def test_own_cap_hold_hidden_by_cap_hit_alerts_once_it_clears(self):
        capped = dict(BRIDGED, budget={"daily_usd": 1})
        self.cap(5)
        self.spend(6)
        self.assertEqual(self.check([capped]), ["budget-hit:2026-03-10"])
        self.assertEqual(self.check([capped]), [])
        self.cap(20)
        self.assertEqual(self.check([capped]), ["hold:alpha:2026-03-10"])
        self.assertEqual(self.check([capped]), [])
        self.assertEqual(len(self.emitted()), 2)

    def test_own_cap_hold_before_cap_hit_is_not_repeated(self):
        capped = dict(BRIDGED, budget={"daily_usd": 1})
        self.cap(5)
        self.spend(1.5)
        self.assertEqual(self.check([capped]), ["hold:alpha:2026-03-10"])
        self.spend(4, routine="beta")
        self.assertEqual(self.check([capped]), ["budget-hit:2026-03-10"])
        self.cap(20)
        self.assertEqual(self.check([capped]), [])
        hold = self.emitted()[0]
        self.assertEqual(hold["details"]["cap_usd"], 1)

    def test_same_second_ok_beats_error(self):
        tool = ["mcp__srv_a__list"]
        self.spend(0.01, ts="2026-03-10T18:00:00Z", tools=tool)
        self.spend(0.01, ts="2026-03-10T18:00:00Z", tools=tool,
                   outcome="error: mcp__srv_a__list failed")
        self.assertEqual(self.check(), [])

    def test_unreadable_usage_log_alerts_with_held_routines(self):
        self.cap(10)
        (self.base / "state" / "claude-usage.jsonl").mkdir()
        capped_plain = dict(PLAIN, budget={"daily_usd": 1})
        self.assertEqual(self.check([BRIDGED, capped_plain]),
                         ["usage-unreadable"])
        [event] = self.emitted()
        self.assertEqual(event["details"]["held"], ["alpha", "plain"])
        self.assertIn("usage log", event["title"])

    def test_unreadable_usage_log_without_any_cap_holds_nothing(self):
        (self.base / "state" / "claude-usage.jsonl").mkdir()
        self.assertEqual(self.check([BRIDGED, OTHER]), ["usage-unreadable"])
        [event] = self.emitted()
        self.assertEqual(event["details"]["held"], [])
        self.assertIn("holding nothing", event["title"])

    def test_fixing_one_of_two_broken_files_does_not_re_alert(self):
        (self.base / "state" / "budget.yaml").write_text("daily_usd: -1\n")
        (self.base / "state" / "claude-usage.jsonl").mkdir()
        self.assertEqual(sorted(self.check()),
                         ["budget-invalid", "usage-unreadable"])
        self.cap(10)
        self.assertEqual(self.check(), [])

    def test_malformed_saved_values_count_as_new(self):
        self.cap(5)
        self.spend(6)
        (self.base / "state" / "alerts.json").write_text(json.dumps(
            {"budget-hit:2026-03-10": {}, "other": None}))
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(self.check(), [])
        self.assertEqual(len(self.emitted()), 1)

    def test_pending_retry_needs_a_working_save(self):
        self.cap(5)
        self.spend(6)
        with mock.patch.object(alerts.events, "emit", return_value=False):
            self.assertEqual(self.check(), [])
        with mock.patch.object(alerts.state, "write_atomic",
                               side_effect=OSError("disk full")):
            self.assertEqual(self.check(), [])
        self.assertEqual(self.emitted(), [])

    def test_invalid_budget_still_reports_own_cap_holds_of_plain_routines(self):
        (self.base / "state" / "budget.yaml").write_text("daily_usd: -1\n")
        capped_plain = dict(PLAIN, budget={"daily_usd": 1})
        self.spend(1.5, routine="plain")
        self.assertEqual(self.check([capped_plain]), ["hold:plain:2026-03-10"])

    def test_connector_order_follows_time_not_file_order(self):
        tool = ["mcp__srv_a__list"]
        self.spend(0.01, ts="2026-03-10T18:00:05Z", tools=tool)
        self.spend(0.01, ts="2026-03-10T18:00:00Z", tools=tool,
                   outcome="error: mcp__srv_a__list failed")
        self.assertEqual(self.check(), [])

    def test_unreadable_usage_log_alerts_without_bridged_routines(self):
        (self.base / "state" / "claude-usage.jsonl").mkdir()
        capped_plain = dict(PLAIN, budget={"daily_usd": 1})
        self.assertEqual(self.check([capped_plain]), ["usage-unreadable"])

    def test_global_hit_suppresses_own_cap_holds_of_bridged_routines(self):
        self.cap(5)
        capped = dict(BRIDGED, budget={"daily_usd": 1})
        self.spend(6)
        self.assertEqual(self.check([capped, OTHER]), ["budget-hit:2026-03-10"])
        self.assertEqual(len(self.emitted()), 1)

    def test_failed_append_is_retried_on_the_next_tick(self):
        self.cap(5)
        self.spend(6)
        with mock.patch.object(alerts.events, "emit", return_value=False):
            self.assertEqual(self.check(), [])
        self.assertEqual(self.emitted(), [])
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(len(self.emitted()), 1)
        self.assertEqual(self.check(), [])

    def test_state_dir_failure_never_raises(self):
        with mock.patch.object(alerts.state, "ensure_private_dir",
                               side_effect=PermissionError("denied")):
            self.assertEqual(self.check(), [])

    def test_unreadable_tick_does_not_clear_sent_alerts(self):
        self.cap(5)
        tool = ["mcp__srv_a__list"]
        self.spend(0.01, ts="2026-03-10T17:00:00Z", tools=tool,
                   outcome="error: mcp__srv_a__list failed")
        self.spend(6)
        self.assertEqual(sorted(self.check()),
                         ["budget-hit:2026-03-10", "connector:srv_a"])
        log = self.base / "state" / "claude-usage.jsonl"
        log.rename(log.with_suffix(".bak"))
        log.mkdir()
        self.assertEqual(self.check(), ["usage-unreadable"])
        log.rmdir()
        log.with_suffix(".bak").rename(log)
        self.assertEqual(self.check(), [])

    def test_broken_budget_file_does_not_clear_cap_hit(self):
        self.cap(5)
        self.spend(6)
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        (self.base / "state" / "budget.yaml").write_text("daily_usd: -1\n")
        self.assertEqual(self.check(), ["budget-invalid"])
        self.cap(5)
        self.assertEqual(self.check(), [])

    def test_failure_after_a_send_does_not_resend_it(self):
        self.cap(5)
        self.spend(6)
        (self.base / "state" / "budget.yaml").write_text(
            "daily_usd: 5\nday_timezone: UTC\n")
        capped = dict(BRIDGED, budget={"daily_usd": 1})
        self.spend(0.01, routine="plain")
        real = alerts.events.build
        calls = []

        def second_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(*args, **kwargs)

        rows = [{"routine": "beta", "armed": "yes", "issues": "1 memory sink"}]
        with mock.patch.object(alerts.events, "build", side_effect=second_fails), \
                mock.patch.object(alerts.status, "routine_rows", return_value=rows):
            self.assertEqual(self.check([capped, OTHER]), [])
        self.assertEqual(len(self.emitted()), 1)
        with mock.patch.object(alerts.status, "routine_rows", return_value=rows):
            self.assertEqual(self.check([capped, OTHER]), ["budget-hit:2026-03-10"])
        self.assertEqual(len(self.emitted()), 2)

    def test_hold_listed_on_budget_invalid_is_not_announced_twice(self):
        capped = dict(BRIDGED, budget={"daily_usd": 1})
        self.spend(1.5)
        (self.base / "state" / "budget.yaml").write_text("daily_usd: -1\n")
        self.assertEqual(self.check([capped]), ["budget-invalid"])
        self.cap(10)
        self.assertEqual(self.check([capped]), ["hold:alpha:2026-03-10"])


if __name__ == "__main__":
    unittest.main()
