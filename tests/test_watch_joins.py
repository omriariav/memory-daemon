import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from workspace_daemon import (
    comms_inbox, comms_joined, config, slack_mcp, slack_watch, watch_joins,
)

from test_comms_joined import INBOX, LANE, NOW, full_text, ids_text

A, B, C, D = "C0EXAMPLE0A", "C0EXAMPLE0B", "C0EXAMPLE0C", "C0EXAMPLE0D"
TODAY = datetime.date(2026, 11, 2)


def joined_row(channel, name=None):
    return {"type": "channel_joined", "channel_id": channel, "name": name,
            "ts": "1.0", "thread_ts": "1.0", "channel_type": "channel"}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.joined = self.dir / "joined.yaml"
        self.watch = self.dir / "watch.yaml"
        self.write_watch()

    def write_watch(self, **extra):
        data = {"channels": [A], "ignore_channels": [B],
                "joined_from": str(self.joined)}
        data.update(extra)
        self.watch.write_text(yaml.safe_dump(data))
        os.chmod(self.watch, 0o600)

    def lists(self):
        return slack_watch.read_joined(self.joined)

    def ids(self, key):
        return [e["id"] for e in self.lists()[key]]


class SuggestTest(Base):
    def test_uncovered_join_is_suggested_and_flagged(self):
        rows = [joined_row(C, "new-room")]
        self.assertEqual(watch_joins.suggest(self.watch, rows, TODAY), 1)
        self.assertIs(rows[0]["watch_suggested"], True)
        self.assertEqual(self.lists()["suggested"],
                         [{"id": C, "name": "new-room", "joined": TODAY}])
        self.assertEqual(os.stat(self.joined).st_mode & 0o777, 0o600)

    def test_watched_ignored_and_known_channels_are_skipped(self):
        slack_watch.write_joined(self.joined, {"dismissed": [{"id": D}]})
        rows = [joined_row(A), joined_row(B), joined_row(D)]
        self.assertEqual(watch_joins.suggest(self.watch, rows, TODAY), 0)
        self.assertFalse(any("watch_suggested" in r for r in rows))
        self.assertEqual(self.ids("suggested"), [])

    def test_a_suggestion_is_made_once(self):
        watch_joins.suggest(self.watch, [joined_row(C)], TODAY)
        rows = [joined_row(C)]
        self.assertEqual(watch_joins.suggest(self.watch, rows, TODAY), 0)
        self.assertNotIn("watch_suggested", rows[0])
        self.assertEqual(self.ids("suggested"), [C])

    def test_discovered_channel_is_covered(self):
        found = self.dir / "found.yaml"
        slack_watch.write_discovered(found, [
            {"id": C, "last_active": TODAY, "reason": "posted"}])
        self.write_watch(discover_from=str(found))
        self.assertEqual(
            watch_joins.suggest(self.watch, [joined_row(C)], TODAY), 0)

    def test_watch_without_joined_from_is_logged_not_raised(self):
        self.watch.write_text(yaml.safe_dump({"channels": [A]}))
        rows = [joined_row(C)]
        with mock.patch.object(watch_joins, "log") as log:
            self.assertEqual(watch_joins.suggest(self.watch, rows, TODAY), 0)
        self.assertIn("joined_from", log.call_args.args[0])
        self.assertNotIn("watch_suggested", rows[0])

    def test_corrupt_joined_file_is_left_alone(self):
        self.joined.write_text("suggested: nope\n")
        with mock.patch.object(watch_joins, "log"):
            self.assertEqual(
                watch_joins.suggest(self.watch, [joined_row(C)], TODAY), 0)
        self.assertEqual(self.joined.read_text(), "suggested: nope\n")

    def test_write_failure_leaves_rows_unflagged(self):
        rows = [joined_row(C)]
        with mock.patch.object(slack_watch, "write_joined",
                               side_effect=OSError("disk full")), \
                mock.patch.object(watch_joins, "log"):
            self.assertEqual(watch_joins.suggest(self.watch, rows, TODAY), 0)
        self.assertNotIn("watch_suggested", rows[0])


class ReviewTest(Base):
    def setUp(self):
        super().setUp()
        watch_joins.suggest(self.watch, [joined_row(C, "new-room"),
                                         joined_row(D)], TODAY)

    def test_approved_channel_is_watched(self):
        later = TODAY + datetime.timedelta(days=1)
        self.assertEqual(watch_joins.approve(self.watch, C, later),
                         f"{C} watched")
        self.assertEqual(self.lists()["channels"],
                         [{"id": C, "name": "new-room", "approved": later}])
        self.assertEqual(self.ids("suggested"), [D])
        self.assertEqual(slack_watch.load(self.watch)["channels"], [A, C])

    def test_dismissed_channel_is_neither_watched_nor_suggested(self):
        self.assertEqual(watch_joins.dismiss(self.watch, D, TODAY),
                         f"{D} dismissed")
        self.assertEqual(self.lists()["dismissed"], [{"id": D}])
        self.assertEqual(slack_watch.load(self.watch)["channels"], [A])
        rows = [joined_row(D)]
        watch_joins.suggest(self.watch, rows, TODAY)
        self.assertNotIn("watch_suggested", rows[0])

    def test_dismiss_stops_watching_an_approved_channel(self):
        watch_joins.approve(self.watch, C, TODAY)
        watch_joins.dismiss(self.watch, C, TODAY)
        self.assertEqual(slack_watch.load(self.watch)["channels"], [A])

    def test_repeat_and_unknown(self):
        watch_joins.approve(self.watch, C, TODAY)
        self.assertEqual(watch_joins.approve(self.watch, C, TODAY),
                         f"{C} is already watched")
        with self.assertRaises(watch_joins.JoinedError):
            watch_joins.approve(self.watch, "C0EXAMPLE0Z", TODAY)

    def test_ignored_channel_is_not_merged_even_if_approved(self):
        slack_watch.write_joined(self.joined, {"channels": [
            {"id": B, "approved": TODAY}]})
        self.assertEqual(slack_watch.load(self.watch)["channels"], [A])

    def test_listing(self):
        watch_joins.approve(self.watch, C, TODAY)
        lines = watch_joins.listing(self.watch)
        self.assertEqual(lines[0], "suggested (1):")
        self.assertIn(f"  {D}  #?  2026-11-02", lines)
        self.assertIn(f"  {C}  #new-room  2026-11-02", lines)


class JoinedFileTest(Base):
    def test_missing_file_is_empty(self):
        self.assertEqual(self.lists(),
                         {"suggested": [], "channels": [], "dismissed": []})

    def test_malformed_files_raise(self):
        for text in (
            "other: []\n",
            "suggested: [{id: nope, joined: 2026-11-02}]\n",
            f"suggested: [{{id: {C}}}]\n",
            f"channels: [{{id: {C}, approved: 2026-11-02, extra: 1}}]\n",
            f"suggested: [{{id: {C}, joined: 2026-11-02}}]\n"
            f"dismissed: [{{id: {C}}}]\n",
            f"dismissed: [{{id: {C}, name: 'bad name'}}]\n",
        ):
            self.joined.write_text(text)
            with self.assertRaises(slack_watch.WatchError, msg=text):
                self.lists()

    def test_corrupt_file_adds_nothing_to_the_watch(self):
        self.joined.write_text("channels: 3\n")
        with mock.patch.object(slack_watch, "log"):
            self.assertEqual(slack_watch.load(self.watch)["channels"], [A])

    def test_joined_from_must_be_a_path(self):
        self.write_watch(joined_from=3)
        with self.assertRaises(slack_watch.WatchError):
            slack_watch.load(self.watch)


class CheckTest(Base):
    def test_check_flags_suggested_rows_in_the_inbox(self):
        base = str(self.dir)
        record = lambda rows: comms_inbox.record(base, rows, 14, NOW)

        def lister(ids, entries=()):
            return lambda calls: [
                ids_text(ids) if c["format"] == "ids_only"
                else full_text(entries) for c in calls]

        suggest = lambda rows: watch_joins.suggest(self.watch, rows, TODAY)
        comms_joined.check(base, LANE, INBOX, lister([A]), None, record,
                           NOW, suggest)
        comms_joined.check(base, LANE, INBOX,
                           lister([A, C], [("new-room", C, False)]), None,
                           record, NOW + 3600, suggest)
        row, = [json.loads(line) for line in
                comms_inbox.path(base).read_text().splitlines()]
        self.assertEqual(row["channel_id"], C)
        self.assertIs(row["watch_suggested"], True)
        self.assertEqual(self.ids("suggested"), [C])

    def test_slack_mcp_passes_suggest_watch(self):
        cfg = {
            "comms_inbox": dict(INBOX, keep_days=14,
                                suggest_watch=str(self.watch)),
            "channels_tool": "mcp__x__slack_list_user_channels",
            "channel_info_tool": None,
        }
        with mock.patch.object(slack_mcp, "_base_dir", str(self.dir)), \
                mock.patch.object(comms_joined, "check") as check:
            slack_mcp._check_joined(cfg, NOW, LANE)
        suggest = check.call_args.args[7]
        rows = [joined_row(C)]
        suggest(rows)
        self.assertIs(rows[0]["watch_suggested"], True)


class ConfigTest(unittest.TestCase):
    def test_suggest_watch_must_be_a_path(self):
        self.assertIsNone(comms_inbox.problem(
            {"joined_channels": True, "suggest_watch": "~/watch.yaml"}))
        for bad in ("", "relative.yaml", 3):
            self.assertIn("suggest_watch", comms_inbox.problem(
                {"suggest_watch": bad}))

    def routines(self, tmp, suggest, joined_from=True, joined=True):
        target = Path(tmp) / "channels.yaml"
        target.write_text(yaml.safe_dump(
            {"channels": [A]} | (
                {"joined_from": str(Path(tmp) / "j.yaml")}
                if joined_from else {})))
        direct = {"id": "direct", "source": {
            "kind": "slack_mcp",
            "comms_inbox": {"joined_channels": joined,
                            "suggest_watch": suggest}}}
        channels = {"id": "channels", "source": {
            "kind": "slack_mcp", "watch": str(target),
            "_watch_joined_from": "x" if joined_from else None}}
        return [direct, channels], str(target)

    def test_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            routines, target = self.routines(tmp, None)
            routines[0]["source"]["comms_inbox"]["suggest_watch"] = target
            self.assertEqual(config.joined_warnings(routines), [])
            routines, target = self.routines(tmp, None, joined_from=False)
            routines[0]["source"]["comms_inbox"]["suggest_watch"] = target
            self.assertIn("joined_from", config.joined_warnings(routines)[0])
            routines, target = self.routines(tmp, None, joined=False)
            routines[0]["source"]["comms_inbox"]["suggest_watch"] = target
            self.assertIn("joined_channels",
                          config.joined_warnings(routines)[0])


if __name__ == "__main__":
    unittest.main()
