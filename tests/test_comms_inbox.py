"""Comms inbox: recorded hits, owner reactions, dedupe, retention, config."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import comms_inbox, config, slack_mcp, slack_watch

from test_slack_mcp import (
    BOB, CHAN, ME, NOW, ROOT, BridgeTestCase, hit_block, routine, search_text,
    source, thread_text, ts_at,
)

CHAN2 = "C0EXAMPLE02"
DM = "D0EXAMPLE03"
ACK = "eyes"
SAVE = "save-for-later-example"
INBOX = {"channels": [CHAN]}


def inbox_source(inbox=None, **over):
    return source(comms_inbox=dict(inbox or INBOX), **over)


def by_filter(table, default=()):
    """Connector search answering per `filters` value."""
    def search(args):
        return search_text(list(table.get(args.get("filters"), default)))
    return search


class InboxTestCase(BridgeTestCase):
    def rows(self):
        file = comms_inbox.path(self.tmp.name)
        if not file.exists():
            return None
        return [json.loads(line) for line in file.read_text().splitlines()]

    def messages(self):
        return [r for r in self.rows() if r["type"] == "message"]

    def reactions(self):
        return [r for r in self.rows() if r["type"] == "owner_reaction"]


class RecordTest(InboxTestCase):
    def test_messages_of_inbox_channels_are_recorded_with_their_thread(self):
        self.connector.search = by_filter({
            f"in:<#{CHAN}> in:<#{CHAN2}>": [
                {"ts": ts_at(10), "text": "question?"},
                {"ts": ts_at(20), "uid": ME, "name": "Me", "root": ts_at(10),
                 "text": "answer"},
                {"ts": ts_at(30), "ch": CHAN2, "text": "elsewhere"},
            ],
        })
        slack_mcp.candidates(inbox_source(channels=[CHAN, CHAN2]))
        rows = self.messages()
        self.assertEqual([r["ts"] for r in rows], [ts_at(10), ts_at(20)])
        first, reply = rows
        self.assertEqual(first, dict(first, **{
            "channel_id": CHAN, "thread_ts": ts_at(10), "user_id": BOB,
            "is_owner": False, "is_bot": False, "text": "question?",
        }))
        self.assertEqual(reply["thread_ts"], ts_at(10))
        self.assertTrue(reply["is_owner"])
        self.assertIn("seen_at", reply)
        mode = comms_inbox.path(self.tmp.name).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_messages_inside_the_settle_window_are_recorded_now(self):
        fresh = f"{int(NOW) - 60}.000100"
        self.connector.search = by_filter({f"in:<#{CHAN}>": [{"ts": fresh}]})
        listing = slack_mcp.candidates(inbox_source(settle_minutes=30))
        self.assertTrue(listing[0]["raw"].get("_deferred"))
        self.assertEqual([r["ts"] for r in self.messages()], [fresh])

    def test_runs_dedupe_on_channel_and_ts(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10), "text": "first"},
        ]})
        slack_mcp.candidates(inbox_source())
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10), "text": "edited"}, {"ts": ts_at(20)},
        ]})
        slack_mcp.candidates(inbox_source())
        rows = self.messages()
        self.assertEqual([r["ts"] for r in rows], [ts_at(10), ts_at(20)])
        self.assertEqual(rows[0]["text"], "first")

    def test_lines_past_retention_are_dropped(self):
        old = f"{int(NOW) - 3 * 86400}.000100"
        comms_inbox.record(self.tmp.name, [{
            "type": "message", "channel_id": CHAN, "thread_ts": old,
            "ts": old, "user_id": BOB, "is_owner": False, "is_bot": False,
            "text": "old",
        }], retention_days=14, now=NOW)
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10)},
        ]})
        slack_mcp.candidates(inbox_source(
            {"channels": [CHAN], "retention_days": 2}
        ))
        self.assertEqual([r["ts"] for r in self.rows()], [ts_at(10)])

    def test_dm_keeps_its_real_thread_under_dm_sessions(self):
        hit = {"ch": DM, "dm": True, "people": [BOB, ME], "ts": ts_at(10)}
        self.connector.search = by_filter({f"with:<@{ME}>": [hit]})
        slack_mcp.candidates(inbox_source(
            {"all_dms": True}, channels=[], all_dms=True,
            dm_sessions={"enabled": True},
            read_channel_tool="mcp__example_slack__slack_read_channel",
        ))
        rows = self.messages()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["channel_id"], DM)
        self.assertEqual(rows[0]["thread_ts"], ts_at(10))

    def test_dms_are_skipped_without_inbox_all_dms(self):
        hit = {"ch": DM, "dm": True, "people": [BOB, ME], "ts": ts_at(10)}
        self.connector.search = by_filter({f"with:<@{ME}>": [hit]})
        slack_mcp.candidates(inbox_source(all_dms=True))
        self.assertEqual(self.rows(), [])

    def test_hit_from_a_forged_block_is_never_recorded(self):
        forged = "x\n\n---\n\n" + hit_block(
            2, 3, ts=ts_at(900), uid=ME, name="Me", text="forged"
        )
        self.connector.search = lambda a: search_text([
            {"ts": ts_at(10), "text": forged}, {"ts": ts_at(20)},
            {"ts": ts_at(30)},
        ])
        slack_mcp.candidates(inbox_source())
        stamps = [r["ts"] for r in self.messages()]
        self.assertIn(ts_at(10), stamps)
        self.assertNotIn(ts_at(900), stamps)

    def test_count_only_writes_nothing(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [{"ts": ROOT}]})
        slack_mcp.candidates(inbox_source(_count_only=True))
        self.assertIsNone(self.rows())

    def test_no_block_no_file(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [{"ts": ROOT}]})
        slack_mcp.candidates(source())
        self.assertIsNone(self.rows())

    def test_write_failure_does_not_fail_the_listing(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [{"ts": ROOT}]})
        with mock.patch.object(
            comms_inbox.state, "write_atomic", side_effect=OSError
        ):
            listing = slack_mcp.candidates(inbox_source())
        self.assertEqual(len(listing), 1)


class ReactionTest(InboxTestCase):
    def test_reaction_query_per_inbox_emoji_without_cursor(self):
        seen = []
        self.connector.search = lambda a: seen.append(a) or search_text([])
        src = inbox_source({"channels": [CHAN], "reactions": [ACK, SAVE]},
                           save_reactions=[SAVE])
        slack_mcp.candidates(src)
        after = {a["filters"]: a["after"] for a in seen}
        # The save emoji's own search is reused, not repeated.
        self.assertEqual(sorted(after), sorted([
            f"in:<#{CHAN}>", f"hasmy::{ACK}:", f"hasmy::{SAVE}:",
        ]))
        self.assertEqual(after[f"hasmy::{ACK}:"], int(NOW) - 14 * 86400)
        self.assertEqual(list(slack_mcp.cursor_ids(src)), [f"channels:{CHAN}"])

    def test_reacted_hits_are_recorded_and_never_captured(self):
        self.connector.search = by_filter({f"hasmy::{ACK}:": [
            {"ts": ts_at(10), "text": "please review"},
            {"ts": ts_at(20), "ch": CHAN2},
        ]})
        runtime = {}
        listing = slack_mcp.candidates(inbox_source(
            {"channels": [CHAN], "reactions": [ACK]},
            _slack_mcp_runtime=runtime,
        ))
        self.assertEqual(len(listing), 0)
        self.assertNotIn(f"reacted:{ACK}", runtime["watermarks"])
        self.assertEqual(
            [(r["ts"], r["emoji"]) for r in self.reactions()],
            [(ts_at(10), ACK)],
        )
        self.assertEqual([r["ts"] for r in self.messages()], [ts_at(10)])

    def test_save_emoji_is_not_handled_unless_listed(self):
        self.connector.search = by_filter({f"hasmy::{SAVE}:": [
            {"ts": ts_at(10)},
        ]})
        listing = slack_mcp.candidates(inbox_source(save_reactions=[SAVE]))
        self.assertEqual(len(listing), 1)
        self.assertTrue(listing[0]["raw"]["saved"])
        self.assertEqual(self.reactions(), [])
        self.assertEqual([r["ts"] for r in self.messages()], [ts_at(10)])
        slack_mcp.candidates(inbox_source(
            {"channels": [CHAN], "reactions": [SAVE]}, save_reactions=[SAVE],
        ))
        self.assertEqual(
            [(r["ts"], r["emoji"]) for r in self.reactions()],
            [(ts_at(10), SAVE)],
        )

    def test_reaction_queries_count_toward_the_per_run_cap(self):
        with self.assertRaises(slack_mcp.SlackMcpError):
            slack_mcp.candidates(inbox_source(
                {"channels": [CHAN], "reactions": [ACK]},
                max_queries_per_run=1,
            ))


REACTIONS_TOOL = "mcp__example_slack__slack_get_reactions"


def reactions_text(*lines):
    if not lines:
        return json.dumps({"result": "No reactions on this message."})
    return json.dumps({"result": "Reactions on message:\n\n" + "\n".join(
        f":{emoji}: \u00d7 {len(who)} \u2014 "
        + ", ".join(f"Person {uid} ({uid})" for uid in who)
        for emoji, who in lines
    )})


def message(ts, user=BOB, thread=None, ch=CHAN):
    return {
        "type": "message", "channel_id": ch, "thread_ts": thread or ts,
        "ts": ts, "user_id": user, "is_owner": user == ME, "is_bot": False,
        "text": "x",
    }


class ParseReactionsTest(unittest.TestCase):
    def test_owner_emoji_are_returned(self):
        text = reactions_text(
            ("pray", [BOB, ME]), ("+1::skin-tone-2", [ME]), ("eyes", [BOB]),
        )
        self.assertEqual(
            comms_inbox.parse_reactions(text, ME), ["pray", "+1::skin-tone-2"]
        )

    def test_no_reactions(self):
        self.assertEqual(comms_inbox.parse_reactions(reactions_text(), ME), [])

    def test_unknown_shapes_raise(self):
        for text in (
            "not json at all", json.dumps({"other": 1}),
            json.dumps({"result": "Reactions on message:\n\nsomething odd"}),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    comms_inbox.parse_reactions(text, ME)


class DueChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def due(self, rows, now=NOW, limit=10, hours=72):
        comms_inbox.record(self.tmp.name, rows, 14, now=now)
        return [
            d["ts"] for d in comms_inbox.due_checks(
                self.tmp.name, limit, hours, now=now,
            )
        ]

    def test_only_the_newest_message_per_conversation(self):
        t1, t2, t3 = (f"{int(NOW) - n}.000100" for n in (300, 200, 100))
        self.assertEqual(self.due([
            message(t1), message(t2),                      # top level: t2
            message(t3, user=ME, thread=t1),               # thread: owner last
        ]), [t3, t2])

    def test_reacted_old_and_bot_messages_are_closed(self):
        old = f"{int(NOW) - 80 * 3600}.000100"
        reacted = f"{int(NOW) - 100}.000100"
        bot = dict(message(f"{int(NOW) - 50}.000100", ch=CHAN2), is_bot=True)
        rows = [
            message(old, ch=DM), message(reacted), bot,
            comms_inbox.reaction_row(
                {"channel": CHAN, "ts": reacted, "root": reacted}, "pray"
            ),
        ]
        self.assertEqual(self.due(rows), [])

    def test_back_off_is_a_quarter_of_the_age(self):
        # Due again once the elapsed time reaches a quarter of the age then:
        # e >= (4h + e) / 4, so e >= 4800s.
        ts = f"{int(NOW) - 4 * 3600}.000100"
        self.assertEqual(self.due([message(ts)]), [ts])
        comms_inbox.mark_checked(self.tmp.name, [f"{CHAN}:{ts}"], 72, now=NOW)
        self.assertEqual(self.due([], now=NOW + 4799), [])
        self.assertEqual(self.due([], now=NOW + 4800), [ts])

    def test_owner_newest_message_is_due_until_a_peer_reacts(self):
        t1, t2 = (f"{int(NOW) - n}.000100" for n in (300, 100))
        self.assertEqual(self.due([message(t1), message(t2, user=ME)]), [t2])
        peer = comms_inbox.peer_row(
            {"channel": CHAN, "ts": t2, "root": t2}, "pray", BOB
        )
        self.assertEqual(self.due([peer]), [])

    def test_owner_bot_and_old_owner_messages_are_not_due(self):
        old = f"{int(NOW) - 80 * 3600}.000100"
        bot = dict(
            message(f"{int(NOW) - 50}.000100", user=ME, ch=CHAN2), is_bot=True
        )
        self.assertEqual(self.due([message(old, user=ME, ch=DM), bot]), [])

    def test_owner_messages_share_the_limit_without_starving_others(self):
        others = [f"{int(NOW) - n}.000100" for n in (400, 300, 200, 100)]
        mine = [f"{int(NOW) - n}.000200" for n in (50, 40)]
        rows = [message(t, ch=f"C0EXAMPLE{i}0") for i, t in enumerate(others)]
        rows += [message(t, user=ME, ch=f"C0OWNER{i}000")
                 for i, t in enumerate(mine)]
        # limit 4: owner messages get 2, the rest 2, newest first overall.
        self.assertEqual(
            self.due(rows, limit=4),
            [mine[1], mine[0], others[3], others[2]],
        )
        # Spare slots go to the other side.
        self.assertEqual(self.due([], limit=6), [mine[1], mine[0]] + others[::-1])
        self.assertEqual(len(self.due([], limit=1)), 1)
        self.assertEqual(self.due([], limit=1), [others[3]])

    def test_newest_first_up_to_the_limit(self):
        stamps = [f"{int(NOW) - n}.000100" for n in (30, 20, 10)]
        rows = [message(t, ch=c) for t, c in zip(stamps, (CHAN, CHAN2, DM))]
        self.assertEqual(self.due(rows, limit=2), stamps[::-1][:2])


class ReactionReadTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.reads = []
        self.answer = reactions_text(("pray", [ME]))

        def thread(args):
            if "limit" in args:
                return json.dumps({"messages": ""})
            self.reads.append(args)
            return self.answer

        self.connector.thread = thread
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10)},
        ]})

    def run_once(self, inbox=None, **over):
        return slack_mcp.candidates(inbox_source(
            inbox, reactions_tool=REACTIONS_TOOL, **over
        ))

    def test_owner_reaction_is_recorded_and_closes_the_message(self):
        self.run_once()
        self.assertEqual(
            self.reads, [{"channel_id": CHAN, "message_ts": ts_at(10)}]
        )
        self.assertEqual(
            [(r["ts"], r["emoji"]) for r in self.reactions()],
            [(ts_at(10), "pray")],
        )
        calls = len(self.connector.calls)
        self.run_once()
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(len(self.connector.calls), calls + 1)  # search only

    def test_peer_reaction_on_an_owner_message_is_recorded(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10), "uid": ME, "name": "Me"},
        ]})
        self.answer = reactions_text(
            ("pray", [BOB, ME]), ("eyes", [ME]), ("+1", [BOB]),
        )
        self.run_once()
        self.run_once()
        peers = [r for r in self.rows() if r["type"] == "peer_reaction"]
        self.assertEqual(
            sorted((r["emoji"], r["user_id"]) for r in peers),
            [("+1", BOB), ("pray", BOB)],
        )
        row = peers[0]
        self.assertEqual(
            (row["channel_id"], row["thread_ts"], row["ts"]),
            (CHAN, ts_at(10), ts_at(10)),
        )
        self.assertIn("seen_at", row)
        self.assertEqual(self.reactions(), [])   # owner's own: not recorded
        self.assertEqual(len(self.reads), 1)     # closed after the first read

    def test_owner_reaction_alone_on_own_message_stays_open(self):
        self.connector.search = by_filter({f"in:<#{CHAN}>": [
            {"ts": ts_at(10), "uid": ME, "name": "Me"},
        ]})
        self.answer = reactions_text(("eyes", [ME]))
        self.run_once()
        self.assertEqual(
            [r for r in self.rows() if r["type"] != "message"], []
        )

    def test_peer_reaction_is_deduped_on_user_and_emoji(self):
        hit = {"channel": CHAN, "ts": ts_at(10), "root": ts_at(10)}
        rows = [comms_inbox.peer_row(hit, "pray", BOB)] * 2 + [
            comms_inbox.peer_row(hit, "pray", ME),
            comms_inbox.peer_row(hit, "eyes", BOB),
        ]
        self.assertEqual(comms_inbox.record(self.tmp.name, rows, 14, NOW), 3)
        self.assertEqual(comms_inbox.record(self.tmp.name, rows, 14, NOW), 0)

    def test_owner_and_peer_checks_share_the_read_budget(self):
        self.connector.search = by_filter({
            f"in:<#{CHAN}>": [{"ts": ts_at(10)}],
            f"in:<#{CHAN2}>": [{"ts": ts_at(20), "uid": ME, "name": "Me",
                                "ch": CHAN2}],
        })
        self.run_once({"channels": [CHAN, CHAN2], "reaction_checks": 1})
        self.assertEqual(len(self.reads), 1)

    def test_track_and_save_emoji_are_not_handled(self):
        self.answer = reactions_text(
            (TRACK, [ME]), (SAVE, [ME]), ("pray", [ME]),
        )
        self.run_once({"channels": [CHAN], "track_reactions": [TRACK]},
                      save_reactions=[SAVE])
        self.assertEqual(
            [r["emoji"] for r in self.reactions()], ["pray"]
        )

    def test_message_without_owner_reaction_stays_open(self):
        self.answer = reactions_text(("pray", [BOB]))
        self.run_once()
        self.assertEqual(self.reactions(), [])
        state = Path(self.tmp.name, "state", comms_inbox.CHECKS_FILE)
        self.assertIn(f"{CHAN}:{ts_at(10)}", json.loads(state.read_text()))

    def test_without_the_tool_nothing_is_read(self):
        slack_mcp.candidates(inbox_source())
        self.assertEqual(self.reads, [])

    def test_unparseable_result_records_nothing(self):
        self.answer = "garbage"
        listing = self.run_once()
        self.assertEqual(len(listing), 1)
        self.assertEqual(self.reactions(), [])

    def test_bridge_failure_does_not_fail_the_listing(self):
        real = self.connector.__call__

        def call(prompt, tools, **kw):
            if REACTIONS_TOOL in tools:
                raise slack_mcp.claude_bridge.BridgeError("down")
            return real(prompt, tools, **kw)

        with mock.patch.object(slack_mcp.claude_bridge, "call_tools", call):
            listing = self.run_once()
        self.assertEqual(len(listing), 1)
        self.assertEqual(self.reactions(), [])

    def test_budget_stop_propagates(self):
        def call(prompt, tools, **kw):
            if REACTIONS_TOOL in tools:
                raise slack_mcp.claude_bridge.BudgetExhausted("cap")
            return self.connector(prompt, tools, **kw)

        with mock.patch.object(slack_mcp.claude_bridge, "call_tools", call):
            with self.assertRaises(slack_mcp.claude_bridge.BudgetExhausted):
                self.run_once()


TRACK = "track-example"
TRACK_FILTER = f"hasmy::{TRACK}:"


class TrackTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.seen = []
        self.table = {}

        def search(args):
            self.seen.append(args)
            return search_text(list(self.table.get(args.get("filters"), ())))

        self.connector.search = search

    def run_once(self, now=NOW, inbox=None, **over):
        with mock.patch.object(slack_mcp, "_now", return_value=now):
            return slack_mcp.candidates(inbox_source(
                inbox or {"track_reactions": [TRACK]}, **over
            ))

    def tracked(self):
        return comms_inbox.load_tracked(self.tmp.name)

    def test_track_query_has_no_cursor_and_looks_back_retention(self):
        src = inbox_source({"track_reactions": [TRACK], "retention_days": 5})
        slack_mcp.candidates(src)
        after = {a["filters"]: a["after"] for a in self.seen}
        self.assertEqual(after[TRACK_FILTER], int(NOW) - 5 * 86400)
        self.assertEqual(list(slack_mcp.cursor_ids(src)), [f"channels:{CHAN}"])

    def test_track_on_own_dm_message_records_and_tracks_the_dm(self):
        self.table[TRACK_FILTER] = [{
            "ch": DM, "dm": True, "people": [BOB, ME], "uid": ME,
            "name": "Me", "ts": ts_at(10), "text": "please approve",
        }]
        listing = self.run_once()
        self.assertEqual(len(listing), 0)
        tracks = [r for r in self.rows() if r["type"] == "track_request"]
        self.assertEqual(
            [(t["channel_id"], t["ts"], t["emoji"]) for t in tracks],
            [(DM, ts_at(10), TRACK)],
        )
        self.assertTrue(self.messages()[0]["is_owner"])
        self.assertEqual(self.tracked()["conversations"], [{
            "channel_id": DM, "thread_ts": ts_at(10), "since": ts_at(10),
            "whole": True,
        }])

    def test_uncovered_tracked_dm_is_searched_from_the_request(self):
        self.table[TRACK_FILTER] = [{
            "ch": DM, "dm": True, "people": [BOB, ME], "uid": ME,
            "ts": ts_at(10),
        }]
        self.run_once()
        self.seen.clear()
        self.table[f"in:<#{DM}>"] = [
            {"ch": DM, "dm": True, "people": [BOB, ME], "ts": ts_at(5)},
            {"ch": DM, "dm": True, "people": [BOB, ME], "ts": ts_at(20),
             "text": "approved"},
        ]
        listing = self.run_once()
        query = [a for a in self.seen if a["filters"] == f"in:<#{DM}>"][0]
        self.assertEqual(query["after"], int(float(ts_at(10))) - 1)
        self.assertEqual(len(listing), 0)
        self.assertEqual(
            [r["ts"] for r in self.messages()], [ts_at(10), ts_at(20)]
        )
        # The scan mark moves the next search up to now - overlap.
        self.seen.clear()
        self.run_once(now=NOW + 600)
        query = [a for a in self.seen if a["filters"] == f"in:<#{DM}>"][0]
        self.assertEqual(query["after"], int(NOW) - 3600)

    def test_dm_covered_by_all_dms_needs_no_extra_query(self):
        self.table[TRACK_FILTER] = [{
            "ch": DM, "dm": True, "people": [BOB, ME], "ts": ts_at(10),
        }]
        self.run_once(all_dms=True)
        self.seen.clear()
        self.run_once(all_dms=True)
        self.assertNotIn(
            f"in:<#{DM}>", [a.get("filters") for a in self.seen]
        )

    def test_tracked_thread_in_a_watched_channel(self):
        self.table[TRACK_FILTER] = [{"ts": ts_at(10)}]
        self.table[f"in:<#{CHAN}>"] = [
            {"ts": ts_at(20), "root": ts_at(10), "text": "reply"},
            {"ts": ts_at(30), "text": "unrelated"},
        ]
        self.run_once()
        self.assertEqual(
            [r["ts"] for r in self.messages()], [ts_at(10), ts_at(20)]
        )
        self.assertEqual(
            sorted(a.get("filters") for a in self.seen),
            sorted([f"in:<#{CHAN}>", TRACK_FILTER]),
        )
        self.assertEqual(
            self.tracked()["conversations"][0]["whole"], False
        )

    def test_requests_expire_with_retention(self):
        old = f"{int(NOW) - 3 * 86400}.000100"
        comms_inbox.save_tracked(self.tmp.name, {
            "conversations": [{"channel_id": DM, "thread_ts": old,
                               "since": old, "whole": True}],
            "scanned": {DM: NOW - 3 * 86400},
        }, retention_days=14, now=NOW)
        self.run_once(inbox={"track_reactions": [TRACK], "retention_days": 2})
        self.assertEqual(
            self.tracked(), {"conversations": [], "scanned": {}}
        )

    def test_tracked_queries_stay_within_the_query_cap(self):
        comms_inbox.save_tracked(self.tmp.name, {
            "conversations": [{"channel_id": DM, "thread_ts": ts_at(10),
                               "since": ts_at(10), "whole": True}],
            "scanned": {},
        }, retention_days=14, now=NOW)
        self.run_once(max_queries_per_run=2)
        self.assertNotIn(
            f"in:<#{DM}>", [a.get("filters") for a in self.seen]
        )


OTHER = "C0EXAMPLE07"  # a channel no query searches
MINE = f"from:<@{ME}>"


class FollowTest(InboxTestCase):
    def setUp(self):
        super().setUp()
        self.seen = []
        self.table = {}
        self.reads = []
        self.replies = []

        def search(args):
            self.seen.append(args)
            return search_text(list(self.table.get(args.get("filters"), ())))

        def thread(args):
            self.reads.append(args)
            return thread_text(args["message_ts"], replies=self.replies)

        self.connector.search = search
        self.connector.thread = thread

    def run_once(self, now=NOW, inbox=None, **over):
        with mock.patch.object(slack_mcp, "_now", return_value=now):
            return slack_mcp.candidates(inbox_source(
                inbox or {"follow_threads": True}, **over
            ))

    def follow(self):
        return comms_inbox.load_follow(self.tmp.name)

    def test_owner_query_scans_from_its_own_mark(self):
        self.run_once()
        mine = [a for a in self.seen if a["filters"] == MINE][0]
        self.assertEqual(mine["after"], int(NOW) - 14 * 86400)
        self.seen.clear()
        self.run_once(now=NOW + 600)
        mine = [a for a in self.seen if a["filters"] == MINE][0]
        self.assertEqual(mine["after"], int(NOW) - 3600)
        self.assertEqual(list(slack_mcp.cursor_ids(inbox_source(
            {"follow_threads": True}
        ))), [f"channels:{CHAN}"])

    def test_reply_in_an_unsearched_channel_is_followed_and_polled(self):
        root = ts_at(10)
        self.table[MINE] = [{
            "ch": OTHER, "uid": ME, "name": "Me", "ts": ts_at(20),
            "root": root, "text": "any update?",
        }]
        listing = self.run_once()
        self.assertEqual(len(listing), 0)
        self.assertIn(f"{OTHER}:{root}", self.follow()["threads"])
        # Polled in the same run: the new follow is due at once.
        self.assertEqual(self.reads, [{
            "channel_id": OTHER, "message_ts": root, "limit": 50,
        }])
        self.replies = [
            (ts_at(20), (ME, "Me"), "any update?"), (ts_at(30), BOB, "done"),
        ]
        self.table.clear()
        self.run_once(now=NOW + 60)
        self.assertEqual(len(self.reads), 1)  # backing off
        self.run_once(now=NOW + 86400)
        self.assertEqual(len(self.reads), 2)
        rows = {r["ts"]: r for r in self.messages()}
        self.assertEqual(rows[ts_at(30)]["user_id"], BOB)
        self.assertEqual(rows[ts_at(30)]["thread_ts"], root)
        self.assertEqual(
            self.follow()["threads"][f"{OTHER}:{root}"]["last"], ts_at(30)
        )

    def test_thread_in_a_searched_channel_is_recorded_without_polls(self):
        self.table[MINE] = [{"uid": ME, "ts": ts_at(10)}]
        self.table[f"in:<#{CHAN}>"] = [
            {"ts": ts_at(40), "root": ts_at(10), "text": "late answer"},
            {"ts": ts_at(50), "text": "unrelated"},
        ]
        self.run_once(inbox={"follow_threads": True})
        self.assertEqual(self.reads, [])
        self.assertEqual(
            [r["ts"] for r in self.messages()], [ts_at(10), ts_at(40)]
        )
        self.assertEqual(
            self.follow()["threads"][f"{CHAN}:{ts_at(10)}"]["last"], ts_at(40)
        )

    def test_dm_messages_are_not_followed(self):
        self.table[MINE] = [{
            "ch": DM, "dm": True, "people": [BOB, ME], "uid": ME,
            "ts": ts_at(10),
        }]
        self.run_once()
        self.assertEqual(self.follow()["threads"], {})

    def test_threads_expire_after_retention_since_last_activity(self):
        old = f"{int(NOW) - 15 * 86400}.000100"
        recent = f"{int(NOW) - 13 * 86400}.000100"
        comms_inbox.save_follow(self.tmp.name, {"threads": {
            f"{OTHER}:{old}": {"last": old, "checked": None},
            f"{OTHER}:{ts_at(1)}": {"last": recent, "checked": NOW},
        }, "scanned": None}, retention_days=30, now=NOW)
        self.run_once()
        self.assertEqual(
            list(self.follow()["threads"]), [f"{OTHER}:{ts_at(1)}"]
        )

    def test_poll_failure_leaves_threads_due(self):
        self.table[MINE] = [{"ch": OTHER, "uid": ME, "ts": ts_at(10)}]
        real = self.connector.__call__

        def call(prompt, tools, **kw):
            if "thread read" in prompt:
                raise slack_mcp.claude_bridge.BridgeError("down")
            return real(prompt, tools, **kw)

        with mock.patch.object(slack_mcp.claude_bridge, "call_tools", call):
            self.run_once()
        entry = self.follow()["threads"][f"{OTHER}:{ts_at(10)}"]
        self.assertIsNone(entry["checked"])

    def test_another_routine_searching_the_channel_stops_the_polls(self):
        root = ts_at(10)
        self.table[MINE] = [{"ch": OTHER, "uid": ME, "ts": root}]
        self.run_once(_routine_id="fast")
        self.assertEqual(len(self.reads), 1)
        # A lane that searches OTHER, without follow_threads, records the
        # followed thread's replies and publishes its coverage.
        self.table = {f"in:<#{OTHER}>": [
            {"ch": OTHER, "ts": ts_at(20), "root": root, "text": "late"},
        ]}
        self.seen.clear()
        self.run_once(
            now=NOW + 60, inbox={"channels": [CHAN]}, channels=[OTHER],
            _routine_id="wide",
        )
        self.assertNotIn(MINE, [a.get("filters") for a in self.seen])
        self.assertIn(ts_at(20), [r["ts"] for r in self.messages()])
        self.run_once(now=NOW + 86400, _routine_id="fast")
        self.assertEqual(len(self.reads), 1)
        # Coverage expires when that lane stops running.
        self.run_once(now=NOW + 3 * 86400, _routine_id="fast")
        self.assertEqual(len(self.reads), 2)

    def test_poll_cap(self):
        self.table[MINE] = [
            {"ch": OTHER, "uid": ME, "ts": ts_at(n)} for n in (10, 20, 30)
        ]
        self.run_once(inbox={"follow_threads": True, "thread_polls": 2})
        self.assertEqual(
            [r["message_ts"] for r in self.reads], [ts_at(30), ts_at(20)]
        )


class ReviewFixTest(InboxTestCase):
    def test_failed_inbox_search_does_not_mark_the_listing_partial(self):
        def search(args):
            if args["filters"] == f"hasmy::{ACK}:":
                return "not json"
            return search_text([{"ts": ts_at(10)}])

        self.connector.search = search
        runtime = {}
        listing = slack_mcp.candidates(inbox_source(
            {"channels": [CHAN], "reactions": [ACK]},
            _slack_mcp_runtime=runtime,
        ))
        self.assertFalse(listing.partial)
        self.assertEqual(runtime["failed_keys"], [])

    def test_capture_outage_still_fails_when_an_inbox_search_works(self):
        def search(args):
            if args["filters"] == f"hasmy::{ACK}:":
                return search_text([])
            return "not json"

        self.connector.search = search
        with self.assertRaisesRegex(slack_mcp.SlackMcpError, "every search"):
            slack_mcp.candidates(inbox_source(
                {"channels": [CHAN], "reactions": [ACK]},
            ))

    def test_shared_files_keep_the_longest_live_retention(self):
        old = f"{int(NOW) - 5 * 86400}.000100"
        comms_inbox.record(self.tmp.name, [message(old)], 14, now=NOW)
        follow = comms_inbox.load_follow(self.tmp.name)
        follow["covered"]["wide:x"] = {
            "at": NOW - 60, "channels": [], "retention_days": 14,
        }
        comms_inbox.save_follow(self.tmp.name, follow, 14, now=NOW)
        slack_mcp.candidates(inbox_source(
            {"channels": [CHAN], "retention_days": 2}
        ))
        self.assertIn(old, [r["ts"] for r in self.rows()])

    def test_save_emoji_is_searched_again_when_its_look_back_is_short(self):
        seen = []
        self.connector.search = lambda a: seen.append(a) or search_text([])
        slack_mcp.candidates(inbox_source(
            {"channels": [CHAN], "reactions": [SAVE]},
            save_reactions=[SAVE], save_reaction_lookback_days=2,
        ))
        afters = sorted(
            a["after"] for a in seen if a["filters"] == f"hasmy::{SAVE}:"
        )
        self.assertEqual(
            afters, [int(NOW) - 14 * 86400, int(NOW) - 2 * 86400]
        )

    def test_uncovered_inbox_channel_is_reported(self):
        slack_mcp._WARNED_UNCOVERED.clear()
        with mock.patch.object(slack_mcp, "log") as log:
            slack_mcp.candidates(inbox_source({"channels": [CHAN2]}))
        self.assertTrue(any(
            "searched by no query" in c.args[0] for c in log.call_args_list
        ))

    def test_malformed_state_files_count_as_empty(self):
        state = Path(self.tmp.name, "state")
        state.mkdir(parents=True, exist_ok=True)
        for body in ("[]", "null", '{"covered": [], "threads": 3}',
                     '{"conversations": {}, "scanned": []}'):
            with self.subTest(body=body):
                (state / comms_inbox.FOLLOW_FILE).write_text(body)
                (state / comms_inbox.TRACKED_FILE).write_text(body)
                self.assertEqual(
                    comms_inbox.load_follow(self.tmp.name)["threads"], {}
                )
                self.assertEqual(
                    comms_inbox.load_tracked(self.tmp.name)["conversations"],
                    [],
                )

    def test_error_results_back_off(self):
        self.connector.errors = lambda tool, args: tool == REACTIONS_TOOL
        comms_inbox.record(self.tmp.name, [message(ts_at(10))], 14, now=NOW)
        slack_mcp.candidates(inbox_source(reactions_tool=REACTIONS_TOOL))
        checks = json.loads(
            Path(self.tmp.name, "state", comms_inbox.CHECKS_FILE).read_text()
        )
        self.assertIn(f"{CHAN}:{ts_at(10)}", checks)


class ConfigTest(unittest.TestCase):
    def problems(self, **over):
        with tempfile.TemporaryDirectory() as tmp:
            return config.validate(routine(tmp, **over))

    def test_valid_block(self):
        self.assertEqual(self.problems(comms_inbox={
            "channels": [CHAN], "reactions": [ACK], "retention_days": 7,
        }), [])
        self.assertEqual(self.problems(
            all_dms=True, comms_inbox={"all_dms": True},
        ), [])

    def test_bad_blocks(self):
        for block in (
            [], {"channels": ["general"]},
            {"channels": [CHAN], "extra": 1},
            {"channels": [CHAN], "reactions": [":eyes:"]},
            {"channels": [CHAN], "reactions": ["a", "b", "c", "d", "e", "f"]},
            {"channels": [CHAN], "retention_days": 0},
            {"channels": [CHAN], "reaction_checks": 31},
            {"track_reactions": ["a", "b", "c", "d"]},
            {"reactions": [ACK], "track_reactions": [ACK]},
            {"channels": [CHAN], "reaction_check_hours": 0},
            {"all_dms": "yes"},
        ):
            with self.subTest(block=block):
                self.assertTrue(any(
                    "comms_inbox" in p for p in self.problems(comms_inbox=block)
                ))

    def test_reactions_tool_must_be_the_read_only_reader(self):
        self.assertEqual(self.problems(
            reactions_tool="mcp__example_slack__slack_get_reactions",
            comms_inbox={"channels": [CHAN], "reaction_checks": 5},
        ), [])
        for tool in (
            "mcp__s__slack_add_reaction", "mcp__s__slack_get_reactions_and_add",
            "slack_get_reactions", "mcp__s__slack_read_thread",
        ):
            with self.subTest(tool=tool):
                text = "\n".join(self.problems(reactions_tool=tool))
                self.assertIn("reactions_tool", text)

    def test_empty_block_is_valid(self):
        self.assertEqual(self.problems(comms_inbox={}), [])

    def test_track_reactions_alone_are_enough(self):
        self.assertEqual(self.problems(
            comms_inbox={"track_reactions": ["track-example"]},
        ), [])
        text = "\n".join(self.problems(
            max_queries_per_run=1,
            comms_inbox={"track_reactions": ["track-example"]},
        ))
        self.assertIn("2 planned searches", text)

    def test_follow_threads_alone_is_enough(self):
        self.assertEqual(self.problems(
            comms_inbox={"follow_threads": True, "thread_polls": 5},
        ), [])
        text = "\n".join(self.problems(
            max_queries_per_run=1, comms_inbox={"follow_threads": True},
        ))
        self.assertIn("2 planned searches", text)
        text = "\n".join(self.problems(
            comms_inbox={"follow_threads": "yes"},
        ))
        self.assertIn("follow_threads", text)

    def test_inbox_all_dms_needs_the_dms_query(self):
        text = "\n".join(self.problems(comms_inbox={"all_dms": True}))
        self.assertIn("needs all_dms: true", text)

    def test_reaction_queries_count_in_the_plan(self):
        text = "\n".join(self.problems(
            max_queries_per_run=1,
            comms_inbox={"channels": [CHAN], "reactions": [ACK]},
        ))
        self.assertIn("2 planned searches", text)

    def test_watch_file_carries_the_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.yaml"
            path.write_text(
                f"channels: [{CHAN}]\n"
                f"comms_inbox: {{channels: [{CHAN}], reactions: [{ACK}]}}\n"
            )
            path.chmod(0o600)
            merged, err = slack_watch.expand(
                {"kind": "slack_mcp", "watch": str(path)}
            )
            self.assertIsNone(err)
            self.assertEqual(merged["comms_inbox"]["reactions"], [ACK])
            merged, _ = slack_watch.expand({"kind": "slack", "watch": str(path)})
            self.assertNotIn("comms_inbox", merged)
            path.write_text(f"channels: [{CHAN}]\ncomms_inbox: {{}}\n")
            merged, err = slack_watch.expand(
                {"kind": "slack_mcp", "watch": str(path)}
            )
            self.assertIsNone(err)
            self.assertEqual(merged["comms_inbox"], {})
            path.write_text(f"channels: [{CHAN}]\n")
            merged, _ = slack_watch.expand(
                {"kind": "slack_mcp", "watch": str(path)}
            )
            self.assertNotIn("comms_inbox", merged)
            path.write_text("comms_inbox: {retention_days: 0}\n")
            with self.assertRaises(slack_watch.WatchError):
                slack_watch.load(str(path))


if __name__ == "__main__":
    unittest.main()
