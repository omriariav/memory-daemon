"""Inbox 0.12.3: "Remind me" (`m`) on a task fills the Detail pane with where
the task came from, its date, the people, the original words, the last
activity and the related items of the context command. Read-only; a missing
part says `not recorded`. Synthetic data only (invented names, ids, text). The
helper classes need no Textual; the pane tests need it."""
import datetime as dt
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_acts_support as support  # noqa: E402

from workspace_daemon.inbox import (context, items as itm, models,  # noqa: E402
                                    sources, tasks)
from workspace_daemon.inbox.actions import FakeActions  # noqa: E402
from workspace_daemon.inbox.store import InboxStore  # noqa: E402

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import REMIND_TASK_ONLY, TASK_KEYS_TOAST, InboxApp
    from workspace_daemon.inbox.widgets import ITEM_PREFIX, Queue

SIZE = (150, 46)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
TODAY = dt.date(2030, 1, 10)
WORDS = ("WORDS-1 Dana asked for the Acme pricing sheet\nWORDS-2 by Friday\n"
         "WORDS-3\nWORDS-4\nWORDS-5\nWORDS-6\nWORDS-7-CUT")


def full_task(n=1, **meta):
    base = dict(due="2030-01-08", date="2030-01-02",
                people=["Dana Example", "Ravi Example"], evidence=WORDS,
                source_ids=["slack:C0EXAMPLE:100.5", "2030-01-02-acme-pricing",
                            "zoom:mtg-77"],
                last_activity="Dana replied in the thread on 2030-01-05")
    base.update(meta)
    raw = support.task_item(n, name=f"Send Acme pricing {n}", **base)
    raw["meta"]["source_ids"] = base["source_ids"]
    return itm.parse_item(raw)


def bare_task(n=2):
    raw = support.task_item(n, name=f"Bare task {n}")
    raw["meta"].pop("date")
    raw["meta"]["source_ids"] = []
    raw["meta"]["people"] = []
    raw["meta"]["evidence"] = None
    return itm.parse_item(raw)


class HelperTest(unittest.TestCase):
    def test_a_source_id_says_what_it_points_at(self):
        for sid, want in (("slack:C0EXAMPLE:1.5", "Slack thread"),
                          ("slack_mcp:C0EXAMPLE:1.5", "Slack thread"),
                          ("gcal:ev1", "calendar event"),
                          ("gmail:m1", "email"),
                          ("zoom:77", "meeting recording"),
                          ("mila:77", "meeting recording"),
                          ("gdrive:abc", "doc"),
                          ("fibery:xyz", "Fibery"),
                          ("2030-01-02-acme-pricing", "memory entry"),
                          ("Weird Id!", "source")):
            self.assertEqual(tasks.source_kind(sid), want, sid)

    def test_last_activity_prefers_the_line_then_updated_then_last_seen(self):
        def view(**meta):
            return models.from_v1(full_task(**meta))
        self.assertEqual(tasks.last_activity_of(view()),
                         "Dana replied in the thread on 2030-01-05")
        self.assertEqual(tasks.last_activity_of(
            view(last_activity=None, updated="2030-01-06")), "2030-01-06")
        epoch = dt.datetime(2030, 1, 7, 12).timestamp()
        self.assertEqual(tasks.last_activity_of(
            view(last_activity="  ", updated=None, last_seen_ts=epoch)),
            "2030-01-07")
        self.assertEqual(tasks.last_activity_of(
            view(last_activity=None, updated="soon")), "")
        self.assertEqual(tasks.last_activity_of(models.from_v1(
            bare_task())), "")

    def test_the_task_date_is_recorded_only_with_meta_date(self):
        self.assertTrue(tasks.has_created(models.from_v1(full_task())))
        self.assertFalse(tasks.has_created(models.from_v1(bare_task())))


class Fixed:
    def __init__(self, rows):
        self.results = [sources.SourceResult("tasks", rows, False, None, None)]

    def __call__(self):
        return self.results


class Related(FakeActions):
    """The context command, gated so a test can see the loading line."""

    def __init__(self, *a, outcome=None, configured=True, **kw):
        super().__init__(*a, **kw)
        self.outcome = outcome
        self.gate = threading.Event()
        self.context_configured = configured

    def fetch_context(self, item, cancel=None):
        self.calls.append(("context", item.key))
        self.gate.wait(10)
        return self.outcome or context.Outcome(True, [
            {"title": "Related captures", "lines": [
                "RELATED-1 pricing call notes", "RELATED-2 Acme thread"]}],
            None)


async def wait_for(pilot, cond, tries=200, step=0.03):
    for _ in range(tries):
        try:
            ok = cond()
        except Exception:
            ok = False
        if ok:
            await pilot.pause()
            return True
        await pilot.pause(step)
    return bool(cond())


async def settle(pilot, app):
    await wait_for(pilot, lambda: app.polled_once and app.current_key
                   is not None)


def make_app(rows, actions=None, **kw):
    store = InboxStore(None)
    actions = actions or Related(store=store, draft_delay=0)
    return InboxApp(Fixed(rows), store, actions, settings=dict(DEFAULTS),
                    undo_s=0.15, leave_s=0, stream_delay=0, view="tasks",
                    today=lambda: TODAY, **kw), actions


def detail(app):
    return str(app.query_one("#thread-body").render())


def toasts(app):
    return [n.message for n in app._notifications]


async def go(pilot, app, key):
    q = app.query_one("#queue", Queue)
    q.highlighted = q.get_option_index(ITEM_PREFIX + key)
    assert await wait_for(pilot, lambda: app.current_key == key), key


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class RemindPaneTest(unittest.IsolatedAsyncioTestCase):
    async def test_m_fills_the_pane_and_the_related_items_arrive_later(self):
        app, actions = make_app([full_task(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn("REMIND ME", detail(app))
            await pilot.press("m")
            self.assertTrue(await wait_for(
                pilot, lambda: "Loading related items" in detail(app)))
            text = detail(app)
            for want in ("Send Acme pricing 1", "REMIND ME",
                         "Slack thread · slack:C0EXAMPLE:100.5",
                         "memory entry · 2030-01-02-acme-pricing",
                         "meeting recording · zoom:mtg-77",
                         "2030-01-02 (8d ago)", "Dana Example, Ravi Example",
                         "Dana replied in the thread on 2030-01-05",
                         "WORDS-1 Dana asked", "WORDS-6"):
                self.assertIn(want, text)
            self.assertNotIn("WORDS-7-CUT", text)      # quoted, trimmed
            self.assertIn("…", text)
            self.assertNotIn("not recorded", text)
            actions.gate.set()                         # the worker answers
            self.assertTrue(await wait_for(
                pilot, lambda: "RELATED-2 Acme thread" in detail(app)))
            text = detail(app)
            self.assertIn("Related captures", text)
            self.assertNotIn("Loading related items", text)
            # read-only: only the context lookup ran, nothing was marked
            self.assertEqual(actions.calls, [("context", "task:1")])
            self.assertEqual(app.store.is_hidden("task:1"), False)
            actions.gate.set()

    async def test_every_missing_part_says_not_recorded(self):
        app, actions = make_app([bare_task(2)])
        actions.gate.set()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("m")
            self.assertTrue(await wait_for(
                pilot, lambda: "RELATED-1" in detail(app)))
            text = detail(app)
            for want in ("source not recorded", "date not recorded",
                         "people not recorded", "last activity not recorded",
                         "original words not recorded"):
                self.assertIn(want, text)

    async def test_without_a_context_command_the_card_says_so(self):
        store = InboxStore(None)
        app, actions = make_app([full_task(1)], Related(
            store=store, draft_delay=0, configured=False))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("m")
            self.assertTrue(await wait_for(
                pilot, lambda: "Related items unavailable" in detail(app)))
            self.assertIn("context is not configured", detail(app))
            self.assertIn("WORDS-1", detail(app))      # the rest still shows
            self.assertEqual(actions.calls, [])

    async def test_a_failed_lookup_and_an_empty_answer_are_told(self):
        for outcome, want in (
                (context.Outcome(False, [], "timed out after 20s"),
                 "Related items unavailable: timed out after 20s"),
                (context.Outcome(True, [{"title": "Related", "lines": []}],
                                 None), "No related items found")):
            store = InboxStore(None)
            actions = Related(store=store, draft_delay=0, outcome=outcome)
            actions.gate.set()
            app, _ = make_app([full_task(1)], actions)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await pilot.press("m")
                self.assertTrue(await wait_for(
                    pilot, lambda: want in detail(app)), detail(app))

    async def test_m_esc_and_moving_close_the_card(self):
        app, actions = make_app([full_task(1), full_task(2)])
        actions.gate.set()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for closer in ("m", "escape"):
                await pilot.press("m")
                self.assertTrue(await wait_for(
                    pilot, lambda: "REMIND ME" in detail(app)))
                self.assertEqual({e.key: e.label for e in app.row_menu_entries()}["m"],
                                 "Close remind me")
                await pilot.press(closer)
                self.assertTrue(await wait_for(
                    pilot, lambda: "REMIND ME" not in detail(app)))
                self.assertEqual({e.key: e.label for e in app.row_menu_entries()}["m"],
                                 "Remind me")
                self.assertIn("Evidence", detail(app))     # the usual card
            await pilot.press("m")
            self.assertTrue(await wait_for(
                pilot, lambda: "REMIND ME" in detail(app)))
            other = "task:2" if app.current_key == "task:1" else "task:1"
            await go(pilot, app, other)
            self.assertTrue(await wait_for(
                pilot, lambda: "REMIND ME" not in detail(app)))

    async def test_one_esc_from_the_card_closes_it_and_returns_to_the_queue(self):
        app, actions = make_app([full_task(1)])
        actions.gate.set()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("m")
            self.assertTrue(await wait_for(
                pilot, lambda: "REMIND ME" in detail(app)))
            await pilot.press("tab")                  # focus the card
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused is not None
                and app.focused.id in ("thread", "draft-pane")))
            self.assertIn(("esc", "close"), app.hints())
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: "REMIND ME" not in detail(app)))
            self.assertEqual(app.focused.id, "queue")

    async def test_one_lookup_is_shared_and_cached(self):
        app, actions = make_app([full_task(1)])
        actions.gate.set()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for _ in range(3):                  # open, close, open again
                await pilot.press("m")
                await wait_for(pilot, lambda: "RELATED-1" in detail(app)
                               or "REMIND ME" not in detail(app))
            self.assertEqual(actions.calls, [("context", "task:1")])

    async def test_m_on_other_rows_still_saves_and_the_palette_lists_it(self):
        store = InboxStore(None)
        reply = itm.make_item(
            id="r:1", source="slack", section="needs_reply", urgency="digest",
            sender="Dana Example", where="#acme", gist="Please review",
            ts=time.time() - 600, link=None, thread=[], draft=None,
            actions=["reply"], meta={})
        actions = Related(store=store, draft_delay=0)
        app = InboxApp(Fixed([reply]), store, actions, settings=dict(DEFAULTS),
                       undo_s=0.15, leave_s=0, stream_delay=0, view="inbox")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.action_remind()                       # the palette entry
            self.assertTrue(await wait_for(
                pilot, lambda: REMIND_TASK_ONLY in toasts(app)))
            self.assertNotIn("REMIND ME", detail(app))
            self.assertIn("Remind me", [c.title for c in
                                        app.get_system_commands(app.screen)])
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            self.assertIn("remind me", str(
                app.screen.query_one("#help-body").render()))

    async def test_the_task_keys_toast_names_m(self):
        self.assertIn("m remind me", TASK_KEYS_TOAST)


if __name__ == "__main__":
    unittest.main()
