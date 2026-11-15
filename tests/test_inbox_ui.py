"""Pilot tests for the inbox UI. Skipped when Textual is not installed."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import importlib.util

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from workspace_daemon import events
    from workspace_daemon.inbox import items as itm, settings, sources
    from workspace_daemon.inbox.actions import FakeActions, LocalActions
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.demo import DemoSource
    from workspace_daemon.inbox.screens import (ConfirmSend, HelpScreen,
                                                SnoozePicker, parse_snooze)
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import Queue

SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def item(i, section="fyi", source="calendar", actions=("ack", "open"), **kw):
    base = dict(
        id=f"t:{i}", source=source, section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender=f"Person {i}", where="room", gist=f"gist {i}", ts=time.time() - 60 * i,
        link=f"https://example.com/{i}", thread=[], draft=None,
        actions=list(actions), meta={})
    base.update(kw)
    return itm.make_item(**base)


class Fixed:
    """A fetch() returning fixed results; `.results` can be swapped."""

    def __init__(self, rows, error=None):
        self.results = [sources.SourceResult("t", rows, False, None, error)]
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.results


async def settle(pilot, app, polls=1):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)  # leave animation and redraws


def make_app(rows, actions=None, store=None, undo_s=0.15, **kw):
    store = store or InboxStore(None)
    actions = actions or FakeActions(store=store, draft_delay=0)
    kw.setdefault("settings", dict(DEFAULTS))
    return InboxApp(Fixed(rows), store, actions, undo_s=undo_s, leave_s=0,
                    stream_delay=0, **kw), store, actions


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class DemoTest(unittest.IsolatedAsyncioTestCase):
    async def test_demo_renders_sections_and_urgent_first(self):
        store = InboxStore(None)
        app = InboxApp(DemoSource(latency=0), store, FakeActions(store=store, draft_delay=0),
                       dict(DEFAULTS), demo=True, stream_delay=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            q = app.query_one("#queue", Queue)
            self.assertGreater(len(q.rows), 10)
            self.assertEqual(q.rows[0].section, "urgent")
            self.assertEqual({i.section for i in app.items},
                             {"urgent", "needs_reply", "waiting", "fyi"})
            self.assertIn(app.current_key, {i.key for i in app.items})

    async def test_demo_empty_state(self):
        store = InboxStore(None)
        app = InboxApp(DemoSource(empty=True, latency=0), store, FakeActions(store=store),
                       dict(DEFAULTS), demo=True, stream_delay=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.items, [])
            self.assertTrue(app.query_one("#zero").display)

    async def test_new_urgent_arrival_toasts_on_later_poll(self):
        store = InboxStore(None)
        src = DemoSource(latency=0)
        app = InboxApp(src, store, FakeActions(store=store, draft_delay=0),
                       dict(DEFAULTS), demo=True, stream_delay=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            before = len(app.items)
            await pilot.press("r")
            await pilot.pause(0.3)
            self.assertEqual(len(app.items), before + 1)
            self.assertTrue(any(r["action"] == "poll" for r in app.audit))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class KeysTest(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [item(1, "urgent", "alert"), item(2, "fyi", "calendar"),
                item(3, "fyi", "email", actions=("open",))]

    async def test_done_then_undo_restores_and_writes_nothing(self):
        app, store, actions = make_app(self.rows(), undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await pilot.pause(0.1)
            self.assertNotIn(first, [i.key for i in app.items])
            await pilot.press("u")
            await pilot.pause(0.1)
            self.assertIn(first, [i.key for i in app.items])
            self.assertFalse(store.is_hidden(first))
            self.assertEqual(actions.calls.count(("mark_done", first)), 0)

    async def test_done_commits_after_the_undo_window(self):
        app, store, actions = make_app(self.rows(), undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await pilot.pause(0.6)
            self.assertTrue(store.is_hidden(first))
            await pilot.press("r")          # a poll must not bring it back
            await pilot.pause(0.3)
            self.assertNotIn(first, [i.key for i in app.items])

    async def test_pending_item_survives_a_poll_hidden(self):
        app, store, _ = make_app(self.rows(), undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await pilot.press("r")
            await pilot.pause(0.3)
            self.assertNotIn(first, [i.key for i in app.items])

    async def test_ack_on_alert_marks_the_store(self):
        app, store, _ = make_app([item(1, "urgent", "alert")], undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            key = app.current_key
            await pilot.press("a")
            await pilot.pause(0.6)
            self.assertIn("ack", store.marks[key])

    async def test_snooze_hides_then_wakes(self):
        clock = [time.time()]
        store = InboxStore(None, clock=lambda: clock[0])
        app, _, _ = make_app([item(1)], store=store, undo_s=0.05)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            key = app.current_key
            await pilot.press("s")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, SnoozePicker)
            await pilot.press("1")                     # one hour
            await pilot.pause(0.6)
            self.assertEqual(app.items, [])
            self.assertIsNotNone(store.snoozed_until(key))
            clock[0] += 3700                           # past the snooze
            with mock.patch("workspace_daemon.inbox.app.time.time", lambda: clock[0]):
                app.refilter()
            await pilot.pause(0.1)
            self.assertEqual([i.key for i in app.items], [key])

    async def test_confirm_modal_defaults_to_cancel_and_sends_nothing(self):
        row = item(1, "needs_reply", "slack", actions=("reply",), draft="hello there")
        app, store, actions = make_app([row])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("a")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmSend)
            self.assertEqual(app.screen.focused.id, "cancel")
            await pilot.press("enter")                 # Cancel is the default
            await pilot.pause(0.1)
            self.assertEqual(actions.sent(), [])
            self.assertEqual(len(app.items), 1)
            self.assertEqual(app.audit[-1]["status"], "cancelled")

    async def test_confirm_then_undo_never_sends(self):
        row = item(1, "needs_reply", "slack", actions=("reply",), draft="hello there")
        app, store, actions = make_app([row], undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("a")
            await pilot.pause(0.1)
            await pilot.press("y")
            await pilot.pause(0.1)
            self.assertEqual(app.items, [])
            await pilot.press("u")
            await pilot.pause(0.1)
            self.assertEqual(len(app.items), 1)
            self.assertEqual(actions.sent(), [])

    async def test_quit_cancels_pending_send_but_commits_done(self):
        rows = [item(1, "needs_reply", "slack", actions=("reply",), draft="hi"),
                item(2, "fyi", "calendar")]
        app, store, actions = make_app(rows, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            reply_key = "t:1"
            app.current_key = reply_key
            await pilot.press("a")
            await pilot.pause(0.1)
            await pilot.press("y")
            await pilot.pause(0.1)
            await pilot.press("d")                     # done on the next row
            await pilot.pause(0.1)
            done_key = app.pending[-1].item.key
            await pilot.press("q")
            await pilot.pause(0.3)
        self.assertEqual(actions.sent(), [])
        self.assertTrue(store.is_hidden(done_key))
        self.assertFalse(store.is_hidden(reply_key))
        self.assertIn("cancelled on quit", [r["status"] for r in app.audit])

    async def test_real_actions_refuse_to_reply(self):
        row = item(1, "needs_reply", "slack", actions=("reply",), draft="hi")
        store = InboxStore(None)
        app, _, _ = make_app([row], actions=LocalActions(store), store=store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("a")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, ConfirmSend)
            self.assertEqual(len(app.items), 1)

    async def test_open_uses_the_link_or_says_there_is_none(self):
        opened = []
        store = InboxStore(None)
        actions = LocalActions(store, opener=lambda url: opened.append(url) or True)
        rows = [item(1, link="https://example.com/a"), item(2, link=None)]
        app, _, _ = make_app(rows, actions=actions, store=store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("o")
            await pilot.pause(0.1)
            await pilot.press("j", "o")
            await pilot.pause(0.1)
        self.assertEqual(opened, ["https://example.com/a"])
        self.assertEqual([r["status"] for r in app.audit if r["action"] == "open"],
                         ["ok", "failed"])

    async def test_open_is_hidden_without_a_link(self):
        rows = [item(1, link="https://example.com/a"), item(2, link=None)]
        app, _, _ = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            by_key = {i.key: i for i in app.items}
            self.assertIn("open", by_key["t:1"].actions)
            self.assertNotIn("open", by_key["t:2"].actions)
            app.current_key = "t:2"
            self.assertNotIn(("o", "open"), app.hints())
            app.current_key = "t:1"
            self.assertIn(("o", "open"), app.hints())

    async def test_poll_while_running_is_skipped_not_stacked(self):
        gate = threading.Event()
        calls = []

        def slow():
            calls.append(1)
            if len(calls) == 2:       # the first poll (on mount) is quick
                gate.wait(5)
            return [sources.SourceResult("t", [item(1)], False, None, None)]

        store = InboxStore(None)
        app = InboxApp(slow, store, FakeActions(store=store), dict(DEFAULTS),
                       leave_s=0, stream_delay=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(app, "notify", wraps=app.notify) as note:
                await pilot.press("r")             # starts the slow poll
                await pilot.pause(0.2)
                self.assertTrue(app.polling)
                await pilot.press("r", "r")        # manual: told, not queued
                await pilot.pause(0.2)
                app.poll()                         # timer-driven: silent skip
                told = [c.args[0] for c in note.call_args_list]
                self.assertEqual(told.count("Poll already running"), 2)
                self.assertEqual(len(calls), 2)    # no extra fetches started
                gate.set()
                await pilot.pause(0.5)
            self.assertFalse(app.polling)
            await pilot.press("r")
            await pilot.pause(0.4)
            self.assertEqual(len(calls), 3)        # polling works again

    async def test_each_poll_reloads_marks_written_by_another_pane(self):
        with tempfile.TemporaryDirectory() as tmp:
            mine = InboxStore(Path(tmp))
            other = InboxStore(Path(tmp))
            fetch = Fixed([item(1), item(2)])
            app = InboxApp(fetch, mine, FakeActions(store=mine), dict(DEFAULTS),
                           leave_s=0, stream_delay=0)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                self.assertEqual(len(app.items), 2)
                other.done("t:1")                  # the other pane marks it
                await pilot.press("r")
                await pilot.pause(0.4)
                self.assertEqual([i.key for i in app.items], ["t:2"])

    async def test_source_filter_and_text_filter(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("f")
            await pilot.pause(0.1)
            shown = {i.source for i in app.visible()}
            self.assertEqual(len(shown), 1)
            await pilot.press("escape")
            await pilot.pause(0.1)
            self.assertEqual(len(app.visible()), 3)
            await pilot.press("slash", "P", "e", "r", "s", "o", "n", "space", "2")
            await pilot.pause(0.1)
            self.assertEqual([i.key for i in app.visible()], ["t:2"])

    async def test_help_audit_and_palette_open(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.press("L")
            await pilot.pause(0.1)
            self.assertEqual(type(app.screen).__name__, "AuditScreen")
            await pilot.press("escape")
            cmds = [c.title for c in app.get_system_commands(app.screen)]
            self.assertIn("Poll now", cmds)

    async def test_theme_toggle(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.theme, "inbox-dark")
            await pilot.press("ctrl+t")
            self.assertEqual(app.theme, "inbox-light")


HEBREW = dict(sender="\u05e0\u05d5\u05e2\u05d4", where="#\u05db\u05dc\u05dc\u05d9",
              gist="\u05d1\u05d3\u05d9\u05e7\u05d4 [x] *y*")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class CopyTest(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [
            item(1, "urgent", "alert", link="https://example.com/a(1)"),
            item(2, "urgent", "alert", link=None, actions=("ack",), **HEBREW),
            item(3, "fyi", "calendar"),
        ]

    async def copied(self, pilot, app, *keys):
        with mock.patch.object(app, "copy_to_clipboard") as clip, \
                mock.patch.object(app, "notify", wraps=app.notify) as toast:
            for key in keys:
                await pilot.press(key)
            await pilot.pause(0.1)
        return clip, toast

    async def test_y_copies_the_focused_item_as_plain_text(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            clip, toast = await self.copied(pilot, app, "y")
            clip.assert_called_once_with(
                "Person 1 \u2014 room \u2014 gist 1\nhttps://example.com/a(1)")
            self.assertEqual(toast.call_args[0][0], "Copied")
            clip, _ = await self.copied(pilot, app, "j", "y")   # no link: one line
            clip.assert_called_once_with(
                "\u05e0\u05d5\u05e2\u05d4 \u2014 #\u05db\u05dc\u05dc\u05d9 \u2014 "
                "\u05d1\u05d3\u05d9\u05e7\u05d4 [x] *y*")

    async def test_big_y_copies_the_whole_section_one_item_per_line(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            clip, toast = await self.copied(pilot, app, "Y")
            clip.assert_called_once_with(
                "Person 1 \u2014 room \u2014 gist 1 \u2014 https://example.com/a(1)\n"
                "\u05e0\u05d5\u05e2\u05d4 \u2014 #\u05db\u05dc\u05dc\u05d9 \u2014 "
                "\u05d1\u05d3\u05d9\u05e7\u05d4 [x] *y*")
            self.assertEqual(toast.call_args[0][0], "Copied")
            # focus the fyi item: only its section is copied
            clip, _ = await self.copied(pilot, app, "j", "j", "Y")
            clip.assert_called_once_with(
                "Person 3 \u2014 room \u2014 gist 3 \u2014 https://example.com/3")

    async def test_section_copy_follows_the_filter(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.filter_text = "person 1"
            app.redraw()
            clip, _ = await self.copied(pilot, app, "Y")
            clip.assert_called_once_with(
                "Person 1 \u2014 room \u2014 gist 1 \u2014 https://example.com/a(1)")

    async def test_markdown_section_from_the_palette(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            cmds = {c.title: c for c in app.get_system_commands(app.screen)}
            for title in ("Copy item", "Copy section", "Copy section as Markdown"):
                self.assertIn(title, cmds)
            with mock.patch.object(app, "copy_to_clipboard") as clip:
                cmds["Copy section as Markdown"].callback()
                clip.assert_called_once_with(
                    "- **Person 1** (room): gist 1 [link](https://example.com/a%281%29)\n"
                    "- **\u05e0\u05d5\u05e2\u05d4** (#\u05db\u05dc\u05dc\u05d9): "
                    "\u05d1\u05d3\u05d9\u05e7\u05d4 \\[x\\] \\*y\\*")
            with mock.patch.object(app, "copy_to_clipboard") as clip:
                cmds["Copy item"].callback()
                self.assertTrue(clip.call_args[0][0].startswith("Person 1 \u2014"))
            with mock.patch.object(app, "copy_to_clipboard") as clip:
                cmds["Copy section"].callback()
                self.assertEqual(clip.call_args[0][0].count("\n"), 1)

    async def test_y_sends_the_osc52_clipboard_escape(self):
        import base64
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            writes = []
            if app._driver is not None:
                real = app._driver.write
                app._driver.write = lambda data: (writes.append(data), real(data))[1]
            await pilot.press("j", "y")
            await pilot.pause(0.1)
            want = ("\u05e0\u05d5\u05e2\u05d4 \u2014 #\u05db\u05dc\u05dc\u05d9 \u2014 "
                    "\u05d1\u05d3\u05d9\u05e7\u05d4 [x] *y*")
            self.assertEqual(app._clipboard, want)
            osc = [w for w in writes if w.startswith("\x1b]52;c;")]
            if app._driver is not None:
                self.assertEqual(len(osc), 1)
                payload = osc[0][len("\x1b]52;c;"):-1]
                self.assertEqual(base64.b64decode(payload).decode("utf-8"), want)

    async def test_toggling_bidi_clears_a_mouse_selection(self):
        thread = [{"who": "Ann", "ts": time.time(), "text": "hello world"}]
        app, _, _ = make_app([item(1, "urgent", "slack", thread=thread)],
                             settings=dict(DEFAULTS))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.mouse_down("#thread-body", offset=(0, 2))
            await pilot.hover("#thread-body", offset=(8, 2))
            await pilot.mouse_up("#thread-body", offset=(8, 2))
            await pilot.pause(0.1)
            self.assertTrue(app.screen.get_selected_text())
            with mock.patch("workspace_daemon.inbox.app.bidi.available",
                            return_value=True):
                await pilot.press("b")
            await pilot.pause(0.1)
            self.assertEqual(app.bidi_mode, "visual")
            self.assertFalse(app.screen.get_selected_text())

    async def test_copy_is_logical_text_even_in_visual_bidi_mode(self):
        cfg = dict(DEFAULTS, bidi_mode="visual")
        app, _, _ = make_app(self.rows(), settings=cfg)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.bidi_mode = "visual"
            await pilot.press("j")
            clip, _ = await self.copied(pilot, app, "y")
            text = clip.call_args[0][0]
            self.assertTrue(text.startswith(HEBREW["sender"] + " \u2014 "))
            self.assertIn(HEBREW["gist"], text)
            self.assertNotIn("\x1b", text)

    async def test_copy_with_nothing_selected_says_so(self):
        app, _, _ = make_app([])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            clip, toast = await self.copied(pilot, app, "y", "Y")
            clip.assert_not_called()
            self.assertEqual(toast.call_args[0][0], "Nothing to copy")

    async def test_hints_and_help_list_the_copy_keys(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("y", "copy"), app.hints())
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            body = str(app.screen.query_one("#help-body").render())
            for needle in ("copy this item", "every item in this section",
                           "show / hide resolved"):
                self.assertIn(needle, body)

    async def test_thread_text_can_be_selected_with_the_mouse(self):
        thread = [{"who": "Ann", "ts": time.time(), "text": "hello world"}]
        app, _, _ = make_app([item(1, "urgent", "slack", thread=thread)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = app.query_one("#thread-body")
            self.assertTrue(body.allow_select)
            self.assertFalse(app.query_one("#queue").allow_select)
            await pilot.mouse_down("#thread-body", offset=(0, 2))
            await pilot.hover("#queue", offset=(20, 4))   # drag out of the pane
            await pilot.mouse_up("#queue", offset=(20, 4))
            await pilot.pause(0.1)
            got = app.screen.get_selected_text()
            self.assertIn("hello world", got)
            self.assertNotIn("gist 1", got)               # stayed inside the thread

    async def test_selection_is_off_in_visual_bidi_mode(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.bidi_mode = "visual"
            self.assertFalse(app.query_one("#thread-body").allow_select)
            self.assertFalse(app.query_one("#draft-view").allow_select)
            app.bidi_mode = "logical"
            self.assertTrue(app.query_one("#draft-view").allow_select)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ResolvedAlertsTest(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [item(1, "urgent", "alert"),
                item(2, "fyi", "alert", meta={"resolved": True}),
                item(3, "fyi", "calendar")]

    async def test_resolved_alerts_are_hidden_until_r(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(sorted(i.key for i in app.items), ["t:1", "t:3"])
            self.assertEqual(app.resolved_hidden, 1)
            await pilot.press("R")
            await pilot.pause(0.1)
            self.assertEqual(sorted(i.key for i in app.items), ["t:1", "t:2", "t:3"])
            self.assertEqual(app.resolved_hidden, 0)
            self.assertEqual(
                [i.key for i in app.query_one("#queue", Queue).rows if i.section == "fyi"],
                ["t:2", "t:3"])
            await pilot.press("R")
            await pilot.pause(0.1)
            self.assertEqual(sorted(i.key for i in app.items), ["t:1", "t:3"])

    async def test_header_counts_hidden_resolved_alerts(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            header = lambda: str(app.query_one("#hdr").render())
            self.assertIn("\u2713 1 resolved (R)", header())
            await pilot.press("R")
            await pilot.pause(0.1)
            self.assertNotIn("resolved (R)", header())
            rows = app.query_one("#queue", Queue).rows
            line = app.query_one("#queue", Queue)._row(
                rows[1], 60, app.pal, time.time(), False)   # t:2, resolved
            self.assertIn(" \u2713 ", line.plain)

    async def test_only_alert_items_can_be_resolved(self):
        rows = [item(1, "fyi", "alert", meta={"resolved": True}),
                item(2, "fyi", "calendar", meta={"resolved": True}),
                item(3, "fyi", "alert", meta={"resolved": "yes"})]
        app, _, _ = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(sorted(i.key for i in app.items), ["t:2", "t:3"])
            self.assertEqual(app.resolved_hidden, 1)

    async def test_a_resolved_alert_never_counts_as_urgent(self):
        app, _, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("R")
            await pilot.pause(0.1)
            self.assertEqual(sum(1 for i in app.items if i.section == "urgent"), 1)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class SafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_markup_in_untrusted_text_is_inert(self):
        nasty = "[bold red]x[/] [/oops] [link=http://x]y[/link]"
        row = item(1, "needs_reply", "email", actions=("reply",), sender=nasty,
                   where="[b]w", gist=nasty, draft=nasty,
                   thread=[{"who": nasty, "ts": time.time(), "text": nasty}])
        app, _, _ = make_app([row])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("a")         # confirm modal shows the destination
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, ConfirmSend)
            await pilot.press("escape")
            await pilot.press("d")
            await pilot.pause(0.2)
            await pilot.press("L")
            await pilot.pause(0.1)

    async def test_source_error_is_shown_and_does_not_crash(self):
        store = InboxStore(None)
        fetch = Fixed([item(1)], error="exit 3: boom")
        app = InboxApp(fetch, store, FakeActions(store=store), dict(DEFAULTS),
                       leave_s=0, stream_delay=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.source_problems(), ["t: exit 3: boom"])
            self.assertEqual(len(app.items), 1)

    async def test_fetch_exception_becomes_a_source_error(self):
        def boom():
            raise RuntimeError("kaput")
        store = InboxStore(None)
        app = InboxApp(boom, store, FakeActions(store=store), dict(DEFAULTS),
                       leave_s=0, stream_delay=0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("kaput", app.source_problems()[0])

    async def test_item_ids_cannot_collide_with_internal_option_ids(self):
        rows = [item(1, id="hdr:urgent"), item(2, id="ghost"),
                item(3, id="empty:fyi")]
        app, _, _ = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(len(app.query_one("#queue", Queue).rows), 3)
            self.assertIn(app.current_key, {"hdr:urgent", "ghost", "empty:fyi"})


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class BidiAppTest(unittest.IsolatedAsyncioTestCase):
    def app(self, bidi_mode="auto"):
        s = dict(DEFAULTS, bidi_mode=bidi_mode)
        return make_app([item(1)], settings=s)[0]

    def test_iterm_defaults_to_logical(self):
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}):
            os.environ.pop("INBOX_BIDI", None)
            os.environ.pop("LC_TERMINAL", None)
            self.assertEqual(self.app().bidi_mode, "logical")

    def test_unknown_terminal_defaults_to_visual_when_available(self):
        from workspace_daemon.inbox import bidi
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "mystery"}):
            os.environ.pop("INBOX_BIDI", None)
            os.environ.pop("LC_TERMINAL", None)
            want = "visual" if bidi.available() else "logical"
            self.assertEqual(self.app().bidi_mode, want)

    def test_env_and_config_override_iterm(self):
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app", "INBOX_BIDI": "logical"}):
            self.assertEqual(self.app("visual").bidi_mode, "logical")

    async def test_b_toggles_and_header_shows_mode(self):
        from workspace_daemon.inbox import bidi
        if not bidi.available():
            self.skipTest("python-bidi not installed")
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "iTerm.app"}):
            os.environ.pop("INBOX_BIDI", None)
            os.environ.pop("LC_TERMINAL", None)
            app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.bidi_mode, "logical")
            await pilot.press("b")
            self.assertEqual(app.bidi_mode, "visual")
            await pilot.press("b")
            self.assertEqual(app.bidi_mode, "logical")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class RealSourceTest(unittest.IsolatedAsyncioTestCase):
    async def test_events_source_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
            ev = events.build("daemon", "alert", None, "daemon:budget-hit:2030-01-01",
                              "Budget cap reached", [], ts=ts,
                              details={"key": "budget-hit:2030-01-01"})
            events.emit(tmp, ev)
            cfg = settings.default_settings()
            store = InboxStore(Path(tmp))
            app = InboxApp(lambda: sources.collect(cfg, Path(tmp)), store,
                           LocalActions(store), cfg, undo_s=0.1, leave_s=0, stream_delay=0)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                self.assertEqual(len(app.items), 1)
                self.assertEqual(app.items[0].section, "urgent")
                await pilot.press("a")
                await pilot.pause(0.6)
            state = json.loads((Path(tmp) / "state" / "inbox-state.json").read_text())
            self.assertEqual(list(state["items"]), [app.all_rows[0].key])


class SnoozeParseTest(unittest.TestCase):
    @unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
    def test_parse_snooze(self):
        now = 1_700_000_000.0
        self.assertEqual(parse_snooze("90m", now), now + 5400)
        self.assertEqual(parse_snooze("2h", now), now + 7200)
        self.assertEqual(parse_snooze("1d", now), now + 86400)
        self.assertGreater(parse_snooze("17:30", now), now)
        for bad in ("", "soon", "25:00", "12:99", "-5m"):
            self.assertIsNone(parse_snooze(bad, now), bad)


if __name__ == "__main__":
    unittest.main()
