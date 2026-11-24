"""A saved top-level channel message is read with the messages before it."""
import unittest
from unittest import mock

from workspace_daemon import claude_bridge, config, slack_mcp, slack_watch

from test_slack_dm_sessions import CHANNEL_TOOL, channel_text, dm_hit
from test_slack_mcp import (
    CHAN, LATE, ME, ROOT, BOB, BridgeTestCase, search_text, thread_text, ts_at,
)
from test_slack_save_reaction import EMOJI, saved_search

SAVED = ts_at(5000)
EARLIER = [
    (ts_at(4000), BOB, "Bob Example", "Did anyone size the example rollout?"),
    (ts_at(4100), ME, "Me Example", "Question about the example rollout cost"),
]


def history(messages, channel=CHAN):
    return channel_text(messages, channel=channel, label="#example")


def ctx_source(**over):
    from test_slack_mcp import source
    cfg = {"save_reactions": [EMOJI], "read_channel_tool": CHANNEL_TOOL}
    cfg.update(over)
    return source(**cfg)


class ContextCase(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.channel_calls = []

        def channel(args):
            self.channel_calls.append(dict(args))
            return history(EARLIER, args["channel_id"])

        self.connector.channel = channel
        self.connector.thread = lambda a: thread_text(
            a["message_ts"], parent_text="Use the second option."
        )

    def listing(self, hits, **over):
        self.connector.search = saved_search(hits)
        return slack_mcp.candidates(ctx_source(**over))

    def fetch(self, candidate, **over):
        return slack_mcp.fetch({"id": "r"}, candidate, ctx_source(**over))


class UnthreadedSavedTest(ContextCase):
    def test_reads_the_messages_just_before_the_saved_one(self):
        listing = self.listing([{"ts": SAVED}])
        self.assertTrue(listing[0]["raw"]["context_wanted"])
        item = self.fetch(listing[0])
        self.assertEqual(len(self.channel_calls), 1)
        call = self.channel_calls[0]
        self.assertEqual(call["channel_id"], CHAN)
        self.assertEqual(call["latest"], SAVED)
        self.assertEqual(float(call["oldest"]), float(SAVED) - 24 * 3600)
        self.assertEqual(call["limit"], 5)
        body = item["body"]
        self.assertIn("Preceding channel context", body)
        self.assertIn("Did anyone size the example rollout?", body)
        self.assertLess(
            body.index("Question about the example rollout cost"),
            body.index("Use the second option."),
        )
        self.assertLess(
            body.index("End of preceding context"),
            body.index("Use the second option."),
        )
        self.assertEqual(item["frontmatter"]["slack_context_messages"], 2)

    def test_the_saved_message_stays_the_anchor(self):
        listing = self.listing([{"ts": SAVED}])
        item = self.fetch(listing[0])
        self.assertEqual(listing[0]["id"], f"saved:slack:{CHAN}:{SAVED}")
        self.assertEqual(item["source_id"], f"slack:{CHAN}:{SAVED}")
        self.assertEqual(item["id"], listing[0]["id"])
        self.assertEqual(item["frontmatter"]["slack_thread_ts"], SAVED)
        self.assertTrue(item["title"].startswith("Use the second option"))
        self.assertIs(item["frontmatter"]["slack_saved"], True)

    def test_context_never_adds_owner_evidence(self):
        listing = self.listing([{"ts": SAVED}])
        item = self.fetch(listing[0])
        self.assertEqual(
            item["frontmatter"]["slack_owner_evidence"], ["saved"]
        )

    def test_settings_change_the_window(self):
        listing = self.listing(
            [{"ts": SAVED}], saved_context_messages=2, saved_context_hours=1,
        )
        self.fetch(listing[0], saved_context_messages=2, saved_context_hours=1)
        call = self.channel_calls[0]
        self.assertEqual(call["limit"], 2)
        self.assertEqual(float(call["oldest"]), float(SAVED) - 3600)

    def test_more_messages_than_the_limit_keep_the_newest(self):
        many = [
            (ts_at(3000 + n), BOB, "Bob Example", f"earlier message {n}")
            for n in range(6)
        ]
        self.connector.channel = lambda a: history(many)
        listing = self.listing([{"ts": SAVED}], saved_context_messages=3)
        item = self.fetch(listing[0], saved_context_messages=3)
        self.assertEqual(item["frontmatter"]["slack_context_messages"], 3)
        self.assertIn("earlier message 5", item["body"])
        self.assertNotIn("earlier message 2", item["body"])

    def test_off_without_the_tool_or_with_zero_messages(self):
        for over in ({"read_channel_tool": None}, {"saved_context_messages": 0}):
            with self.subTest(over=over):
                listing = self.listing([{"ts": SAVED}], **over)
                self.assertNotIn("context_wanted", listing[0]["raw"])
                item = self.fetch(listing[0], **over)
                self.assertNotIn("Preceding", item["body"])
        self.assertEqual(self.channel_calls, [])

    def test_bot_messages_follow_include_bots(self):
        bot = [(ts_at(4200), "B0EXAMPLE1", "Example App", "app notice")]
        self.connector.channel = lambda a: history(EARLIER + bot)
        listing = self.listing([{"ts": SAVED}])
        item = self.fetch(listing[0])
        self.assertNotIn("app notice", item["body"])

    def test_context_is_bounded_like_a_thread(self):
        long = [(ts_at(4000 + n), BOB, "Bob Example", "x" * 400)
                for n in range(4)]
        self.connector.channel = lambda a: history(long)
        over = {"max_thread_chars": 500}
        listing = self.listing([{"ts": SAVED}], **over)
        item = self.fetch(listing[0], **over)
        self.assertLessEqual(item["frontmatter"]["slack_context_messages"], 2)
        self.assertIn("[earlier messages omitted]", item["body"])

    def test_two_saved_messages_share_one_context_session(self):
        other = ts_at(6000)
        listing = self.listing([{"ts": SAVED}, {"ts": other}])
        self.assertEqual(len(listing), 2)
        for candidate in listing:
            self.fetch(candidate)
        self.assertEqual(len(self.channel_calls), 2)
        sessions = [
            p for p, (tools, _kw) in zip(
                self.connector.prompts, self.connector.calls
            ) if tools[0] == CHANNEL_TOOL
        ]
        self.assertEqual(len(sessions), 1)

    def test_an_unplanned_call_result_is_never_used(self):
        self.connector.skip = lambda tool, args, n: tool == CHANNEL_TOOL
        self.connector.extra = [{
            "channel_id": CHAN, "latest": ts_at(9000),
            "oldest": ts_at(1), "limit": 5,
        }]
        listing = self.listing([{"ts": SAVED}])
        self.connector.extra = []
        item = self.fetch(listing[0])
        self.assertNotIn("Preceding", item["body"])


class ContextFailureTest(ContextCase):
    def capture_without_context(self, **kw):
        listing = self.listing([{"ts": SAVED}])
        with mock.patch.object(slack_mcp, "log") as log:
            item = self.fetch(listing[0])
        self.assertNotIn("Preceding", item["body"])
        self.assertIn("Use the second option.", item["body"])
        self.assertNotIn("slack_context_messages", item["frontmatter"])
        self.assertTrue(any(
            "WARN" in c.args[0] and "context" in c.args[0]
            for c in log.call_args_list
        ))
        return item

    def test_a_failed_read_still_captures(self):
        self.connector.errors = lambda tool, args: tool == CHANNEL_TOOL
        self.capture_without_context()

    def test_an_unparseable_result_still_captures(self):
        self.connector.channel = lambda a: "not json"
        self.capture_without_context()

    def test_a_session_outage_still_captures(self):
        def boom(prompt, tools, **kw):
            if CHANNEL_TOOL in tools:
                raise claude_bridge.BridgeError("session down")
            return self.connector(prompt, tools, **kw)

        listing = self.listing([{"ts": SAVED}])
        with mock.patch.object(claude_bridge, "call_tools", boom), \
                mock.patch.object(slack_mcp, "log"):
            item = self.fetch(listing[0])
        self.assertNotIn("Preceding", item["body"])

    def test_a_budget_stop_is_not_swallowed(self):
        def stop(prompt, tools, **kw):
            if CHANNEL_TOOL in tools:
                raise claude_bridge.PausedError("paused")
            return self.connector(prompt, tools, **kw)

        listing = self.listing([{"ts": SAVED}])
        with mock.patch.object(claude_bridge, "call_tools", stop):
            with self.assertRaises(claude_bridge.PausedError):
                self.fetch(listing[0])


class ScopeTest(ContextCase):
    def test_saved_thread_reply_reads_from_the_thread_root(self):
        listing = self.listing([{"ts": LATE, "root": ROOT}])
        self.assertEqual(listing[0]["id"], f"saved:slack:{CHAN}:{ROOT}")
        self.assertNotIn("context_wanted", listing[0]["raw"])
        seen = []

        def thread(args):
            seen.append(args["message_ts"])
            return thread_text(
                args["message_ts"], parent_text="Thread root text",
                replies=[(LATE, BOB, "The saved reply")],
            )

        self.connector.thread = thread
        item = self.fetch(listing[0])
        self.assertEqual(seen, [ROOT])
        self.assertEqual(self.channel_calls, [])
        self.assertEqual(item["frontmatter"]["slack_thread_ts"], ROOT)
        self.assertIn("Thread root text", item["body"])
        self.assertIn("The saved reply", item["body"])
        self.assertNotIn("Preceding", item["body"])

    def test_a_root_with_replies_is_a_thread_not_a_lone_message(self):
        listing = self.listing([{"ts": ROOT, "replies": 2}])
        self.assertNotIn("context_wanted", listing[0]["raw"])
        self.fetch(listing[0])
        self.assertEqual(self.channel_calls, [])

    def test_replies_found_on_read_cancel_the_context_read(self):
        listing = self.listing([{"ts": ROOT}])
        self.assertTrue(listing[0]["raw"]["context_wanted"])
        self.connector.thread = lambda a: thread_text(
            a["message_ts"], replies=[(LATE, BOB, "late reply")],
        )
        item = self.fetch(listing[0])
        self.assertEqual(self.channel_calls, [])
        self.assertNotIn("Preceding", item["body"])

    def test_a_direct_message_is_left_as_it_was(self):
        for sessions in (None, {"enabled": True}):
            with self.subTest(sessions=sessions):
                over = {"all_dms": True}
                if sessions:
                    over["dm_sessions"] = sessions
                listing = self.listing(
                    [dm_hit(100, ch="D0EXAMPLE03")], channels=[], **over
                )
                self.assertEqual(len(listing), 1)
                self.assertNotIn("context_wanted", listing[0]["raw"])
                if sessions:
                    # the session reader (its own channel read) owns DMs
                    self.assertTrue(listing[0]["raw"]["dm_session"])
                else:
                    self.fetch(listing[0], **over)
                    self.assertEqual(self.channel_calls, [])

    def test_a_group_dm_is_left_as_it_was(self):
        listing = self.listing(
            [{"ts": SAVED, "label": "Group DM", "ch": "G0EXAMPLE04"}],
            all_dms=True,
        )
        self.assertNotIn("context_wanted", listing[0]["raw"])

    def test_an_excluded_channel_is_never_read(self):
        listing = self.listing([{"ts": SAVED}], exclude_channels=[CHAN])
        self.assertEqual(listing, [])
        self.assertEqual(self.channel_calls, [])

    def test_ordinary_unsaved_messages_are_not_read_for_context(self):
        self.connector.search = lambda a: search_text(
            [] if (a.get("filters") or "").startswith("hasmy::")
            else [{"ts": SAVED}]
        )
        listing = slack_mcp.candidates(ctx_source(settle_minutes=0))
        item = self.fetch(listing[0])
        self.assertEqual(self.channel_calls, [])
        self.assertNotIn("Preceding", item["body"])


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        cfg = slack_mcp.settings({})
        self.assertEqual(cfg["saved_context_messages"], 5)
        self.assertEqual(cfg["saved_context_hours"], 24)

    def problems(self, **over):
        rid = {
            "id": "r", "source": {
                "kind": "slack_mcp", "channels": [CHAN],
                "search_tool": "mcp__s__slack_search_public_and_private",
                "read_thread_tool": "mcp__s__slack_read_thread",
                "self_user_id": ME, **over,
            },
            "analyze": {"provider": "claude", "model": "haiku",
                        "instruction": "x"},
            "memory": {"store": "/store"},
        }
        return [p for p in config.validate(rid) if "saved_context" in p]

    def test_source_validation(self):
        self.assertEqual(self.problems(saved_context_messages=3), [])
        self.assertEqual(self.problems(saved_context_messages=0), [])
        for over in (
            {"saved_context_messages": 21}, {"saved_context_messages": -1},
            {"saved_context_hours": 0}, {"saved_context_hours": 169},
            {"saved_context_hours": True},
        ):
            with self.subTest(over=over):
                self.assertEqual(len(self.problems(**over)), 1)

    def test_watch_file_keys(self):
        merged, error = None, None
        with mock.patch.object(slack_watch, "load", return_value={
            "channels": [], "saved_context_messages": 3,
            "saved_context_hours": 6, "bootstrap": "3d",
        }):
            merged, error = slack_watch.expand(
                {"kind": "slack_mcp", "watch": "~/w.yaml"}
            )
            self.assertIsNone(error)
            self.assertEqual(merged["saved_context_messages"], 3)
            self.assertEqual(merged["saved_context_hours"], 6)
            inline, _ = slack_watch.expand({
                "kind": "slack_mcp", "watch": "~/w.yaml",
                "saved_context_messages": 9,
            })
            self.assertEqual(inline["saved_context_messages"], 9)
            slack, _ = slack_watch.expand({"kind": "slack", "watch": "~/w.yaml"})
            self.assertNotIn("saved_context_messages", slack)

    def test_watch_file_parse(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.yaml"
            path.write_text(
                "channels: [C0EXAMPLE01]\nsaved_context_messages: 4\n"
                "saved_context_hours: 12\n"
            )
            path.chmod(0o600)
            loaded = slack_watch.load(str(path))
            self.assertEqual(loaded["saved_context_messages"], 4)
            self.assertEqual(loaded["saved_context_hours"], 12)
            path.write_text("saved_context_hours: 500\n")
            with self.assertRaises(slack_watch.WatchError):
                slack_watch.load(str(path))
            path.write_text("channels: [C0EXAMPLE01]\n")
            self.assertNotIn(
                "saved_context_messages", slack_watch.load(str(path))
            )


if __name__ == "__main__":
    unittest.main()
