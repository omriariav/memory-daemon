"""Inbox 0.12.1: the `?` keyboard reference scrolls (keys and mouse wheel)
when its content is taller than the screen. 0.12.4: the wheel scrolls it
wherever the pointer is (the box is 66 wide; the rest of the screen is the
modal backdrop).

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
    from workspace_daemon.inbox.screens import ConfirmSend, ConfirmWrite, HelpScreen

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


def wheel(app, kind, x, y):
    """One wheel notch as the driver posts it: screen coordinates, no widget."""
    app.post_message(kind(None, x, y, 0, 0, 0, False, False, False,
                          screen_x=x, screen_y=y))


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

    async def test_the_wheel_scrolls_wherever_the_pointer_is(self):
        # A real wheel event carries screen coordinates and goes through the
        # App and Screen hit-testing, as the driver delivers it. It used to
        # scroll only over the body text: the backdrop, border, padding and
        # title took it and did nothing.
        for size in (SMALL, (236, 60)):
            app = make_app()
            async with app.run_test(size=size) as pilot:
                await settle(pilot, app)
                pane = await open_help(pilot, app)
                box = app.screen.query_one("#help")
                title = app.screen.query_one("#help-title")
                step = app.scroll_sensitivity_y
                spots = {
                    "backdrop left": (1, 10),
                    "backdrop right": (size[0] - 2, 10),
                    "backdrop above": (box.region.x + 10, 0),
                    "border": (box.region.x, box.region.y + 8),
                    "padding": (box.region.x + 1, box.region.y + 8),
                    "title": (title.region.x + 3, title.region.y),
                    "body": (pane.region.x + 5, pane.region.y + 5),
                }
                for name, (x, y) in spots.items():
                    for kind, sign in ((events.MouseScrollDown, 1),
                                       (events.MouseScrollUp, -1)):
                        # one notch moves `step` lines, three together 3 x step
                        for notches in (1, 3):
                            pane.scroll_to(0, 20, animate=False, immediate=True)
                            await pilot.pause()
                            self.assertEqual(pane.scroll_y, 20)
                            for _ in range(notches):
                                wheel(app, kind, x, y)
                            want = 20 + sign * notches * step
                            self.assertTrue(await wait_for(
                                pilot, lambda: pane.scroll_y == want),
                                (size, name, kind.__name__, notches,
                                 pane.scroll_y, want))
                            await pilot.pause(0.1)
                            self.assertEqual(pane.scroll_y, want,
                                             (size, name, kind.__name__, notches))

    async def test_the_wheel_scrolls_a_confirm_screen_anywhere(self):
        # ConfirmSend and ConfirmWrite share the dead zone: a long text in a
        # narrow box, the wheel over the backdrop.
        long_text = "\n".join(f"line {n} of a long draft" for n in range(80))
        rows = [("Item", "row"), ("Text", long_text)]
        screens = {
            "send": lambda: ConfirmSend("Send", "Person in #room", long_text),
            "write": lambda: ConfirmWrite("Save", rows, "Save"),
        }
        for name, make in screens.items():
            app = make_app()
            async with app.run_test(size=SMALL) as pilot:
                await settle(pilot, app)
                app.push_screen(make())
                self.assertTrue(await wait_for(
                    pilot, lambda: app.screen.query("#confirm-scroll")))
                pane = app.screen.query_one("#confirm-scroll")
                self.assertTrue(await wait_for(pilot, lambda: pane.max_scroll_y > 6))
                box = app.screen.query_one("#confirm")
                step = app.scroll_sensitivity_y
                for x, y in ((0, 10), (box.region.x, 10), (box.region.x + 1, 10)):
                    pane.scroll_to(0, 10, animate=False, immediate=True)
                    await pilot.pause()
                    for _ in range(3):
                        wheel(app, events.MouseScrollDown, x, y)
                    self.assertTrue(await wait_for(
                        pilot, lambda: pane.scroll_y == 10 + 3 * step),
                        (name, x, y, pane.scroll_y))
                    for _ in range(3):
                        wheel(app, events.MouseScrollUp, x, y)
                    self.assertTrue(await wait_for(
                        pilot, lambda: pane.scroll_y == 10), (name, x, y))

    async def test_the_last_lines_are_visible_after_end(self):
        app = make_app()
        async with app.run_test(size=SMALL) as pilot:
            await settle(pilot, app)
            pane = await open_help(pilot, app)
            await pilot.press("end")
            self.assertTrue(await wait_for(
                pilot, lambda: pane.scroll_y == pane.max_scroll_y))
            box = app.screen.query_one("#help")
            # the pane (and so the last line of the reference) lies inside the
            # box, not under its border or past the screen
            self.assertTrue(await wait_for(
                pilot, lambda: box.content_region.contains_region(pane.region)))

    async def test_escape_question_mark_and_q_close(self):
        for key in ("escape", "question_mark", "q"):
            app = make_app()
            async with app.run_test(size=SMALL) as pilot:
                await settle(pilot, app)
                await open_help(pilot, app)
                await pilot.press(key)
                self.assertTrue(await wait_for(
                    pilot, lambda: not isinstance(app.screen, HelpScreen)), key)

    async def test_a_tall_screen_shows_it_all_without_scrolling(self):
        app = make_app()
        async with app.run_test(size=BIG) as pilot:
            await settle(pilot, app)
            await pilot.press("question_mark")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, HelpScreen)))
            body = app.screen.query_one("#help-body")
            pane = app.screen.query_one("#help-scroll")
            box = app.screen.query_one("#help")
            self.assertTrue(await wait_for(pilot, lambda: body.size.height > 0))
            self.assertTrue(await wait_for(
                pilot, lambda: box.content_region.contains_region(pane.region)))
            self.assertEqual(pane.max_scroll_y, 0)


if __name__ == "__main__":
    unittest.main()
