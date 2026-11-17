"""Pilot tests for the views (1-5), the groups, the Feed rules and Health."""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from workspace_daemon.inbox import __version__, items as itm, sources
    from workspace_daemon.inbox import feedrules, health as healthmod
    from workspace_daemon.inbox.actions import FakeActions
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import (ChoicePicker, HelpScreen,
                                                RulesScreen)
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import Queue

SIZE = (130, 40)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def item(i, section="needs_reply", source="slack", actions=("reply",),
         ago_min=None, **kw):
    ago = (ago_min if ago_min is not None else i) * 60
    base = dict(
        id=f"t:{i}", source=source, section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender=f"Person {i}", where="room", gist=f"gist {i}",
        ts=time.time() - ago, link=f"https://example.com/{i}", thread=[],
        draft=None, actions=list(actions), meta={})
    base.update(kw)
    return itm.make_item(**base)


def feed_item(i, routine="cal", kind="update", ago_min=None, **meta):
    return item(i, "fyi", "calendar", ("ack",), ago_min=ago_min,
                meta=dict({"routine": routine, "kind": kind,
                           "title": f"Title {i}"}, **meta))


class Fixed:
    def __init__(self, rows, delay=0.0):
        self.rows, self.delay = rows, delay

    def __call__(self):
        if self.delay:
            time.sleep(self.delay)
        return [sources.SourceResult("src", self.rows, False, None, None)]


async def settle(pilot, app):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)


async def until(pilot, cond, tries=100, step=0.05):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(step)
    return cond()


def make_app(rows, store=None, delay=0.0, undo_s=0.15, **kw):
    store = store or InboxStore(None)
    kw.setdefault("settings", dict(DEFAULTS))
    return InboxApp(Fixed(rows, delay), store,
                    FakeActions(store=store, draft_delay=0), undo_s=undo_s,
                    leave_s=0, stream_delay=0, **kw), store


def mixed_rows():
    return [
        item(1, "urgent", "alert", ("ack",)),
        item(2, "needs_reply"), item(3, "needs_reply"),
        item(4, "waiting", actions=("nudge",), ago_min=60 * 30,
             draft="a nudge"),
        item(5, "waiting", actions=("nudge",), ago_min=60 * 5),
        feed_item(6, "cal"), feed_item(7, "cal"), feed_item(8, "sheet-watch"),
    ]


def snapshot(**kw):
    now = time.time()
    base = {"at": now, "error": None,
            "scheduler": {"armed": True, "last_tick": now - 40,
                          "paused": None},
            "routines": [], "groups": [], "alerts": [], "connectors": [],
            "spend": {"total": 6.0, "cap": 20.0}, "heartbeats": []}
    base.update(kw)
    return base


def routine_snapshot(alerts=()):
    rows = [{"routine": "slack-mcp", "status": "waiting", "every": "5m",
             "last_attempt": "8m ago", "last_capture": "14m ago",
             "issues": "-"},
            {"routine": "zoom-meetings", "status": "attention",
             "every": "15m", "last_attempt": "20m ago", "last_capture": "-",
             "issues": "1 last-run error"}]
    ok = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 180))
    routines, groups = healthmod.build_routines(
        rows, {"slack-mcp": {"state": "ok", "at": ok},
               "zoom-meetings": {"state": "error", "at": ok}}, list(alerts),
        lambda v: time.time() - 180)
    return snapshot(routines=routines, groups=groups, alerts=list(alerts))


def alert_item(i, key, routine, **meta):
    return item(i, "urgent", "alert", ("ack",), meta=dict(
        {"key": key, "routine": routine, "kind": "alert"}, **meta))


class ReservedKeysTest(unittest.TestCase):
    def test_x_and_M_are_reserved_for_the_feed(self):
        from workspace_daemon.inbox import settings
        self.assertTrue({"x", "M"} <= settings.RESERVED_KEYS)
        for key in "xM":
            with self.assertRaises(settings.SettingsError) as ctx:
                settings.validate({"actions": [{
                    "key": key, "label": "x", "argv": ["x"]}]})
            self.assertIn("already used by the inbox", str(ctx.exception))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ViewSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_keys_1_to_5_switch_the_views(self):
        app, _ = make_app(mixed_rows(), health=lambda: snapshot())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            shown = lambda: {w: app.query_one(w).display for w in
                             ("#main", "#routines", "#tasks", "#health")}
            self.assertEqual(app.view, "inbox")
            self.assertEqual(shown(), {"#main": True, "#routines": False,
                                       "#tasks": False, "#health": False})
            expect = {"2": "waiting", "3": "tasks", "4": "feed",
                      "5": "health", "1": "inbox"}
            for key, name in expect.items():
                await pilot.press(key)
                await pilot.pause(0.15)
                self.assertEqual(app.view, name, key)
            await pilot.press("3")
            await pilot.pause(0.1)
            self.assertEqual(shown(), {"#main": False, "#routines": False,
                                       "#tasks": True, "#health": False})
            self.assertIn("0.11", str(app.query_one("#tasks").render()))
            await pilot.press("4")
            await pilot.pause(0.1)
            self.assertEqual(shown(), {"#main": True, "#routines": True,
                                       "#tasks": False, "#health": False})
            await pilot.press("5")
            await pilot.pause(0.1)
            self.assertEqual(shown(), {"#main": False, "#routines": False,
                                       "#tasks": False, "#health": True})

    async def test_each_view_lists_its_own_rows(self):
        app, _ = make_app(mixed_rows(), health=lambda: snapshot())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            rows = lambda: [r.key for r in app.query_one("#queue", Queue).rows]
            self.assertEqual(rows(), ["t:1", "t:2", "t:3", "t:4"])
            await pilot.press("2")
            await pilot.pause(0.15)
            self.assertEqual(sorted(rows()), ["t:4", "t:5"])
            await pilot.press("4")
            await pilot.pause(0.15)
            self.assertEqual(sorted(rows()), ["t:6", "t:7", "t:8"])

    async def test_the_tab_bar_counts_each_view(self):
        rows = mixed_rows() + [alert_item(9, "attention:zoom-meetings",
                                          "zoom-meetings")]
        snap = routine_snapshot([{"key": "attention:zoom-meetings",
                                  "routine": "zoom-meetings",
                                  "since": time.time() - 600}])
        app, _ = make_app(rows, health=lambda: snap)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await until(pilot, lambda: app.health_snap is not None)
            await pilot.pause(0.2)
            head = str(app.query_one("#hdr").render())
            self.assertIn(__version__, head)
            self.assertIn("[1 Inbox 5]", head)      # urgent x2, owe x2, nudge
            self.assertIn("2 Waiting 2", head)
            self.assertIn("3 Tasks", head)
            self.assertIn("4 Feed 3", head)
            self.assertIn("5 Health ⚠1", head)
            await pilot.press("4")
            await pilot.pause(0.15)
            self.assertIn("[4 Feed 3]", str(app.query_one("#hdr").render()))

    async def test_marks_and_undo_work_in_every_view(self):
        app, store = make_app(mixed_rows(), undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for view_key, row_key in (("2", "t:5"), ("4", "t:6"), ("1", "t:2")):
                await pilot.press(view_key)
                await pilot.pause(0.15)
                app.current_key = row_key
                app.redraw(keep=row_key)
                await pilot.pause(0.05)
                await pilot.press("d")
                await pilot.pause(0.05)
                self.assertNotIn(row_key, [i.key for i in app.items])
                await pilot.press("u")
                await pilot.pause(0.1)
                self.assertIn(row_key, [i.key for i in app.items])
                self.assertFalse(store.is_hidden(row_key))
            # a done mark in one view hides the very same row in the others
            app.undo_s = 0.1
            await pilot.press("1")
            await pilot.pause(0.1)
            app.current_key = "t:4"
            app.redraw(keep="t:4")
            await pilot.press("d")
            await pilot.pause(0.6)
            self.assertTrue(store.is_hidden("t:4"))
            await pilot.press("2")
            await pilot.pause(0.15)
            self.assertEqual([r.key for r in app.query_one("#queue", Queue).rows],
                             ["t:5"])

    async def test_snooze_and_pending_hold_in_the_waiting_view(self):
        app, store = make_app(mixed_rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("2")
            await pilot.pause(0.15)
            key = app.current_key
            await pilot.press("s", "1")
            await pilot.pause(0.5)
            self.assertIsNotNone(store.snoozed_until(key))
            self.assertNotIn(key, [i.key for i in app.items])


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class GroupJumpTest(unittest.IsolatedAsyncioTestCase):
    async def test_brackets_jump_between_sections(self):
        app, _ = make_app(mixed_rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.current_key, "t:1")            # URGENT
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.current_key, "t:2")            # I OWE
            await pilot.press("j")
            await pilot.pause(0.1)
            self.assertEqual(app.current_key, "t:3")
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.current_key, "t:4")            # NUDGE DUE
            await pilot.press("left_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.current_key, "t:2")            # first of I OWE
            await pilot.press("left_square_bracket", "left_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.current_key, "t:1")            # stays at the top

    async def test_brackets_jump_between_people_and_routines(self):
        rows = [item(1, "waiting", actions=("nudge",), ago_min=600,
                     sender="Ann Test"),
                item(2, "waiting", actions=("nudge",), ago_min=60,
                     sender="Bo Test"),
                item(3, "waiting", actions=("nudge",), ago_min=300,
                     sender="Ann Test"),
                feed_item(4, "a-routine"), feed_item(5, "b-routine")]
        app, _ = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("2")
            await pilot.pause(0.15)
            self.assertEqual(app.current_key, "t:1")            # Ann, oldest ask
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            self.assertEqual(app.current_key, "t:2")            # Bo
            await pilot.press("4")
            await pilot.pause(0.15)
            first = app.current_key
            await pilot.press("right_square_bracket")
            await pilot.pause(0.1)
            self.assertNotEqual(app.current_key, first)

    async def test_the_old_section_keys_no_longer_exist(self):
        keys = {k for b in InboxApp.BINDINGS for k in b.key.split(",")}
        self.assertFalse(any(b.action.startswith("section")
                             for b in InboxApp.BINDINGS))
        self.assertTrue({"1", "2", "3", "4", "5", "left_square_bracket",
                         "right_square_bracket"} <= keys)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class StartupFocusInEveryViewTest(unittest.IsolatedAsyncioTestCase):
    """0.8.1's fix: arrows work at start without a click, in every view."""

    def rows(self, view):
        if view == "inbox":
            return [item(1), item(2), item(3)]
        if view == "waiting":
            return [item(1, "waiting", actions=("nudge",)),
                    item(2, "waiting", actions=("nudge",), sender="Other"),
                    item(3, "waiting", actions=("nudge",), sender="Third")]
        return [feed_item(1), feed_item(2), feed_item(3)]

    async def check(self, view, delay):
        app, _ = make_app(self.rows(view), delay=delay, view=view)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            q = app.query_one("#queue", Queue)
            self.assertIs(app.focused, q)
            first = q.highlighted
            self.assertIsNotNone(first)
            await pilot.press("down")
            await pilot.pause(0.1)
            self.assertNotEqual(q.highlighted, first)
            self.assertEqual(app.current_key,
                             q.get_option_at_index(q.highlighted).id[2:])

    async def test_inbox(self):
        await self.check("inbox", 0.0)
        await self.check("inbox", 0.5)

    async def test_waiting(self):
        await self.check("waiting", 0.0)
        await self.check("waiting", 0.5)

    async def test_feed(self):
        await self.check("feed", 0.0)
        await self.check("feed", 0.5)

    async def test_switching_views_gives_the_queue_the_focus(self):
        app, _ = make_app(mixed_rows(), health=lambda: snapshot())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("5", "1", "3", "2", "4"):
                await pilot.press(key)
                await pilot.pause(0.15)
                if app.view in ("inbox", "waiting", "feed"):
                    self.assertIs(app.focused, app.query_one("#queue"), key)
                    await pilot.press("down")
                    await pilot.pause(0.05)
                    self.assertIsNotNone(app.query_one("#queue").highlighted)
            await pilot.press("5")
            await pilot.pause(0.15)
            self.assertIs(app.focused, app.query_one("#health"))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class WaitingViewUiTest(unittest.IsolatedAsyncioTestCase):
    async def test_groups_headers_and_tracking(self):
        rows = [item(1, "waiting", actions=("nudge",), ago_min=3 * 24 * 60,
                     sender="Ann Test"),
                item(2, "waiting", actions=("nudge",), ago_min=60,
                     sender="Ann Test"),
                item(3, "waiting", actions=("nudge",), ago_min=500,
                     sender="Bo Test", meta={"via": "track_reaction"})]
        app, _ = make_app(rows, view="waiting")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            q = app.query_one("#queue", Queue)
            heads = [str(o.prompt) for o in q.options if o.id.startswith("hdr:")]
            self.assertEqual(len(heads), 2)
            self.assertIn("Ann Test", heads[0])
            self.assertIn("2 open · oldest 3d", heads[0])
            self.assertIn("TRACKING", heads[1])
            row = q._row(app.items[0], 60, app.pal, time.time(), False)
            self.assertIn("→", row.plain)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class FeedUiTest(unittest.IsolatedAsyncioTestCase):
    def settings(self, rules=(), window_h=24.0):
        return dict(DEFAULTS, feed={"window_h": window_h,
                                    "rules": list(rules)})

    async def test_grouped_by_routine_with_the_routine_list(self):
        rows = [feed_item(1, "cal"), feed_item(2, "cal"),
                feed_item(3, "sheet-watch", ago_min=0.2)]
        app, _ = make_app(rows, view="feed", settings=self.settings())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            q = app.query_one("#queue", Queue)
            heads = [str(o.prompt) for o in q.options if o.id.startswith("hdr:")]
            self.assertTrue(heads[0].startswith("SHEET-WATCH"))
            self.assertTrue(heads[1].startswith("CAL"))
            pane = str(app.query_one("#routines").render())
            self.assertIn("all", pane)
            self.assertIn("cal", pane)
            self.assertIn("sheet-watch", pane)
            self.assertIn("last 24h", app.query_one("#left").border_title)

    async def test_f_cycles_the_routine_filter_and_esc_clears_it(self):
        rows = [feed_item(1, "cal"), feed_item(2, "sheet-watch")]
        app, _ = make_app(rows, view="feed", settings=self.settings())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("f")
            await pilot.pause(0.1)
            self.assertIsNotNone(app.feed_routine)
            self.assertEqual(len(app.visible()), 1)
            await pilot.press("f", "f")
            await pilot.pause(0.1)
            self.assertIsNone(app.feed_routine)
            await pilot.press("f", "escape")
            await pilot.pause(0.1)
            self.assertIsNone(app.feed_routine)
            self.assertEqual(len(app.visible()), 2)

    async def test_config_rules_hide_digest_and_promote(self):
        rules = [{"routine": "cal", "kind": "rsvp", "action": "hide"},
                 {"routine": "sheet-watch", "action": "digest"},
                 {"routine": "cal", "kind": "cancel", "action": "inbox"}]
        rows = [feed_item(1, "cal", "rsvp"), feed_item(2, "cal", "update"),
                feed_item(3, "cal", "cancel"),
                feed_item(4, "sheet-watch", "change", ago_min=1),
                feed_item(5, "sheet-watch", "change", ago_min=2)]
        app, _ = make_app(rows, settings=self.settings(rules))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            inbox = [r.key for r in app.query_one("#queue", Queue).rows]
            self.assertEqual(inbox, ["t:3"])             # promoted
            await pilot.press("4")
            await pilot.pause(0.15)
            rows = app.query_one("#queue", Queue).rows
            keys = [r.key for r in rows]
            self.assertIn("t:2", keys)
            self.assertNotIn("t:1", keys)                # hidden
            self.assertNotIn("t:3", keys)                # in the Inbox
            digest = next(r for r in rows if r.key.startswith("digest:"))
            self.assertEqual(len(digest.members), 2)
            self.assertIn("muted", str(app.query_one("#routines").render()))
            # done on the digest row marks its rows
            app.current_key = digest.key
            app.redraw(keep=digest.key)
            await pilot.press("d")
            await pilot.pause(0.6)
            self.assertEqual(
                sorted(k for k in ("t:4", "t:5") if app.store.is_hidden(k)),
                ["t:4", "t:5"])

    async def test_old_rows_age_out_and_a_shows_them(self):
        rows = [feed_item(1, "cal", ago_min=60),
                feed_item(2, "cal", ago_min=60 * 30)]
        app, _ = make_app(rows, view="feed", settings=self.settings())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual([r.key for r in app.visible()], ["t:1"])
            await pilot.press("A")
            await pilot.pause(0.4)
            self.assertEqual(sorted(r.key for r in app.visible()), ["t:1", "t:2"])

    async def test_the_mute_picker_writes_a_ui_rule(self):
        rows = [feed_item(1, "cal", "rsvp"), feed_item(2, "cal", "update")]
        with tempfile.TemporaryDirectory() as base:
            store = InboxStore(Path(base))
            app, _ = make_app(rows, store=store, view="feed",
                              settings=self.settings())
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                app.current_key = "t:1"
                app.redraw(keep="t:1")
                await pilot.press("x")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, ChoicePicker)
                await pilot.press("1")                # this kind from cal
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, ChoicePicker)
                await pilot.press("1")                # for 1 day
                await pilot.pause(0.3)
                self.assertEqual(len(app.screen_stack), 1)
                (rule,) = InboxStore(Path(base)).feed_rules()
                self.assertEqual((rule["routine"], rule["kind"],
                                  rule["action"]), ("cal", "rsvp", "hide"))
                self.assertAlmostEqual(rule["until"] - rule["created"], 86400,
                                       delta=1)
                self.assertEqual([r.key for r in app.visible()], ["t:2"])
                self.assertIn("cal · rsvp",
                              str(app.query_one("#routines").render()))
                doc = json.loads((Path(base) / "state"
                                  / "inbox-state.json").read_text())
                self.assertEqual(len(doc["prefs"]["feed_rules"]), 1)
                self.assertFalse((Path(base) / "inbox.yaml").exists())

    async def test_less_like_this_digests_instead_of_hiding(self):
        rows = [feed_item(1, "cal", "update", ago_min=1),
                feed_item(2, "cal", "update", ago_min=2),
                feed_item(3, "cal", "new", ago_min=3)]
        app, store = make_app(rows, view="feed", settings=self.settings())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.current_key = "t:1"
            app.redraw(keep="t:1")
            await pilot.press("minus")
            await pilot.pause(0.3)
            (rule,) = store.feed_rules()
            self.assertEqual((rule["action"], rule["kind"], rule["until"]),
                             ("digest", "update", None))
            keys = [r.key for r in app.visible()]
            self.assertIn("t:3", keys)
            self.assertEqual(len([k for k in keys if k.startswith("digest:")]), 1)

    async def test_x_outside_the_feed_says_so(self):
        app, store = make_app(mixed_rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(store.feed_rules(), [])

    async def test_the_rules_screen_lists_and_toggles_ui_rules(self):
        cfg_rules = [{"routine": "cal", "kind": "rsvp", "action": "hide"}]
        rows = [feed_item(1, "sheet-watch", "change"),
                feed_item(2, "cal", "update")]
        store = InboxStore(None)
        rule = feedrules.new_ui_rule(
            from_row(rows[0]), "routine", "hide", None, time.time())
        store.add_feed_rule(rule)
        app, _ = make_app(rows, store=store, view="feed",
                          settings=self.settings(cfg_rules))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual([r.key for r in app.visible()], ["t:2"])
            await pilot.press("M")
            await pilot.pause(0.3)
            screen = app.screen
            self.assertIsInstance(screen, RulesScreen)
            table = screen.query_one("#rules-table")
            self.assertEqual(table.row_count, 2)
            first = [str(c) for c in table.get_row_at(0)]
            self.assertEqual(first[0], "here")             # UI rules first
            self.assertIn("sheet-watch", first[3])
            self.assertEqual([str(c) for c in table.get_row_at(1)][0],
                             "inbox.yaml")
            await pilot.press("space")                     # switch it off
            await pilot.pause(0.3)
            self.assertFalse(store.feed_rules()[0]["enabled"])
            self.assertEqual(sorted(r.key for r in app.visible()),
                             ["t:1", "t:2"])
            self.assertEqual(str(table.get_row_at(0)[1]), "no")
            await pilot.press("j", "space")                # file rule: refused
            await pilot.pause(0.2)
            self.assertEqual(len(store.feed_rules()), 1)
            await pilot.press("k", "delete")               # remove it
            await pilot.pause(0.3)
            self.assertEqual(store.feed_rules(), [])
            await pilot.press("M")                         # M closes it too
            await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)

    async def test_M_toggle_opens_and_closes(self):
        app, _ = make_app([feed_item(1)], view="feed")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("M")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, RulesScreen)
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)


def from_row(raw):
    from workspace_daemon.inbox.models import from_v1
    return from_v1(raw)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class HealthUiTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_health_view_renders_needs_you_and_the_table(self):
        alerts = [{"key": "attention:zoom-meetings",
                   "routine": "zoom-meetings", "since": time.time() - 1800}]
        rows = [alert_item(1, "attention:zoom-meetings", "zoom-meetings",
                           cause="connector_auth", remedy="sign in again",
                           last_error="token refused",
                           since=time.time() - 2040)]
        snap = routine_snapshot(alerts)
        snap["connectors"] = [{"name": "claude_ai_Chat_Server", "state": "ok",
                               "at": time.time()}]
        snap["heartbeats"] = [{"name": "poller", "state": "warn",
                               "age_s": None, "note": "no heartbeat file"}]
        app, _ = make_app(rows, health=lambda: snap)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await until(pilot, lambda: app.health_snap is not None)
            await pilot.press("5")
            await pilot.pause(0.3)
            body = str(app.query_one("#health-body").render())
            self.assertIn("NEEDS YOU 1", body)
            self.assertIn("zoom-meetings", body)
            self.assertIn("connector sign-in expired", body)
            self.assertIn("34m", body)                       # since
            self.assertIn("last error: token refused", body)
            self.assertIn("→ sign in again", body)
            self.assertIn("ROUTINES", body)
            self.assertIn("slack-mcp", body)
            self.assertIn("failing", body)
            self.assertIn("CONNECTORS", body)
            self.assertIn("Chat Server", body)
            self.assertIn("$6.00 today of $20 cap", body)
            self.assertIn("poller", body)
            self.assertIn("scheduler armed",
                          app.query_one("#health").border_title)
            strip = str(app.query_one("#strip").render())
            self.assertIn("slack ✓", strip)
            self.assertIn("zoom ✗ connector sign-in expired", strip)
            self.assertIn("$6/$20", strip)

    async def test_alerts_without_details_degrade_to_the_title(self):
        alerts = [{"key": "attention:zoom-meetings",
                   "routine": "zoom-meetings", "since": time.time() - 600}]
        rows = [alert_item(1, "attention:zoom-meetings", "zoom-meetings")]
        app, _ = make_app(rows, health=lambda: routine_snapshot(alerts))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await until(pilot, lambda: app.health_snap is not None)
            await pilot.press("5")
            await pilot.pause(0.3)
            body = str(app.query_one("#health-body").render())
            self.assertIn("NEEDS YOU 1", body)
            self.assertIn("gist 1", body)                    # the alert's title
            self.assertNotIn("→", body)
            self.assertIn("zoom ✗ failing", str(app.query_one("#strip").render()))

    async def test_benign_alerts_do_not_need_you(self):
        alerts = [{"key": "interrupted:slack-mcp", "routine": "slack-mcp",
                   "since": time.time() - 600}]
        rows = [alert_item(1, "interrupted:slack-mcp", "slack-mcp",
                           benign=True)]
        app, _ = make_app(rows, health=lambda: routine_snapshot(alerts))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await until(pilot, lambda: app.health_snap is not None)
            self.assertEqual(app.needs_you(), [])
            self.assertNotIn("⚠", str(app.query_one("#hdr").render()))

    async def test_the_reader_runs_off_the_ui_thread_and_failures_are_shown(self):
        import threading
        seen = []

        def reader():
            seen.append(threading.current_thread() is threading.main_thread())
            raise RuntimeError("boom")

        app, _ = make_app([item(1)], health=reader)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await until(pilot, lambda: app.health_snap is not None)
            self.assertEqual(seen, [False])
            self.assertIn("health read failed", app.health_snap["error"])
            await pilot.press("5")
            await pilot.pause(0.2)
            self.assertIn("health read failed",
                          str(app.query_one("#health-body").render()))

    async def test_r_refreshes_the_health_snapshot(self):
        calls = []

        def reader():
            calls.append(1)
            return snapshot()

        app, _ = make_app([item(1)], health=reader)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await until(pilot, lambda: len(calls) >= 1)
            before = len(calls)
            await pilot.press("r")
            self.assertTrue(await until(pilot, lambda: len(calls) > before))

    async def test_no_reader_means_no_strip(self):
        app, _ = make_app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertFalse(app.query_one("#strip").display)
            await pilot.press("5")
            await pilot.pause(0.2)
            self.assertIn("Reading", str(app.query_one("#health-body").render()))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class HelpAndPaletteTest(unittest.IsolatedAsyncioTestCase):
    async def test_help_describes_the_views_and_the_feed_keys(self):
        app, _ = make_app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("question_mark")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, HelpScreen)
            body = str(app.screen.query_one("#help-body").render())
            for needle in ("Inbox · Waiting · Tasks · Feed · Health",
                           "[ / ]", "mute this row", "less like this",
                           "rules"):
                self.assertIn(needle, body)
            self.assertNotIn("jump to section", body)

    async def test_the_hints_name_the_views(self):
        app, _ = make_app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("1-5", "views"), app.hints())
            self.assertEqual(app.hints()[-1], ("?", "help"))

    async def test_palette_has_view_jumps(self):
        app, _ = make_app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            cmds = [c.title for c in app.get_system_commands(app.screen)]
            for title in ("Go to Feed", "Go to Health", "Next group",
                          "Feed rules"):
                self.assertIn(title, cmds)


if __name__ == "__main__":
    unittest.main()
