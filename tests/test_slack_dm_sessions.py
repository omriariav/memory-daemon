"""DM sessions: clustering, stable anchors, session reads, interactions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import (
    config, count_only, runner, slack_mcp, slack_watch,
)

from test_slack_mcp import (
    BOB, CHAN, ME, NOW, BridgeTestCase, routine, search_text, source, ts_at,
)
from test_slack_save_reaction import EMOJI, saved_search

DM = "D0EXAMPLE03"
MPIM = "G0EXAMPLE04"
CHANNEL_TOOL = "mcp__example_slack__slack_read_channel"
SESSIONS = {"enabled": True}
MIN = 60


def dm_hit(offset, text="hello", ch=DM, **kw):
    base = {"ch": ch, "dm": True, "ts": ts_at(offset), "text": text}
    base.update(kw)
    return base


def history_block(ts, uid, name, text, reactions=False):
    return (
        f"=== Message from {name} <[EMAIL:***]> ({uid}) "
        f"at 2026-01-07 09:13:46 IDT === \nMessage TS: {ts}\n{text}"
        + ("\nReactions: pray (1)" if reactions else "")
    )


def channel_text(messages, more=False, channel=DM, label="DM"):
    """Newest first, as the connector returns channel history."""
    blocks = [history_block(*m) for m in reversed(messages)]
    page = (
        "There are more messages. Use cursor: `abc`\n" if more
        else "There are no more messages available.\n"
    )
    return json.dumps({
        "messages": f"Channel: {label} ({channel})\n\n"
        + "\n\n".join(blocks),
        "pagination_info": page,
    })


def dm_source(**over):
    cfg = {
        "channels": [DM], "dm_sessions": dict(SESSIONS),
        "read_channel_tool": CHANNEL_TOOL,
    }
    cfg.update(over)
    return source(**cfg)


class SessionCase(BridgeTestCase):
    def listing(self, hits, ordinary_only=True, **over):
        self.connector.search = lambda a: search_text(list(hits))
        return slack_mcp.candidates(dm_source(**over))

    def anchors(self, listing):
        return sorted(c["raw"]["source_id"] for c in listing)


class ClusteringTest(SessionCase):
    def test_messages_within_gaps_are_one_candidate(self):
        hits = [dm_hit(o) for o in (100, 160, 220, 400, 460)]
        out = self.listing(hits)
        self.assertEqual(len(out), 1)
        candidate = out[0]
        self.assertEqual(
            candidate["id"], f"slack:{DM}:{ts_at(100)}@{ts_at(460)}"
        )
        self.assertEqual(candidate["raw"]["source_id"], f"slack:{DM}:{ts_at(100)}")
        self.assertTrue(candidate["raw"]["dm_session"])
        self.assertEqual(candidate["raw"]["anchor"], ts_at(100))
        self.assertIn("DM session", candidate["title"])

    def test_pause_longer_than_the_gap_makes_two_sessions(self):
        hits = [dm_hit(100), dm_hit(160), dm_hit(160 + 121 * MIN),
                dm_hit(220 + 121 * MIN)]
        out = self.listing(hits)
        self.assertEqual(
            self.anchors(out),
            sorted([f"slack:{DM}:{ts_at(100)}",
                    f"slack:{DM}:{ts_at(160 + 121 * MIN)}"]),
        )

    def test_gap_boundary_is_inclusive(self):
        out = self.listing([dm_hit(100), dm_hit(100 + 120 * MIN)])
        self.assertEqual(len(out), 1)

    def test_gap_minutes_is_configurable(self):
        hits = [dm_hit(100), dm_hit(100 + 11 * MIN)]
        out = self.listing(hits, dm_sessions={"enabled": True, "gap_minutes": 10})
        self.assertEqual(len(out), 2)

    def test_max_span_splits_a_long_conversation(self):
        # One message every 100 minutes never exceeds the gap, but the
        # session may span at most 12 hours.
        offsets = [100 + n * 100 * MIN for n in range(15)]
        out = self.listing([dm_hit(o) for o in offsets])
        self.assertEqual(len(out), 2)
        first, second = sorted(out, key=lambda c: float(c["raw"]["anchor"]))
        self.assertEqual(first["raw"]["anchor"], ts_at(offsets[0]))
        last_in_first = max(o for o in offsets if o - offsets[0] <= 12 * 3600)
        self.assertEqual(first["id"].split("@")[1], ts_at(last_in_first))
        self.assertEqual(
            second["raw"]["anchor"],
            ts_at(min(o for o in offsets if o - offsets[0] > 12 * 3600)),
        )

    def test_disabled_flag_keeps_one_candidate_per_message(self):
        hits = [dm_hit(o) for o in (100, 160, 220)]
        out = self.listing(hits, dm_sessions={"enabled": False})
        self.assertEqual(len(out), 3)
        self.assertFalse(any(c["raw"].get("dm_session") for c in out))
        out = self.listing(hits, dm_sessions=None)
        self.assertEqual(len(out), 3)
        self.assertFalse(
            Path(self.tmp.name, "state", "slack-mcp-dm-sessions.json").exists()
        )

    def test_channels_are_unaffected(self):
        hits = [{"ts": ts_at(o)} for o in (100, 160, 220)]
        self.connector.search = lambda a: search_text(hits)
        out = slack_mcp.candidates(dm_source(channels=[CHAN]))
        self.assertEqual(len(out), 3)
        self.assertFalse(any(c["raw"].get("dm_session") for c in out))

    def test_threaded_dm_messages_keep_per_thread_behaviour(self):
        root = ts_at(100)
        hits = [
            dm_hit(100, replies=2),                        # parent with replies
            dm_hit(150, root=root),                        # a reply
            dm_hit(5000, text="other", replies=0, root=ts_at(4000)),
        ]
        out = self.listing(hits)
        by_id = {c["raw"]["source_id"]: c for c in out}
        self.assertEqual(
            set(by_id), {f"slack:{DM}:{root}", f"slack:{DM}:{ts_at(4000)}"}
        )
        self.assertFalse(any(c["raw"].get("dm_session") for c in out))

    def test_legacy_private_channel_is_not_a_dm(self):
        hit = {"channel": MPIM, "label": "Private", "replies": 0,
               "ts": ts_at(1), "root": ts_at(1), "is_bot": False}
        self.assertIsNone(slack_mcp.dm_kind(hit))
        self.assertEqual(slack_mcp.dm_kind(dict(hit, label="DM")), None)
        self.assertEqual(slack_mcp.dm_kind(dict(hit, label="Group DM")), "mpim")
        self.assertEqual(
            slack_mcp.dm_kind(dict(hit, channel=DM, label="DM")), "im"
        )

    def test_group_dm_sessions_and_channel_types(self):
        hits = [dm_hit(100, ch=MPIM, label="Group DM", people=[BOB, ME]),
                dm_hit(160, ch=MPIM, label="Group DM", people=[BOB, ME])]
        self.connector.search = lambda a: search_text(list(hits))
        out = slack_mcp.candidates(dm_source(channels=[MPIM]))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["raw"]["dm_session"])
        out = slack_mcp.candidates(dm_source(
            channels=[MPIM],
            dm_sessions={"enabled": True, "channel_types": ["im"]},
        ))
        self.assertEqual(len(out), 2)

    def test_bot_messages_never_anchor_a_session(self):
        hits = [dm_hit(100, bot=True), dm_hit(160)]
        out = self.listing(hits)
        self.assertEqual(self.anchors(out), [f"slack:{DM}:{ts_at(160)}"])


class AcrossRunsTest(SessionCase):
    def test_growing_session_keeps_its_anchor_and_entry(self):
        first = self.listing([dm_hit(100), dm_hit(160)])
        self.assertEqual(len(first), 1)
        again = self.listing([dm_hit(100 + 90 * MIN)])
        self.assertEqual(len(again), 1)
        self.assertEqual(
            again[0]["raw"]["source_id"], first[0]["raw"]["source_id"]
        )
        self.assertNotEqual(again[0]["id"], first[0]["id"])
        self.assertEqual(
            again[0]["id"],
            f"slack:{DM}:{ts_at(100)}@{ts_at(100 + 90 * MIN)}",
        )

    def test_late_message_after_the_gap_starts_a_new_session(self):
        first = self.listing([dm_hit(100), dm_hit(160)])
        later = self.listing([dm_hit(160 + 130 * MIN)])
        self.assertNotEqual(
            later[0]["raw"]["source_id"], first[0]["raw"]["source_id"]
        )

    def test_overlap_relist_of_a_processed_session_keeps_its_version(self):
        first = self.listing([dm_hit(100), dm_hit(400)])
        # The overlap window lists only the older message again.
        again = self.listing([dm_hit(100)])
        self.assertEqual(again[0]["id"], first[0]["id"])
        store = {first[0]["id"]: {"processed_at": "x"}}
        out = self.listing([dm_hit(100)], _processed=_Ledger(store))
        self.assertEqual(list(out), [])

    def test_span_limit_applies_when_attaching(self):
        self.listing([dm_hit(100)])
        # 11h later is still inside the gap chain only if linked; a single
        # message 13h after the anchor never extends it.
        out = self.listing([dm_hit(100 + 13 * 3600)],
                           dm_sessions={"enabled": True, "gap_minutes": 1440})
        self.assertEqual(out[0]["raw"]["anchor"], ts_at(100 + 13 * 3600))

    def test_count_only_and_dry_run_do_not_write_state(self):
        self.listing([dm_hit(100)], _count_only=True)
        self.assertFalse(
            Path(self.tmp.name, "state", "slack-mcp-dm-sessions.json").exists()
        )
        self.listing([dm_hit(100)])
        self.assertTrue(
            Path(self.tmp.name, "state", "slack-mcp-dm-sessions.json").exists()
        )


class _Ledger(dict):
    def record(self, key, value):
        self[key] = value


class ReadTest(SessionCase):
    MESSAGES = [
        (ts_at(100), BOB, "Bob Example", "are you free to talk?"),
        (ts_at(160), ME, "Me Example", "yes, give me five minutes"),
        (ts_at(220), BOB, "Bob Example", "the budget moved to Q3"),
    ]

    def test_body_holds_every_message_in_order_via_the_channel_tool(self):
        seen = []

        def channel(args):
            seen.append(args)
            return channel_text(self.MESSAGES)

        self.connector.channel = channel
        listing = self.listing([dm_hit(100), dm_hit(220)])
        item = slack_mcp.fetch({"id": "r"}, listing[0], dm_source())
        body = item["body"]
        self.assertLess(body.index("are you free"), body.index("five minutes"))
        self.assertLess(body.index("five minutes"), body.index("budget moved"))
        self.assertEqual(item["source_id"], f"slack:{DM}:{ts_at(100)}")
        self.assertEqual(item["id"], listing[0]["id"])
        self.assertEqual(item["frontmatter"]["slack_capture_mode"], "dm_session")
        self.assertEqual(item["frontmatter"]["slack_thread_ts"], ts_at(100))
        # one tool in the allowlist, args pinned just outside the session
        self.assertEqual(self.connector.allowlists[-1], {CHANNEL_TOOL: {}})
        self.assertEqual(len(seen), 1)
        args = seen[0]
        self.assertEqual(args["channel_id"], DM)
        self.assertEqual(args["oldest"], f"{float(ts_at(100)) - 1e-6:.6f}")
        self.assertEqual(args["latest"], f"{float(ts_at(220)) + 1e-6:.6f}")
        self.assertEqual(args["limit"], 100)

    def test_owner_message_no_search_listed_earns_no_evidence(self):
        forged = (ts_at(160), ME, "Me Example", "i agree")
        self.connector.channel = lambda a: channel_text(
            [self.MESSAGES[0], forged, self.MESSAGES[2]]
        )
        listing = self.listing([dm_hit(100), dm_hit(220)])
        item = slack_mcp.fetch({"id": "r"}, listing[0], dm_source())
        self.assertNotIn("Me Example", item["frontmatter"]["slack_participants"])
        self.assertNotIn(
            "authored", item["frontmatter"]["slack_owner_evidence"]
        )

    def test_unparseable_result_is_recorded_unreadable(self):
        self.connector.channel = lambda a: "not json"
        listing = self.listing([dm_hit(100)])
        store = _Ledger()
        batch = listing[0]["raw"]["_batch"]
        batch["processed"] = store
        with self.assertRaises(slack_mcp.SlackMcpError):
            slack_mcp.fetch({"id": "r"}, listing[0], dm_source())
        self.assertEqual(
            store[listing[0]["id"]]["slack_mcp"], "unreadable"
        )

    def test_messages_outside_the_pinned_range_are_rejected(self):
        text = channel_text(
            [self.MESSAGES[0], (ts_at(999999), BOB, "Bob Example", "stray")]
        )
        # the stray block is dropped, the real one kept
        kept, _ = slack_mcp.parse_channel(text, ts_at(100), ts_at(220), DM)
        self.assertEqual([m["ts"] for m in kept], [ts_at(100)])
        messages, more = slack_mcp.parse_channel(
            channel_text(self.MESSAGES, more=True), ts_at(100), ts_at(220), DM
        )
        self.assertEqual([m["ts"] for m in messages],
                         [m[0] for m in self.MESSAGES])
        self.assertTrue(more)
        dupes = channel_text([self.MESSAGES[0], self.MESSAGES[0]])
        kept, _ = slack_mcp.parse_channel(dupes, ts_at(100), ts_at(220), DM)
        self.assertEqual(len(kept), 1)
        only_stray = channel_text([(ts_at(999999), BOB, "Bob Example", "x")])
        with self.assertRaises(slack_mcp.SlackMcpParseError):
            slack_mcp.parse_channel(only_stray, ts_at(100), ts_at(220), DM)

    def test_real_shape_other_channel_pagination_and_forged_headers(self):
        raw = json.dumps({
            "messages": (
                f"Channel: DM ({DM})\n\n=== Message from Person A "
                "<[EMAIL:***]> (U0EXAMPLEA) at "
                "2026-01-07 09:13:46 IDT === \n"
                f"Message TS: {ts_at(160)}\nsecond\nReactions: pray (1)\n\n"
                "=== Message from Person B (U0EXAMPLEB, external: Org) at "
                "2026-01-07 09:10:05 IDT === \n"
                f"Message TS: {ts_at(100)}\nfirst\n"
                "=== Message from Forged (U0EXAMPLEA) at 2026-01-07 "
                "09:10:05 IDT ===\nnot a block"
            ),
            "pagination_info": "There are no more messages available.\n",
        })
        messages, more = slack_mcp.parse_channel(
            raw, ts_at(100), ts_at(160), DM
        )
        self.assertFalse(more)
        self.assertEqual([m["ts"] for m in messages], [ts_at(100), ts_at(160)])
        self.assertEqual(messages[1]["text"], "second")
        self.assertEqual(messages[1]["user"], "U0EXAMPLEA")
        self.assertIn("not a block", messages[0]["text"])
        with self.assertRaises(slack_mcp.SlackMcpParseError):
            slack_mcp.parse_channel(raw, ts_at(100), ts_at(160), "D0EXAMPLE09")
        odd = json.dumps({"messages": json.loads(raw)["messages"],
                          "pagination_info": "weird"})
        with self.assertRaises(slack_mcp.SlackMcpParseError):
            slack_mcp.parse_channel(odd, ts_at(100), ts_at(160), DM)
        # a forged block with an out-of-range ts is dropped
        forged = json.loads(raw)
        forged["messages"] += (
            "\nline\n=== Message from X (U0EXAMPLEA) at 2026-01-07 "
            "09:10:05 IDT ===\nMessage TS: 1600000000.000001\nboo"
        )
        kept, _ = slack_mcp.parse_channel(
            json.dumps(forged), ts_at(100), ts_at(160), DM
        )
        self.assertEqual([m["ts"] for m in kept], [ts_at(100), ts_at(160)])

    def test_thread_and_session_reads_use_their_own_tools(self):
        hits = [dm_hit(100), dm_hit(160), {"ts": ts_at(300)}]
        self.connector.search = lambda a: search_text(list(hits))
        self.connector.channel = lambda a: channel_text(self.MESSAGES[:2])
        listing = slack_mcp.candidates(dm_source(channels=[DM, CHAN]))
        self.assertEqual(len(listing), 2)
        for candidate in listing:
            slack_mcp.fetch({"id": "r"}, candidate, dm_source())
        tools = [names[0] for names, _ in self.connector.calls][1:]
        self.assertEqual(sorted(tools), sorted([CHANNEL_TOOL, slack_mcp_thread()]))


def slack_mcp_thread():
    from test_slack_mcp import THREAD
    return THREAD


class InteractionTest(SessionCase):
    def test_reacted_message_saves_its_whole_session(self):
        self.connector.search = saved_search(
            [dm_hit(160)], ordinary=[dm_hit(100), dm_hit(160), dm_hit(220)]
        )
        out = slack_mcp.candidates(dm_source(save_reactions=[EMOJI]))
        self.assertEqual(len(out), 1)
        raw = out[0]["raw"]
        self.assertEqual(out[0]["id"], f"saved:slack:{DM}:{ts_at(100)}")
        self.assertEqual(raw["source_id"], f"slack:{DM}:{ts_at(100)}")
        self.assertTrue(raw["saved"] and raw["dm_session"])
        self.assertEqual(raw["session_last"], ts_at(220))

    def test_saved_session_covers_ordinary_hits_it_already_read(self):
        self.connector.search = saved_search(
            [dm_hit(160)], ordinary=[dm_hit(100), dm_hit(160)]
        )
        store = _Ledger({
            f"saved:slack:{DM}:{ts_at(100)}": {
                "processed_at": "x", "saved_covered_ts": ts_at(160),
            },
        })
        out = slack_mcp.candidates(dm_source(
            save_reactions=[EMOJI], _processed=store,
        ))
        self.assertEqual(list(out), [])

    def test_trivial_only_session_is_skipped_as_one(self):
        self.connector.channel = lambda a: channel_text([
            (ts_at(100), BOB, "Bob Example", "hi"),
            (ts_at(160), ME, "Me Example", "hey"),
            (ts_at(220), BOB, "Bob Example", "thanks"),
        ])
        listing = self.listing_for([100, 160, 220])
        self.assertEqual(len(listing), 1)
        item = slack_mcp.fetch({"id": "r"}, listing[0], dm_source())
        r = {"id": "r", "analyze": {"provider": "claude", "model": "haiku",
                                    "instruction": "x", "skip_trivial": True},
             "memory": {"store": "/store"}}
        item["source_kind"] = "slack_mcp"
        self.assertTrue(runner._trivial_verdict(r, item))
        self.connector.channel = lambda a: channel_text([
            (ts_at(100), BOB, "Bob Example", "hi"),
            (ts_at(160), ME, "Me Example", "which floor is the meeting on?"),
            (ts_at(220), BOB, "Bob Example", "thanks"),
        ])
        listing = self.listing_for([100, 160, 220], base=5000)
        item = slack_mcp.fetch({"id": "r"}, listing[0], dm_source())
        item["source_kind"] = "slack_mcp"
        self.assertIsNone(runner._trivial_verdict(r, item))

    def listing_for(self, offsets, base=0):
        self.connector.search = lambda a: search_text(
            [dm_hit(o) for o in offsets]
        )
        # a fresh channel state so both sessions start clean
        for path in Path(self.tmp.name, "state").glob("slack-mcp-dm-*"):
            path.unlink()
        return slack_mcp.candidates(dm_source())

    def test_count_only_reports_sessions_apart_from_threads(self):
        def claim(session):
            raw = {"dm_session": True} if session else {}
            return {"source": {"kind": "slack_mcp"},
                    "candidate": {"id": f"x{session}", "raw": raw}}

        totals = {"matched": 0, "processed": 0, "skipped": 0}
        with mock.patch.object(count_only, "log") as log:
            count_only.report(
                {"r": [claim(True), claim(False)]}, {}, "/nonexistent", totals
            )
        line = log.call_args_list[0].args[0]
        self.assertIn("1 Slack thread(s), 1 DM session(s)", line)


class ReviewFixesTest(SessionCase):
    A = (ts_at(100), BOB, "Bob Example", "anchor message")
    B = (ts_at(160), ME, "Me Example", "second message")
    C = (ts_at(220), BOB, "Bob Example", "third message")

    def state_file(self):
        return Path(self.tmp.name, "state", "slack-mcp-dm-sessions.json")

    def test_thread_on_the_anchor_is_read_into_the_session(self):
        self.listing([dm_hit(100), dm_hit(160)])
        thread_calls = []

        def thread(args):
            thread_calls.append(args)
            from test_slack_mcp import thread_text
            return thread_text(
                root=ts_at(100), parent_text="anchor message",
                replies=[(ts_at(300), BOB, "a reply in the thread")],
            )

        self.connector.thread = thread
        self.connector.channel = lambda a: channel_text([self.A, self.B])
        hits = [
            dm_hit(100, replies=1), dm_hit(220),
            dm_hit(300, root=ts_at(100)),
        ]
        out = self.listing(hits)
        self.assertEqual(len(out), 1)
        candidate = out[0]
        raw = candidate["raw"]
        self.assertTrue(raw["dm_session"] and raw["anchor_thread"])
        self.assertEqual(raw["source_id"], f"slack:{DM}:{ts_at(100)}")
        self.assertEqual(candidate["id"].split("@")[1], ts_at(300))
        self.connector.channel = lambda a: channel_text(
            [self.A, self.B, self.C]
        )
        item = slack_mcp.fetch({"id": "r"}, candidate, dm_source())
        body = item["body"]
        for text in ("anchor message", "second message", "third message",
                     "a reply in the thread"):
            self.assertIn(text, body)
        self.assertEqual(len(thread_calls), 1)
        self.assertEqual(item["frontmatter"]["slack_capture_mode"], "dm_session")

    def test_late_found_earlier_message_joins_and_anchor_is_fixed(self):
        first = self.listing([dm_hit(1000), dm_hit(1060)])
        anchor = first[0]["raw"]["anchor"]
        out = self.listing([dm_hit(700)])  # 5 min before the first message
        self.assertEqual(len(out), 1)
        raw = out[0]["raw"]
        self.assertEqual(raw["anchor"], anchor)
        self.assertEqual(raw["session_first"], ts_at(700))
        self.assertEqual(raw["session_last"], ts_at(1060))
        self.assertEqual(out[0]["id"], first[0]["id"] + f"~{ts_at(700)}")

    def test_reply_only_run_keeps_the_session_identity_and_body(self):
        self.listing([dm_hit(100), dm_hit(160)])
        from test_slack_mcp import thread_text
        self.connector.thread = lambda a: thread_text(
            root=ts_at(100), parent_text="anchor message",
            replies=[(ts_at(300), BOB, "late thread reply")],
        )
        self.connector.channel = lambda a: channel_text([self.A, self.B])
        for hits in (
            [dm_hit(300, root=ts_at(100))],
            [dm_hit(100, replies=1), dm_hit(300, root=ts_at(100))],
        ):
            out = self.listing(hits)
            self.assertEqual(len(out), 1)
            raw = out[0]["raw"]
            self.assertTrue(raw["dm_session"] and raw["anchor_thread"])
            self.assertEqual(out[0]["id"], f"slack:{DM}:{ts_at(100)}@{ts_at(300)}")
            item = slack_mcp.fetch({"id": "r"}, out[0], dm_source())
            for text in ("anchor message", "second message",
                         "late thread reply"):
                self.assertIn(text, item["body"])
            self.assertEqual(item["frontmatter"]["slack_capture_mode"],
                             "dm_session")

    def test_thread_on_another_message_stays_a_thread(self):
        self.listing([dm_hit(100), dm_hit(160)])
        out = self.listing([dm_hit(300, root=ts_at(160))])
        self.assertFalse(out[0]["raw"].get("dm_session"))

    def test_moved_first_is_a_new_version_processed_once(self):
        first = self.listing([dm_hit(1000), dm_hit(1060)])
        ledger = _Ledger({first[0]["id"]: {"processed_at": "x"}})
        out = self.listing([dm_hit(700)], _processed=ledger)
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0]["id"],
            f"slack:{DM}:{ts_at(1000)}@{ts_at(1060)}~{ts_at(700)}",
        )
        self.assertEqual(out[0]["raw"]["source_id"], first[0]["raw"]["source_id"])
        ledger[out[0]["id"]] = {"processed_at": "y"}
        again = self.listing([dm_hit(700), dm_hit(1000)], _processed=ledger)
        self.assertEqual(list(again), [])
        # failure holds parse the version stamp through the suffix
        runtime = {}
        candidate = dict(out[0], raw=dict(
            out[0]["raw"], _batch={"runtime": runtime}
        ))
        self.assertTrue(slack_mcp.note_failure(candidate))
        self.assertEqual(
            set(runtime["failed_floor"].values()), {float(ts_at(1060))}
        )

    def test_state_is_checked_writable_before_any_search(self):
        with mock.patch.object(
            slack_mcp.state, "write_atomic", side_effect=OSError("disk")
        ):
            with self.assertRaises(slack_mcp.SlackMcpError):
                self.listing([dm_hit(100)])
        self.assertEqual(self.connector.calls, [])

    def test_late_found_message_respects_the_span_cap(self):
        self.listing([dm_hit(12 * 3600 + 200)])
        out = self.listing(
            [dm_hit(100)],
            dm_sessions={"enabled": True, "gap_minutes": 1440},
        )
        self.assertEqual(out[0]["raw"]["anchor"], ts_at(100))

    def test_prune_is_by_time_not_count(self):
        # 25 sessions inside the look-back all survive; old ones go.
        base = int(NOW - 3600) - 1700000000
        hits = [dm_hit(base - n * 3 * 3600) for n in range(25)]
        self.listing(hits, bootstrap="30d", overlap="1h")
        data = json.loads(self.state_file().read_text())
        self.assertEqual(len(data["default"][DM]), 25)
        old = dm_hit(base - 40 * 86400)
        self.listing([old, dm_hit(base)], bootstrap="3d")
        data = json.loads(self.state_file().read_text())
        self.assertTrue(all(
            float(s["last"]) >= NOW - 4 * 86400 - 14 * 86400
            for s in data["default"][DM]
        ))
        self.assertNotIn(ts_at(base - 40 * 86400), [
            s["anchor"] for s in data["default"][DM]
        ])

    def test_state_is_keyed_by_routine(self):
        self.listing([dm_hit(100)], _routine_id="one")
        self.listing([dm_hit(100)], _routine_id="two")
        data = json.loads(self.state_file().read_text())
        self.assertEqual(set(data), {"one", "two"})

    def test_state_write_failure_fails_the_run_closed(self):
        with mock.patch.object(
            slack_mcp.state, "write_atomic", side_effect=OSError("disk")
        ):
            with self.assertRaises(slack_mcp.SlackMcpError):
                self.listing([dm_hit(100)])

    def test_long_session_follows_the_cursor_to_its_first_message(self):
        later = [self.B, self.C]
        earlier = [self.A]
        pages = []

        def channel(args):
            pages.append(args)
            if args.get("cursor") == "abc":
                return channel_text(earlier)
            return channel_text(later, more=True)

        self.connector.channel = channel
        listing = self.listing([dm_hit(100), dm_hit(220)])
        item = slack_mcp.fetch({"id": "r"}, listing[0], dm_source())
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["cursor"], "abc")
        self.assertIn("anchor message", item["body"])
        self.assertNotIn("earlier messages omitted", item["body"])

    def test_page_cap_marks_the_omission_without_a_parent(self):
        self.connector.channel = lambda a: channel_text(
            [self.B, self.C], more=True
        )
        listing = self.listing(
            [dm_hit(100), dm_hit(220)], max_session_pages=2
        )
        item = slack_mcp.fetch(
            {"id": "r"}, listing[0], dm_source(max_session_pages=2)
        )
        self.assertEqual(len(self.connector.calls), 3)  # search + 2 pages
        self.assertIn("[earlier messages omitted]", item["body"])
        self.assertNotIn("anchor message", item["body"])

    def test_plan_cost_counts_dm_reads_as_their_own_sessions(self):
        sessions, _ = slack_mcp.plan_cost(2, "/nonexistent", None, 0.1, 1)
        self.assertEqual(sessions, 2)
        sessions, _ = slack_mcp.plan_cost(2, "/nonexistent", None, 0.1, 0)
        self.assertEqual(sessions, 1)


class ConfigTest(unittest.TestCase):
    def problems(self, **over):
        with tempfile.TemporaryDirectory() as tmp:
            return config.validate(routine(tmp, **over))

    def test_valid_and_required_tool(self):
        self.assertEqual(self.problems(
            dm_sessions={"enabled": True, "gap_minutes": 30,
                         "max_span_hours": 6, "channel_types": ["im"]},
            read_channel_tool=CHANNEL_TOOL, all_dms=True,
        ), [])
        self.assertEqual(
            self.problems(dm_sessions={"enabled": False}), []
        )
        text = "\n".join(self.problems(
            dm_sessions={"enabled": True}, all_dms=True,
        ))
        self.assertIn("read_channel_tool", text)
        text = "\n".join(self.problems(
            dm_sessions={"enabled": True}, read_channel_tool=CHANNEL_TOOL,
        ))
        self.assertIn("needs all_dms", text)
        self.assertIn("max_session_pages", "\n".join(self.problems(
            max_session_pages=0,
        )))

    def test_invalid_values(self):
        for value in (
            "yes", {"enabled": "yes"}, {"gap_minutes": 0},
            {"gap_minutes": True}, {"max_span_hours": 100},
            {"channel_types": []}, {"channel_types": ["channel"]},
            {"bogus": 1},
        ):
            with self.subTest(value=value):
                text = "\n".join(self.problems(
                    dm_sessions=value, read_channel_tool=CHANNEL_TOOL,
                ))
                self.assertIn("dm_sessions", text)

    def test_tool_must_be_the_read_only_channel_reader(self):
        for tool in ("mcp__s__slack_send_message", "mcp__s__slack_read_thread",
                     "mcp__s__slack_read_channel_and_update"):
            with self.subTest(tool=tool):
                text = "\n".join(self.problems(read_channel_tool=tool))
                self.assertIn("read_channel_tool", text)

    def test_watch_file_carries_dm_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watch.yaml"
            path.write_text(
                "channels: [C0EXAMPLE01]\n"
                "dm_sessions: {enabled: true, gap_minutes: 45}\n"
            )
            path.chmod(0o600)
            loaded = slack_watch.load(str(path))
            self.assertEqual(
                loaded["dm_sessions"], {"enabled": True, "gap_minutes": 45}
            )
            merged, err = slack_watch.expand(
                {"kind": "slack_mcp", "watch": str(path)}
            )
            self.assertIsNone(err)
            self.assertEqual(merged["dm_sessions"]["gap_minutes"], 45)
            merged, _ = slack_watch.expand({"kind": "slack", "watch": str(path)})
            self.assertNotIn("dm_sessions", merged)
            path.write_text("dm_sessions: {gap_minutes: 0}\n")
            with self.assertRaises(slack_watch.WatchError):
                slack_watch.load(str(path))

    def test_settings_defaults(self):
        cfg = slack_mcp.settings(source())
        self.assertEqual(cfg["dm_sessions"], {
            "enabled": False, "gap_minutes": 120, "max_span_hours": 12,
            "channel_types": ["im", "mpim"],
        })
        cfg = slack_mcp.settings(source(dm_sessions={"enabled": True}))
        self.assertTrue(cfg["dm_sessions"]["enabled"])
        self.assertEqual(cfg["dm_sessions"]["gap_minutes"], 120)


if __name__ == "__main__":
    unittest.main()
