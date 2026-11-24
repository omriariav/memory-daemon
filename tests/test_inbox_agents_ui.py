"""Inbox 0.13.2 pilot tests: the sessions strip, view 6 (Agents) and the Inbox
without working agent rows. Synthetic data only; needs Textual."""
import importlib.util
import time
import unittest

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from workspace_daemon.inbox import items as itm, sources
    from workspace_daemon.inbox.actions import FakeActions
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import HelpScreen
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import Queue

SIZE = (130, 40)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def agent(i, section="fyi", bucket="working", ago_min=None, **meta):
    ago = (ago_min if ago_min is not None else i) * 60
    meta = dict({"kind": "agent", "bucket": bucket}, **meta)
    return itm.make_item(
        id=f"ag:{i}", source="other", section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender=f"bot-{i}", where="agents · helper", gist=f"gist {i}",
        ts=time.time() - ago, link=None, thread=[], draft=None,
        actions=["done"] if section == "needs_reply" else [], meta=meta)


def plain(i, section="needs_reply"):
    return itm.make_item(
        id=f"t:{i}", source="slack", section=section, urgency="digest",
        sender=f"Person {i}", where="room", gist=f"plain {i}",
        ts=time.time() - i * 60, link=None, thread=[], draft=None,
        actions=["reply"], meta={})


def lines(n, level="ok"):
    return [{"text": f"status line {k}", "level": level} for k in range(n)]


class Fixed:
    """A fetch returning one command source's rows and status lines."""

    def __init__(self, rows, status=(), name="agents"):
        self.rows, self.status, self.name = rows, list(status), name

    def __call__(self):
        return [sources.SourceResult(self.name, self.rows, False, None, None,
                                     self.status)]


async def settle(pilot, app):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)


async def until(pilot, cond, tries=100, step=0.05):
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


def make_app(rows, status=(), store=None, **kw):
    store = store or InboxStore(None)
    kw.setdefault("settings", dict(DEFAULTS))
    return InboxApp(Fixed(rows, status), store,
                    FakeActions(store=store, draft_delay=0), undo_s=0.15,
                    leave_s=0, stream_delay=0, **kw), store


def strip_text(app):
    return str(app.query_one("#sessions").render())


def queue_keys(app):
    return [r.key for r in app.query_one("#queue", Queue).rows]


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class SessionsStripTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_strip_shows_the_status_lines(self):
        status = [{"text": "day session live", "level": "ok"},
                  {"text": "worker one waiting on you", "level": "warn"},
                  {"text": "worker two stuck", "level": "alert"}]
        app, _ = make_app([plain(1)], status)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            strip = app.query_one("#sessions")
            self.assertTrue(strip.display)
            text = strip_text(app)
            for line in status:
                self.assertIn(line["text"], text)
            self.assertNotIn("more", text)
            self.assertEqual(len(text.splitlines()), 3)

    async def test_the_level_picks_the_colour_only(self):
        status = [{"text": "a", "level": "ok"}, {"text": "b", "level": "warn"},
                  {"text": "c", "level": "alert"}]
        app, _ = make_app([plain(1)], status)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            out = app.query_one("#sessions").render_strip(
                app, app.pal, SIZE[0])
            colours = []
            for span in out.spans:
                if out.plain[span.start:span.end] == " ● ":
                    colours.append(str(span.style.color.name
                                       if span.style.color else ""))
            self.assertEqual(len(set(colours)), 3)
            self.assertEqual(len(out.plain.splitlines()), 3)

    async def test_it_is_capped_at_four_lines_then_a_more_line(self):
        app, _ = make_app([plain(1)], lines(7))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            shown = strip_text(app).splitlines()
            self.assertEqual(len(shown), 5)
            self.assertIn("status line 3", shown[3])
            self.assertNotIn("status line 4", "\n".join(shown))
            self.assertIn("+3 more · 6", shown[4])

    async def test_exactly_the_cap_has_no_more_line(self):
        app, _ = make_app([plain(1)], lines(4))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(len(strip_text(app).splitlines()), 4)
            self.assertNotIn("more", strip_text(app))

    async def test_strip_lines_in_the_settings_sets_the_cap(self):
        cfg = dict(DEFAULTS, strip_lines=2)
        app, _ = make_app([plain(1)], lines(5), settings=cfg)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            shown = strip_text(app).splitlines()
            self.assertEqual(len(shown), 3)
            self.assertIn("+3 more · 6", shown[2])

    async def test_hidden_when_no_source_sends_status(self):
        app, _ = make_app([plain(1)], [])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertFalse(app.query_one("#sessions").display)
            self.assertEqual(app.status_lines(), [])

    async def test_the_lines_of_every_source_are_listed_in_order(self):
        def fetch():
            return [
                sources.SourceResult("one", [plain(1)], False, None, None,
                                     [{"text": "from one", "level": "ok"}]),
                sources.SourceResult("two", [], False, None, None, ()),
                sources.SourceResult("three", [], False, None, None,
                                     [{"text": "from three", "level": "warn"}])]
        store = InboxStore(None)
        app = InboxApp(fetch, store, FakeActions(store=store, draft_delay=0),
                       undo_s=0.15, leave_s=0, stream_delay=0,
                       settings=dict(DEFAULTS))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            text = strip_text(app)
            self.assertLess(text.index("from one"), text.index("from three"))

    async def test_the_strip_is_pinned_in_every_view(self):
        app, _ = make_app([plain(1), agent(2)], lines(2),
                          health=lambda: None)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in "123456":
                await pilot.press(key)
                await pilot.pause(0.1)
                self.assertTrue(app.query_one("#sessions").display, key)
                self.assertIn("status line 0", strip_text(app), key)

    async def test_the_palette_toggle_hides_and_shows_it(self):
        store = InboxStore(None)
        app, _ = make_app([plain(1)], lines(2), store=store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertTrue(app.query_one("#sessions").display)
            titles = [c.title for c in app.get_system_commands(app.screen)]
            self.assertIn("Show / hide sessions strip", titles)
            app.action_toggle_strip()
            await pilot.pause(0.1)
            self.assertFalse(app.query_one("#sessions").display)
            self.assertEqual(store.get_pref("sessions_strip"), "off")
            app.action_toggle_strip()
            await pilot.pause(0.1)
            self.assertTrue(app.query_one("#sessions").display)
            self.assertEqual(store.get_pref("sessions_strip"), "on")

    async def test_the_choice_survives_a_restart(self):
        store = InboxStore(None)
        store.set_pref("sessions_strip", "off")
        app, _ = make_app([plain(1)], lines(2), store=store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertFalse(app.strip_on)
            self.assertFalse(app.query_one("#sessions").display)

    async def test_the_toggle_has_no_key_of_its_own(self):
        keys = {k for b in InboxApp.BINDINGS for k in b.key.split(",")}
        actions = {b.action for b in InboxApp.BINDINGS}
        self.assertNotIn("toggle_strip", actions)
        self.assertIn("6", keys)

    async def test_status_text_is_displayed_not_run(self):
        evil = [{"text": "$(touch /tmp/never) `x` ; rm -rf ~ [bold]x[/bold]",
                 "level": "warn"}]
        app, _ = make_app([plain(1)], evil)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("$(touch /tmp/never)", strip_text(app))
            self.assertIn("[bold]x[/bold]", strip_text(app))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class AgentsViewUiTest(unittest.IsolatedAsyncioTestCase):
    def rows(self):
        return [plain(1),
                agent(2, "needs_reply", "needs_you", live=True),
                agent(3, "fyi", "working", live=True),
                agent(4, "fyi", "working", live=False),
                agent(5, "fyi", "working", rules_stale=True, live=True),
                agent(6, "fyi", "done")]

    async def test_key_6_opens_the_agents_view_grouped(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("6")
            self.assertTrue(await until(pilot, lambda: app.view == "agents"))
            self.assertTrue(app.query_one("#main").display)
            self.assertEqual(queue_keys(app),
                             ["ag:2", "ag:3", "ag:4", "ag:5", "ag:6"])
            heads = [str(o.prompt) for o in app.query_one("#queue").options
                     if o.id and o.id.startswith("hdr:")]
            self.assertEqual([h.split()[0] + " " + h.split()[1]
                              if h.startswith("NEEDS") else h.split()[0]
                              for h in heads],
                             ["NEEDS YOU", "WORKING", "DONE"])
            self.assertEqual(app.query_one("#left").border_title, "Agents")

    async def test_the_tab_bar_names_and_counts_view_6(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertTrue(await until(
                pilot, lambda: "6 Agents 5" in str(app.query_one("#hdr").render())))

    async def test_rows_carry_where_age_and_the_marks(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("6")
            await until(pilot, lambda: app.view == "agents")
            opts = {o.id: str(o.prompt) for o in app.query_one("#queue").options
                    if o.id}
            first = lambda k: opts["i:" + k].splitlines()[0]
            self.assertIn("agents · helper", first("ag:3"))
            self.assertRegex(first("ag:3"), r"\b3m\b")
            self.assertIn("live", first("ag:3"))
            self.assertNotIn("no session", first("ag:3"))
            self.assertIn("no session", first("ag:4"))
            self.assertIn("old rules", first("ag:5"))
            self.assertNotIn("live", first("ag:6"))      # absent: no mark
            self.assertNotIn("no session", first("ag:6"))
            self.assertNotIn("old rules", first("ag:3"))

    async def test_the_inbox_keeps_needs_you_agents_and_drops_working_ones(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(sorted(queue_keys(app)), ["ag:2", "t:1"])
            self.assertIn("1 Inbox 2", str(app.query_one("#hdr").render()))

    async def test_the_feed_does_not_list_agent_rows(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("4")
            await until(pilot, lambda: app.view == "feed")
            self.assertEqual([i.key for i in app.visible()], [])
            self.assertTrue(app.query_one("#zero").display)

    async def test_a_mark_in_view_6_is_the_same_mark_as_in_the_inbox(self):
        app, store = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("6")
            await until(pilot, lambda: app.view == "agents")
            self.assertEqual(app.current_key, "ag:2")
            await pilot.press("d")
            self.assertTrue(await until(
                pilot, lambda: "ag:2" not in queue_keys(app)))
            await pilot.press("1")
            await until(pilot, lambda: app.view == "inbox")
            self.assertNotIn("ag:2", queue_keys(app))

    async def test_enter_opens_the_row_menu_in_view_6(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("6")
            await until(pilot, lambda: app.view == "agents")
            await pilot.press("enter")
            self.assertTrue(await until(
                pilot, lambda: type(app.screen).__name__ == "RowMenu"))

    async def test_an_empty_view_says_what_lists_agents(self):
        app, _ = make_app([plain(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("6")
            await until(pilot, lambda: app.view == "agents")
            self.assertTrue(app.query_one("#zero").display)
            self.assertIn("meta.kind: agent", str(app.query_one("#zero").render()))

    async def test_the_palette_and_the_help_know_view_6(self):
        app, _ = make_app(self.rows())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            titles = [c.title for c in app.get_system_commands(app.screen)]
            self.assertIn("Go to Agents", titles)
            await pilot.press("question_mark")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, HelpScreen)))
            from workspace_daemon.inbox.screens import HELP
            text = " ".join(k + " " + v for _t, rows in HELP for k, v in rows)
            self.assertIn("1 2 3 4 5 6", text)
            self.assertIn("Show / hide sessions strip", text)


if __name__ == "__main__":
    unittest.main()
