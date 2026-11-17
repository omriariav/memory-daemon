"""Alert causes: classification, last_error, since, remedies, review items."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path

from test_alert_streaks import LogBuilder, ROUTINE, START, stamp
from workspace_daemon import alerts, events, state

AUTH = ("tool returned an error: mcp__demo_srv__list_things (auth failed)")
SIGN_IN = ("tool returned no usable result: mcp__demo_srv__get_assets "
           "(auth failed)")
NEAR_DUP = ("routine=slack-direct memory ERROR: memory add failed (exit 2): "
            "near-duplicate of an existing entry")


class ClassifyTest(unittest.TestCase):
    def test_cause_from_log_text(self):
        for text, cause in (
            (AUTH, "connector_auth"),
            ("MCP server needs you to sign in again (run /mcp to "
             "re-authenticate)", "connector_auth"),
            ("daily budget exhausted", "budget"),
            ("memory add failed (exit 2): near-duplicate of x",
             "memory_duplicate"),
            ("not captured: Calendar match confidence=low", "meeting_match"),
            ("connection refused by the connector", "connector_down"),
            ("claude run timed out after 180s", "unknown"),
            ("claude stream had no init event", "connector_down"),
            ("tool returned no usable result: mcp__demo_srv__x", "tool_error"),
            ("tool returned an error: mcp__demo_srv__x", "tool_error"),
            ("could not resolve host", "run_state"),
            ("something odd happened", "unknown"),
        ):
            self.assertEqual(alerts.cause_of(text), cause, text)

    def test_auth_words_in_a_message_body_are_not_auth(self):
        for text in (
            "routine=r memory ERROR: memory add failed (exit 1): the note "
            "says authentication, unauthorized and token_expired",
            "claude said invalid_auth and unauthenticated",
        ):
            self.assertNotEqual(alerts.cause_of(text), "connector_auth", text)

    def test_server_comes_from_the_tool_name(self):
        self.assertEqual(alerts.server_of(AUTH), "demo_srv")
        self.assertEqual(alerts.server_of(
            "x mcp__claude_ai_Demo__tool failed"), "claude_ai_Demo")
        self.assertIsNone(alerts.server_of("no tool here"))

    def test_remedies_are_generic_and_name_only_the_server(self):
        self.assertEqual(
            alerts.remedy_for("connector_auth", "demo_srv"),
            "Sign in to the demo_srv connector again (in Claude Code: /mcp), "
            "then the routine retries on its next run")
        self.assertIn("affected connector",
                      alerts.remedy_for("connector_auth"))
        self.assertEqual(set(alerts.REMEDIES), set(alerts.CAUSES))


class CleanErrorTest(unittest.TestCase):
    def test_ids_addresses_and_urls_are_removed(self):
        text = ("routine=r1 source=x FATAL: failed for 123456789 "
                "abcDEF123456xyz9 jane@example.com https://example.com/a?b=1 "
                "item 42 ok")
        out = alerts.clean_error(text, "r1")
        self.assertEqual(
            out, "source=x FATAL: failed for <id> <id> <email> <url> item 42 ok")

    def test_id_fields_and_slack_ids_are_removed(self):
        # Built from pieces so the source holds no id-shaped literal.
        chan, user, group = "C" + "08AB12CD", "U" + "02ABCD3EF", "G" + "01234ABC"
        out = alerts.clean_error(
            f"routine=r1 ERROR id=slack:{chan}:1700000000.123456: boom", "r1")
        self.assertEqual(out, "ERROR id=<id> boom")
        out = alerts.clean_error(f"user {user} not found in {group}")
        self.assertEqual(out, "user <id> not found in <id>")
        self.assertEqual(alerts.clean_error("thread_id=t-9 gone"),
                         "thread_id=<id> gone")

    def test_plain_capitals_are_not_slack_ids(self):
        text = "CONN" + "ECTION DROPPED"
        self.assertEqual(alerts.clean_error(text), text)

    def test_names_with_digits_are_kept(self):
        text = ("tool returned an error: mcp__srv2__get_things_v2 "
                "for claude-sonnet-4-5")
        self.assertEqual(alerts.clean_error(text), text)
        self.assertEqual(alerts.clean_error("hash 9f86d081884c7d659a2f"),
                         "hash <id>")

    def test_cut_to_160_characters(self):
        out = alerts.clean_error("word " * 100)
        self.assertLessEqual(len(out), alerts.LAST_ERROR_CHARS)
        self.assertEqual(len(out), 159)  # trailing space stripped

    def test_tool_names_survive(self):
        self.assertIn("mcp__demo_srv__list_things",
                      alerts.clean_error(AUTH))


class TailTest(unittest.TestCase):
    def test_only_the_tail_is_read_and_a_partial_line_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            path.write_text("".join(f"line {i:05d}\n" for i in range(50)))
            lines = alerts.read_log_tail(path, cap=100)
            self.assertLessEqual(len("\n".join(lines)), 100)
            self.assertEqual(lines[-1], "line 00049")
            self.assertTrue(all(l.startswith("line ") and len(l) == 10
                                for l in lines))

    def test_missing_log_is_empty(self):
        self.assertEqual(alerts.read_log_tail("/nonexistent/run.log"), [])


class CauseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "logs").mkdir()
        (self.base / "state").mkdir()
        self.log = LogBuilder(self.base / "logs" / "run.log")

    def check(self, routines=(ROUTINE,)):
        return alerts.check(self.base, list(routines), now=self.log.t + 10)

    def emitted(self):
        path = self.base / "state" / events.FILE_NAME
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]


class AttentionCauseTest(CauseBase):
    def fail(self, text, times=3):
        for _ in range(times):
            self.log.tick(text)

    def test_expired_sign_in_names_its_cause(self):
        self.fail(SIGN_IN)
        self.assertEqual(self.check(), ["attention:slack-direct"])
        [event] = self.emitted()
        details = event["details"]
        self.assertEqual(event["title"],
                         "slack-direct: connector sign-in expired")
        self.assertEqual(details["cause"], "connector_auth")
        self.assertEqual(details["connector"], "demo_srv")
        self.assertEqual(details["issues"], ["3 failed runs in a row"])
        self.assertEqual(details["last_error"],
                         "source=slack_mcp FATAL: " + SIGN_IN)
        self.assertEqual(details["remedy"], alerts.remedy_for(
            "connector_auth", "demo_srv"))

    def test_since_is_the_start_of_the_streak(self):
        self.log.tick()  # clean tick before the streak
        self.fail(AUTH, times=2)
        first_start = self.log.t - 300  # the first failing tick
        self.log.tick(AUTH)
        self.check()
        [event] = self.emitted()
        self.assertEqual(event["details"]["since"], stamp(first_start))

    def test_last_error_is_the_newest_line_of_the_streak(self):
        self.log.tick("tool returned an error: mcp__demo_srv__old")
        self.log.tick("tool returned an error: mcp__demo_srv__mid")
        self.log.tick("first line", "tool returned an error: "
                      "mcp__demo_srv__newest")
        self.check()
        [event] = self.emitted()
        self.assertTrue(
            event["details"]["last_error"].endswith("mcp__demo_srv__newest"))

    def test_errors_before_the_streak_are_not_used(self):
        self.log.tick("tool returned an error: mcp__demo_srv__x (auth failed)")
        self.log.tick()  # clean: ends that streak
        self.fail("tool returned an error: mcp__demo_srv__y")
        self.check()
        [event] = self.emitted()
        self.assertEqual(event["details"]["cause"], "tool_error")
        self.assertNotIn("auth", event["details"]["last_error"])

    def test_last_error_is_cut_and_id_free(self):
        self.fail("failure for 99887766554433 " + "x " * 200)
        self.check()
        [event] = self.emitted()
        last = event["details"]["last_error"]
        self.assertLessEqual(len(last), alerts.LAST_ERROR_CHARS)
        self.assertNotIn("99887766554433", last)

    def test_tool_error_names_the_connector_for_overrides(self):
        self.fail("tool returned an error: mcp__demo_srv__list_things")
        self.check()
        [event] = self.emitted()
        self.assertEqual(event["details"]["cause"], "tool_error")
        self.assertEqual(event["details"]["connector"], "demo_srv")

    def test_unknown_error_keeps_the_old_title(self):
        self.fail("something odd happened")
        self.check()
        [event] = self.emitted()
        self.assertEqual(event["title"], "slack-direct needs attention: "
                                         "3 failed runs in a row")
        self.assertEqual(event["details"]["cause"], "unknown")

    def test_missing_log_adds_no_error_fields(self):
        # A ledger issue with no run.log at all: text and issues unchanged.
        state.save(self.base, {"a": {
            "rule_id": "slack-direct", "processed_at": stamp(START),
            "expand_fallback": True}})
        self.assertFalse((self.base / "logs" / "run.log").exists())
        routines = [{"id": "slack-direct", "enabled": True,
                     "schedule": {"every": "1h"}}]
        self.assertEqual(alerts.check(self.base, routines, now=START + 10),
                         ["attention:slack-direct"])
        [event] = self.emitted()
        self.assertEqual(event["title"], "slack-direct needs attention: "
                                         "1 source expansion")
        details = event["details"]
        self.assertEqual(details["issues"], ["1 source expansion"])
        self.assertEqual(details["cause"], "unknown")
        self.assertNotIn("last_error", details)
        self.assertNotIn("since", details)

    def test_budget_hold_has_a_budget_cause(self):
        (self.base / "state" / "budget.yaml").write_text(
            "daily_usd: 1\nday_timezone: UTC\n")
        (self.base / "state" / "claude-usage.jsonl").write_text(json.dumps({
            "ts": stamp(START), "tools": [], "outcome": "ok",
            "total_cost_usd": 2, "routine": "slack-direct"}) + "\n")
        self.log.t = START
        alerts.check(self.base, [{"id": "slack-direct",
                                  "analyze": {"provider": "claude"}}],
                     now=START + 60)
        causes = {e["details"]["key"][:10]: e["details"]["cause"]
                  for e in self.emitted()}
        self.assertEqual(set(causes.values()), {"budget"})


class ConnectorCauseTest(CauseBase):
    def spend(self, ts, outcome="ok"):
        path = self.base / "state" / "claude-usage.jsonl"
        row = {"ts": ts, "tools": ["mcp__demo_srv__list"], "outcome": outcome,
               "total_cost_usd": 0.01, "routine": "slack-direct"}
        with path.open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    def test_auth_failure_is_a_sign_in_alert(self):
        day = datetime.datetime.fromtimestamp(START, datetime.timezone.utc)
        t = lambda h: day.replace(hour=h).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.spend(t(8))
        self.spend(t(9), "error: tool returned an error: "
                         "mcp__demo_srv__list (auth failed)")
        self.spend(t(11), "error: tool returned an error: "
                          "mcp__demo_srv__list (auth failed)")
        self.log.t = START + 5 * 3600
        self.assertEqual(self.check(), ["connector:demo_srv"])
        [event] = self.emitted()
        details = event["details"]
        self.assertEqual(details["cause"], "connector_auth")
        self.assertEqual(details["since"], t(9))
        self.assertIn("auth failed", details["last_error"])
        self.assertEqual(details["connector"], "demo_srv")
        self.assertIn("sign-in expired", event["title"])
        self.assertIn("demo_srv", details["remedy"])

    def test_unexplained_failure_is_connector_down_and_tool_error_kept(self):
        for outcome, cause in (
                ("error: mcp__demo_srv__list failed", "connector_down"),
                ("error: tool returned an error: mcp__demo_srv__list",
                 "tool_error")):
            self.setUp()
            self.spend("2026-03-10T09:00:00Z", outcome)
            self.log.t = START + 5 * 3600
            self.check()
            [event] = self.emitted()
            self.assertEqual(event["details"]["cause"], cause)
            if cause == "connector_down":
                self.assertIn("demo_srv", event["details"]["remedy"])


class ReviewItemTest(CauseBase):
    ROUTINES = [{"id": "slack-direct", "enabled": True,
                 "schedule": {"every": "1h"}}]

    def run_check(self):
        return alerts.check(self.base, self.ROUTINES, now=START + 10)

    def test_meeting_match_alone_raises_nothing(self):
        state.save(self.base, {"a": {
            "rule_id": "slack-direct", "processed_at": stamp(START),
            "calendar_match_rejected": True}})
        self.assertEqual(self.run_check(), [])
        self.assertEqual(self.emitted(), [])

    def test_meeting_match_is_not_listed_with_real_issues(self):
        state.save(self.base, {
            "a": {"rule_id": "slack-direct", "processed_at": stamp(START),
                  "calendar_match_rejected": True},
            "b": {"rule_id": "slack-direct", "processed_at": stamp(START),
                  "expand_fallback": True}})
        self.assertEqual(self.run_check(), ["attention:slack-direct"])
        self.assertEqual(self.emitted()[0]["details"]["issues"],
                         ["1 source expansion"])

    def test_routines_without_issues_are_not_diagnosed(self):
        from unittest import mock
        with mock.patch.object(alerts, "_diagnose") as diagnose:
            self.assertEqual(self.run_check(), [])
        diagnose.assert_not_called()

    DUP = "memory add failed (exit 2): near-duplicate of e1"
    REAL = "memory add failed (exit 1): store locked"

    def sink_errors(self, *texts):
        """One ledger row per stored error (oldest first), and its log line."""
        rows = {}
        for n, text in enumerate(texts):
            rows[f"r{n}"] = {
                "rule_id": "slack-direct",
                "processed_at": stamp(START + n), "memory_error": text}
            self.log.lines.append(
                f"{stamp(START + n)} routine=slack-direct memory ERROR: {text}")
        state.save(self.base, rows)
        self.log.flush()

    def test_near_duplicate_memory_error_raises_nothing(self):
        self.sink_errors(self.DUP)
        self.assertEqual(self.run_check(), [])

    def test_only_near_duplicates_raise_nothing(self):
        self.sink_errors(self.DUP, self.DUP)
        self.assertEqual(self.run_check(), [])

    def test_real_error_after_a_near_duplicate_still_alerts(self):
        self.sink_errors(self.DUP, self.REAL)
        self.assertEqual(self.run_check(), ["attention:slack-direct"])
        details = self.emitted()[0]["details"]
        self.assertEqual(details["issues"], ["2 memory sink"])
        self.assertIn("store locked", details["last_error"])

    def test_real_error_before_a_near_duplicate_still_alerts(self):
        self.sink_errors(self.REAL, self.DUP)
        self.assertEqual(self.run_check(), ["attention:slack-direct"])
        self.assertIn("store locked",
                      self.emitted()[0]["details"]["last_error"])

    def test_the_result_does_not_flip_between_ticks(self):
        self.sink_errors(self.REAL, self.DUP)
        for _ in range(3):
            alerts.check(self.base, self.ROUTINES, now=START + 10)
        self.assertEqual(len(self.emitted()), 1)  # one alert, never cleared
        self.assertEqual(self.run_check(), [])

    def test_memory_error_title_keeps_needs_attention(self):
        self.sink_errors(self.REAL)
        self.run_check()
        self.assertEqual(self.emitted()[0]["title"],
                         "slack-direct needs attention: 1 memory sink")

    def test_final_meeting_match_alerts(self):
        state.save(self.base, {"a": {
            "rule_id": "slack-direct", "processed_at": stamp(START),
            "calendar_match_rejected": True, "calendar_match_final": True}})
        self.assertEqual(self.run_check(), ["attention:slack-direct"])
        [event] = self.emitted()
        self.assertEqual(event["details"]["issues"],
                         ["1 meeting not captured"])
        self.assertEqual(event["details"]["cause"], "meeting_match")
        self.assertIn("needs attention", event["title"])

    def test_final_meeting_match_older_than_a_day_is_dropped(self):
        state.save(self.base, {"a": {
            "rule_id": "slack-direct",
            "processed_at": stamp(START - 25 * 3600),
            "calendar_match_rejected": True, "calendar_match_final": True}})
        self.assertEqual(self.run_check(), [])

    def test_status_counts_meeting_match_for_a_day_only(self):
        from workspace_daemon import status
        state.save(self.base, {
            "new": {"rule_id": "slack-direct", "processed_at": stamp(START),
                    "calendar_match_rejected": True},
            "old": {"rule_id": "slack-direct",
                    "processed_at": stamp(START - 3 * 86400),
                    "calendar_match_rejected": True}})
        rows = status.routine_rows(
            self.base, self.ROUTINES, {"routines": {}}, now=START + 10)
        self.assertEqual(rows[0]["issues"], "1 meeting match")

    def test_a_streak_of_only_near_duplicates_raises_nothing(self):
        for _ in range(3):
            self.log.tick("memory ERROR: memory add failed (exit 2): "
                          "near-duplicate of e1")
        # the fixture prefixes the routine; tick lines read "... FATAL: ..."
        lines = [l.replace("source=slack_mcp FATAL: memory ERROR:",
                           "memory ERROR:") for l in self.log.lines]
        (self.base / "logs" / "run.log").write_text("\n".join(lines) + "\n")
        self.assertEqual(self.check(), [])


class RemedyConfigTest(CauseBase):
    def config(self, text):
        (self.base / "state" / "daemon.yaml").write_text(text)

    def test_longest_prefix_replaces_the_remedy(self):
        self.config(
            "alert_remedies:\n"
            "  'attention:': 'Ask the owner of {routine}.'\n"
            "  'attention:slack': 'Re-run the {server} sign-in script.'\n")
        for _ in range(3):
            self.log.tick(AUTH)
        self.check()
        [event] = self.emitted()
        self.assertEqual(event["details"]["remedy"],
                         "Re-run the demo_srv sign-in script.")
        self.assertEqual(event["details"]["cause"], "connector_auth")

    def test_other_keys_keep_the_generic_remedy(self):
        self.config("alert_remedies:\n  'connector:': 'custom'\n")
        for _ in range(3):
            self.log.tick(AUTH)
        self.check()
        self.assertEqual(self.emitted()[0]["details"]["remedy"],
                         alerts.remedy_for("connector_auth", "demo_srv"))

    def test_bad_config_adds_nothing(self):
        for text in ("::: not yaml [", "alert_remedies: 5\n", "- a\n",
                     "alert_remedies:\n  'x': 7\n  'y': ''\n"):
            self.config(text)
            self.assertEqual(alerts.load_remedies(self.base), {}, text)
        self.assertEqual(alerts.load_remedies(self.base / "nowhere"), {})

    def test_override_text_is_one_line_and_capped(self):
        self.config("alert_remedies:\n  'a': \"x\\ny\"\n")
        self.assertEqual(alerts.load_remedies(self.base), {"a": "x y"})
        self.config("alert_remedies:\n  'a': '" + "z" * 900 + "'\n")
        self.assertEqual(len(alerts.load_remedies(self.base)["a"]),
                         alerts.MAX_REMEDY_CHARS)


if __name__ == "__main__":
    unittest.main()
