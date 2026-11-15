"""Pilot tests for the inbox UI. Skipped when Textual is not installed."""
import json
import os
import tempfile
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
            self.assertEqual(self.app().bidi_mode, "logical")

    def test_unknown_terminal_defaults_to_visual_when_available(self):
        from workspace_daemon.inbox import bidi
        with mock.patch.dict(os.environ, {"TERM_PROGRAM": "mystery"}):
            os.environ.pop("INBOX_BIDI", None)
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
