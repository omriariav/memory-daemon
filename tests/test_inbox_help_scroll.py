"""Inbox 0.12.1: the `?` keyboard reference scrolls (keys and mouse wheel)
when its content is taller than the screen.

Fictional data only. Needs Textual; the class skips without it."""
import importlib.util
import unittest

from workspace_daemon.inbox import items as itm, sources
from workspace_daemon.inbox.actions import FakeActions
from workspace_daemon.inbox.store import InboxStore

try:    # run by discovery (tests/ on sys.path) or by dotted module name
    from test_inbox_draft import wait_for
except ImportError:
    from tests.test_inbox_draft import wait_for

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from textual import events
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import HelpScreen

DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
SMALL = (100, 24)     # the reference is far taller than 24 lines
BIG = (150, 170)      # ... and fits in 170


class Fixed:
    def __call__(self):
        row = itm.make_item(
            id="t:1", source="slack", section="fyi", urgency="digest",
            sender="Person", where="#room", gist="gist", ts=1893492000.0,
            link=None, draft=None, actions=["ack"])
        return [sources.SourceResult("t", [row], False, None, None)]


def make_app():
    store = InboxStore(None)
    return InboxApp(Fixed(), store, FakeActions(store=store, draft_delay=0),
                    settings=dict(DEFAULTS), undo_s=30, leave_s=0,
                    stream_delay=0, view="feed")


async def settle(pilot, app):
    await wait_for(pilot, lambda: app.polled_once, tries=200, step=0.02)
    await pilot.pause(0.25)


async def open_help(pilot, app):
    await pilot.press("question_mark")
    assert await wait_for(pilot, lambda: isinstance(app.screen, HelpScreen))
    pane = app.screen.query_one("#help-scroll")
    assert await wait_for(pilot, lambda: pane.max_scroll_y > 0)
    return pane


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class HelpScrollTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_body_is_a_focused_scroll_container(self):
        app = make_app()
        async with app.run_test(size=SMALL) as pilot:
            await settle(pilot, app)
            pane = await open_help(pilot, app)
            self.assertTrue(await wait_for(pilot, lambda: app.focused is pane))
            self.assertEqual(pane.scroll_y, 0)

    async def test_keys_scroll_down_and_back_up(self):
        app = make_app()
        async with app.run_test(size=SMALL) as pilot:
            await settle(pilot, app)
            pane = await open_help(pilot, app)
            for key in ("j", "down"):
                before = pane.scroll_y
                await pilot.press(key)
                self.assertTrue(await wait_for(
                    pilot, lambda: pane.scroll_y > before), key)
            for key in ("k", "up"):
                before = pane.scroll_y
                await pilot.press(key)
                self.assertTrue(await wait_for(
                    pilot, lambda: pane.scroll_y < before), key)

    async def test_page_keys_and_home_end(self):
        app = make_app()
        async with app.run_test(size=SMALL) as pilot:
            await settle(pilot, app)
            pane = await open_help(pilot, app)
            await pilot.press("pagedown")
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y > 5))
            mid = pane.scroll_y
            await pilot.press("pageup")
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y < mid))
            await pilot.press("end")
            self.assertTrue(await wait_for(
                pilot, lambda: pane.scroll_y == pane.max_scroll_y))
            await pilot.press("home")
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y == 0))

    async def test_the_mouse_wheel_scrolls(self):
        app = make_app()
        async with app.run_test(size=SMALL) as pilot:
            await settle(pilot, app)
            pane = await open_help(pilot, app)
            pane.scroll_to(0, 0, animate=False)
            await pilot.pause()
            body = app.screen.query_one("#help-body")
            body.post_message(events.MouseScrollDown(
                body, 5, 5, 0, 0, 0, False, False, False))
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y > 0))

    async def test_escape_and_question_mark_still_close(self):
        app = make_app()
        async with app.run_test(size=SMALL) as pilot:
            await settle(pilot, app)
            await open_help(pilot, app)
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, HelpScreen)))
            await open_help(pilot, app)
            await pilot.press("question_mark")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, HelpScreen)))

    async def test_a_tall_screen_shows_it_all_without_scrolling(self):
        app = make_app()
        async with app.run_test(size=BIG) as pilot:
            await settle(pilot, app)
            await pilot.press("question_mark")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, HelpScreen)))
            pane = app.screen.query_one("#help-scroll")
            await pilot.pause()
            self.assertEqual(pane.max_scroll_y, 0)


if __name__ == "__main__":
    unittest.main()
