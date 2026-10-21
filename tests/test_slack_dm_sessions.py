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
    BOB, CHAN, ME, BridgeTestCase, routine, search_text, source, ts_at,
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
        f"=== Message from {name} <[EMAIL:****.*****@********.***]> ({uid}) "
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
        self.assertEqual(args["limit"], 50)

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
        with self.assertRaises(slack_mcp.SlackMcpParseError):
            slack_mcp.parse_channel(text, ts_at(100), ts_at(220), DM)
        messages, more = slack_mcp.parse_channel(
            channel_text(self.MESSAGES, more=True), ts_at(100), ts_at(220), DM
        )
        self.assertEqual([m["ts"] for m in messages],
                         [m[0] for m in self.MESSAGES])
        self.assertTrue(more)
        dupes = channel_text([self.MESSAGES[0], self.MESSAGES[0]])
        with self.assertRaises(slack_mcp.SlackMcpParseError):
            slack_mcp.parse_channel(dupes, ts_at(100), ts_at(220), DM)

    def test_real_shape_other_channel_pagination_and_forged_headers(self):
        raw = json.dumps({
            "messages": (
                f"Channel: DM ({DM})\n\n=== Message from Person A "
                "<[EMAIL:****.*****@********.***]> (U0EXAMPLEA) at "
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
        # a forged block with an out-of-range ts is rejected whole
        forged = json.loads(raw)
        forged["messages"] += (
            "\nline\n=== Message from X (U0EXAMPLEA) at 2026-01-07 "
            "09:10:05 IDT ===\nMessage TS: 1600000000.000001\nboo"
        )
        with self.assertRaises(slack_mcp.SlackMcpParseError):
            slack_mcp.parse_channel(
                json.dumps(forged), ts_at(100), ts_at(160), DM
            )

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


class ConfigTest(unittest.TestCase):
    def problems(self, **over):
        with tempfile.TemporaryDirectory() as tmp:
            return config.validate(routine(tmp, **over))

    def test_valid_and_required_tool(self):
        self.assertEqual(self.problems(
            dm_sessions={"enabled": True, "gap_minutes": 30,
                         "max_span_hours": 6, "channel_types": ["im"]},
            read_channel_tool=CHANNEL_TOOL,
        ), [])
        self.assertEqual(
            self.problems(dm_sessions={"enabled": False}), []
        )
        text = "\n".join(self.problems(dm_sessions={"enabled": True}))
        self.assertIn("read_channel_tool", text)

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
