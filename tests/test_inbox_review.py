"""Comms inbox review fixes: reactor parsing, run order, bot-text window,
seen reactions, watched-search details."""
import json
import tempfile
from unittest import mock

from workspace_daemon import claude_bridge, comms_inbox, slack_mcp

from test_comms_inbox import (
    BOB, CAROL, CHAN, DM, ME, NOW, REACTIONS_TOOL, TRACK, InboxTestCase,
    by_filter, inbox_source, message, reactions_text, search_text, ts_at,
)
from test_inbox_gaps import (
    DRIVE, DRIVE_TEXT, READ_CHANNEL, WATCH, WATCH2, drive_history,
)


def raw_reactions(*lines):
    return json.dumps({"result": "Reactions on message:\n\n" + "\n".join(lines)})


def drive_row(ts, text="", user=DRIVE, ch=DM):
    return {
        "type": "message", "channel_id": ch, "thread_ts": ts, "ts": ts,
        "user_id": user, "is_owner": False, "is_bot": True, "text": text,
        "channel_type": "im",
    }


class ReactorParseTest(InboxTestCase):
    def test_normal_multi_reactor_line(self):
        got = comms_inbox.parse_reactors(raw_reactions(
            f":eyes: × 2 — Bob Example ({BOB}), Carol Example ({CAROL})"
        ))
        self.assertEqual(got, [("eyes", [BOB, CAROL])])

    def test_display_name_holding_the_owner_id_fails_closed(self):
        text = raw_reactions(
            f":eyes: × 1 — Dana ({ME}) (U0EXAMPLEG)"
        )
        with self.assertRaises(ValueError):
            comms_inbox.parse_reactors(text)
        with self.assertRaises(ValueError):
            comms_inbox.parse_reactions(text, ME)

    def test_comma_variant_fails_closed(self):
        with self.assertRaises(ValueError):
            comms_inbox.parse_reactors(raw_reactions(
                f":eyes: × 2 — Eve ({ME}) (U0EXAMPLEH), "
                "Mallory (U0EXAMPLEG)"
            ))
        with self.assertRaises(ValueError):
            comms_inbox.parse_reactors(raw_reactions(
                f":eyes: × 1 — Eve ({ME}), Mallory (U0EXAMPLEG)"
            ))

    def test_truncated_list_fails_closed(self):
        with self.assertRaises(ValueError):
            comms_inbox.parse_reactors(raw_reactions(
                f":eyes: × 5 — Bob ({BOB}), Carol ({CAROL})"
            ))

    def test_spoofed_name_never_becomes_an_owner_reaction(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10)},
        ]})
        self.connector.thread = lambda args: (
            json.dumps({"messages": ""}) if "limit" in args
            else raw_reactions(
                f":pray: × 1 — Dana ({ME}) (U0EXAMPLEG)"
            )
        )
        slack_mcp.candidates(inbox_source(
            {"channels": [CHAN]}, reactions_tool=REACTIONS_TOOL,
        ))
        self.assertEqual(self.reactions(), [])


class RunOrderTest(InboxTestCase):
    def test_a_guard_stop_in_the_bot_text_session_keeps_the_search_rows(self):
        table = by_filter({f"in:<#{CHAN}>": [{"ts": ts_at(10)}],
                           f"with:<@{ME}>": [
            {"ch": DM, "dm": True, "people": [DRIVE, ME], "uid": DRIVE,
             "ts": ts_at(20), "text": ""},
        ]})
        inner = self.connector

        def call(prompt, tools, **kw):
            if next(iter(tools)) == READ_CHANNEL:
                raise claude_bridge.PausedError("paused")
            return inner(prompt, tools, **kw)

        inner.search = table
        with mock.patch.object(claude_bridge, "call_tools", call):
            with self.assertRaises(claude_bridge.PausedError):
                slack_mcp.candidates(inbox_source(
                    {"channels": [CHAN], "all_dms": True,
                     "bot_text_users": [DRIVE]},
                    all_dms=True, read_channel_tool=READ_CHANNEL,
                    max_file_reads_per_run=0,
                ))
        self.assertEqual(
            sorted(r["ts"] for r in self.messages()), [ts_at(10), ts_at(20)]
        )


class BotWindowTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.reads = []
        self.page = lambda args: drive_history(args["channel_id"], [])

        def channel(args):
            self.reads.append(args)
            return self.page(args)

        self.connector.channel = channel

    def run_once(self, now=NOW):
        with mock.patch.object(slack_mcp, "_now", return_value=now):
            return slack_mcp.candidates(inbox_source(
                {"all_dms": True, "bot_text_users": [DRIVE]}, channels=[],
                all_dms=True, read_channel_tool=READ_CHANNEL,
                max_file_reads_per_run=0,
            ))

    def seed(self, *rows):
        comms_inbox.record(self.tmp.name, list(rows), 14, now=NOW)

    def test_window_is_bounded_by_the_empty_messages(self):
        self.seed(drive_row(ts_at(10)))
        newer = [(ts_at(100 + n), BOB, "Bob", "chat") for n in range(150)]
        self.page = lambda args: drive_history(DM, [
            (ts_at(10), DRIVE, "Drive", DRIVE_TEXT)] + newer)
        self.run_once()
        self.assertEqual(float(self.reads[0]["oldest"]), float(ts_at(10)) - 1)
        self.assertEqual(float(self.reads[0]["latest"]), float(ts_at(10)) + 1)
        self.assertEqual(self.messages()[0]["text"], DRIVE_TEXT)
        self.assertEqual(len(self.messages()), 1)

    def test_window_shrinks_as_rows_fill(self):
        self.seed(drive_row(ts_at(10)), drive_row(ts_at(400)))
        self.page = lambda args: drive_history(DM, [
            (ts_at(400), DRIVE, "Drive", DRIVE_TEXT)])
        self.run_once()
        self.assertEqual(float(self.reads[0]["latest"]), float(ts_at(400)) + 1)
        self.page = lambda args: drive_history(DM, [
            (ts_at(10), DRIVE, "Drive", DRIVE_TEXT)])
        self.run_once(now=NOW + 60)
        self.assertEqual(float(self.reads[1]["latest"]), float(ts_at(10)) + 1)
        self.assertEqual(float(self.reads[1]["oldest"]), float(ts_at(10)) - 1)

    def test_an_unparsed_page_counts_a_try_for_every_empty_message(self):
        self.seed(drive_row(ts_at(10)), drive_row(ts_at(20)))
        self.page = lambda args: json.dumps({"messages": "garbage"})
        self.run_once()
        self.assertEqual(
            comms_inbox.load_bot_tries(self.tmp.name),
            {f"{DM}:{ts_at(10)}": 1, f"{DM}:{ts_at(20)}": 1},
        )
        for n in range(1, 5):
            self.run_once(now=NOW + n)
        self.assertEqual(len(self.reads), comms_inbox.MAX_BOT_TEXT_TRIES)

    def test_the_dm_with_the_oldest_empty_row_is_read_first(self):
        for n in range(6):
            self.seed(drive_row(ts_at(10 + n), ch=f"D0EXAMPLE1{n}"))
        self.run_once()
        self.assertEqual(
            sorted(a["channel_id"] for a in self.reads),
            [f"D0EXAMPLE1{n}" for n in range(4)],
        )

    def test_missing_tool_is_warned_about_once(self):
        slack_mcp._WARNED_UNCOVERED.discard("bot_text")
        self.seed(drive_row(ts_at(10)))
        with mock.patch.object(slack_mcp, "log") as log:
            for _ in range(2):
                slack_mcp.candidates(inbox_source(
                    {"all_dms": True, "bot_text_users": [DRIVE]}, channels=[],
                    all_dms=True,
                ))
        warned = [c for c in log.call_args_list
                  if "bot_text_users needs" in str(c)]
        self.assertEqual(len(warned), 1)
        self.assertEqual(self.reads, [])


class SeenReactionTest(InboxTestCase):
    def rows_for(self, *extra):
        comms_inbox.record(self.tmp.name, [
            message(ts_at(10), user=BOB), *extra,
        ], 14, now=NOW)

    def due(self, **kw):
        return comms_inbox.due_checks(self.tmp.name, 10, 72, NOW + 600, **kw)

    def owner_reaction(self, emoji, ts=ts_at(10)):
        return {"type": "owner_reaction", "channel_id": CHAN,
                "thread_ts": ts, "ts": ts, "emoji": emoji}

    def test_seen_reaction_does_not_close_polling(self):
        self.rows_for(self.owner_reaction("eyes"))
        self.assertEqual(self.due(), [])  # today's behaviour
        self.assertEqual(len(self.due(seen={"eyes"})), 1)

    def test_a_closing_reaction_still_closes(self):
        self.rows_for(self.owner_reaction("eyes"), self.owner_reaction("pray"))
        self.assertEqual(self.due(seen={"eyes"}), [])

    def test_bot_dm_messages_are_polled_one_by_one(self):
        comms_inbox.record(self.tmp.name, [
            drive_row(ts_at(10), "a"), drive_row(ts_at(20), "b"),
        ], 14, now=NOW)
        self.assertEqual(self.due(), [])
        got = self.due(bot_users={DRIVE})
        self.assertEqual([d["ts"] for d in got], [ts_at(20), ts_at(10)])

    def test_other_bots_stay_unpolled(self):
        comms_inbox.record(self.tmp.name, [
            drive_row(ts_at(10), "a", user="U0EXAMPLEE"),
        ], 14, now=NOW)
        self.assertEqual(self.due(bot_users={DRIVE}), [])

    def test_end_to_end_owner_closes_a_drive_message_after_a_seen_emoji(self):
        comms_inbox.record(self.tmp.name, [
            drive_row(ts_at(10), DRIVE_TEXT),
        ], 14, now=NOW)
        answers = [reactions_text(("eyes", [ME]))]
        self.connector.thread = lambda args: answers[0]

        def run(now):
            with mock.patch.object(slack_mcp, "_now", return_value=now):
                slack_mcp.candidates(inbox_source(
                    {"all_dms": True, "bot_text_users": [DRIVE],
                     "seen_reactions": ["eyes"]}, channels=[], all_dms=True,
                    read_channel_tool=READ_CHANNEL, max_file_reads_per_run=0,
                    reactions_tool=REACTIONS_TOOL,
                ))

        self.connector.channel = lambda args: drive_history(DM, [])
        run(NOW)
        self.assertEqual([r["emoji"] for r in self.reactions()], ["eyes"])
        answers[0] = reactions_text(("eyes", [ME]), ("pray", [ME]))
        run(NOW + 30000)  # past the back-off
        self.assertEqual(
            sorted(r["emoji"] for r in self.reactions()), ["eyes", "pray"]
        )

    def test_validation(self):
        self.assertIsNone(comms_inbox.problem({"seen_reactions": ["eyes"]}))
        for bad in (
            {"seen_reactions": [":eyes:"]},
            {"seen_reactions": ["a", "a"]},
            {"seen_reactions": [TRACK], "track_reactions": [TRACK]},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(comms_inbox.problem(bad))
        self.assertEqual(comms_inbox.settings({})["seen_reactions"], [])


class WatchedDetailsTest(InboxTestCase):
    def cfg(self, **over):
        return slack_mcp.settings(inbox_source(
            {"watch_channels": [WATCH, WATCH2]}, **over
        ))

    def test_a_new_channel_is_searched_apart_from_the_known_ones(self):
        queries = slack_mcp._watched_queries(
            self.cfg(), {WATCH: int(NOW) - 600}, 10, NOW
        )
        by_filter_ = {q["filters"]: q["after"] for q in queries}
        self.assertEqual(by_filter_, {
            f"in:<#{WATCH}>": int(NOW) - 600 - 3600,
            f"in:<#{WATCH2}>": int(NOW) - 14 * 86400,
        })

    def test_capped_watched_search_is_logged(self):
        self.connector.search = lambda a: search_text(
            [{"ts": ts_at(10), "ch": WATCH}], cursor="NEXT"
        ) if a.get("filters") == f"in:<#{WATCH}>" else search_text([])
        with mock.patch.object(slack_mcp, "log") as log:
            slack_mcp.candidates(inbox_source(
                {"watch_channels": [WATCH]}, max_search_pages=1,
            ))
        self.assertTrue(any(
            "watched-channel search hit" in str(c) for c in log.call_args_list
        ))

    def test_unchanged_marks_do_not_rewrite_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(comms_inbox.state, "write_atomic") as write:
                comms_inbox.save_watched(tmp, {}, 14, now=NOW)
                self.assertEqual(write.call_count, 0)
                comms_inbox.save_watched(tmp, {WATCH: int(NOW)}, 14, now=NOW)
                self.assertEqual(write.call_count, 1)
            comms_inbox.save_watched(tmp, {WATCH: int(NOW)}, 14, now=NOW)
            with mock.patch.object(comms_inbox.state, "write_atomic") as write:
                comms_inbox.save_watched(tmp, {WATCH: int(NOW)}, 14, now=NOW)
                self.assertEqual(write.call_count, 0)


NOTE = (
    "Error: result (55,315 characters) exceeds maximum allowed tokens. "
    "Output has been saved to /tmp/example.txt"
)


class OversizedReadTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.reads = []
        self.answer = lambda args: NOTE

        def channel(args):
            self.reads.append(args)
            return self.answer(args)

        self.connector.channel = channel
        comms_inbox.record(self.tmp.name, [
            drive_row(ts_at(10)), drive_row(ts_at(20)),
        ], 14, now=NOW)

    def run_once(self, now=NOW, **over):
        cfg = dict(
            channels=[], all_dms=True, read_channel_tool=READ_CHANNEL,
            max_file_reads_per_run=0,
        )
        cfg.update(over)
        with mock.patch.object(slack_mcp, "_now", return_value=now):
            return slack_mcp.candidates(inbox_source(
                {"all_dms": True, "bot_text_users": [DRIVE]}, **cfg
            ))

    def test_the_note_is_recognised(self):
        self.assertTrue(slack_mcp.oversized_result(NOTE))
        self.assertTrue(slack_mcp.oversized_result("  " + NOTE))
        self.assertTrue(slack_mcp.oversized_result(
            "MCP tool slack_read_channel response (31000 tokens) exceeds "
            "maximum allowed tokens (25000)."
        ))
        self.assertFalse(slack_mcp.oversized_result(
            '{"messages": "exceeds maximum allowed tokens"}'
        ))
        for text in ("", None, "not json", '{"messages": "x"}',
                     "Error: result (3 characters) is fine"):
            self.assertFalse(slack_mcp.oversized_result(text))
        with self.assertRaises(slack_mcp.SlackMcpOversized):
            slack_mcp.parse_channel(NOTE, "1.0", "2.0", DM)

    def test_bot_text_read_uses_a_small_page(self):
        self.answer = lambda a: drive_history(DM, [
            (ts_at(10), DRIVE, "Drive", DRIVE_TEXT),
            (ts_at(20), DRIVE, "Drive", DRIVE_TEXT),
        ])
        self.run_once()
        self.assertEqual(
            [a["limit"] for a in self.reads], [slack_mcp.BOT_TEXT_READ_LIMIT]
        )
        self.assertEqual(slack_mcp.BOT_TEXT_READ_LIMIT, 15)

    def test_oversized_page_is_retried_halved_and_counts_no_try(self):
        self.answer = lambda a: NOTE if a["limit"] > 7 else drive_history(DM, [
            (ts_at(10), DRIVE, "Drive", DRIVE_TEXT),
            (ts_at(20), DRIVE, "Drive", DRIVE_TEXT),
        ])
        self.run_once()
        self.assertEqual([a["limit"] for a in self.reads], [15, 7])
        self.assertEqual(len(self.messages()), 2)
        self.assertTrue(all(r["text"] for r in self.messages()))
        self.assertEqual(comms_inbox.load_bot_tries(self.tmp.name), {})

    def test_always_oversized_stops_after_three_and_counts_one_try(self):
        self.run_once()
        self.assertEqual([a["limit"] for a in self.reads], [15, 7, 3])
        # One try per empty message after the last, smallest read.
        self.assertEqual(
            comms_inbox.load_bot_tries(self.tmp.name),
            {f"{DM}:{ts_at(10)}": 1, f"{DM}:{ts_at(20)}": 1},
        )
        self.assertEqual([r["text"] for r in self.messages()], ["", ""])
        for n in range(1, 6):
            self.run_once(now=NOW + n)
        self.assertEqual(len(self.reads), 9)  # 3 sessions x 3 tries, then none

    def test_file_scan_backs_off_and_keeps_its_mark(self):
        with mock.patch.object(slack_mcp, "log") as log:
            slack_mcp.candidates(inbox_source(
                {"all_dms": True}, channels=[], all_dms=True,
                read_channel_tool=READ_CHANNEL,
            ))
        saved = slack_mcp._load_file_state()
        self.assertNotIn(DM, saved["marks"])
        self.assertEqual(saved["fails"][DM]["n"], 1)
        self.assertTrue(any(
            "client size limit" in str(c) for c in log.call_args_list
        ))
