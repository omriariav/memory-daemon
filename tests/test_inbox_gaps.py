"""Comms inbox: watched channels, bot text, and who a reaction belongs to."""
import json
import tempfile
from unittest import mock

from workspace_daemon import comms_inbox, config, slack_mcp, slack_watch

from test_comms_inbox import (
    ACK, BOB, CAROL, CHAN, CHAN2, DM, ME, NOW, REACTIONS_TOOL,
    SAVE, TRACK, TRACK_FILTER, InboxTestCase, by_filter, inbox_source,
    message, reactions_text, routine, search_text, ts_at,
)

WATCH = "C0EXAMPLE07"
WATCH2 = "C0EXAMPLE08"
DRIVE = "U0EXAMPLED"
OTHER_APP = "U0EXAMPLEE"
READ_CHANNEL = "mcp__example_slack__slack_read_channel"
STAMP = "2026-01-07 09:13:46 IDT"

DRIVE_TEXT = (
    "<@U0EXAMPLEF|Pat Example> commented on\n"
    "Example design doc\n"
    "https://drive-thirdparty.example/redirect\n"
    "*<https://docs.google.com/document/d/1EXAMPLEDOC/edit?disco=x|"
    "Example design doc>*\n"
    "&gt; _a comment_\n"
    "\"who owns this section?\"\n"
    "reply\n"
    "Reply to this thread to reply to the comment"
)


def drive_history(channel, messages):
    """Channel history as the connector renders an app's messages."""
    blocks = [
        f"=== Message from {name} <https://example.invalid/app> ({uid}) "
        f"at {STAMP} ===\nMessage TS: {ts}\n{text}"
        for ts, uid, name, text in reversed(messages)
    ]
    return json.dumps({
        "messages": f"Channel: DM ({channel})\n\n" + "\n\n".join(blocks),
        "pagination_info": "There are no more messages available.\n",
    })


class WatchChannelsTest(InboxTestCase):
    def run_once(self, inbox=None, now=NOW, **over):
        block = {"watch_channels": [WATCH]}
        block.update(inbox or {})
        with mock.patch.object(slack_mcp, "_now", return_value=now):
            return slack_mcp.candidates(inbox_source(block, **over))

    def test_searched_channel_records_top_level_posts_of_anyone(self):
        self.connector.search = by_filter({f"in:<#{WATCH}>": [
            {"ts": ts_at(10), "ch": WATCH, "text": "top level"},
            {"ts": ts_at(20), "ch": WATCH, "uid": CAROL, "root": ts_at(10),
             "text": "a reply"},
            {"ts": ts_at(30), "ch": WATCH, "bot": True, "text": "bot post"},
            {"ts": ts_at(40), "ch": WATCH, "uid": ME, "name": "Me",
             "text": "mine"},
        ]})
        self.run_once(channels=[WATCH])
        rows = self.messages()
        self.assertEqual([r["ts"] for r in rows], [ts_at(10), ts_at(40)])
        self.assertEqual(rows[0]["channel_type"], "channel")
        self.assertFalse(rows[0]["is_owner"])
        self.assertTrue(rows[1]["is_owner"])
        self.assertEqual(rows[0]["thread_ts"], ts_at(10))
        # No inbox-only search: the routine's own channel query was reused.
        self.assertEqual(len(self.connector.calls), 1)

    def test_include_bots_keeps_bot_posts(self):
        self.connector.search = by_filter({f"in:<#{WATCH}>": [
            {"ts": ts_at(30), "ch": WATCH, "bot": True, "text": "bot post"},
        ]})
        self.run_once(channels=[WATCH], include_bots=True)
        self.assertEqual([r["ts"] for r in self.messages()], [ts_at(30)])
        self.assertTrue(self.messages()[0]["is_bot"])

    def test_other_searched_channels_are_not_recorded(self):
        self.connector.search = by_filter({
            f"in:<#{WATCH}> in:<#{CHAN2}>": [
                {"ts": ts_at(10), "ch": WATCH},
                {"ts": ts_at(20), "ch": CHAN2},
            ],
        })
        self.run_once(channels=[WATCH, CHAN2])
        self.assertEqual([r["ts"] for r in self.messages()], [ts_at(10)])

    def test_unsearched_channel_gets_an_inbox_only_search(self):
        seen = []

        def search(args):
            seen.append(args)
            if args.get("filters") == f"in:<#{WATCH}>":
                return search_text([
                    {"ts": ts_at(10), "ch": WATCH, "text": "top level"},
                    {"ts": ts_at(20), "ch": WATCH, "uid": CAROL,
                     "root": ts_at(10), "text": "reply"},
                ])
            return search_text([])

        self.connector.search = search
        runtime = {}
        listing = self.run_once(_slack_mcp_runtime=runtime)
        self.assertEqual(len(listing), 0)  # never captured
        self.assertEqual([r["ts"] for r in self.messages()], [ts_at(10)])
        watched = [a for a in seen if a["filters"] == f"in:<#{WATCH}>"]
        self.assertEqual(len(watched), 1)
        self.assertEqual(watched[0]["after"], int(NOW) - 14 * 86400)
        self.assertNotIn(f"watched:{WATCH}", runtime["watermarks"])
        self.assertEqual(
            comms_inbox.load_watched(self.tmp.name), {WATCH: int(NOW)}
        )
        # Next run lists from the mark minus the overlap, and dedupes.
        seen.clear()
        self.run_once(now=NOW + 600)
        watched = [a for a in seen if a["filters"] == f"in:<#{WATCH}>"]
        self.assertEqual(watched[0]["after"], int(NOW) - 3600)
        self.assertEqual(len(self.messages()), 1)

    def test_watched_search_counts_toward_the_cap_and_is_skipped_over_it(self):
        seen = []
        self.connector.search = lambda a: seen.append(a) or search_text([])
        self.run_once(max_queries_per_run=1)  # the channels query fills it
        self.assertFalse(
            [a for a in seen if a["filters"] == f"in:<#{WATCH}>"]
        )

    def test_watched_chunks_are_capped_per_run(self):
        cfg = slack_mcp.settings(inbox_source(
            {"watch_channels": [f"C0EXAMPLE{n:02d}" for n in range(30)]}
        ))
        queries = slack_mcp._watched_queries(cfg, {}, 10, NOW)
        self.assertEqual(len(queries), comms_inbox.MAX_WATCHED_QUERIES)
        self.assertTrue(all(q["kind"] == "watched" for q in queries))
        self.assertEqual(len(queries[0]["allowed"]), 15)
        self.assertEqual(slack_mcp._watched_queries(cfg, {}, 1, NOW)[0]["kind"],
                         "watched")
        self.assertEqual(slack_mcp._watched_queries(cfg, {}, 0, NOW), [])

    def test_excluded_and_searched_channels_get_no_extra_search(self):
        cfg = slack_mcp.settings(inbox_source(
            {"watch_channels": [WATCH, WATCH2]}, channels=[WATCH],
            exclude_channels=[WATCH2],
        ))
        self.assertEqual(slack_mcp._watched_queries(cfg, {}, 5, NOW), [])

    def test_failed_watched_search_keeps_its_mark_and_fails_nothing_else(self):
        self.connector.errors = lambda tool, args: False

        def search(args):
            if args.get("filters") == f"in:<#{WATCH}>":
                return "not json"
            return search_text([])

        self.connector.search = search
        self.run_once()
        self.assertEqual(comms_inbox.load_watched(self.tmp.name), {})

    def test_hit_in_both_a_channels_list_and_watch_is_stored_once(self):
        self.connector.search = by_filter({f"in:<#{WATCH}>": [
            {"ts": ts_at(10), "ch": WATCH},
        ]})
        self.run_once({"channels": [WATCH]}, channels=[WATCH])
        self.assertEqual(len(self.messages()), 1)

    def test_old_posts_outside_retention_are_dropped(self):
        old = f"{int(NOW) - 20 * 86400}.000100"
        self.connector.search = by_filter({f"in:<#{WATCH}>": [
            {"ts": old, "ch": WATCH},
        ]})
        self.run_once(channels=[WATCH])
        self.assertFalse(self.rows())

    def test_validation(self):
        good = {"watch_channels": [WATCH]}
        self.assertIsNone(comms_inbox.problem(good))
        for bad in (
            {"watch_channels": "C0EXAMPLE07"},
            {"watch_channels": ["general"]},
            {"watch_channels": [DM]},
            {"watch_channels": [WATCH, WATCH]},
            {"watch_channels": [f"C0EXAMPLE{n:02d}" for n in range(31)]},
            {"bot_text_users": [CHAN]},
            {"bot_text_users": [DRIVE, DRIVE]},
            {"bot_text_users": [f"U0EXAMPLE{n:02d}" for n in range(11)]},
        ):
            with self.subTest(bad=bad):
                self.assertTrue(comms_inbox.problem(bad))
        cfg = comms_inbox.settings(good)
        self.assertEqual(cfg["watch_channels"], [WATCH])
        self.assertEqual(cfg["bot_text_users"], [])

    def test_watch_file_accepts_the_new_keys(self):
        self.assertIsNone(comms_inbox.problem(
            {"watch_channels": [WATCH], "bot_text_users": [DRIVE]}
        ))
        with tempfile.TemporaryDirectory() as tmp:
            file = f"{tmp}/watch.yaml"
            with open(file, "w") as handle:
                handle.write(
                    "channels: [%s]\ncomms_inbox:\n  watch_channels: [%s]\n"
                    "  bot_text_users: [%s]\n" % (CHAN, WATCH, DRIVE)
                )
            data = slack_watch.load(file)
        self.assertIn("comms_inbox", str(data))


class BotTextTest(InboxTestCase):
    INBOX = {"all_dms": True, "bot_text_users": [DRIVE]}

    def setUp(self):
        super().setUp()
        self.reads = []
        self.pages = {}

        def channel(args):
            self.reads.append(args)
            return drive_history(args["channel_id"], self.pages.get(
                args["channel_id"], []))

        self.connector.channel = channel

    def run_once(self, inbox=None, now=NOW, **over):
        cfg = dict(
            channels=[], all_dms=True, read_channel_tool=READ_CHANNEL,
            max_file_reads_per_run=0,  # the file scan's reads are not under test
        )
        cfg.update(over)
        with mock.patch.object(slack_mcp, "_now", return_value=now):
            return slack_mcp.candidates(inbox_source(
                inbox or self.INBOX, **cfg
            ))

    def search_drive(self, *stamps, text="", user=DRIVE, ch=DM):
        table = by_filter({f"with:<@{ME}>": [
            {"ch": ch, "dm": True, "people": [user, ME], "uid": user,
             "name": "Drive", "bot": True, "ts": ts, "text": text}
            for ts in stamps
        ]})
        # hit_block gives every bot the same id: put the app's own in.
        self.connector.search = lambda args: table(args).replace(
            "From:  (ID: U00)  [BOT]", f"From: Drive (ID: {user})  [BOT]"
        )

    def test_empty_search_text_is_completed_from_the_channel(self):
        self.search_drive(ts_at(10))
        self.pages[DM] = [(ts_at(10), DRIVE, "Drive", DRIVE_TEXT)]
        self.run_once()
        rows = self.messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], DRIVE_TEXT)
        self.assertTrue(rows[0]["is_bot"])
        self.assertEqual(rows[0]["user_id"], DRIVE)
        self.assertEqual(rows[0]["channel_type"], "im")
        self.assertTrue(rows[0]["peer_is_bot"])
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(self.reads[0]["channel_id"], DM)

    def test_bot_text_row_is_redacted_and_capped(self):
        token = "xoxb-" + "1" * 30
        long = "word " * 2000
        self.search_drive(ts_at(10), ts_at(20))
        self.pages[DM] = [
            (ts_at(10), DRIVE, "Drive", f"token {token}"),
            (ts_at(20), DRIVE, "Drive", long),
        ]
        self.run_once()
        first, second = self.messages()
        self.assertNotIn(token, first["text"])
        self.assertIn("[REDACTED]", first["text"])
        self.assertEqual(len(second["text"]), comms_inbox.MAX_TEXT)
        self.assertEqual(comms_inbox.MAX_TEXT, 4000)

    def test_stored_empty_rows_are_backfilled_once(self):
        old = {
            "type": "message", "channel_id": DM, "thread_ts": ts_at(10),
            "ts": ts_at(10), "user_id": DRIVE, "is_owner": False,
            "is_bot": True, "text": "", "channel_type": "im",
        }
        comms_inbox.record(self.tmp.name, [old], 14, now=NOW)
        self.pages[DM] = [(ts_at(10), DRIVE, "Drive", DRIVE_TEXT)]
        self.run_once()
        rows = self.messages()
        self.assertEqual([r["text"] for r in rows], [DRIVE_TEXT])
        self.assertEqual(len(rows), 1)  # patched, not duplicated
        self.assertEqual(len(self.reads), 1)
        # Nothing is empty any more: no further read.
        self.run_once(now=NOW + 60)
        self.assertEqual(len(self.reads), 1)

    def test_only_listed_users_text_is_patched(self):
        rows = [{
            "type": "message", "channel_id": DM, "thread_ts": ts_at(10),
            "ts": ts_at(10), "user_id": OTHER_APP, "is_owner": False,
            "is_bot": True, "text": "", "channel_type": "im",
        }]
        comms_inbox.record(self.tmp.name, rows, 14, now=NOW)
        comms_inbox.record(
            self.tmp.name, [dict(rows[0], text="late")], 14, now=NOW
        )
        self.assertEqual(self.messages()[0]["text"], "")
        comms_inbox.record(
            self.tmp.name, [dict(rows[0], text="late")], 14, now=NOW,
            text_users={OTHER_APP},
        )
        self.assertEqual(self.messages()[0]["text"], "late")
        comms_inbox.record(
            self.tmp.name, [dict(rows[0], text="again")], 14, now=NOW,
            text_users={OTHER_APP},
        )
        self.assertEqual(self.messages()[0]["text"], "late")  # never rewritten

    def test_other_bots_stay_as_they_were(self):
        self.search_drive(ts_at(10), user=OTHER_APP)
        self.pages[DM] = [(ts_at(10), OTHER_APP, "Other", "full text")]
        self.run_once()
        self.assertEqual(self.messages()[0]["text"], "")
        self.assertEqual(self.reads, [])

    def test_only_the_listed_users_messages_are_taken_from_the_page(self):
        self.search_drive(ts_at(10))
        self.pages[DM] = [
            (ts_at(10), DRIVE, "Drive", DRIVE_TEXT),
            (ts_at(15), BOB, "Bob", "a person's message"),
        ]
        self.run_once()
        self.assertEqual([r["user_id"] for r in self.messages()], [DRIVE])

    def test_rows_with_text_cause_no_read(self):
        self.search_drive(ts_at(10), text="already here")
        self.run_once()
        self.assertEqual(self.reads, [])
        self.assertEqual(self.messages()[0]["text"], "already here")

    def test_a_message_that_stays_empty_is_tried_three_times(self):
        self.search_drive(ts_at(10))
        self.pages[DM] = [(ts_at(10), DRIVE, "Drive", "")]
        for n in range(5):
            self.run_once(now=NOW + n)
        self.assertEqual(len(self.reads), comms_inbox.MAX_BOT_TEXT_TRIES)
        self.assertEqual(self.messages()[0]["text"], "")

    def test_reads_are_bounded_per_run_and_skip_other_channel_types(self):
        for n in range(6):
            comms_inbox.record(self.tmp.name, [{
                "type": "message", "channel_id": f"D0EXAMPLE1{n}",
                "thread_ts": ts_at(10 + n), "ts": ts_at(10 + n),
                "user_id": DRIVE, "is_owner": False, "is_bot": True,
                "text": "", "channel_type": "im",
            }], 14, now=NOW)
        comms_inbox.record(self.tmp.name, [{
            "type": "message", "channel_id": CHAN, "thread_ts": ts_at(5),
            "ts": ts_at(5), "user_id": DRIVE, "is_owner": False,
            "is_bot": True, "text": "", "channel_type": "channel",
        }], 14, now=NOW)
        self.run_once()
        self.assertEqual(len(self.reads), comms_inbox.MAX_BOT_TEXT_READS)
        self.assertNotIn(CHAN, {a["channel_id"] for a in self.reads})
        self.assertEqual(
            len([c for c in self.connector.calls
                 if READ_CHANNEL in c[0]]), 1)  # one bridge session

    def test_excluded_dm_is_never_read(self):
        self.search_drive(ts_at(10))
        self.run_once(exclude_channels=[DM])
        self.assertEqual(self.reads, [])

    def test_no_read_without_the_tool_or_option(self):
        self.search_drive(ts_at(10))
        self.run_once(read_channel_tool=None)
        self.run_once({"all_dms": True})
        self.assertEqual(self.reads, [])

    def test_failed_read_is_retried_next_run(self):
        self.search_drive(ts_at(10))
        self.pages[DM] = [(ts_at(10), DRIVE, "Drive", DRIVE_TEXT)]
        self.connector.errors = lambda tool, args: tool == READ_CHANNEL
        self.run_once()
        self.assertEqual(self.messages()[0]["text"], "")
        self.connector.errors = lambda tool, args: False
        self.run_once(now=NOW + 60)
        self.assertEqual(self.messages()[0]["text"], DRIVE_TEXT)

    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            def problems(inbox, **over):
                return config.validate(routine(
                    tmp, comms_inbox=inbox, all_dms=True, **over
                ))

            ok = problems(
                self.INBOX, read_channel_tool=READ_CHANNEL,
            )
            self.assertEqual(ok, [])
            self.assertTrue(any(
                "read_channel_tool" in p for p in problems(self.INBOX)
            ))
            self.assertTrue(any(
                "all_dms" in p for p in problems(
                    {"bot_text_users": [DRIVE]},
                    read_channel_tool=READ_CHANNEL,
                )
            ))


class ReactionAttributionTest(InboxTestCase):
    """Reactions belong to the owner only when the owner made them."""

    def kinds(self):
        return sorted(
            (r["type"], r["emoji"], r.get("user_id"))
            for r in self.rows() if "emoji" in r
        )

    def test_plain_hits_never_write_reaction_rows(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10)}, {"ts": ts_at(20), "uid": CAROL},
            {"ts": ts_at(30), "uid": ME, "name": "Me"},
        ]})
        slack_mcp.candidates(inbox_source({
            "channels": [CHAN], "reactions": [ACK],
            "track_reactions": [TRACK],
        }, save_reactions=[SAVE]))
        self.assertEqual(self.kinds(), [])

    def test_owner_reaction_by_search_names_only_its_own_emoji(self):
        self.connector.search = by_filter({
            f"hasmy::{ACK}:": [{"ts": ts_at(10)}],
            f"in:<#{CHAN}>": [{"ts": ts_at(20)}],
        })
        slack_mcp.candidates(inbox_source({
            "channels": [CHAN], "reactions": [ACK, "pray"],
        }))
        self.assertEqual(self.kinds(), [("owner_reaction", ACK, None)])
        self.assertEqual(self.reactions()[0]["ts"], ts_at(10))

    def test_track_request_comes_only_from_the_owners_track_search(self):
        self.connector.search = by_filter({
            TRACK_FILTER: [{"ts": ts_at(10)}],
            f"in:<#{CHAN}>": [{"ts": ts_at(20)}],
            f"hasmy::{ACK}:": [],
        })
        slack_mcp.candidates(inbox_source({
            "channels": [CHAN], "reactions": [ACK],
            "track_reactions": [TRACK],
        }))
        tracks = [r for r in self.rows() if r["type"] == "track_request"]
        self.assertEqual([(r["ts"], r["emoji"]) for r in tracks],
                         [(ts_at(10), TRACK)])
        self.assertEqual(self.reactions(), [])

    def test_save_search_is_a_capture_not_a_reaction_row(self):
        self.connector.search = by_filter({
            f"hasmy::{SAVE}:": [{"ts": ts_at(10)}],
        })
        slack_mcp.candidates(inbox_source(
            {"channels": [CHAN]}, save_reactions=[SAVE]
        ))
        self.assertEqual(self.kinds(), [])

    def reaction_run(self, answer, own, inbox=None):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10), **({"uid": ME, "name": "Me"} if own else {})},
        ]})
        self.connector.thread = lambda args: (
            json.dumps({"messages": ""}) if "limit" in args else answer
        )
        slack_mcp.candidates(inbox_source(
            inbox or {"channels": [CHAN], "track_reactions": [TRACK]},
            reactions_tool=REACTIONS_TOOL, save_reactions=[SAVE],
        ))

    def test_a_reaction_by_someone_else_is_never_an_owner_reaction(self):
        self.reaction_run(
            reactions_text(("pray", [BOB]), ("eyes", [CAROL, BOB])), own=False
        )
        self.assertEqual(self.kinds(), [])

    def test_owner_among_the_reactors_is_an_owner_reaction(self):
        self.reaction_run(
            reactions_text(("pray", [BOB, ME]), ("eyes", [BOB])), own=False
        )
        self.assertEqual(self.kinds(), [("owner_reaction", "pray", None)])

    def test_track_and_save_emoji_from_the_owner_are_not_owner_reactions(self):
        self.reaction_run(
            reactions_text((TRACK, [ME]), (SAVE, [ME])), own=False
        )
        self.assertEqual(self.kinds(), [])

    def test_a_peer_reaction_is_only_recorded_on_the_owners_message(self):
        self.reaction_run(
            reactions_text(("pray", [BOB, ME]), (TRACK, [BOB])), own=True
        )
        self.assertEqual(self.kinds(), [("peer_reaction", "pray", BOB)])
        self.assertEqual(self.reactions(), [])

    def test_ignored_reactors_are_never_peer_reactions(self):
        self.reaction_run(
            reactions_text(("pray", [BOB, CAROL])), own=True,
            inbox={"channels": [CHAN], "ignore_reactors": [BOB]},
        )
        self.assertEqual(self.kinds(), [("peer_reaction", "pray", CAROL)])

    def test_watched_and_bot_text_paths_write_no_reaction_rows(self):
        self.connector.search = by_filter({f"in:<#{WATCH}>": [
            {"ts": ts_at(10), "ch": WATCH, "uid": CAROL},
        ]})
        slack_mcp.candidates(inbox_source(
            {"watch_channels": [WATCH], "reactions": [ACK]}
        ))
        self.assertEqual(self.kinds(), [])
        self.assertEqual(len(self.messages()), 1)
        self.assertEqual(message(ts_at(1))["is_owner"], False)
