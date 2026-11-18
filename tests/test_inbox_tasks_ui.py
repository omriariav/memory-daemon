"""Pilot tests for the Tasks view and its keys (inbox 0.12.0): the sections and
the tab count, the Inbox DUE TODAY section, d / D (the decision prompt is
required, the outcome prompt), the s due picker, x with an optional reason,
undo, a conflict, the ops-log inverse, a read-only source, `o` and the date
rolling over midnight. A fake command source records the requests and keeps
the task list, so a refresh shows what was done. Synthetic data only. Skipped
when Textual is not installed. Every wait polls a condition (no fixed pauses
decide a result)."""
import datetime as dt
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_acts_support as support  # noqa: E402

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from workspace_daemon.inbox import items as itm, sources, views
    from workspace_daemon.inbox.actions import LocalActions
    from workspace_daemon.inbox.app import (ACTS_OFF, DECISION_REQUIRED,
                                            TASK_CONFLICT_TOAST,
                                            TASK_KEYS_TOAST, InboxApp)
    from workspace_daemon.inbox.screens import (ChoicePicker, ConfirmWrite,
                                                DateScreen, NoteScreen,
                                                OpsScreen)
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import (ITEM_PREFIX, Queue,
                                                task_detail)

SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
TODAY = dt.date(2030, 1, 10)       # a Thursday


def day(offset):
    return (TODAY + dt.timedelta(days=offset)).isoformat()


def rows():
    return [
        support.task_item(1, due=day(-2), people=["Ann Example", "Bo Sample"],
                          date="2030-01-02"),                    # overdue
        support.task_item(2, due=day(0), link="https://example.com/t2"),
        support.task_item(3, due=day(2), people=["Cy Test"]),    # this week
        support.task_item(4, "decision", people=["Di Demo"],
                          evidence="Two options were discussed.\nPick one."),
        support.task_item(5),                                    # no date
        support.task_item(6, due=day(20)),                       # later
    ]


class FromSource:
    """The app's fetch: lists the fake source's current items."""

    def __init__(self, src, name="tasks"):
        self.src, self.name = src, name

    def __call__(self):
        found = [itm.parse_item(r) for r in self.src.items()]
        return [sources.SourceResult(self.name, found, False, None, None)]


async def wait_for(pilot, cond, tries=200, step=0.03):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(step)
    return cond()


async def settle(pilot, app):
    await wait_for(pilot, lambda: app.polled_once and app.current_key
                   is not None)


def toasts(app):
    return [n.message for n in app._notifications]


def keys_of(app):
    return [i.key for i in app.visible()]


async def go(pilot, app, key):
    q = app.query_one("#queue", Queue)
    q.highlighted = q.get_option_index(ITEM_PREFIX + key)
    assert await wait_for(pilot, lambda: app.current_key == key), key


async def type_text(pilot, text):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)


def section_of(app, key):
    row = next(i for i in app.all_rows if i.key == key)
    group = views.group_of(app._views["tasks"], row)
    return group.key if group else None


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.src = support.FakeSource()
        self.src.set_items(rows())
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = InboxStore(self.base)
        self.today = TODAY
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.tmp.cleanup)

    def actions(self, acts=None, **kw):
        cfg = (support.acts_cfg(self.src, name="tasks",
                                acts=support.TASK_ACTS)
               if acts is None else acts)
        return LocalActions(self.store, base_dir=self.base, acts=cfg, **kw)

    def app(self, view="tasks", actions=None, undo_s=0.15, **kw):
        return InboxApp(FromSource(self.src), self.store,
                        actions or self.actions(),
                        settings=dict(DEFAULTS), undo_s=undo_s, leave_s=0,
                        stream_delay=0, view=view,
                        today=lambda: self.today, **kw)

    def header(self, app):
        return app.query_one("#hdr").render_header(app, app.pal, 150).plain


class ViewTest(Base):
    async def test_sections_order_and_the_tab_count(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            titles = [g.title for g in app.view_groups()]
            self.assertEqual(titles, ["OVERDUE", "DUE TODAY", "THIS WEEK",
                                      "LATER", "DECIDE", "NO DATE"])
            self.assertEqual(keys_of(app), ["task:1", "task:2", "task:3",
                                            "task:6", "task:4", "task:5"])
            self.assertIn("3 Tasks 2", self.header(app))    # overdue + today
            self.assertIn("0.12.0", self.header(app))
            self.assertEqual(app.query_one("#left").border_title, "Tasks")

    async def test_row_text_and_the_detail_card(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            q = app.query_one("#queue", Queue)
            first = q.get_option_at_index(q.get_option_index(
                ITEM_PREFIX + "task:1")).prompt
            plain = first.plain
            self.assertIn("☐", plain)
            self.assertIn("Task 1", plain)
            self.assertIn("2d late", plain)
            self.assertIn("8d", plain)                  # created 2030-01-02
            self.assertIn("Ann, Bo", plain)
            decision = q.get_option_at_index(q.get_option_index(
                ITEM_PREFIX + "task:4")).prompt.plain
            self.assertIn("◇", decision)
            self.assertIn("-", decision)
            await go(pilot, app, "task:4")
            self.assertEqual(app.query_one("#thread").border_title, "Detail")
            self.assertFalse(app.query_one("#draft-pane").display)
            item = app.current
            text = task_detail(item, app.pal, 60, False, TODAY).plain
            for want in ("Task 4", "decision", "none", "Di Demo",
                         "Two options were discussed.", "Pick one.", "src-4"):
                self.assertIn(want, text)
            await go(pilot, app, "task:1")
            text = task_detail(app.current, app.pal, 60, False, TODAY).plain
            self.assertIn("2030-01-08 (2d late)", text)
            self.assertIn("2030-01-02 (8d ago)", text)

    async def test_the_inbox_has_a_due_today_section(self):
        app = self.app(view="inbox")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            groups = {g.key: keys_of_group for g, keys_of_group in (
                (g, [i.key for i in g.items]) for g in app._views["inbox"])}
            self.assertEqual(groups["due_today"], ["task:1", "task:2"])
            self.assertEqual(groups["urgent"], [])
            self.assertEqual(keys_of(app), ["task:1", "task:2"])
            self.assertIn("1 Inbox 2", self.header(app))
            await pilot.press("2")
            self.assertTrue(await wait_for(pilot, lambda: app.view == "waiting"))
            self.assertEqual(keys_of(app), [])
            await pilot.press("4")
            self.assertTrue(await wait_for(pilot, lambda: app.view == "feed"))
            self.assertEqual(app.feed_data.total, 0)

    async def test_the_date_rolling_over_midnight_moves_the_sections(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(section_of(app, "task:2"), "today")
            self.assertEqual(section_of(app, "task:3"), "week")
            self.today = TODAY + dt.timedelta(days=2)       # two nights pass
            app.refilter()                                  # the 30 s tick
            self.assertEqual(section_of(app, "task:2"), "overdue")
            self.assertEqual(section_of(app, "task:3"), "today")
            app.tick_header()
            self.assertIn("3 Tasks 3", self.header(app))

    async def test_a_view_with_no_tasks_says_so(self):
        self.src.set_items([])
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle_empty(pilot, app)
            self.assertIn("No open tasks",
                          str(app.query_one("#zero").render()))


async def settle_empty(pilot, app):
    await wait_for(pilot, lambda: app.polled_once)
    await wait_for(pilot, lambda: app.query_one("#zero").display)


class CloseTest(Base):
    async def test_d_on_a_todo_closes_it_at_once_with_an_empty_note(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertFalse(isinstance(app.screen, NoteScreen))
            self.assertTrue(await wait_for(pilot, self.src.requests))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"], req["args"]),
                             ("close", "task:1", {"note": ""}))
            self.assertEqual(req["meta"]["kind"], "task")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            self.assertNotIn("task:1", keys_of(app))
            self.assertTrue(self.store.is_hidden("task:1"))
            self.assertEqual([i["id"] for i in self.src.items()][:1],
                             ["task:2"])

    async def test_a_decision_needs_its_text(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "task:4")
            await pilot.press("d")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, NoteScreen)))
            self.assertEqual(app.screen.title_text, "Decision:")
            await pilot.press("enter")                       # empty: nothing
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, NoteScreen)))
            self.assertIn(DECISION_REQUIRED, toasts(app))
            self.assertEqual(self.src.requests(), [])
            self.assertIn("task:4", keys_of(app))
            await pilot.press("d")                           # cancelled
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await type_text(pilot, "ship it")
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, NoteScreen)))
            self.assertEqual(self.src.requests(), [])
            await pilot.press("d")                           # decided
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await type_text(pilot, "ship it")
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, self.src.requests))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"], req["args"]),
                             ("close", "task:4", {"note": "ship it"}))
            self.assertTrue(await wait_for(
                pilot, lambda: "task:4" not in keys_of(app)))

    async def test_D_asks_for_the_outcome_on_a_todo(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("D")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, NoteScreen)))
            self.assertEqual(app.screen.title_text, "Outcome:")
            await type_text(pilot, "sent the doc")
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, self.src.requests))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["args"]),
                             ("close", {"note": "sent the doc"}))

    async def test_D_cancelled_sends_nothing_and_an_empty_outcome_is_allowed(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("D")
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await pilot.press("escape")
            await wait_for(pilot, lambda: not isinstance(app.screen, NoteScreen))
            self.assertEqual(self.src.requests(), [])
            await pilot.press("D")
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, self.src.requests))
            self.assertEqual(self.src.requests()[0]["args"], {"note": ""})

    async def test_D_on_a_decision_asks_for_the_decision(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "task:4")
            await pilot.press("D")
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            self.assertEqual(app.screen.title_text, "Decision:")

    async def test_undo_inside_the_window_sends_nothing(self):
        app = self.app(undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(
                pilot, lambda: "task:1" not in keys_of(app)))
            await pilot.press("u")
            self.assertTrue(await wait_for(
                pilot, lambda: "task:1" in keys_of(app)))
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("task:1"))

    async def test_a_conflict_brings_the_row_back_with_the_task_toast(self):
        self.src.configure("conflict")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "conflict" for r in app.audit)))
            self.assertTrue(await wait_for(
                pilot, lambda: "task:1" in keys_of(app)))
            self.assertIn(TASK_CONFLICT_TOAST, toasts(app))
            self.assertFalse(self.store.is_hidden("task:1"))

    async def test_a_failure_keeps_the_mark_and_the_op_for_a_retry(self):
        self.src.configure("fail")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: app.act_pending == 1))
            self.assertTrue(self.store.is_hidden("task:1"))
            self.assertNotIn("task:1", keys_of(app))


class DropTest(Base):
    async def test_x_drops_with_a_reason(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, NoteScreen)))
            self.assertEqual(app.screen.title_text, "Reason:")
            await type_text(pilot, "not needed")
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, self.src.requests))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"], req["args"]),
                             ("drop", "task:1", {"note": "not needed"}))
            self.assertTrue(await wait_for(
                pilot, lambda: "task:1" not in keys_of(app)))

    async def test_x_with_no_text_drops_with_an_empty_note(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, self.src.requests))
            self.assertEqual(self.src.requests()[0]["args"], {"note": ""})

    async def test_esc_cancels_the_drop(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await type_text(pilot, "oops")
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, NoteScreen)))
            self.assertIn("Cancelled. Nothing sent.", toasts(app))
            self.assertEqual(self.src.requests(), [])
            self.assertIn("task:1", keys_of(app))
            self.assertFalse(self.store.is_hidden("task:1"))

    async def test_the_drop_can_be_undone_in_the_source_from_L(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            await wait_for(pilot, lambda: isinstance(app.screen, NoteScreen))
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            await pilot.press("L")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            await pilot.press("u")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmWrite)))
            await pilot.press("y")
            self.assertTrue(await wait_for(
                pilot, lambda: len(self.src.requests()) == 2))
            self.assertEqual(self.src.requests()[1]["action"], "reopen")
            # the inbox fetches the list again: the task is back
            self.assertTrue(await wait_for(
                pilot, lambda: "task:1" in keys_of(app)))
            self.assertFalse(self.store.is_hidden("task:1"))


class DueTest(Base):
    async def pick(self, pilot, app, key):
        await pilot.press("s")
        self.assertTrue(await wait_for(
            pilot, lambda: isinstance(app.screen, ChoicePicker)))
        await pilot.press(key)

    async def sends(self, key, want):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, key)
            self.assertTrue(await wait_for(pilot, self.src.requests))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"], req["args"]),
                             ("due", "task:1", {"date": want}))
            self.assertEqual(req["meta"]["due"], day(-2))

    async def test_tomorrow(self):
        await self.sends("1", "2030-01-11")

    async def test_next_sunday(self):
        await self.sends("2", "2030-01-13")

    async def test_plus_one_week(self):
        await self.sends("3", "2030-01-17")

    async def test_no_date_sends_null(self):
        await self.sends("5", None)

    async def test_the_row_moves_section_and_stays_until_a_refresh(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(section_of(app, "task:1"), "overdue")
            await self.pick(pilot, app, "1")                 # tomorrow
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            self.assertEqual(section_of(app, "task:1"), "week")
            self.assertIn("3 Tasks 1", self.header(app))     # no longer overdue
            self.assertEqual(app.current_key, "task:1")
            # the source list has the new date; a refresh keeps it there
            self.assertEqual(self.src.items()[0]["meta"]["due"], day(1))
            app.poll(manual=True)
            self.assertTrue(await wait_for(pilot, lambda: not app.polling))
            self.assertEqual(section_of(app, "task:1"), "week")
            self.assertEqual(app._due_marks, {})

    async def test_a_refresh_before_the_act_settled_does_not_undo_the_move(self):
        self.src.configure("ok", sleep=0.6)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "1")
            self.assertTrue(await wait_for(pilot, lambda: app.running))
            self.assertEqual(section_of(app, "task:1"), "week")
            app.poll(manual=True)                       # the old list arrives
            self.assertTrue(await wait_for(pilot, lambda: not app.polling))
            self.assertEqual(section_of(app, "task:1"), "week")
            self.assertTrue(await wait_for(pilot, lambda: not app.running))

    async def test_pick_date_checks_the_date(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "4")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, DateScreen)))
            await type_text(pilot, "2030-02-30")             # not a real day
            await pilot.press("enter")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, DateScreen)
            self.assertEqual(self.src.requests(), [])
            hint = str(app.screen.query_one("#note-hint").render())
            self.assertIn("Not a date", hint)
            for _ in range(10):
                await pilot.press("backspace")
            await type_text(pilot, "2030-03-05")
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, self.src.requests))
            self.assertEqual(self.src.requests()[0]["args"],
                             {"date": "2030-03-05"})
            self.assertTrue(await wait_for(
                pilot, lambda: section_of(app, "task:1") == "later"))

    async def test_pick_date_can_be_cancelled(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "4")
            await wait_for(pilot, lambda: isinstance(app.screen, DateScreen))
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, DateScreen)))
            self.assertEqual(self.src.requests(), [])
            self.assertEqual(section_of(app, "task:1"), "overdue")

    async def test_no_date_moves_the_task_to_no_date_and_back_off_the_inbox(self):
        app = self.app(view="inbox")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("task:1", keys_of(app))
            await self.pick(pilot, app, "5")
            self.assertTrue(await wait_for(
                pilot, lambda: "task:1" not in keys_of(app)))
            self.assertEqual(section_of(app, "task:1"), "nodate")

    async def test_undo_inside_the_window_takes_the_move_back(self):
        app = self.app(undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "1")
            self.assertTrue(await wait_for(
                pilot, lambda: section_of(app, "task:1") == "week"))
            await pilot.press("u")
            self.assertTrue(await wait_for(
                pilot, lambda: section_of(app, "task:1") == "overdue"))
            self.assertEqual(self.src.requests(), [])
            self.assertEqual(app._due_marks, {})

    async def test_a_conflict_takes_the_shown_date_back(self):
        self.src.configure("conflict")
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "1")
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "conflict" for r in app.audit)))
            self.assertTrue(await wait_for(
                pilot, lambda: section_of(app, "task:1") == "overdue"))
            self.assertIn(TASK_CONFLICT_TOAST, toasts(app))

    async def test_the_inverse_from_L_restores_the_previous_date(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "3")                 # +1 week
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            await pilot.press("L")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            (row,) = app.screen.rows
            self.assertEqual((row["action"], row["prev_due"]),
                             ("due", day(-2)))
            await pilot.press("u")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmWrite)))
            await pilot.press("y")
            self.assertTrue(await wait_for(
                pilot, lambda: len(self.src.requests()) == 2))
            back = self.src.requests()[1]
            self.assertEqual((back["action"], back["args"]),
                             ("due", {"date": day(-2)}))
            self.assertTrue(await wait_for(
                pilot, lambda: section_of(app, "task:1") == "overdue"
                and self.src.items()[0]["meta"]["due"] == day(-2)))

    async def test_two_moves_in_a_row_log_the_date_shown_as_previous(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await self.pick(pilot, app, "1")                 # to Jan 11
            self.assertTrue(await wait_for(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            await self.pick(pilot, app, "3")                 # to Jan 17
            self.assertTrue(await wait_for(
                pilot, lambda: len(self.src.requests()) == 2))
            self.assertEqual(self.src.requests()[1]["meta"]["due"], day(1))


class ReadOnlyTest(Base):
    async def test_a_source_with_no_acts_only_says_so(self):
        app = self.app(actions=self.actions(acts={}))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("d", "D", "s", "x"):
                await pilot.press(key)
                self.assertTrue(await wait_for(
                    pilot, lambda: ACTS_OFF in toasts(app)))
                self.assertNotIsInstance(app.screen, (NoteScreen,
                                                      ChoicePicker))
            self.assertEqual(self.src.requests(), [])
            self.assertIn("task:1", keys_of(app))
            self.assertFalse(self.store.is_hidden("task:1"))
            keys = {k for k, _label in app.hints()}
            self.assertFalse(keys & {"d", "D", "s", "x"})
            self.assertEqual(app.act_pending, 0)

    async def test_a_source_listing_only_close_gates_the_other_keys(self):
        app = self.app(actions=self.actions(acts=support.acts_cfg(
            self.src, name="tasks", acts=["close"])))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("s")
            self.assertTrue(await wait_for(pilot, lambda: any(
                "does not list the due act" in t for t in toasts(app))))
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, self.src.requests))


class KeysTest(Base):
    async def test_o_opens_the_link_or_says_there_is_none(self):
        opened = []
        app = self.app(actions=self.actions(opener=lambda url: opened.append(url)
                                            or True))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("o")                           # task:1: no link
            self.assertTrue(await wait_for(pilot,
                                           lambda: "no link" in toasts(app)))
            self.assertEqual(opened, [])
            await go(pilot, app, "task:2")
            await pilot.press("o")
            self.assertTrue(await wait_for(pilot, lambda: opened))
            self.assertEqual(opened, ["https://example.com/t2"])

    async def test_the_reply_keys_do_not_act_on_a_task(self):
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("a", "n", "e", "w", "exclamation_mark"):
                await pilot.press(key)
            self.assertTrue(await wait_for(
                pilot, lambda: TASK_KEYS_TOAST in toasts(app)))
            self.assertEqual(self.src.requests(), [])
            self.assertNotIsInstance(app.screen, (NoteScreen, ChoicePicker))

    async def test_a_second_act_on_a_row_waits_for_the_first(self):
        self.src.configure("ok", sleep=0.6)
        app = self.app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("s")
            await wait_for(pilot, lambda: isinstance(app.screen, ChoicePicker))
            await pilot.press("1")
            self.assertTrue(await wait_for(pilot, lambda: app.pending
                                           or app.running))
            await pilot.press("s")
            self.assertTrue(await wait_for(pilot, lambda: any(
                "already in progress" in t for t in toasts(app))))
            self.assertNotIsInstance(app.screen, ChoicePicker)
            self.assertTrue(await wait_for(pilot, lambda: not app.running
                                           and not app.pending))
            self.assertEqual(len(self.src.requests()), 1)

    async def test_quitting_inside_the_window_cancels_a_due_move(self):
        app = self.app(undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("s")
            await wait_for(pilot, lambda: isinstance(app.screen, ChoicePicker))
            await pilot.press("1")
            self.assertTrue(await wait_for(pilot, lambda: app.pending))
            with mock.patch.object(app, "exit"):
                await app.action_quit()
            self.assertEqual(self.src.requests(), [])
            self.assertEqual(app.actions.pending_count(), 0)

    async def test_quitting_inside_the_window_keeps_a_close_for_a_retry(self):
        app = self.app(undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: app.pending))
            with mock.patch.object(app, "exit"):
                await app.action_quit()
            self.assertEqual(self.src.requests(), [])
            self.assertTrue(self.store.is_hidden("task:1"))
            self.assertEqual(app.actions.pending_count(), 1)


if __name__ == "__main__":
    unittest.main()
