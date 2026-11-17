"""The Health snapshot and the health strip (synthetic fixtures)."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import health, items as itm, sources
from workspace_daemon.inbox.models import from_v1

NOW = 1_700_000_000.0
H = 3600.0
MIN = 60.0


def local(year, month, day, hour, minute=0):
    """Epoch of a local wall-clock time (the window is in local time)."""
    return datetime(year, month, day, hour, minute).timestamp()


def row(routine, status="waiting", issues="-", every="5m", last="8m ago",
        capture="14m ago"):
    return {"routine": routine, "status": status, "issues": issues,
            "every": every, "last_attempt": last, "last_capture": capture}


def ok_tick(ago_s=180):
    return {"state": "ok", "at": time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - ago_s))}


def epoch_of(text):
    return NOW - 180


class RoutineStateTest(unittest.TestCase):
    def build(self, rows, ticks, alerts=()):
        return health.build_routines(rows, ticks, list(alerts), epoch_of)

    def state_of(self, routines, rid):
        return next(r for r in routines if r["id"] == rid)["state"]

    def test_group_is_the_text_before_the_first_separator(self):
        self.assertEqual(health.routine_group("slack-mcp"), "slack")
        self.assertEqual(health.routine_group("calendar_cache"), "calendar")
        self.assertEqual(health.routine_group("zoom"), "zoom")

    def test_states_from_status_rows(self):
        rows = [
            row("slack-mcp"),
            row("zoom-meetings", issues="1 last-run error"),
            row("mila-meetings", issues="1 meeting match"),
            row("fibery-inbound", issues="overlap"),
            row("calendar-cache", status="in-tick"),
            row("gmail-mcp", status="disabled"),
            row("drive-watch", status="paused"),
        ]
        ticks = {"slack-mcp": ok_tick(), "zoom-meetings": {"state": "error",
                 "at": ok_tick()["at"]}}
        routines, groups = self.build(rows, ticks)
        got = {r["id"]: r["state"] for r in routines}
        self.assertEqual(got, {
            "slack-mcp": "ok", "zoom-meetings": "fail",
            "mila-meetings": "warn", "fibery-inbound": "busy",
            "calendar-cache": "busy", "gmail-mcp": "off",
            "drive-watch": "warn"})
        names = {g["name"]: g for g in groups}
        self.assertNotIn("gmail", names)          # a disabled routine is off
        self.assertEqual(names["slack"]["state"], "ok")
        self.assertEqual(names["slack"]["good_at"], NOW - 180)
        self.assertEqual(names["zoom"]["state"], "fail")

    def test_a_group_is_as_bad_as_its_worst_routine(self):
        rows = [row("slack-direct"), row("slack-mcp", issues="1 last-run error")]
        ticks = {"slack-direct": ok_tick()}
        _routines, groups = self.build(rows, ticks)
        (group,) = groups
        self.assertEqual((group["name"], group["state"], group["count"]),
                         ("slack", "fail", 2))

    def test_an_attention_alert_marks_the_routine_failing(self):
        rows = [row("zoom-meetings")]
        alerts = [{"key": "attention:zoom-meetings",
                   "routine": "zoom-meetings", "since": NOW - 600}]
        routines, _ = self.build(rows, {"zoom-meetings": ok_tick()}, alerts)
        self.assertEqual(self.state_of(routines, "zoom-meetings"), "fail")

    def test_a_budget_hold_alert_is_attention_not_failing(self):
        rows = [row("zoom-meetings")]
        alerts = [{"key": "hold:zoom-meetings:2030-01-01",
                   "routine": "zoom-meetings", "since": NOW - 600}]
        routines, _ = self.build(rows, {"zoom-meetings": ok_tick()}, alerts)
        self.assertEqual(self.state_of(routines, "zoom-meetings"), "warn")

    def test_table_columns(self):
        routines, _ = self.build([row("slack-mcp", every="5m", last="8m ago",
                                      capture="14m ago")],
                                 {"slack-mcp": ok_tick()})
        r = routines[0]
        self.assertEqual((r["every"], r["last_run"], r["last_capture"]),
                         ("5m", "8m", "14m"))


class StripTest(unittest.TestCase):
    def snap(self, **kw):
        base = {"groups": [], "routines": [], "spend": {}, "heartbeats": []}
        base.update(kw)
        return base

    def test_cells_for_each_state_and_spend(self):
        routines, groups = health.build_routines(
            [row("slack-mcp"), row("zoom-meetings", issues="1 last-run error"),
             row("mila-meetings", issues="1 meeting match"),
             row("fibery-inbound", issues="overlap")],
            {"slack-mcp": ok_tick(), "zoom-meetings": {
                "state": "error", "at": ok_tick()["at"]}}, [], epoch_of)
        snap = self.snap(groups=groups, routines=routines,
                         spend={"total": 6.2, "cap": 20.0})
        cells = dict((t.split()[0], (s, t))
                     for s, t in health.strip_cells(snap, NOW))
        self.assertEqual(cells["slack"], ("ok", "slack ✓3m"))
        self.assertEqual(cells["zoom"], ("fail", "zoom ✗ failing"))
        self.assertEqual(cells["mila"], ("warn", "mila ⚠ 1 meeting match"))
        self.assertEqual(cells["fibery"], ("busy", "fibery ◌ busy"))
        self.assertEqual(cells["$6/$20"], ("ok", "$6/$20"))

    def test_spend_over_the_cap_warns(self):
        cells = health.strip_cells(
            self.snap(spend={"total": 20.0, "cap": 20.0}), NOW)
        self.assertEqual(cells, [("warn", "$20/$20")])

    def test_spend_without_a_cap(self):
        self.assertEqual(health.strip_cells(
            self.snap(spend={"total": 3.0, "cap": None}), NOW),
            [("ok", "$3")])

    def test_the_cause_of_an_alert_words_a_failing_cell(self):
        routines, groups = health.build_routines(
            [row("zoom-meetings", issues="1 last-run error")],
            {"zoom-meetings": {"state": "error", "at": ok_tick()["at"]}}, [],
            epoch_of)
        snap = self.snap(groups=groups, routines=routines)
        infos = {"zoom-meetings": {"key": "attention:zoom-meetings",
                                   "cause": "connector_auth"}}
        (cell,) = health.strip_cells(snap, NOW, infos)
        self.assertEqual(cell, ("fail", "zoom ✗ connector sign-in expired"))

    def test_heartbeat_cells(self):
        snap = self.snap(heartbeats=[
            {"name": "poller", "state": "ok", "age_s": 120.0, "note": ""},
            {"name": "sync", "state": "warn", "age_s": None,
             "note": "no heartbeat file"},
            {"name": "late", "state": "ok", "age_s": None,
             "note": "off hours"}])
        self.assertEqual(health.strip_cells(snap, NOW), [
            ("ok", "poller ✓2m"), ("warn", "sync ⚠ no heartbeat file"),
            ("ok", "late ✓ off hours")])


def alert_row(key, **meta):
    base = {"key": key, "kind": "alert"}
    base.update(meta)
    return from_v1(itm.make_item(
        id=f"events:alert-active:{key}", source="alert", section="urgent",
        urgency="urgent", sender="daemon", where="daemon", gist="alert",
        ts=NOW - 600, meta=base, actions=["ack"]))


class AlertInfoTest(unittest.TestCase):
    def test_details_are_read_when_present(self):
        info = health.alert_info([alert_row(
            "attention:zoom", routine="zoom", cause="connector_auth",
            remedy="sign in again", last_error="401", since=NOW - 3600,
            connector="zoom-server")])
        got = info["zoom"]
        self.assertEqual((got["cause"], got["remedy"], got["last_error"],
                          got["connector"]),
                         ("connector_auth", "sign in again", "401",
                          "zoom-server"))
        self.assertEqual(health.cause_text(got), "connector sign-in expired")

    def test_missing_details_degrade_to_the_title(self):
        info = health.alert_info([alert_row("attention:zoom", routine="zoom")])
        self.assertNotIn("cause", info["zoom"])
        self.assertEqual(health.cause_text(info["zoom"]), "")
        self.assertEqual(info["zoom"]["title"], "alert")

    def test_resolved_and_benign_alerts_are_left_out(self):
        rows = [alert_row("attention:a", routine="a", resolved=True),
                alert_row("attention:b", routine="b", benign=True),
                alert_row("attention:c", routine="c")]
        self.assertEqual(list(health.alert_info(rows)), ["c"])

    def test_an_unknown_cause_reads_as_words(self):
        self.assertEqual(health.cause_text({"cause": "new_thing_x"}),
                         "new thing x")

    def test_event_details_flow_into_the_item_and_thread(self):
        with tempfile.TemporaryDirectory() as base:
            state = Path(base) / "state"
            state.mkdir()
            details = {"key": "attention:zoom", "routine": "zoom",
                       "issues": ["last run failed"],
                       "cause": "connector_auth",
                       "last_error": "token refused",
                       "remedy": "sign in again",
                       "since": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(NOW - 7200)),
                       "connector": "zoom-server"}
            ev = events.build("zoom", "alert", None, "daemon:attention:zoom",
                              "zoom: connector sign-in expired", [],
                              ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(NOW - 600)),
                              details=details)
            (state / "events.jsonl").write_text(json.dumps(ev) + "\n")
            (item,) = sources.load_events(base, now=NOW).items
        meta = item["meta"]
        self.assertEqual(meta["cause"], "connector_auth")
        self.assertEqual(meta["remedy"], "sign in again")
        self.assertAlmostEqual(meta["since"], NOW - 7200, delta=1)
        thread = {m["who"]: m["text"] for m in item["thread"]}
        self.assertEqual(thread["cause"],
                         "connector sign-in expired (zoom-server)")
        self.assertEqual(thread["last error"], "token refused")
        self.assertEqual(thread["remedy"], "sign in again")
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(NOW - 7200))
        self.assertEqual(thread["active since"], stamp)   # local time

    def test_an_alert_without_the_fields_keeps_the_old_thread(self):
        with tempfile.TemporaryDirectory() as base:
            state = Path(base) / "state"
            state.mkdir()
            ev = events.build("zoom", "alert", None, "daemon:attention:zoom",
                              "zoom needs attention", [],
                              ts=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime(NOW - 600)),
                              details={"key": "attention:zoom",
                                       "routine": "zoom",
                                       "issues": ["last run failed"]})
            (state / "events.jsonl").write_text(json.dumps(ev) + "\n")
            (item,) = sources.load_events(base, now=NOW).items
        who = [m["who"] for m in item["thread"]]
        self.assertNotIn("cause", who)
        self.assertNotIn("remedy", who)
        self.assertIn("active since", who)
        self.assertEqual(who[-1], "hint")


class HoursTest(unittest.TestCase):
    def test_parse(self):
        days, start, end = health.parse_hours("sun-thu 09:00-19:00")
        self.assertEqual(days, {6, 0, 1, 2, 3})
        self.assertEqual((start, end), (540, 1140))
        self.assertEqual(health.parse_hours("09:00-17:30")[0], set(range(7)))
        self.assertEqual(health.parse_hours("mon,wed 08:00-09:00")[0], {0, 2})
        self.assertEqual(health.parse_hours("fri-mon 08:00-09:00")[0],
                         {4, 5, 6, 0})
        for bad in ("", "soon", "sun-xyz 09:00-19:00", "9-17", "25:00-26:00",
                    "mon-wed,fri 09:00-10:00", None):
            self.assertIsNone(health.parse_hours(bad), bad)

    def test_window(self):
        window = health.parse_hours("sun-thu 09:00-19:00")
        # 2030-01-06 is a Sunday, 2030-01-11 a Friday
        self.assertTrue(health.in_hours(window, local(2030, 1, 6, 10)))
        self.assertFalse(health.in_hours(window, local(2030, 1, 6, 19)))
        self.assertFalse(health.in_hours(window, local(2030, 1, 6, 8, 59)))
        self.assertFalse(health.in_hours(window, local(2030, 1, 11, 10)))
        self.assertTrue(health.in_hours(None, local(2030, 1, 11, 3)))

    def test_a_window_may_cross_midnight(self):
        window = health.parse_hours("mon 22:00-02:00")
        self.assertTrue(health.in_hours(window, local(2030, 1, 7, 23)))  # Mon
        self.assertTrue(health.in_hours(window, local(2030, 1, 8, 1)))   # Tue
        self.assertFalse(health.in_hours(window, local(2030, 1, 8, 3)))
        self.assertFalse(health.in_hours(window, local(2030, 1, 7, 1)))


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.path = os.path.join(self.base, "hb")

    def beat(self, mtime=None, **kw):
        if mtime is not None:
            Path(self.path).write_text("x")
            os.utime(self.path, (mtime, mtime))
        hb = {"name": "poller", "path": self.path, "stale_min": 10}
        hb.update(kw)
        return hb

    def test_fresh_file_is_ok(self):
        now = local(2030, 1, 6, 12)
        got = health.heartbeat_state(self.beat(now - 5 * MIN), self.base, now)
        self.assertEqual((got["state"], got["note"]), ("ok", ""))
        self.assertAlmostEqual(got["age_s"], 300, delta=1)

    def test_stale_file_warns(self):
        now = local(2030, 1, 6, 12)
        got = health.heartbeat_state(self.beat(now - 11 * MIN), self.base, now)
        self.assertEqual((got["state"], got["note"]), ("warn", "stale"))

    def test_missing_file_warns(self):
        now = local(2030, 1, 6, 12)
        got = health.heartbeat_state(self.beat(), self.base, now)
        self.assertEqual((got["state"], got["note"], got["age_s"]),
                         ("warn", "no heartbeat file", None))

    def test_hours_window_decides_whether_stale_matters(self):
        hours = "sun-thu 09:00-19:00"
        inside = local(2030, 1, 6, 12)            # Sunday noon
        got = health.heartbeat_state(
            self.beat(inside - H, hours=hours), self.base, inside)
        self.assertEqual(got["state"], "warn")
        outside = local(2030, 1, 11, 12)          # a Friday
        got = health.heartbeat_state(
            self.beat(outside - H, hours=hours), self.base, outside)
        self.assertEqual((got["state"], got["note"]), ("ok", "off hours"))
        night = local(2030, 1, 6, 22)
        got = health.heartbeat_state(self.beat(hours=hours), self.base, night)
        self.assertEqual(got["state"], "ok")      # no file, but off hours

    def test_a_fresh_file_outside_hours_is_plain_ok(self):
        now = local(2030, 1, 11, 12)
        got = health.heartbeat_state(
            self.beat(now - MIN, hours="sun-thu 09:00-19:00"), self.base, now)
        self.assertEqual((got["state"], got["note"]), ("ok", ""))

    def test_relative_path_is_under_the_base(self):
        Path(self.path).write_text("x")
        got = health.heartbeat_state(
            {"name": "p", "path": "hb", "stale_min": 5}, self.base,
            time.time())
        self.assertEqual(got["state"], "ok")


class ReaderTest(unittest.TestCase):
    """HealthReader against a fixture base directory (no launchd)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "routines").mkdir()
        (self.base / "logs").mkdir()
        (self.base / "state").mkdir()
        for rid in ("slack-mcp", "zoom-meetings", "calendar-cache"):
            (self.base / "routines" / f"{rid}.yaml").write_text(
                f"id: {rid}\nschedule:\n  every: 5m\n")
        ts = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(NOW - ago))
        (self.base / "logs" / "run.log").write_text(
            f"{ts(400)} tick: due=slack-mcp, zoom-meetings\n"
            f"{ts(399)} routine=zoom-meetings step ERROR boom\n"
            f"{ts(398)} tick done: 0 processed, 0 already-seen, 1 error(s)\n"
            f"{ts(200)} tick: due=slack-mcp\n"
            f"{ts(199)} tick done: 0 processed, 0 already-seen, 0 error(s)\n")
        (self.base / "state" / "schedule.json").write_text(json.dumps(
            {"slack-mcp": {"last_attempted_epoch": NOW - 200},
             "zoom-meetings": {"last_attempted_epoch": NOW - 400}}))
        (self.base / "state" / "alerts.json").write_text(json.dumps(
            {"attention:zoom-meetings": ts(300)}))
        (self.base / "state" / "claude-usage.jsonl").write_text(
            json.dumps({"ts": ts(100), "total_cost_usd": 1.5,
                        "routine": "slack-mcp", "outcome": "ok",
                        "tools": ["mcp__slackserver__read"]}) + "\n")
        (self.base / "state" / "budget.yaml").write_text("daily_usd: 20\n")

    def test_snapshot(self):
        reader = health.HealthReader(self.base, launchd=False)
        snap = reader.read(NOW)
        self.assertIsNone(snap["error"], snap["error"])
        by_id = {r["id"]: r for r in snap["routines"]}
        self.assertEqual(by_id["slack-mcp"]["state"], "ok")
        self.assertEqual(by_id["zoom-meetings"]["state"], "fail")
        self.assertEqual([a["key"] for a in snap["alerts"]],
                         ["attention:zoom-meetings"])
        self.assertEqual(snap["alerts"][0]["routine"], "zoom-meetings")
        self.assertEqual(snap["spend"]["cap"], 20.0)
        self.assertAlmostEqual(snap["spend"]["total"], 1.5)
        names = {g["name"]: g["state"] for g in snap["groups"]}
        self.assertEqual(names["slack"], "ok")
        self.assertEqual(names["zoom"], "fail")
        self.assertEqual([c["name"] for c in snap["connectors"]],
                         ["slackserver"])

    def test_reading_writes_nothing(self):
        def tree():
            return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                          for p in self.base.rglob("*") if p.is_file())
        before = tree()
        health.HealthReader(self.base, launchd=False).read(NOW)
        self.assertEqual(tree(), before)

    def test_unchanged_files_hit_the_cache(self):
        reader = health.HealthReader(self.base, launchd=False)
        calls = []
        original = reader._collect
        reader._collect = lambda now: calls.append(now) or original(now)
        reader.read(NOW)
        reader.read(NOW + 30)
        self.assertEqual(len(calls), 1)
        # a changed file refreshes it
        with open(self.base / "logs" / "run.log", "a") as fh:
            fh.write("")
            os.utime(fh.name, (NOW + 40, NOW + 40))
        reader.read(NOW + 45)
        self.assertEqual(len(calls), 2)
        # and so does age (launchd state has no file to watch)
        reader.read(NOW + 45 + health.MAX_AGE_S + 1)
        self.assertEqual(len(calls), 3)

    def test_heartbeats_are_checked_on_every_read(self):
        beat = self.base / "hb"
        beat.write_text("x")
        os.utime(beat, (NOW - 60, NOW - 60))
        reader = health.HealthReader(
            self.base, [{"name": "poller", "path": str(beat),
                         "stale_min": 5}], launchd=False)
        self.assertEqual(reader.read(NOW)["heartbeats"][0]["state"], "ok")
        self.assertEqual(reader.read(NOW + 10 * MIN)["heartbeats"][0]["state"],
                         "warn")

    def test_a_broken_base_gives_an_error_not_an_exception(self):
        reader = health.HealthReader(self.base / "nowhere", launchd=False)
        snap = reader.read(NOW)
        self.assertEqual(snap["routines"], [])
        self.assertIsNone(snap["spend"]["total"])

    def test_malformed_routine_file_is_reported(self):
        (self.base / "routines" / "bad.yaml").write_text("- not\n- a mapping\n")
        snap = health.HealthReader(self.base, launchd=False).read(NOW)
        self.assertIn("routines:", snap["error"])


if __name__ == "__main__":
    unittest.main()
