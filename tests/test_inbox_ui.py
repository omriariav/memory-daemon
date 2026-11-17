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
    from workspace_daemon.inbox import __version__, items as itm, settings, sources
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
        app, _, _ = make_app([item(1)], store=store, undo_s=0.05, view="feed")
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
                item(2, "needs_reply", "slack", actions=("reply",), draft="yo")]
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
        app, _, _ = make_app(rows, actions=actions, store=store, view="feed")
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
                app.poll()                         # timer-driven: queued, silent
                app.poll()                         # ... and merged with the first
                told = [c.args[0] for c in note.call_args_list]
                self.assertEqual(told.count("Poll already running"), 2)
                self.assertEqual(len(calls), 2)    # no extra fetches started
                gate.set()
                await pilot.pause(0.6)
            self.assertFalse(app.polling)
            self.assertEqual(len(calls), 3)        # the queued one ran, once
            await pilot.press("r")
            await pilot.pause(0.4)
            self.assertEqual(len(calls), 4)        # polling works again

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
        rows = [item(1, "urgent", "alert"),
                item(2, "needs_reply", "slack", actions=("reply",)),
                item(3, "needs_reply", "email", actions=("open",))]
        app, _, _ = make_app(rows)
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
            self.assertIn(f"v{__version__}", str(app.screen.query_one("#help-title").render()))
            self.assertIn(__version__, str(app.query_one("#hdr").render()))
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
            # the Feed: only the group of the selected row is copied
            await pilot.press("4")
            await pilot.pause(0.1)
            clip, _ = await self.copied(pilot, app, "Y")
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
            for needle in ("copy this item", "every item in this group",
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
            await pilot.press("4")
            await pilot.pause(0.1)
            self.assertEqual(
                [i.key for i in app.query_one("#queue", Queue).rows],
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
            await pilot.press("4")
            await pilot.pause(0.1)
            rows = app.query_one("#queue", Queue).rows
            line = app.query_one("#queue", Queue)._row(
                rows[0], 60, app.pal, time.time(), False)   # t:2, resolved
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
        app, _, _ = make_app(rows, view="feed")
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



class AllAwareFetch:
    """A fetch() that takes `show_all`; records each call and adds one item
    only while the toggle is on."""

    def __init__(self, extra=None):
        self.calls = []
        self.extra = extra or []

    def __call__(self, show_all=False):
        self.calls.append(show_all)
        rows = [item(1, "fyi", "calendar")] + (self.extra if show_all else [])
        return [sources.SourceResult("t", rows, False, None, None)]


SAVED_ROWS = [
    {"id": "s1", "source": "slack", "kind": "update", "ts": time.time() - 600,
     "title": "Launch checklist thread", "link": None, "source_id": "slack:C1:1"},
    {"id": "s2", "source": "email", "kind": "new", "ts": time.time() - 3600,
     "title": "Contract terms [b]x[/b]", "link": "https://example.com/mail/1",
     "source_id": "gmail:abcdef0123456789"},
]


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ShowAllKeyTest(unittest.IsolatedAsyncioTestCase):
    def make(self, fetch):
        store = InboxStore(None)
        return InboxApp(fetch, store, FakeActions(store=store, draft_delay=0),
                        dict(DEFAULTS), undo_s=0.15, leave_s=0, stream_delay=0), store

    async def wait_polls(self, pilot, fetch, n):
        for _ in range(200):
            if len(fetch.calls) >= n and not pilot.app.polling:
                break
            await pilot.pause(0.02)
        await pilot.pause(0.2)

    async def test_off_by_default_then_a_toggles_and_repolls(self):
        fetch = AllAwareFetch(extra=[item(2, "urgent", "slack", actions=("ack",))])
        app, _ = self.make(fetch)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual((fetch.calls, app.show_all), ([False], False))
            self.assertNotIn(" all ", str(app.query_one("#hdr").render()))
            await pilot.press("A")
            await self.wait_polls(pilot, fetch, 2)
            self.assertEqual((fetch.calls, app.show_all), ([False, True], True))
            self.assertIn(" all ", str(app.query_one("#hdr").render()))
            self.assertEqual(sorted(i.key for i in app.items), ["t:1", "t:2"])
            await pilot.press("A")
            await self.wait_polls(pilot, fetch, 3)
            self.assertEqual(fetch.calls, [False, True, False])
            self.assertEqual([i.key for i in app.items], ["t:1"])
            self.assertNotIn(" all ", str(app.query_one("#hdr").render()))

    async def test_items_revealed_by_all_do_not_raise_urgent_alerts(self):
        fetch = AllAwareFetch(extra=[item(2, "urgent", "slack", actions=("ack",))])
        app, _ = self.make(fetch)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(app, "bell") as bell:
                await pilot.press("A")
                await self.wait_polls(pilot, fetch, 2)
                bell.assert_not_called()

    async def test_toggle_during_a_poll_polls_again_with_the_new_state(self):
        gate = threading.Event()
        calls = []

        def slow(show_all=False):
            calls.append(show_all)
            if len(calls) == 1:
                gate.wait(5)
            return [sources.SourceResult("t", [item(1)], False, None, None)]

        app, _ = self.make(slow)
        async with app.run_test(size=SIZE) as pilot:
            for _ in range(100):
                if calls:
                    break
                await pilot.pause(0.02)
            await pilot.press("A")      # first poll is still running
            gate.set()
            for _ in range(200):
                if len(calls) >= 2 and not app.polling:
                    break
                await pilot.pause(0.02)
            self.assertEqual(calls, [False, True])

    async def test_a_plain_fetch_without_show_all_still_works(self):
        app, _, _ = make_app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("A")
            await pilot.pause(0.4)
            self.assertTrue(app.show_all)
            self.assertEqual([i.key for i in app.items], ["t:1"])

    async def test_real_collect_passes_all_only_to_declaring_sources(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.log"
            script = Path(tmp) / "src.py"
            script.write_text(
                "import json, sys\n"
                f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                "print(json.dumps({'v': 1, 'items': []}))\n")
            argv = [sys.executable, str(script)]
            cfg = settings.validate({"sources": [
                {"name": "p", "type": "command", "argv": argv + ["p"]},
                {"name": "w", "type": "command", "argv": argv + ["w"],
                 "supports_all": True}]})

            def fetch(show_all=False):
                return sources.collect(cfg, Path(tmp), show_all=show_all)

            app, _ = self.make(fetch)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await pilot.press("A")
                for _ in range(200):
                    if log.read_text().count("\n") >= 4 and not app.polling:
                        break
                    await pilot.pause(0.05)
            lines = log.read_text().split("\n")
            self.assertCountEqual([l for l in lines if l], [
                "p list", "w list", "p list", "w list --all"])

    async def test_footer_help_and_palette_list_the_new_keys(self):
        app, _, _ = make_app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("A", "all"), app.hints())
            self.assertIn(("S", "saved"), app.hints())
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            body = str(app.screen.query_one("#help-body").render())
            self.assertIn("show all", body)
            self.assertIn("saved", body)
            await pilot.press("escape")
            cmds = [c.title for c in app.get_system_commands(app.screen)]
            self.assertIn("Show all (toggle)", cmds)
            self.assertIn("Saved to memory", cmds)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class SavedKeyTest(unittest.IsolatedAsyncioTestCase):
    def make(self, rows=None, error=None):
        store = InboxStore(None)
        actions = FakeActions(store=store, draft_delay=0)
        saved = None if rows is None and error is None else (lambda: (rows or [], error))
        app = InboxApp(Fixed([item(1, "fyi", "calendar")]), store, actions,
                       dict(DEFAULTS), undo_s=0.15, leave_s=0, stream_delay=0,
                       saved=saved, saved_days=2)
        return app, store, actions

    async def test_s_opens_the_feed_filtered_to_saved_and_esc_clears_it(self):
        app, _, _ = self.make(SAVED_ROWS)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.view, "inbox")
            await pilot.press("S")
            await pilot.pause(0.3)
            self.assertEqual((app.view, app.feed_routine),
                             ("feed", "saved to memory"))
            self.assertEqual(len(app.screen_stack), 1)
            rows = app.query_one("#queue", Queue).rows
            self.assertEqual([r.gist for r in rows],
                             ["Launch checklist thread",
                              "Contract terms [b]x[/b]"])     # literal text
            self.assertEqual([r.source for r in rows], ["slack", "email"])
            self.assertTrue(all(r.saved for r in rows))
            self.assertEqual(dict(app.feed_data.counts)["saved to memory"], 2)
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertIsNone(app.feed_routine)
            self.assertEqual({r.gist for r in app.query_one("#queue", Queue).rows},
                             {"gist 1", "Launch checklist thread",
                              "Contract terms [b]x[/b]"})

    async def test_o_opens_the_link_of_a_saved_row(self):
        app, _, actions = self.make(SAVED_ROWS)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            await pilot.pause(0.3)
            self.assertNotIn(("o", "open"), app.hints())   # first row: no link
            await pilot.press("j", "o")       # second row: has a link
            await pilot.pause(0.1)
            self.assertEqual([c[0] for c in actions.calls if c[0] == "open"], ["open"])
            self.assertIn("open", [r["action"] for r in app.audit])

    async def test_saved_rows_have_no_draft_actions_and_no_rules(self):
        app, store, actions = self.make(SAVED_ROWS)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            await pilot.pause(0.3)
            await pilot.press("a", "n", "e", "x", "minus")
            await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)   # no picker opened
            self.assertEqual(app.pending, [])
            self.assertEqual(store.marks, {})
            self.assertEqual(store.feed_rules(), [])
            self.assertEqual([c for c in actions.calls
                              if c[0] not in ("draft",)], [])

    async def test_a_saved_row_that_the_queue_already_shows_is_not_listed_twice(self):
        row = item(1, "fyi", "email", meta={"source_id": "gmail:abcdef0123456789"})
        store = InboxStore(None)
        app = InboxApp(Fixed([row]), store, FakeActions(store=store),
                       dict(DEFAULTS), leave_s=0, stream_delay=0,
                       saved=lambda: (SAVED_ROWS, None), saved_days=2)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(dict(app.feed_data.counts)["saved to memory"], 1)

    async def test_empty_and_error_states(self):
        app, _, _ = self.make([], None)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            await pilot.pause(0.3)
            self.assertTrue(app.query_one("#zero").display)
            self.assertIn("saved to memory", str(app.query_one("#zero").render()))
            await pilot.press("o")            # nothing to open: no crash
            await pilot.pause(0.1)
            await pilot.press("escape")
        app, _, _ = self.make([], "cannot read events: denied")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            await pilot.pause(0.3)
            self.assertIn("denied", str(app.query_one("#zero").render()))

    async def test_a_failing_reader_does_not_end_the_session(self):
        store = InboxStore(None)

        def boom():
            raise RuntimeError("nope")

        app = InboxApp(Fixed([item(1)]), store, FakeActions(store=store),
                       dict(DEFAULTS), leave_s=0, stream_delay=0, saved=boom)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            await pilot.pause(0.3)
            self.assertEqual(app.view, "feed")
            self.assertIn("RuntimeError", str(app.query_one("#zero").render()))

    async def test_without_a_reader_s_only_says_so(self):
        app, _, _ = self.make()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(app.view, "inbox")      # nothing to show: stays

    async def test_demo_has_saved_rows(self):
        from workspace_daemon.inbox.demo import demo_saved
        rows, err = demo_saved()
        self.assertIsNone(err)
        self.assertGreaterEqual(len(rows), 3)



class CountingUnit:
    """A refreshable unit for the app: counts runs, lists one item, watches
    the given paths."""

    def __init__(self, name, paths=(), uses_all=False, delay=0.0, items=None):
        self.name, self.paths, self.uses_all = name, list(paths), uses_all
        self.delay = delay
        self.items = items
        self.runs, self.times, self.alls = 0, [], []
        self.active = self.peak = 0
        self.reeval = None          # set to a callable(now) to enable

    def watch(self):
        return list(self.paths)

    def run(self, now, show_all):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            self.runs += 1
            self.times.append(time.monotonic())
            self.alls.append(show_all)
            if self.delay:
                time.sleep(self.delay)
            rows = self.items if self.items is not None else [
                item(1, "fyi", "calendar", id=f"{self.name}:1")]
            return [sources.SourceResult(self.name, rows, False, None, None)]
        finally:
            self.active -= 1


def unit_app(units, poll_s=300.0, watch_s=0.05, debounce_s=0.0, saved=None,
             **kw):
    store = InboxStore(None)
    cfg = dict(DEFAULTS, watch_interval_s=watch_s)
    app = InboxApp(lambda: [], store, FakeActions(store=store, draft_delay=0),
                   cfg, undo_s=0.15, leave_s=0, stream_delay=0, poll_s=poll_s,
                   units=units, saved=saved, **kw)
    app.watcher.debounce_s = debounce_s
    return app


async def until(pilot, cond, tries=150, step=0.04):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(step)
    return cond()


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ChangeDrivenRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def f(self, name, text="x"):
        path = self.dir / name
        path.write_text(text)
        return str(path)

    async def test_a_changed_file_refreshes_only_its_source(self):
        a = CountingUnit("a", [self.f("a.txt")])
        b = CountingUnit("b", [self.f("b.txt")])
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual((a.runs, b.runs), (1, 1))
            (self.dir / "a.txt").write_text("changed, and longer")
            self.assertTrue(await until(pilot, lambda: a.runs == 2))
            await pilot.pause(0.4)
            self.assertEqual((a.runs, b.runs), (2, 1))
            # b's rows are still listed from its cached result
            self.assertEqual(sorted(i.key for i in app.items), ["a:1", "b:1"])

    async def test_no_change_means_no_re_run(self):
        a = CountingUnit("a", [self.f("a.txt")])
        b = CountingUnit("b", [])           # no watch list: fallback only
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.pause(0.8)          # ~15 watch ticks
            self.assertEqual((a.runs, b.runs), (1, 1))

    async def test_the_fallback_timer_refreshes_everything(self):
        a = CountingUnit("a", [self.f("a.txt")])
        b = CountingUnit("b", [])
        app = unit_app([a, b], poll_s=0.5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertTrue(await until(
                pilot, lambda: a.runs >= 2 and b.runs >= 2, tries=100))

    async def test_r_refreshes_everything(self):
        a = CountingUnit("a", [self.f("a.txt")])
        b = CountingUnit("b", [])
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: b.runs == 2))
            self.assertEqual(a.runs, 2)

    async def test_a_burst_of_changes_is_debounced_per_source(self):
        class Fake:
            t = 1000.0

            def __call__(self):
                return self.t

        clock = Fake()
        a = CountingUnit("a", [self.f("a.txt")])
        app = unit_app([a], watch_s=1000.0, debounce_s=0.6)   # ticks are manual
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.watcher.clock = clock
            app.watcher.commit()                  # baseline at fake t=1000
            stamps = []
            for n in range(14):                   # a write every fake 0.1 s
                clock.t += 0.1
                (self.dir / "a.txt").write_text("x" * (n + 2))
                before = a.runs
                app.watch_tick()
                await until(pilot, lambda: not app.polling)
                if a.runs > before:
                    stamps.append(round(clock.t, 1))
            self.assertEqual(stamps, [1000.6, 1001.2])
            clock.t += 1.0                        # quiet now: the held change fires
            app.watch_tick()
            await until(pilot, lambda: a.runs == 4 and not app.polling)
            self.assertEqual(a.runs, 4)           # the first run + three

    async def test_a_watched_file_that_appears_counts_as_a_change(self):
        later = self.dir / "later.jsonl"
        a = CountingUnit("a", [str(later)])
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.pause(0.3)
            self.assertEqual(a.runs, 1)
            later.write_text("hello")
            self.assertTrue(await until(pilot, lambda: a.runs == 2))

    async def test_stat_errors_do_not_crash_or_refresh(self):
        a = CountingUnit("a", [self.f("a.txt")])
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)

            def boom(path):
                raise PermissionError("denied")

            app.watcher._stat = boom
            app.watcher.commit()            # baseline: every path now errors
            await pilot.pause(0.5)
            self.assertEqual(a.runs, 1)
            self.assertTrue(app.is_running)

    async def test_refreshes_never_overlap_and_a_change_is_picked_up_after(self):
        a = CountingUnit("a", [self.f("a.txt")], delay=0.5)
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            for _ in range(100):
                if a.runs:
                    break
                await pilot.pause(0.02)
            (self.dir / "a.txt").write_text("changed while the first run is busy")
            self.assertTrue(await until(pilot, lambda: a.runs == 2, tries=200))
            self.assertEqual(a.peak, 1)

    async def test_the_header_shows_updated_age_not_a_countdown(self):
        a = CountingUnit("a", [self.f("a.txt")])
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            head = str(app.query_one("#hdr").render())
            self.assertIn("updated", head)
            self.assertIn("ago", head)
            self.assertNotIn("next poll", head)

    async def test_the_oldest_source_sets_the_header_age(self):
        a = CountingUnit("a", [self.f("a.txt")])
        b = CountingUnit("b", [])
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.updated_at["b"] -= 120
            self.assertGreaterEqual(app.oldest_update_age(), 119)
            app.tick_header()
            self.assertIn("updated 2m ago", str(app.query_one("#hdr").render()))

    async def test_a_source_error_still_shows(self):
        a = CountingUnit("a", [self.f("a.txt")])
        a.run = lambda now, show_all: [
            sources.SourceResult("a", [], False, None, "exit 3: boom")]
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("a: exit 3: boom", app.source_problems())

    async def test_a_raising_unit_becomes_a_source_error(self):
        a = CountingUnit("a", [])

        def boom(now, show_all):
            raise RuntimeError("bug")

        a.run = boom
        b = CountingUnit("b", [])
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertTrue(any("poll failed" in p for p in app.source_problems()))
            self.assertEqual([i.key for i in app.items], ["b:1"])

    async def test_a_toggle_re_runs_only_sources_that_support_all(self):
        plain = CountingUnit("plain", [])
        wide = CountingUnit("wide", [], uses_all=True)
        app = unit_app([plain, wide])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("A")
            self.assertTrue(await until(pilot, lambda: wide.runs == 2))
            await pilot.pause(0.2)
            self.assertEqual((plain.runs, wide.alls), (1, [False, True]))

    async def test_duplicate_ids_across_units_are_flagged_after_a_partial_refresh(self):
        shared = [item(1, "fyi", "calendar", id="dup:1")]
        a = CountingUnit("a", [self.f("a.txt")], items=shared)
        b = CountingUnit("b", [], items=list(shared))
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            (self.dir / "a.txt").write_text("changed, longer")
            self.assertTrue(await until(pilot, lambda: a.runs == 2))
            await pilot.pause(0.3)
            self.assertEqual([i.key for i in app.items], ["dup:1"])
            self.assertTrue(any("duplicate id" in p for p in app.source_problems()))

    async def test_the_saved_rows_are_read_with_each_refresh(self):
        events_file = self.dir / "events.jsonl"
        events_file.write_text("one")
        calls = []

        def saved():
            calls.append(1)
            n = len(events_file.read_text())
            return [{"id": f"s{i}", "source": "slack", "kind": "new",
                     "ts": time.time() - i, "title": f"Row {i}", "link": None,
                     "source_id": f"slack:{i}"} for i in range(n)], None

        app = unit_app([CountingUnit("a", [str(events_file)])], saved=saved)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.pause(0.3)
            self.assertEqual(len(calls), 1)         # the first refresh
            self.assertEqual(dict(app.feed_data.counts)["saved to memory"], 3)
            before = len(calls)
            await pilot.pause(0.5)
            self.assertEqual(len(calls), before)    # no change: no reads
            events_file.write_text("one two")       # the events file changed
            self.assertTrue(await until(
                pilot, lambda: dict(app.feed_data.counts).get(
                    "saved to memory") == 7))


    async def test_a_fallback_refresh_that_collides_is_queued_not_dropped(self):
        a = CountingUnit("a", [], delay=0.4)
        b = CountingUnit("b", [])
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("r")                 # a slow full refresh
            await until(pilot, lambda: app.polling, tries=50)
            app.poll()                             # the fallback timer fires
            app.poll()
            self.assertEqual(a.runs, 2)            # nothing extra started
            await until(pilot, lambda: a.runs == 3 and not app.polling, tries=200)
            await pilot.pause(0.6)
            self.assertEqual((a.runs, b.runs), (3, 3))   # one queued full refresh

    async def test_a_queued_toggle_and_fallback_merge_into_a_full_refresh(self):
        wide = CountingUnit("wide", [], uses_all=True, delay=0.4)
        plain = CountingUnit("plain", [])
        app = unit_app([wide, plain])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("r")
            await until(pilot, lambda: app.polling, tries=50)
            await pilot.press("A")                 # subset, queued
            app.poll()                             # full, merges and wins
            await until(pilot, lambda: wide.runs == 3 and not app.polling, tries=200)
            await pilot.pause(0.5)
            self.assertEqual((wide.runs, plain.runs), (3, 3))
            self.assertEqual(wide.alls[-1], True)

    async def test_an_unchanged_watched_source_counts_as_fresh(self):
        a = CountingUnit("a", [self.f("a.txt")])
        b = CountingUnit("b", [])                  # nothing to watch
        app = unit_app([a, b])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.updated_at["a"] -= 300
            app.updated_at["b"] -= 300
            await until(pilot, lambda: time.time() - app.updated_at["a"] < 5)
            self.assertLess(time.time() - app.updated_at["a"], 5)
            self.assertGreaterEqual(time.time() - app.updated_at["b"], 299)
            self.assertEqual(a.runs, 1)            # fresh without a re-run
            self.assertGreaterEqual(app.oldest_update_age(), 299)

    async def test_a_changed_but_debounced_source_is_not_called_fresh(self):
        a = CountingUnit("a", [self.f("a.txt")])
        app = unit_app([a], debounce_s=3600.0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.watcher.commit()                   # last_run = now
            app.updated_at["a"] -= 300
            (self.dir / "a.txt").write_text("changed, longer")
            await pilot.pause(0.4)
            self.assertGreaterEqual(time.time() - app.updated_at["a"], 299)
            self.assertEqual(a.runs, 1)

    async def test_the_clock_is_re_applied_without_running_a_source(self):
        a = CountingUnit("a", [])
        calls = []

        def reeval(now):
            calls.append(now)
            title = "later" if len(calls) > 1 else "first"
            return [sources.SourceResult("a", [item(1, "fyi", "calendar",
                                                     id=f"a:{title}")],
                                         False, None, None)]

        a.reeval = reeval
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual([i.key for i in app.items], ["a:1"])
            app.updated_at["a"] -= 300
            app.reeval()
            await until(pilot, lambda: calls and not app.polling)
            await pilot.pause(0.3)
            self.assertEqual([i.key for i in app.items], ["a:first"])
            self.assertEqual(a.runs, 1)            # no source was run
            self.assertGreaterEqual(time.time() - app.updated_at["a"], 299)
            app.reeval()
            await until(pilot, lambda: len(calls) == 2 and not app.polling)
            await pilot.pause(0.3)
            self.assertEqual([i.key for i in app.items], ["a:later"])

    async def test_a_unit_without_reeval_is_left_alone(self):
        a = CountingUnit("a", [])
        app = unit_app([a])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.reeval()
            await pilot.pause(0.3)
            self.assertEqual((a.runs, [i.key for i in app.items]), (1, ["a:1"]))

    async def test_invites_list_soonest_first_after_the_replies(self):
        now = time.time()
        rows = [
            item(1, "needs_reply", "slack", id="s:1", actions=("ack",)),
            item(2, "needs_reply", "calendar", id="inv:late", ts=now + 5 * 86400 + 3600,
                 actions=("ack",), meta={"kind": "invite"}),
            item(3, "needs_reply", "calendar", id="inv:soon", ts=now + 3700,
                 actions=("ack",), meta={"kind": "invite"}),
        ]
        app, _, _ = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual([i.key for i in app.visible()],
                             ["s:1", "inv:soon", "inv:late"])
            q = app.query_one("#queue", Queue)
            text = " ".join(str(q.get_option_at_index(n).prompt)
                            for n in range(q.option_count))
            self.assertIn("in 1h", text)
            self.assertIn("in 5d", text)



class WatchOffWithoutUnitsTest(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
    async def test_a_plain_fetch_app_has_no_watcher(self):
        app, _, _ = make_app([item(1)])
        self.assertIsNone(app.watcher)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("updated", str(app.query_one("#hdr").render()))


if __name__ == "__main__":
    unittest.main()
