"""Inbox 0.13.6 tests: an away row. `meta.away_until` / `meta.away_note` give
the row a badge, the detail pane a line, and hold the nudge (a toast, a greyed
menu entry). A past date is ignored. The pure part (the text rules, the badge
width math) needs no Textual; the pilot classes skip without it. Synthetic data
only: generic names, no real people or companies."""
import datetime as dt
import importlib.util
import time
import unittest

from workspace_daemon.inbox import items as itm, models, sources

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from rich.cells import cell_len
    from workspace_daemon.inbox import rowmenu, widgets
    from workspace_daemon.inbox.actions import FakeActions
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import ConfirmSend, RowMenu
    from workspace_daemon.inbox.store import InboxStore

TODAY = dt.date.today()
FUTURE = TODAY + dt.timedelta(days=9)
PAST = TODAY - dt.timedelta(days=1)
SIZE = (130, 44)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def until_text(day):
    return f"away until {day:%a} {day.day} {day:%b}"


def waiting(i=1, **meta):
    return itm.make_item(
        id=f"w:{i}", source="slack", section="waiting", urgency="digest",
        sender=f"Person {i}", where="DM", gist=f"Check the rollout plan {i}",
        ts=time.time() - 3600 * i, link=f"https://example.com/{i}", thread=[],
        draft="Any update?", actions=["nudge", "done", "snooze", "open"],
        meta=meta)


def ask(i=1, **meta):
    return itm.make_item(
        id=f"a:{i}", source="slack", section="needs_reply", urgency="digest",
        sender=f"Person {i}", where="DM", gist=f"Question {i}",
        ts=time.time() - 60 * i, link=None, thread=[], draft=None,
        actions=["reply", "done"], meta=meta)


class AwayTextTest(unittest.TestCase):
    def test_the_note_wins(self):
        self.assertEqual(models.away_text(
            {"away_note": "away until Mon 30 Nov", "away_until": FUTURE.isoformat()}),
            "away until Mon 30 Nov")
        self.assertEqual(models.away_text({"away_note": "out sick"}), "out sick")

    def test_the_date_is_the_fallback(self):
        self.assertEqual(models.away_text({"away_until": FUTURE.isoformat()}),
                         until_text(FUTURE))
        self.assertEqual(models.away_text(
            {"away_until": "2030-11-30"}, today=dt.date(2030, 1, 1)),
            "away until Sat 30 Nov")

    def test_neither_is_plain_away(self):
        self.assertEqual(models.away_text({"away_until": None, "away_note": "away"}),
                         "away")
        self.assertEqual(models.away_text({"away_note": "  \n "}), "")
        self.assertEqual(models.away_text({"away_until": "someday"}), "away")

    def test_not_away(self):
        for meta in ({}, {"away_until": None}, {"away_note": ""}, {"away_until": ""},
                     {"away_until": None, "away_note": None}, None, "x"):
            with self.subTest(meta=meta):
                self.assertEqual(models.away_text(meta), "")

    def test_a_past_date_is_ignored_even_with_a_note(self):
        self.assertEqual(models.away_text({"away_until": PAST.isoformat()}), "")
        self.assertEqual(models.away_text(
            {"away_until": PAST.isoformat(), "away_note": "away until yesterday"}), "")

    def test_today_is_still_away(self):
        self.assertEqual(models.away_text({"away_until": TODAY.isoformat()}),
                         until_text(TODAY))

    def test_the_note_is_one_line_and_capped(self):
        text = models.away_text({"away_note": "out\x00 of\noffice\t" + "x" * 100})
        self.assertNotIn("\n", text)
        self.assertNotIn("\x00", text)
        self.assertLessEqual(len(text), models.AWAY_NOTE_MAX)
        self.assertTrue(text.startswith("out of office xxx"))

    def test_an_item_reads_its_own_meta(self):
        item = models.Item(key="k", section="waiting", who="P", where="DM",
                           ts=time.time(), gist="g",
                           meta={"away_until": FUTURE.isoformat()})
        self.assertEqual(item.away, until_text(FUTURE))
        self.assertEqual(models.Item(key="k", section="fyi", who="P", where="DM",
                                     ts=time.time(), gist="g").away, "")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class BadgeWidthTest(unittest.TestCase):
    def item(self, task=None, **meta):
        return models.Item(key="k", section="waiting", who="P", where="DM",
                           ts=time.time(), gist="g", meta=meta, task=task)

    class Card:
        name, role, state, completed = "scout-1", "researcher", "in progress", False

    def test_no_away_meta_no_badge(self):
        it = self.item()
        self.assertIsNone(widgets.away_badge(it))
        self.assertEqual(widgets.row_badges(it, 80), [])

    def test_the_away_badge_is_warn_and_alone(self):
        it = self.item(away_note="away until Mon 30 Nov")
        self.assertEqual(widgets.row_badges(it, 80),
                         [("away until Mon 30 Nov", "warn")])

    def test_both_badges_when_they_fit(self):
        it = self.item(task=(None, self.Card()), away_note="away until Mon 30 Nov")
        badges = widgets.row_badges(it, 100)
        self.assertEqual([b[0] for b in badges],
                         ["▶ scout-1 · researcher · in progress",
                          "away until Mon 30 Nov"])
        self.assertEqual(badges[1][1], "warn")

    def test_when_both_do_not_fit_the_away_badge_shrinks(self):
        it = self.item(task=(None, self.Card()), away_note="away until Mon 30 Nov")
        badges = widgets.row_badges(it, 70)
        self.assertEqual([b[0] for b in badges],
                         ["▶ scout-1 · researcher · in progress", "away"])

    def test_a_lone_badge_that_does_not_fit_shrinks_too(self):
        it = self.item(away_note="out of the office until the new year starts")
        self.assertEqual(widgets.row_badges(it, 40), [("away", "warn")])

    def test_the_badges_never_overflow_and_the_gist_keeps_its_minimum(self):
        it = self.item(task=(None, self.Card()), away_note="away until Mon 30 Nov")
        for width in range(20, 140):
            with self.subTest(width=width):
                badges = widgets.row_badges(it, width)
                used = sum(cell_len(b[0]) + 2 for b in badges)
                self.assertTrue(badges[0][0].startswith("▶"))   # the task badge stays
                self.assertLessEqual(used, max(width - 4, cell_len(badges[0][0]) + 2))
                if len(badges) == 2 and badges[1][0] != "away":
                    self.assertGreaterEqual(width - 4 - used, widgets.MIN_GIST)
                if len(badges) == 2:
                    self.assertIn(badges[1][0], ("away", "away until Mon 30 Nov"))

    def test_a_task_row_has_no_away_badge(self):
        it = models.Item(key="k", section="fyi", who="P", where="x",
                         ts=time.time(), gist="g", source="other",
                         meta={"kind": "task", "away_note": "away"})
        self.assertEqual(widgets.row_badges(it, 80), [])


async def settle(pilot, app):
    for _ in range(300):
        if app.polled_once and app.current_key is not None:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.3)


async def until(pilot, cond, tries=300, step=0.05):
    for _ in range(tries):
        try:
            ok = cond()
        except Exception:
            ok = False
        if ok:
            await pilot.pause()
            return True
        await pilot.pause(step)
    return cond()


def toasts(app):
    return [f"{n.title} {n.message}".strip() for n in app._notifications]


def row_text(app, key):
    opts = {o.id: str(o.prompt) for o in app.query_one("#queue").options
            if o.id}
    return opts.get("i:" + key, "")


class Fixed:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return [sources.SourceResult("src", self.rows, False, None, None)]


def make_app(rows, view="waiting"):
    store = InboxStore(None)
    actions = FakeActions(store=store, draft_delay=0)
    return InboxApp(Fixed(rows), store, actions, settings=dict(DEFAULTS),
                    undo_s=30.0, leave_s=0, stream_delay=0, view=view)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class RowAndDetailTest(unittest.IsolatedAsyncioTestCase):
    async def test_note_date_and_bare_badges(self):
        rows = [waiting(1, away_note="away until Mon 30 Nov",
                        away_until=FUTURE.isoformat()),
                waiting(2, away_until=FUTURE.isoformat()),
                waiting(3, away_until=None, away_note="away, return unknown"),
                waiting(4, away_until=None, away_note="away"),
                waiting(5)]
        app = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("away until Mon 30 Nov", row_text(app, "w:1"))
            self.assertIn(until_text(FUTURE), row_text(app, "w:2"))
            self.assertIn("away, return unknown", row_text(app, "w:3"))
            self.assertTrue(row_text(app, "w:4").rstrip().endswith("away"))
            self.assertNotIn("away", row_text(app, "w:5"))

    async def test_the_gist_is_cut_not_the_badge(self):
        long = "Check the rollout plan " * 8
        row = itm.make_item(
            id="w:9", source="slack", section="waiting", urgency="digest",
            sender="Person 9", where="DM", gist=long, ts=time.time() - 60,
            link=None, thread=[], draft="x", actions=["nudge"],
            meta={"away_note": "away until Mon 30 Nov"})
        app = make_app([row])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            text = row_text(app, "w:9")
            self.assertIn("away until Mon 30 Nov", text)
            self.assertIn("…", text)
            width = app.query_one("#queue").size.width
            for line in text.split("\n"):
                self.assertLessEqual(cell_len(line.rstrip()), width, line)

    def agent(self, **meta):
        return itm.make_item(
            id="ag:1", source="other", section="fyi", urgency="digest",
            sender="scout-1", where="agents", gist="reading", ts=time.time(),
            link=None, thread=[], draft=None, actions=[],
            meta=dict({"kind": "agent", "source_key": "w:1", "name": "s1",
                       "role": "dev", "state": "running",
                       "bucket": "working"}, **meta))

    async def test_the_task_badge_and_the_away_badge_coexist(self):
        app = make_app([waiting(1, away_note="away until Mon 30 Nov"),
                        self.agent()])
        async with app.run_test(size=(180, 40)) as pilot:
            await settle(pilot, app)
            text = row_text(app, "w:1")
            self.assertIn("▶ s1 · dev · running", text)
            self.assertIn("away until Mon 30 Nov", text)
            width = app.query_one("#queue").size.width
            for line in text.split("\n"):
                self.assertLessEqual(cell_len(line.rstrip()), width, line)

    async def test_when_both_do_not_fit_the_task_badge_stays(self):
        app = make_app([waiting(1, away_note="away until Mon 30 Nov"),
                        self.agent(name="scout-1", role="researcher",
                                   state="in_progress")])
        async with app.run_test(size=(150, 40)) as pilot:
            await settle(pilot, app)
            text = row_text(app, "w:1")
            self.assertIn("▶ scout-1 · researcher · in progress", text)
            self.assertNotIn("away until", text)
            self.assertTrue(text.rstrip().endswith("away"), text)
            width = app.query_one("#queue").size.width
            for line in text.split("\n"):
                self.assertLessEqual(cell_len(line.rstrip()), width, line)

    async def test_the_detail_pane_shows_the_away_line(self):
        app = make_app([waiting(1, away_note="away until Mon 30 Nov")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = str(app.query_one("#thread-body").render())
            self.assertIn("! away until Mon 30 Nov · nudge held", body)

    async def test_a_reply_row_shows_the_line_without_the_hold_word(self):
        app = make_app([ask(1, away_note="out sick")], view="inbox")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = str(app.query_one("#thread-body").render())
            self.assertIn("! out sick", body)
            self.assertNotIn("nudge held", body)

    async def test_a_past_date_shows_nothing(self):
        app = make_app([waiting(1, away_until=PAST.isoformat(),
                                away_note="away until yesterday")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn("away", row_text(app, "w:1"))
            self.assertNotIn("away", str(app.query_one("#thread-body").render()))

    async def test_an_item_without_away_meta_is_unchanged(self):
        app = make_app([waiting(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn("away", row_text(app, "w:1"))
            self.assertIn(("a", "nudge"), app.hints())


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class NudgeHeldTest(unittest.IsolatedAsyncioTestCase):
    async def test_n_is_blocked_with_a_toast(self):
        app = make_app([waiting(1, away_note="away until Mon 30 Nov")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("n")
            self.assertTrue(await until(pilot, lambda: any(
                "Nudge held: away until Mon 30 Nov" in t for t in toasts(app))),
                toasts(app))
            await pilot.pause(0.3)
            self.assertFalse(isinstance(app.screen, ConfirmSend))
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(list(app.pending), [])

    async def test_a_blocks_it_too_and_the_footer_says_so(self):
        app = make_app([waiting(1, away_until=FUTURE.isoformat())])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("a", "nudge held"), app.hints())
            await pilot.press("a")
            self.assertTrue(await until(pilot, lambda: any(
                f"Nudge held: {until_text(FUTURE)}" in t for t in toasts(app))))
            self.assertEqual(len(app.screen_stack), 1)

    async def test_the_menu_lists_nudge_greyed_and_running_it_shows_the_toast(self):
        app = make_app([waiting(1, away_note="away until Mon 30 Nov")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entry = next(e for e in app.row_menu_entries() if e.key == "a")
            self.assertEqual(entry.label, "Nudge…")
            self.assertEqual(entry.disabled_reason,
                             "Nudge held: away until Mon 30 Nov")
            await pilot.press("enter")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, RowMenu)))
            shown = " ".join(str(o.prompt) for o in
                             app.screen.query_one("#rowmenu-list").options)
            self.assertIn("Nudge held: away until Mon 30 Nov", shown)
            await pilot.press("n")
            self.assertTrue(await until(
                pilot, lambda: not isinstance(app.screen, RowMenu)))
            self.assertTrue(await until(pilot, lambda: any(
                "Nudge held: away until Mon 30 Nov" in t for t in toasts(app))))
            self.assertFalse(isinstance(app.screen, ConfirmSend))

    async def test_other_actions_are_unchanged(self):
        app = make_app([waiting(1, away_note="away")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entries = {e.key: e for e in app.row_menu_entries()}
            for key in ("d", "s", "o"):
                self.assertIn(key, entries)
                self.assertEqual(entries[key].disabled_reason, "")
            self.assertEqual(entries["a"].disabled_reason, "Nudge held: away")

    async def test_a_past_date_allows_the_nudge(self):
        app = make_app([waiting(1, away_until=PAST.isoformat(),
                                away_note="away until yesterday")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entry = next(e for e in app.row_menu_entries() if e.key == "a")
            self.assertEqual(entry.disabled_reason, "")
            await pilot.press("n")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ConfirmSend)))
            self.assertFalse(any("Nudge held" in t for t in toasts(app)))

    async def test_an_item_without_away_meta_nudges_as_before(self):
        app = make_app([waiting(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entry = next(e for e in app.row_menu_entries() if e.key == "a")
            self.assertEqual((entry.label, entry.detail, entry.disabled_reason),
                             ("Nudge…", "confirm first", ""))
            await pilot.press("n")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ConfirmSend)))
            self.assertEqual(app.screen.text, "Any update?")


if __name__ == "__main__":
    unittest.main()
