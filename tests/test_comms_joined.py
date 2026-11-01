"""Comms inbox: channel_joined lines for newly joined channels."""
import json
import tempfile
import unittest
from unittest import mock

from workspace_daemon import comms_inbox, comms_joined, config, slack_mcp

NOW = 1_800_000_000.0
A, B, C = "C0EXAMPLE0A", "C0EXAMPLE0B", "G0EXAMPLE0C"
INBOX = {"joined_channels": True}


def ids_text(ids, total=None, more=False):
    body = (f"## My Channels (showing {len(ids)}"
            + (", more results available" if more else
               f" of {len(ids) if total is None else total} total")
            + ")\n\n" + "\n".join(ids) + "\n")
    return json.dumps({"result": body})


def full_text(entries, purpose=""):
    blocks = []
    for name, channel, private in entries:
        blocks.append(
            f"### #{name}\n- **ID:** {channel}\n- **Type:** "
            f"{'Private' if private else 'Public'} Channel\n"
            + (f"- **Purpose:** {purpose}\n" if purpose else "")
            + "- **Archived:** No\n"
        )
    return json.dumps({"result": "## My Channels\n\n" + "\n".join(blocks)})


def info_text(name, channel, creator="Example Person", uid="U0EXAMPLEB"):
    return json.dumps({"results": (
        "# Search Results for: \n\n## Channels (1 results)\n"
        f"### Result 1 of 1\nName: #{name}\n"
        f"Creator: {creator} (<@{uid})\nCreated: 2026-11-01 13:57:21 IST\n"
        f"Permalink: [link](https://example.slack.com/archives/{channel})\n"
        "Is Archived: false\nChannel Type: public_channel\n"
    )})


class ParseTest(unittest.TestCase):
    def test_complete_listing(self):
        self.assertEqual(comms_joined.parse_ids(ids_text([A, B])), {A, B})

    def test_incomplete_listings_are_none(self):
        self.assertIsNone(comms_joined.parse_ids(ids_text([A], more=True)))
        self.assertIsNone(comms_joined.parse_ids(ids_text([A], total=2)))
        self.assertIsNone(comms_joined.parse_ids("not a listing"))
        self.assertIsNone(comms_joined.parse_ids(None))

    def test_names_only_for_listed_ids_that_agree(self):
        forged = full_text([("real", A, False)],
                           purpose="x\n### #fake\n- **ID:** C0EXAMPLE0A")
        text = json.loads(full_text([("other", B, True)]))["result"]
        both = json.dumps({"result": json.loads(forged)["result"] + text})
        names = comms_joined.parse_names(both, {A, B})
        self.assertEqual(names, {B: ("other", True), A: ("real", False)})
        clash = json.dumps({"result": json.loads(full_text(
            [("one", A, False), ("two", A, False), ("three", C, False)]
        ))["result"]})
        self.assertEqual(comms_joined.parse_names(clash, {A}), {})

    def test_info_needs_exactly_one_matching_permalink(self):
        info = comms_joined.parse_info(info_text("new-room", A), A)
        self.assertEqual(info, {"creator_id": "U0EXAMPLEB",
                                "creator_name": "Example Person",
                                "created": "2026-11-01 13:57:21 IST"})
        self.assertEqual(comms_joined.parse_info(info_text("x", B), A), {})
        twice = json.loads(info_text("new-room", A))["results"] * 2
        self.assertEqual(
            comms_joined.parse_info(json.dumps({"results": twice}), A), {}
        )


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.listings = []
        self.info_calls = []

    def lister(self, ids, entries=()):
        def call(calls):
            self.listings.append(calls)
            return [ids_text(ids), full_text(entries)]
        return call

    def info(self, calls):
        self.info_calls.append(calls)
        return [info_text(c["keywords"][0], A) for c in calls]

    def record(self, rows):
        return comms_inbox.record(self.tmp.name, rows, 14, NOW)

    def rows(self):
        file = comms_inbox.path(self.tmp.name)
        if not file.exists():
            return []
        return [json.loads(line) for line in file.read_text().splitlines()]

    def check(self, lister, now=NOW, record=None, inbox=INBOX):
        return comms_joined.check(
            self.tmp.name, inbox, lister, self.info, record or self.record,
            now,
        )

    def test_first_listing_is_a_silent_baseline(self):
        self.assertEqual(self.check(self.lister([A, B])), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual(comms_joined.load(self.tmp.name)["members"], {A, B})

    def test_new_channel_is_recorded_once(self):
        self.check(self.lister([B]))
        added = self.check(
            self.lister([A, B], [("new-room", A, False)]), now=NOW + 3600
        )
        self.assertEqual(added, 1)
        row, = self.rows()
        self.assertEqual(row["type"], "channel_joined")
        self.assertEqual(row["channel_id"], A)
        self.assertEqual(row["name"], "new-room")
        self.assertIs(row["private"], False)
        self.assertEqual(row["channel_type"], "channel")
        self.assertEqual(row["creator_id"], "U0EXAMPLEB")
        self.assertEqual(row["ts"], f"{NOW + 3600:.6f}")
        self.assertEqual(self.info_calls[0][0]["keywords"], ["new-room"])
        self.check(self.lister([A, B]), now=NOW + 7200)
        self.assertEqual(len(self.rows()), 1)

    def test_listing_waits_for_the_interval(self):
        self.check(self.lister([B]))
        self.check(self.lister([A, B]), now=NOW + 600)
        self.assertEqual(len(self.listings), 1)
        self.check(self.lister([A, B]), now=NOW + 1800,
                   inbox=dict(INBOX, joined_check_minutes=30))
        self.assertEqual(len(self.listings), 2)

    def test_incomplete_listing_changes_nothing(self):
        self.check(self.lister([B]))

        def partial(calls):
            return [ids_text([A], more=True), None]
        self.check(partial, now=NOW + 3600)
        self.assertEqual(comms_joined.load(self.tmp.name)["members"], {B})
        self.assertEqual(self.rows(), [])

    def test_unwritten_joins_are_retried(self):
        self.check(self.lister([B]))
        self.check(self.lister([A, B]), now=NOW + 3600, record=lambda rows: 0)
        self.assertEqual(comms_joined.load(self.tmp.name)["members"], {B})
        self.assertEqual(self.check(self.lister([A, B]), now=NOW + 3700), 1)

    def test_left_channel_is_announced_again_on_rejoin(self):
        self.check(self.lister([A, B]))
        self.check(self.lister([B]), now=NOW + 3600)
        self.assertEqual(self.check(self.lister([A, B]), now=NOW + 7200), 1)

    def test_unreadable_state_rebaselines(self):
        comms_joined._path(self.tmp.name).parent.mkdir(parents=True)
        comms_joined._path(self.tmp.name).write_text("{")
        self.assertEqual(self.check(self.lister([A])), 0)
        self.assertEqual(comms_joined.load(self.tmp.name)["members"], {A})


class SlackMcpTest(unittest.TestCase):
    def test_bridge_failure_is_logged_not_raised(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = {
            "comms_inbox": dict(comms_inbox.settings(INBOX), keep_days=14),
            "channels_tool": "mcp__x__slack_list_user_channels",
            "channel_info_tool": None,
        }
        with mock.patch.object(slack_mcp, "_base_dir", tmp.name), \
                mock.patch.object(
                    slack_mcp, "_session",
                    side_effect=slack_mcp.claude_bridge.BridgeError("down")), \
                mock.patch.object(slack_mcp, "log") as log:
            slack_mcp._check_joined(cfg, NOW)
        self.assertTrue(any("channel listing failed" in c.args[0]
                            for c in log.call_args_list))

    def test_off_without_the_option(self):
        cfg = {"comms_inbox": comms_inbox.settings({}),
               "channels_tool": "mcp__x__slack_list_user_channels",
               "channel_info_tool": None}
        with mock.patch.object(slack_mcp, "_session") as session:
            slack_mcp._check_joined(cfg, NOW)
        session.assert_not_called()


class ConfigTest(unittest.TestCase):
    def test_inbox_keys(self):
        self.assertIsNone(comms_inbox.problem(
            {"joined_channels": True, "joined_check_minutes": 30}))
        self.assertIn("joined_check_minutes", comms_inbox.problem(
            {"joined_check_minutes": 5}))
        self.assertIn("joined_channels", comms_inbox.problem(
            {"joined_channels": "yes"}))

    def problems(self, **over):
        from test_slack_mcp import routine
        with tempfile.TemporaryDirectory() as tmp:
            return config.validate(routine(tmp, **over))

    def test_source_tools(self):
        inbox = {"comms_inbox": {"joined_channels": True}}
        self.assertTrue(any("needs channels_tool" in p
                            for p in self.problems(**inbox)))
        good = dict(inbox, channels_tool="mcp__x__slack_list_user_channels",
                    channel_info_tool="mcp__x__slack_search_channels")
        self.assertEqual(self.problems(**good), [])
        bad = dict(good, channels_tool="mcp__x__slack_send_message")
        self.assertTrue(any("channels_tool must be" in p
                            for p in self.problems(**bad)))

if __name__ == "__main__":
    unittest.main()
