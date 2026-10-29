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

    def test_failed_delivery_is_retried_on_the_next_tick(self):
        self.cap(5)
        self.spend(6)
        with mock.patch.object(alerts.state, "write_atomic",
                               side_effect=OSError("disk full")):
            self.assertEqual(self.check(), [])
        self.assertEqual(self.emitted(), [])
        self.assertEqual(self.check(), ["budget-hit:2026-03-10"])
        self.assertEqual(len(self.emitted()), 1)


if __name__ == "__main__":
    unittest.main()
