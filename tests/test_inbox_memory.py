"""Inbox 0.8.0/0.8.1: items the daemon already saved to memory show a mark.

Synthetic data only (fake names, fake memory and Slack ids); nothing reads
live data. The non-Textual classes run without Textual."""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon.inbox import items as itm, settings, sources, writes
from workspace_daemon.inbox.actions import FakeActions, LocalActions
from workspace_daemon.inbox.store import InboxStore

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import HelpScreen
    from workspace_daemon.inbox.widgets import SAVED_GLYPH, THREAD_GLYPH, Queue

CHAN = "C" + "0123ABCDEF"   # fake; built so no literal looks like an id
THREAD_TS = "1700000000.000100"
MSG_TS = "1700000500.000200"
SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
NOW = time.time()


def event(source_id, memory_id=None, ts=None, **kw):
    row = {"id": f"ev-{source_id}-{memory_id}-{ts}", "source_id": source_id,
           "ts": ts or "2030-01-01T10:00:00Z"}
    if memory_id is not None:
        row["memory_id"] = memory_id
    row.update(kw)
    return row


def item(i=1, source="slack", **meta):
    return itm.make_item(
        id=f"t:{i}", source=source, section="fyi", urgency="digest",
        sender=f"Person {i}", where="#room", gist=f"gist {i}", ts=NOW - 60 * i,
        link=None, thread=[], draft=None, actions=["ack"], meta=meta)


class IndexTest(unittest.TestCase):
    def test_maps_source_ids_to_memory_ids_newest_wins(self):
        idx = sources.memory_index([
            event("slack:C1:1.0", "mem-old", "2030-01-01T10:00:00Z"),
            event("slack:C1:1.0", "mem-new", "2030-01-02T10:00:00Z"),
            event("gmail:abc", "mem-mail"),
        ])
        self.assertEqual(idx, {"slack:C1:1.0": "mem-new",
                               "gmail:abc": "mem-mail"})

    def test_rows_without_a_clean_pair_are_ignored(self):
        idx = sources.memory_index([
            event("slack:C1:1.0"),                     # no memory id
            event("slack:C2:1.0", "Bad Id"),           # malformed memory id
            event("slack:C3:1.0", "-lead"),
            event("slack:C4:1.0", 5),
            event("bad\nid", "mem-1"),                 # not a clean one-liner
            event("", "mem-2"), event(None, "mem-3"),
            event("x" * 400, "mem-4"),
            "junk", None, 7,
            event("slack:C9:1.0", "mem-ok")])
        self.assertEqual(idx, {"slack:C9:1.0": "mem-ok"})


class MatchTest(unittest.TestCase):
    INDEX = {f"slack:{CHAN}:{THREAD_TS}": "mem-thread",
             f"slack:{CHAN}:{MSG_TS}": "mem-msg",
             "gmail:abc": "mem-mail"}

    def find(self, meta, index=None):
        return sources.memory_id_for(meta, self.INDEX if index is None
                                     else index)

    def test_meta_memory_id_wins(self):
        self.assertEqual(self.find({"memory_id": "mem-own",
                                    "source_id": "gmail:abc"}), "mem-own")

    def test_a_malformed_meta_memory_id_is_not_used(self):
        self.assertEqual(self.find({"memory_id": "Not Valid",
                                    "source_id": "gmail:abc"}), "mem-mail")
        self.assertEqual(self.find({"memory_id": "Not Valid"}), "")

    def test_source_id_in_the_index(self):
        self.assertEqual(self.find({"source_id": "gmail:abc"}), "mem-mail")
        self.assertEqual(self.find({"source_id": "gmail:other"}), "")

    def test_slack_reply_level_then_message_level_are_exact(self):
        reply_id = f"slack:{CHAN}:{THREAD_TS}:{MSG_TS}"
        full = {"reply_to": {"channel": CHAN, "thread_ts": THREAD_TS,
                             "message_ts": MSG_TS}}
        idx = dict(self.INDEX, **{reply_id: "mem-reply"})
        self.assertEqual(sources.memory_match(full, idx), ("mem-reply", ""))
        # without a reply-level entry, the message itself is the next try
        self.assertEqual(sources.memory_match(full, self.INDEX),
                         ("mem-msg", ""))
        only_msg = {"reply_to": {"channel": CHAN, "message_ts": MSG_TS}}
        self.assertEqual(sources.memory_match(only_msg, self.INDEX),
                         ("mem-msg", ""))

    def test_a_saved_thread_root_is_a_thread_level_match_only(self):
        full = {"reply_to": {"channel": CHAN, "thread_ts": THREAD_TS,
                             "message_ts": MSG_TS}}
        idx = {f"slack:{CHAN}:{THREAD_TS}": "mem-thread"}
        self.assertEqual(sources.memory_match(full, idx), ("", "mem-thread"))
        only_thread = {"reply_to": {"channel": CHAN, "thread_ts": THREAD_TS}}
        self.assertEqual(sources.memory_match(only_thread, self.INDEX),
                         ("", "mem-thread"))
        self.assertEqual(sources.memory_id_for(only_thread, self.INDEX), "")

    def test_the_root_message_itself_is_exact(self):
        root = {"reply_to": {"channel": CHAN, "thread_ts": THREAD_TS,
                             "message_ts": THREAD_TS}}
        idx = {f"slack:{CHAN}:{THREAD_TS}": "mem-thread"}
        self.assertEqual(sources.memory_match(root, idx),
                         ("mem-thread", ""))

    def test_a_message_that_is_not_saved_has_no_match(self):
        meta = {"reply_to": {"channel": CHAN, "thread_ts": "1700000009.000900",
                             "message_ts": "1700000009.000901"}}
        self.assertEqual(sources.memory_match(meta, self.INDEX), ("", ""))

    def test_invalid_reply_to_never_matches(self):
        for reply in ({"channel": "bad", "thread_ts": THREAD_TS},
                      {"channel": CHAN, "thread_ts": "nope"},
                      {"thread_ts": THREAD_TS}, "x", None, []):
            self.assertEqual(self.find({"reply_to": reply}), "", reply)

    def test_fallback_thread_level_match_without_reply_to(self):
        meta = {"channel_id": CHAN, "thread_ts": THREAD_TS}
        self.assertEqual(sources.memory_match(meta, self.INDEX),
                         ("", "mem-thread"))
        self.assertEqual(sources.memory_id_for(meta, self.INDEX), "")

    def test_fallback_exact_match_via_message_ts(self):
        meta = {"channel_id": CHAN, "thread_ts": THREAD_TS,
                "message_ts": MSG_TS}
        self.assertEqual(sources.memory_match(meta, self.INDEX),
                         ("mem-msg", ""))
        reply_id = f"slack:{CHAN}:{THREAD_TS}:{MSG_TS}"
        idx = dict(self.INDEX, **{reply_id: "mem-reply"})
        self.assertEqual(sources.memory_match(meta, idx), ("mem-reply", ""))
        root = {"channel_id": CHAN, "thread_ts": THREAD_TS,
                "message_ts": THREAD_TS}
        self.assertEqual(sources.memory_match(root, self.INDEX),
                         ("mem-thread", ""))

    def test_reply_to_wins_over_the_fallback_keys(self):
        other = "C" + "9ZYXWVUT1"
        meta = {"reply_to": {"channel": CHAN, "thread_ts": THREAD_TS},
                "channel_id": other, "thread_ts": "1700000009.000900",
                "message_ts": MSG_TS}
        # reply_to's thread matches; the fallback message_ts is not used
        self.assertEqual(sources.memory_match(meta, self.INDEX),
                         ("", "mem-thread"))
        idx = {f"slack:{other}:1700000009.000900": "mem-other"}
        self.assertEqual(sources.memory_match(meta, idx), ("", ""))
        # a reply_to with a bad channel does not block the fallback
        bad = {"reply_to": {"channel": "bad"}, "channel_id": CHAN,
               "thread_ts": THREAD_TS}
        self.assertEqual(sources.memory_match(bad, self.INDEX),
                         ("", "mem-thread"))

    def test_invalid_fallback_values_are_ignored(self):
        for meta in ({"channel_id": "bad", "thread_ts": THREAD_TS},
                     {"channel_id": 5, "thread_ts": THREAD_TS},
                     {"thread_ts": THREAD_TS},
                     {"channel_id": CHAN},
                     {"channel_id": CHAN, "thread_ts": "nope",
                      "message_ts": "also-nope"},
                     {"channel_id": CHAN, "thread_ts": [THREAD_TS]}):
            self.assertEqual(sources.memory_match(meta, self.INDEX),
                             ("", ""), meta)
        # one bad stamp is dropped, the other still matches
        half = {"channel_id": CHAN, "thread_ts": "nope",
                "message_ts": MSG_TS}
        self.assertEqual(sources.memory_match(half, self.INDEX),
                         ("mem-msg", ""))

    def test_fallback_keys_never_make_a_send_destination(self):
        meta = {"channel_id": CHAN, "thread_ts": THREAD_TS,
                "message_ts": MSG_TS}
        self.assertIsNone(writes.reply_to(meta))
        for what in ("send", "draft"):
            plan = writes.send_plan(meta, "hello", what=what)
            self.assertFalse(plan.ok)
            self.assertTrue(plan.dest_refused)
        self.assertIsNone(writes.react_target(meta))
        # matching them must not have added a destination either
        sources.memory_match(meta, self.INDEX)
        self.assertEqual(meta, {"channel_id": CHAN, "thread_ts": THREAD_TS,
                                "message_ts": MSG_TS})

    def test_odd_meta_does_not_crash(self):
        for meta in (None, "x", [], {"source_id": ["a"]}, {"source_id": 5}):
            self.assertEqual(self.find(meta), "")

    def test_annotate_copies_and_leaves_unmatched_items_alone(self):
        saved = item(1, source_id="gmail:abc")
        plain = item(2, kind="update")
        cal = item(3, source="calendar", kind="invite",
                   source_id="gcal:evt_20300101T100000Z")
        res = [sources.SourceResult("t", [saved, plain, cal], False, None,
                                    None)]
        (out,) = sources.annotate_memory(res, self.INDEX)
        by = {i["id"]: i for i in out.items}
        self.assertEqual(by["t:1"]["meta"]["memory_id"], "mem-mail")
        self.assertNotIn("memory_id", by["t:2"]["meta"])
        self.assertNotIn("memory_id", by["t:3"]["meta"])   # never "unsaved"
        self.assertNotIn("memory_id", saved["meta"])       # input untouched
        self.assertIs(sources.annotate_memory(res, {}), res)

    def test_annotate_marks_a_thread_level_match_apart(self):
        idx = {f"slack:{CHAN}:{THREAD_TS}": "mem-thread"}
        row = item(1, reply_to={"channel": CHAN, "thread_ts": THREAD_TS,
                                "message_ts": MSG_TS})
        res = [sources.SourceResult("t", [row], False, None, None)]
        (out,) = sources.annotate_memory(res, idx)
        meta = out.items[0]["meta"]
        self.assertEqual(meta["thread_memory_id"], "mem-thread")
        self.assertNotIn("memory_id", meta)


class EventsUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def write(self, rows):
        path = self.base / "state" / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_events_unit_exposes_and_rebuilds_the_index(self):
        cfg = settings.validate({})
        units = sources.make_units(cfg, self.base)
        self.write([event("slack:C1:1.0", "mem-a")])
        for u in units:
            u.run(time.time(), False)
        self.assertEqual(sources.units_memory_index(units),
                         {"slack:C1:1.0": "mem-a"})
        self.write([event("slack:C1:1.0", "mem-b")])   # events.jsonl changed
        for u in units:
            u.run(time.time(), False)
        self.assertEqual(sources.units_memory_index(units),
                         {"slack:C1:1.0": "mem-b"})

    def test_collect_marks_a_command_sources_slack_item(self):
        self.write([event(f"slack:{CHAN}:{THREAD_TS}", "mem-thread")])
        row = item(1, reply_to={"channel": CHAN, "thread_ts": THREAD_TS})
        cfg = settings.validate({"sources": [
            {"name": "events", "type": "events"},
            {"name": "cmd", "type": "command", "argv": ["x"]}]})
        ok = sources.SourceResult("cmd", [row], False, None, None)
        with mock.patch.object(sources, "run_command_source",
                               return_value=ok):
            results = sources.collect(cfg, self.base, now=time.time())
        found = [i for r in results for i in r.items if i["id"] == "t:1"]
        # only a thread id in the reply_to: the thread is in memory
        self.assertEqual(found[0]["meta"]["thread_memory_id"], "mem-thread")
        self.assertNotIn("memory_id", found[0]["meta"])


class Fixed:
    def __init__(self, rows):
        self.results = [sources.SourceResult("t", rows, False, None, None)]

    def __call__(self):
        return self.results


async def settle(pilot, app):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)


def make_app(rows, actions):
    store = InboxStore(None)
    return InboxApp(Fixed(rows), store, actions(store),
                    settings=dict(DEFAULTS), undo_s=0.15, leave_s=0,
                    stream_delay=0)


def toasts(app):
    return [f"{n.title} {n.message}".strip() for n in app._notifications]


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class MemoryMarkUiTest(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [item(1, memory_id="mem-one"), item(2),
                item(3, source="calendar", kind="invite"),
                item(4, thread_memory_id="mem-root")]

    async def test_only_items_in_memory_carry_the_mark(self):
        app = make_app(self.rows(), lambda s: FakeActions(store=s,
                                                          draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            by = {i.key: i for i in app.items}
            self.assertEqual([k for k, i in by.items() if i.saved], ["t:1"])
            q = app.query_one("#queue", Queue)
            text = " ".join(str(q.get_option_at_index(n).prompt)
                            for n in range(q.option_count))
            self.assertEqual(text.count(SAVED_GLYPH), 1)
            self.assertEqual(text.count(THREAD_GLYPH), 1)   # t:4 only

    async def test_detail_view_names_the_memory(self):
        app = make_app(self.rows(), lambda s: FakeActions(store=s,
                                                          draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = lambda: str(app.query_one("#thread-body").render())
            app.current_key = "t:1"
            app.redraw()
            await pilot.pause(0.2)
            self.assertIn("In memory: mem-one", body())
            app.current_key = "t:2"
            app.redraw()
            await pilot.pause(0.2)
            self.assertNotIn("In memory", body())
            self.assertNotIn("Thread in memory", body())
            app.current_key = "t:4"
            app.redraw()
            await pilot.pause(0.2)
            self.assertIn("Thread in memory: mem-root", body())
            self.assertNotIn("\nIn memory", body())

    async def test_m_on_a_saved_item_toasts_and_runs_nothing(self):
        def actions(store):
            acts = FakeActions(store=store, draft_delay=0)
            acts.save = mock.Mock(side_effect=AssertionError("ran save"))
            return acts
        app = make_app(self.rows(), actions)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.current_key = "t:1"
            await pilot.pause(0.1)
            await pilot.press("m")
            await pilot.pause(0.2)
            self.assertTrue(any("Already in memory (mem-one)" in t
                                for t in toasts(app)), toasts(app))
            self.assertEqual(app.pending, [])
            self.assertNotEqual(type(app.screen).__name__, "ConfirmWrite")

    async def test_m_on_a_thread_level_match_offers_save_anyway(self):
        app = make_app(self.rows(), lambda s: FakeActions(store=s,
                                                          draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.current_key = "t:4"
            await pilot.pause(0.1)
            await pilot.press("m")
            await pilot.pause(0.2)
            self.assertEqual(type(app.screen).__name__, "ConfirmWrite")
            text = " ".join(str(w.render()) for w in app.screen.query("Static"))
            self.assertIn("Thread already in memory", text)
            self.assertIn("mem-root", text)
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertTrue(any("thread is already in memory" in t
                                for t in toasts(app)), toasts(app))
            self.assertEqual(app.pending, [])

    async def test_the_label_says_by_hand_when_save_is_configured(self):
        app = make_app(self.rows(), lambda s: FakeActions(store=s,
                                                          draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("m", "save by hand"), app.hints())

    async def test_m_is_hidden_everywhere_without_save(self):
        app = make_app(self.rows(), lambda s: LocalActions(s))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("t:1", "t:2"):
                app.current_key = key
                await pilot.pause(0.1)
                self.assertNotIn("m", [k for k, _ in app.hints()])
            titles = [c.title for c in app.get_system_commands(app.screen)]
            self.assertNotIn("Save item to memory", titles)
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, HelpScreen)
            self.assertFalse(app.screen.save_on)
            text = " ".join(str(w.render())
                            for w in app.screen.query("Static"))
            self.assertNotIn("save to memory by hand", text)
            self.assertNotIn("RSVP / save", text)
            self.assertNotIn("both confirm", text)
            self.assertIn("RSVP", text)

    async def test_help_lists_m_when_save_is_configured(self):
        app = make_app(self.rows(), lambda s: FakeActions(store=s,
                                                          draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            text = " ".join(str(w.render())
                            for w in app.screen.query("Static"))
            self.assertIn("save to memory by hand", text)


if __name__ == "__main__":
    unittest.main()
