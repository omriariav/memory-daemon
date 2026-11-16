"""Inbox 0.5.0: the thread pane's order (`v`, `thread_order:`).

Fictional data only. The store and settings classes run without Textual; the
pane tests need it."""
import importlib.util
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workspace_daemon.inbox import items as itm, settings, sources
from workspace_daemon.inbox.actions import FakeActions
from workspace_daemon.inbox.store import InboxStore

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp

SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def thread_item(i=1, **kw):
    base = dict(
        id=f"t:{i}", source="slack", section="fyi", urgency="digest",
        sender="Person", where="#room", gist="gist",
        ts=1893492000.0, link=None, draft=None, actions=["ack"],
        meta={"thread_note": "NOTE: a dim line about the thread"},
        thread=[{"who": "Ann", "ts": 1893492000.0 - 300, "text": "FIRST message"},
                {"who": "Bob", "ts": 1893492000.0 - 200, "text": "SECOND message"},
                {"who": "Cy", "ts": 1893492000.0 - 100, "text": "THIRD message"}])
    base.update(kw)
    return itm.make_item(**base)


class SettingsTest(unittest.TestCase):
    def test_default_is_newest(self):
        self.assertEqual(settings.default_settings()["thread_order"], "newest")

    def test_values(self):
        for value in ("newest", "oldest"):
            self.assertEqual(
                settings.validate({"thread_order": value})["thread_order"], value)
        for bad in ("latest", "", None, 1, True):
            with self.assertRaises(settings.SettingsError) as ctx:
                settings.validate({"thread_order": bad})
            self.assertIn("thread_order must be one of", str(ctx.exception))

    def test_v_is_reserved_so_actions_cannot_take_it(self):
        self.assertIn("v", settings.RESERVED_KEYS)
        with self.assertRaises(settings.SettingsError):
            settings.validate({"actions": [{
                "key": "v", "label": "x", "argv": ["x"]}]})


class StorePrefTest(unittest.TestCase):
    def test_pref_round_trips_through_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = InboxStore(Path(tmp))
            self.assertIsNone(store.get_pref("thread_order"))
            store.set_pref("thread_order", "oldest")
            again = InboxStore(Path(tmp))                 # a restart
            self.assertEqual(again.get_pref("thread_order"), "oldest")
            self.assertEqual(oct((Path(tmp) / "state" / "inbox-state.json")
                                 .stat().st_mode & 0o777), "0o600")

    def test_marks_and_prefs_do_not_clobber_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = InboxStore(Path(tmp)), InboxStore(Path(tmp))
            a.set_pref("thread_order", "oldest")
            b.done("t:1")                                  # b had not seen it
            c = InboxStore(Path(tmp))
            self.assertEqual(c.get_pref("thread_order"), "oldest")
            self.assertTrue(c.is_hidden("t:1"))
            c.restore("t:1")
            self.assertEqual(
                InboxStore(Path(tmp)).get_pref("thread_order"), "oldest")

    def test_bad_names_and_values_are_refused_and_bad_files_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = InboxStore(Path(tmp))
            for name, value in (("thread_order", "sideways"), ("other", "x")):
                with self.assertRaises(ValueError):
                    store.set_pref(name, value)
            path = Path(tmp) / "state" / "inbox-state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"v": 1, "items": {}, "prefs": '
                            '{"thread_order": "sideways", "x": "y"}}')
            self.assertEqual(InboxStore(Path(tmp)).prefs, {})

    def test_in_memory_store_keeps_it_for_the_session(self):
        store = InboxStore(None)
        store.set_pref("thread_order", "oldest")
        self.assertEqual(store.get_pref("thread_order"), "oldest")
        store.done("t:1")
        self.assertEqual(store.get_pref("thread_order"), "oldest")


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


def make_app(rows, store, **cfg):
    return InboxApp(Fixed(rows), store, FakeActions(store=store, draft_delay=0),
                    settings=dict(DEFAULTS, **cfg), undo_s=30, leave_s=0,
                    stream_delay=0)


def thread_text(app):
    return str(app.query_one("#thread-body").render())


def order_of(text):
    marks = ["THIRD", "SECOND", "FIRST"]
    return [m for _pos, m in sorted((text.index(m), m) for m in marks)]


def worker_threads():
    return [t for t in threading.enumerate()
            if t.name in ("inbox-action-choices", "inbox-action-prepare",
                          "inbox-context-fetch", "inbox-summary")]


class WorkerCleanup:
    """A lookup thread of the app under test must be gone when the test ends
    (they are not daemon threads: one left waiting would hang the run)."""

    async def asyncTearDown(self):
        deadline = time.monotonic() + 15
        for t in worker_threads():
            t.join(max(0.1, deadline - time.monotonic()))
        self.assertEqual([t.name for t in worker_threads()], [],
                         "a worker thread outlived its test")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ThreadPaneTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    async def test_a_thread_note_means_no_start_marker(self):
        app = make_app([thread_item()], InboxStore(None))   # carries a note
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            text = thread_text(app)
            self.assertEqual(order_of(text), ["THIRD", "SECOND", "FIRST"])
            self.assertNotIn("thread start", text)          # may be a cut thread

    async def test_default_is_newest_first_with_the_start_marker_last(self):
        app = make_app([thread_item(meta={})], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            text = thread_text(app)
            self.assertEqual(app.thread_order, "newest")
            self.assertEqual(order_of(text), ["THIRD", "SECOND", "FIRST"])
            self.assertIn("thread start", text)
            self.assertGreater(text.index("thread start"), text.index("FIRST"))

    async def test_v_reverses_it_and_back(self):
        store = InboxStore(None)
        app = make_app([thread_item()], store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("v")
            await pilot.pause(0.2)
            text = thread_text(app)
            self.assertEqual(app.thread_order, "oldest")
            self.assertEqual(order_of(text), ["FIRST", "SECOND", "THIRD"])
            self.assertNotIn("thread start", text)
            self.assertTrue(any("oldest first" in f"{n.message}"
                                for n in app._notifications))
            await pilot.press("v")
            await pilot.pause(0.2)
            self.assertEqual(order_of(thread_text(app)),
                             ["THIRD", "SECOND", "FIRST"])

    async def test_the_note_and_header_stay_on_top_in_both_orders(self):
        app = make_app([thread_item()], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for _ in range(2):
                text = thread_text(app)
                self.assertTrue(text.startswith("Person"))
                note, first = text.index("NOTE:"), min(
                    text.index(m) for m in ("FIRST", "SECOND", "THIRD"))
                self.assertLess(note, first)
                await pilot.press("v")
                await pilot.pause(0.2)

    async def test_a_single_message_has_no_start_marker_and_a_note_only_item_works(self):
        one = thread_item(meta={}, thread=[
            {"who": "Ann", "ts": 1893491000.0, "text": "FIRST message"}])
        app = make_app([one], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn("thread start", thread_text(app))
        empty = thread_item(thread=[])
        app = make_app([empty], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("NOTE:", thread_text(app))
            await pilot.press("v")
            await pilot.pause(0.1)
            self.assertIn("NOTE:", thread_text(app))

    async def test_the_pane_starts_at_the_top_after_a_toggle(self):
        long_thread = [{"who": f"P{n}", "ts": 1893490000.0 + n,
                        "text": "line " * 40} for n in range(30)]
        app = make_app([thread_item(thread=long_thread)], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            pane = app.query_one("#thread")
            pane.scroll_end(animate=False)
            await pilot.pause(0.2)
            self.assertGreater(pane.scroll_y, 0)
            await pilot.press("v")
            await pilot.pause(0.3)
            self.assertEqual(pane.scroll_y, 0)

    async def test_the_queue_is_not_reordered(self):
        rows = [thread_item(1, ts=1893492000.0), thread_item(2, ts=1893491000.0)]
        app = make_app(rows, InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            before = [i.key for i in app.visible()]
            await pilot.press("v")
            await pilot.pause(0.2)
            self.assertEqual([i.key for i in app.visible()], before)

    async def test_the_setting_is_the_starting_order(self):
        app = make_app([thread_item()], InboxStore(None), thread_order="oldest")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(order_of(thread_text(app)),
                             ["FIRST", "SECOND", "THIRD"])

    async def test_the_choice_persists_across_a_restart_and_beats_the_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = make_app([thread_item()], InboxStore(Path(tmp)))
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await pilot.press("v")                 # newest -> oldest
                await pilot.pause(0.2)
            self.assertEqual(
                InboxStore(Path(tmp)).get_pref("thread_order"), "oldest")
            app = make_app([thread_item()], InboxStore(Path(tmp)))   # restart
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                self.assertEqual(app.thread_order, "oldest")
                self.assertEqual(order_of(thread_text(app)),
                                 ["FIRST", "SECOND", "THIRD"])
                await pilot.press("v")
                await pilot.pause(0.2)
            app = make_app([thread_item()], InboxStore(Path(tmp)),
                           thread_order="oldest")
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                self.assertEqual(app.thread_order, "newest")   # saved wins

    async def test_a_store_that_cannot_write_still_toggles_and_says_so(self):
        store = InboxStore(None)
        store.set_pref = lambda *a: (_ for _ in ()).throw(OSError(28, "disk full"))
        app = make_app([thread_item()], store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("v")
            await pilot.pause(0.2)
            self.assertEqual(app.thread_order, "oldest")
            self.assertTrue(any("Order not saved" in f"{n.message}"
                                for n in app._notifications))

    async def test_footer_help_and_palette_list_it(self):
        app = make_app([thread_item()], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("v", "order"), app.hints())
            self.assertEqual(app.hints()[-1], ("?", "help"))
            titles = {c.title for c in app.get_system_commands(app.screen)}
            self.assertIn("Toggle thread order", titles)
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            self.assertIn("thread order",
                          str(app.screen.query_one("#help-body").render()))

    async def test_v_does_nothing_while_a_modal_is_open(self):
        app = make_app([thread_item()], InboxStore(None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            await pilot.press("v")
            await pilot.pause(0.1)
            self.assertEqual(app.thread_order, "newest")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class KeyTest(unittest.TestCase):
    def test_v_is_bound_once_to_the_toggle(self):
        keys = [k for b in InboxApp.BINDINGS for k in b.key.split(",")]
        self.assertEqual(keys.count("v"), 1)
        action = {k: b.action for b in InboxApp.BINDINGS
                  for k in b.key.split(",")}["v"]
        self.assertEqual(action, "toggle_thread_order")
        self.assertTrue(hasattr(InboxApp, "action_toggle_thread_order"))

    def test_p_stays_free_for_actions(self):
        keys = {k for b in InboxApp.BINDINGS for k in b.key.split(",")}
        self.assertNotIn("p", keys)
        self.assertIn("p", settings.ACTION_KEY_CHOICES)


if __name__ == "__main__":
    unittest.main()
