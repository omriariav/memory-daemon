"""Inbox 0.12.2: the Tasks view (and the others) scroll at a short terminal.

Many synthetic task rows and a long detail card in a 100x24 terminal: the
cursor stays visible while j/k/arrows move it, PgUp/PgDn and Home/End move it
by a page and to the ends, the mouse wheel scrolls the list, and the long
detail card scrolls. Synthetic data only. Needs Textual; skips without it."""
import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_acts_support as support  # noqa: E402

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from textual import events
    from workspace_daemon.inbox import items as itm, sources
    from workspace_daemon.inbox.actions import FakeActions
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import ITEM_PREFIX, Queue

try:
    from test_inbox_draft import wait_for
except ImportError:
    from tests.test_inbox_draft import wait_for

SIZE = (100, 24)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
TODAY = dt.date(2030, 1, 10)
N = 45
LONG = "\n".join(f"Evidence line {n}: a synthetic sentence for the card."
                 for n in range(60))


class Fixed:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        found = [itm.parse_item(r) for r in self.rows]
        return [sources.SourceResult("tasks", found, False, None, None)]


def task_rows():
    return [support.task_item(n, due=(TODAY + dt.timedelta(days=n % 5))
                              .isoformat(), people=["Ann Example"],
                              evidence=LONG if n == 1 else None)
            for n in range(1, N + 1)]


def feed_row(n, section="fyi", source="slack"):
    return itm.make_item(
        id=f"f:{n}", source=source, section=section, urgency="digest",
        sender=f"Person {n}", where="#room", gist=f"gist {n}",
        ts=1893492000.0 + n, link=None, draft=None, actions=["ack"])


class FeedFixed:
    """40 rows of one section, from 40 different people."""

    def __init__(self, section):
        self.rows = [feed_row(n, section) for n in range(40)]

    def __call__(self):
        return [sources.SourceResult("t", list(self.rows), False, None, None)]


def visible(q, key):
    """True when the whole option of `key` lies inside the queue's viewport."""
    idx = q.get_option_index(ITEM_PREFIX + key)
    top = q._index_to_line[idx]
    bottom = top + q._heights[idx] - 1
    return q.scroll_offset.y <= top and bottom < q.scroll_offset.y + q.size.height


def inside(child, parent):
    """True when `child` lies inside `parent`'s content area (a scroll
    container that overflows its parent clips its last rows)."""
    return parent.content_region.contains_region(child.region)


def wheel(widget, down=True):
    cls = events.MouseScrollDown if down else events.MouseScrollUp
    widget.post_message(cls(widget, 5, 5, 0, 0, 0, False, False, False))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ScrollBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = InboxStore(Path(self.tmp.name))

    def app(self, fetch, view):
        return InboxApp(fetch, self.store,
                        FakeActions(store=self.store, draft_delay=0),
                        settings=dict(DEFAULTS), undo_s=30, leave_s=0,
                        stream_delay=0, view=view, today=lambda: TODAY)

    async def settle(self, pilot, app):
        self.assertTrue(await wait_for(pilot, lambda: app.polled_once
                                       and app.current_key is not None,
                                       tries=300))

    async def step(self, pilot, app, key):
        """Press `key` and wait for the selection to change."""
        before = app.current_key
        await pilot.press(key)
        self.assertTrue(await wait_for(
            pilot, lambda: app.current_key != before), key)

    async def assert_cursor_visible(self, pilot, app, q, why):
        key = app.current_key
        self.assertIsNotNone(key, why)
        self.assertTrue(await wait_for(pilot, lambda: visible(q, key)), why)


class TasksScrollTest(ScrollBase):
    async def test_cursor_stays_visible_with_j_k_and_arrows(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            self.assertIs(app.focused, q)
            for key in ("j", "down"):
                for n in range(18):
                    await self.step(pilot, app, key)
                    await self.assert_cursor_visible(pilot, app, q, (key, n))
            self.assertGreater(q.scroll_offset.y, 0)
            for key in ("k", "up"):
                for n in range(18):
                    await self.step(pilot, app, key)
                    await self.assert_cursor_visible(pilot, app, q, (key, n))

    async def test_page_keys_home_end_and_g(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            left = app.query_one("#left")
            first = app.current_key
            await self.step(pilot, app, "pagedown")
            await self.assert_cursor_visible(pilot, app, q, "pagedown")
            self.assertGreater(q.scroll_offset.y, 0)
            await self.step(pilot, app, "pageup")
            await self.assert_cursor_visible(pilot, app, q, "pageup")
            self.assertEqual(app.current_key, first)
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y == 0))     # header shows
            # another PgUp at the top keeps the cursor on the first row
            await pilot.press("pageup")
            await pilot.pause(0.2)    # negative check: it must not change
            self.assertEqual(app.current_key, first)
            self.assertIsNotNone(q.highlighted)
            await self.step(pilot, app, "end")
            await self.assert_cursor_visible(pilot, app, q, "end")
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y == q.max_scroll_y))
            self.assertTrue(inside(q, left))
            last = app.current_key
            await pilot.press("pagedown")
            await pilot.pause(0.2)    # negative check: already at the end
            self.assertEqual(app.current_key, last)
            await self.step(pilot, app, "home")
            self.assertEqual(app.current_key, first)
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y == 0))
            await self.step(pilot, app, "G")
            await self.assert_cursor_visible(pilot, app, q, "G")
            await self.step(pilot, app, "g")
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y == 0))

    async def test_the_mouse_wheel_scrolls_the_list(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            self.assertEqual(q.scroll_offset.y, 0)
            wheel(q)
            self.assertTrue(await wait_for(pilot, lambda: q.scroll_offset.y > 0))
            down = q.scroll_offset.y
            wheel(q, down=False)
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y < down))

    async def test_a_long_detail_card_scrolls(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            card = app.query_one("#thread")
            q.highlighted = q.get_option_index(ITEM_PREFIX + "task:1")
            self.assertTrue(await wait_for(
                pilot, lambda: app.current_key == "task:1"
                and card.max_scroll_y > 0))
            await pilot.press("tab")
            self.assertTrue(await wait_for(pilot, lambda: app.focused is card))
            for key in ("j", "down", "pagedown"):
                before = card.scroll_y
                await pilot.press(key)
                self.assertTrue(await wait_for(
                    pilot, lambda: card.scroll_y > before), key)
            await pilot.press("end")
            self.assertTrue(await wait_for(
                pilot, lambda: card.scroll_y == card.max_scroll_y))
            self.assertTrue(inside(card, app.query_one("#right")))
            before = card.scroll_y
            await pilot.press("k")
            self.assertTrue(await wait_for(pilot, lambda: card.scroll_y < before))
            await pilot.press("home")
            self.assertTrue(await wait_for(pilot, lambda: card.scroll_y == 0))
            wheel(card)
            self.assertTrue(await wait_for(pilot, lambda: card.scroll_y > 0))

    async def test_a_redraw_keeps_the_card_where_it_was_scrolled(self):
        # The cause of "the Tasks view does not scroll": every redraw (a
        # refresh, a resize) drew the same card again and snapped it to the top.
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            card = app.query_one("#thread")
            q.highlighted = q.get_option_index(ITEM_PREFIX + "task:1")
            self.assertTrue(await wait_for(
                pilot, lambda: app.current_key == "task:1"
                and card.max_scroll_y > 0))
            card.scroll_to(y=card.max_scroll_y, animate=False)
            self.assertTrue(await wait_for(
                pilot, lambda: card.scroll_y == card.max_scroll_y))
            app.redraw()
            await pilot.pause(0.3)    # negative check: the card must not move
            self.assertEqual(card.scroll_y, card.max_scroll_y)
            app.poll(quiet=True)
            self.assertTrue(await wait_for(
                pilot, lambda: not app.polling, tries=300))
            await pilot.pause(0.3)    # negative check, after the refresh
            self.assertEqual(card.scroll_y, card.max_scroll_y)
            # another task starts at the top
            await self.step(pilot, app, "k")
            self.assertTrue(await wait_for(pilot, lambda: card.scroll_y == 0))

    async def test_a_resize_keeps_the_cursor_visible(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            for _ in range(16):
                await self.step(pilot, app, "j")
            await self.assert_cursor_visible(pilot, app, q, "before")
            for rows in (23, 21, 18):
                await pilot.resize_terminal(SIZE[0], rows)
                self.assertTrue(await wait_for(
                    pilot, lambda: app.size.height == rows))
                await self.assert_cursor_visible(pilot, app, q, rows)


class ViewportTest(ScrollBase):
    """A redraw (a refresh every 30 s or so) must not move the viewport the
    reader scrolled to; the list used to snap to the cursor or the top."""

    async def test_a_redraw_after_the_wheel_keeps_the_offset(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            for _ in range(8):      # the cursor is low in the first page
                await self.step(pilot, app, "j")
            wheel(q)                # one notch: the cursor stays in view
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y > 0))
            await self.assert_cursor_visible(pilot, app, q, "after wheel")
            offset, key = q.scroll_offset.y, app.current_key
            app.redraw()
            await pilot.pause(0.4)    # negative check: the offset must not move
            self.assertEqual(q.scroll_offset.y, offset)
            self.assertEqual(app.current_key, key)

    async def test_a_redraw_keeps_the_viewport_with_the_cursor_mid_view(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            for _ in range(20):
                await self.step(pilot, app, "j")
            for _ in range(5):
                await self.step(pilot, app, "k")
            await self.assert_cursor_visible(pilot, app, q, "after k")
            offset, key = q.scroll_offset.y, app.current_key
            self.assertGreater(offset, 0)
            app.redraw()
            await pilot.pause(0.4)    # negative check: the viewport stays
            self.assertEqual(q.scroll_offset.y, offset)
            self.assertEqual(app.current_key, key)
            await self.assert_cursor_visible(pilot, app, q, "after redraw")

    async def test_no_frame_is_painted_at_a_snapped_offset(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            for _ in range(20):
                await self.step(pilot, app, "j")
            for _ in range(5):
                await self.step(pilot, app, "k")
            await self.assert_cursor_visible(pilot, app, q, "before")
            offset = q.scroll_offset.y
            self.assertGreater(offset, 4)
            frames = []
            real = Queue.render_lines

            def spy(widget, crop):
                if widget is q:
                    frames.append(widget.scroll_offset.y)
                return real(widget, crop)
            with unittest.mock.patch.object(Queue, "render_lines", spy):
                for _ in range(3):
                    app.redraw()
                    await pilot.pause()
                self.assertTrue(await wait_for(pilot, lambda: bool(frames)))
                await pilot.pause(0.3)    # let every queued frame paint
            self.assertEqual(set(frames), {offset}, frames)

    async def test_a_view_switch_starts_the_new_list_at_its_header(self):
        class Mixed:
            def __call__(self):
                rows = [itm.parse_item(r) for r in task_rows()]
                rows += [feed_row(n, "urgent") for n in range(40)]
                return [sources.SourceResult("m", rows, False, None, None)]
        app = self.app(Mixed(), "inbox")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            for _ in range(30):
                await self.step(pilot, app, "j")
            self.assertGreater(q.scroll_offset.y, 10)
            await pilot.press("3")
            self.assertTrue(await wait_for(
                pilot, lambda: app.view == "tasks"
                and app.current_key.startswith("task:")))
            self.assertTrue(await wait_for(
                pilot, lambda: q.scroll_offset.y == 0))   # the header shows
            await pilot.press("1")
            self.assertTrue(await wait_for(pilot, lambda: app.view == "inbox"))
            self.assertTrue(await wait_for(
                pilot, lambda: visible(q, app.current_key)))

    async def test_a_redraw_still_brings_a_hidden_cursor_into_view(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            for _ in range(8):
                wheel(q)    # the wheel scrolls the cursor row out of view
            self.assertTrue(await wait_for(
                pilot, lambda: not visible(q, app.current_key)))
            app.redraw()
            self.assertTrue(await wait_for(
                pilot, lambda: visible(q, app.current_key)))

    async def test_g_and_home_reach_the_top_when_already_on_the_first_row(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            q = app.query_one("#queue", Queue)
            first = app.current_key
            for key in ("g", "home"):
                for _ in range(2):
                    wheel(q)
                self.assertTrue(await wait_for(
                    pilot, lambda: q.scroll_offset.y > 0))
                await pilot.press(key)
                self.assertTrue(await wait_for(
                    pilot, lambda: q.scroll_offset.y == 0), key)
                self.assertEqual(app.current_key, first)

    async def test_a_new_message_on_top_keeps_the_readers_place(self):
        thread = [{"who": f"P{n}", "ts": 1893490000.0 + n,
                   "text": "line " * 40} for n in range(30)]
        row = itm.make_item(
            id="t:1", source="slack", section="urgent", urgency="urgent",
            sender="Person", where="#room", gist="gist", ts=1893492000.0,
            link=None, draft=None, actions=["ack"], thread=thread)
        app = self.app(lambda: [sources.SourceResult("t", [row], False, None,
                                                      None)], "inbox")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            pane = app.query_one("#thread")
            self.assertTrue(await wait_for(pilot, lambda: pane.max_scroll_y > 0))
            pane.scroll_to(y=10, animate=False)
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y == 10))
            before = pane.virtual_size.height
            app.current.messages.append(type(app.current.messages[0])(
                "Late", 1893499000.0, "a new reply\n" * 6))
            app.redraw()
            self.assertTrue(await wait_for(
                pilot, lambda: pane.virtual_size.height > before
                and pane.scroll_y == 10 + pane.virtual_size.height - before))


class HintsTest(ScrollBase):
    async def test_task_hints_follow_the_focus(self):
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            keys = lambda: [k for k, _v in app.hints()]  # noqa: E731
            self.assertEqual(keys()[-2:], ["⏎", "?"])   # the queue's footer
            self.assertNotIn("tab", keys())            # not in the footer; the help has it
            await pilot.press("tab")
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused is app.query_one("#thread")))
            self.assertIn(("j/k", "scroll"), app.hints())
            self.assertIn(("esc", "queue"), app.hints())
            self.assertNotIn(("j/k", "move"), app.hints())


class ContextGuardTest(ScrollBase):
    async def test_context_render_returns_when_the_pane_body_is_gone(self):
        from textual.css.query import NoMatches
        from workspace_daemon.inbox.widgets import ContextPane
        app = self.app(Fixed(task_rows()), "tasks")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            app.context_open = True
            with unittest.mock.patch.object(
                    ContextPane, "show", side_effect=NoMatches("#context-body")):
                app.context_render()          # must not raise
            self.assertIsNone(app._ctx_sig)   # and must not claim a drawing
            app.context_open = False


class OtherViewsScrollTest(ScrollBase):
    async def test_views_1_2_and_4_scroll_at_a_short_height(self):
        for view, section in (("inbox", "urgent"), ("waiting", "waiting"),
                              ("feed", "fyi")):
            with self.subTest(view=view):
                app = self.app(FeedFixed(section), view)
                async with app.run_test(size=(100, 12)) as pilot:
                    await self.settle(pilot, app)
                    q = app.query_one("#queue", Queue)
                    self.assertIs(app.focused, q)
                    for n in range(25):
                        await self.step(pilot, app, "j")
                        await self.assert_cursor_visible(pilot, app, q, n)
                    await self.step(pilot, app, "pagedown")
                    await self.assert_cursor_visible(pilot, app, q, "pgdn")
                    await self.step(pilot, app, "end")
                    await self.assert_cursor_visible(pilot, app, q, "end")
                    self.assertTrue(inside(q, app.query_one("#left")))
                    await self.step(pilot, app, "home")
                    self.assertTrue(await wait_for(
                        pilot, lambda: q.scroll_offset.y == 0))
                    await pilot.press("pageup")
                    await pilot.pause(0.1)    # negative: no cleared cursor
                    self.assertIsNotNone(q.highlighted)

    async def test_a_long_thread_keeps_its_scroll_over_a_redraw(self):
        row = itm.make_item(
            id="t:1", source="slack", section="urgent", urgency="urgent",
            sender="Person", where="#room", gist="gist", ts=1893492000.0,
            link=None, draft=None, actions=["ack"],
            thread=[{"who": f"P{n}", "ts": 1893490000.0 + n,
                     "text": "line " * 40} for n in range(30)])
        app = self.app(lambda: [sources.SourceResult("t", [row], False, None,
                                                      None)], "inbox")
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            pane = app.query_one("#thread")
            self.assertTrue(await wait_for(pilot, lambda: pane.max_scroll_y > 0))
            pane.scroll_to(y=5, animate=False)
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y == 5))
            app.redraw()
            await pilot.pause(0.3)    # negative check: it must not move
            self.assertEqual(pane.scroll_y, 5)


if __name__ == "__main__":
    unittest.main()
