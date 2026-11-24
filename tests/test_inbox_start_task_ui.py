"""Inbox 0.13.3 pilot tests: start a task from a row. The `t` modal (local
defaults, the background draft that fills only untouched fields, Enter before
the draft returns, Esc), the 5 s undo window before the `launch` call, failure
toasts, the badge on the row a card came from, "Go to task", `o` on a finished
task, and the footer, menu and palette. The launcher is a scripted fake
backend (the protocol itself is in test_inbox_launcher.py); nothing here runs a
real command, claude or the network. Synthetic data only; needs Textual."""
import importlib.util
import threading
import time
import unittest

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from workspace_daemon.inbox import items as itm, launcher, rowmenu, sources
    from workspace_daemon.inbox import screens
    from workspace_daemon.inbox.actions import FakeActions, Result
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import RowMenu, TaskScreen
    from workspace_daemon.inbox.store import InboxStore

SIZE = (130, 44)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
CFG = {"argv": ["x"], "timeout_s": 30.0, "draft_timeout_s": 90.0,
       "roles": ["researcher", "analyst", "reviewer", "reader"],
       "models": ["haiku", "sonnet", "opus"],
       "efforts": ["low", "medium", "high"]}
REPORT = "https://example.com/report/scout"


def plain(i=1, section="needs_reply", **kw):
    base = dict(
        id=f"t:{i}", source="slack", section=section, urgency="digest",
        sender=f"Person {i}", where="#room", gist=f"Check the rollout plan {i}",
        ts=time.time() - i * 60, link=f"https://example.com/{i}", thread=[],
        draft=None, actions=["reply"], meta={})
    base.update(kw)
    return itm.make_item(**base)


def agent(i=1, source_key="t:1", link=None, **meta):
    meta = dict({"kind": "agent", "source_key": source_key, "name": "scout-1",
                 "role": "researcher", "state": "in_progress",
                 "bucket": "working"}, **meta)
    return itm.make_item(
        id=f"ag:{i}", source="other", section="fyi", urgency="digest",
        sender="scout-1", where="agents · researcher", gist="reading",
        ts=time.time() - 30, link=link, thread=[], draft=None, actions=[],
        meta=meta)


class Fixed:
    """A fetch returning one source's rows; `rows` can be swapped between
    polls."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return [sources.SourceResult("src", self.rows, False, None, None)]


if HAVE_TEXTUAL:
    class LauncherActions(FakeActions):
        """The demo backend plus a scripted task launcher."""

        task_launcher = CFG
        task_launcher_configured = True

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.drafts, self.launches = [], []
            self.draft_wait = 0.0
            self.draft_reply = launcher.Draft(True, {}, [], "")
            self.launch_reply = Result(True, "started", name="")
            self.draft_cancelled = False

        def task_draft(self, item, fields, taken=(), cancel=None):
            self.drafts.append((item.key, dict(fields), set(taken)))
            end = time.time() + self.draft_wait
            while time.time() < end:
                if cancel is not None and cancel.is_set():
                    self.draft_cancelled = True
                    return launcher.Draft(False, {}, [], "cancelled")
                time.sleep(0.01)
            return self.draft_reply

        def task_launch(self, item, fields):
            self.launches.append((item.key, dict(fields)))
            return self.launch_reply


async def settle(pilot, app):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)


async def until(pilot, cond, tries=120, step=0.05):
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


def modal(app):
    return app.screen if isinstance(app.screen, TaskScreen) else None


def row_text(app, key):
    opts = {o.id: str(o.prompt) for o in app.query_one("#queue").options
            if o.id}
    return opts.get("i:" + key, "")


def make_app(rows, actions=None, undo_s=0.2, view="inbox", **kw):
    store = InboxStore(None)
    actions = actions or LauncherActions(store=store, draft_delay=0)
    fetch = Fixed(rows)
    app = InboxApp(fetch, store, actions, settings=dict(DEFAULTS),
                   undo_s=undo_s, leave_s=0, stream_delay=0, view=view, **kw)
    return app, actions, fetch


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class NoLauncherTest(unittest.IsolatedAsyncioTestCase):
    async def test_t_says_so_and_nothing_else_changes(self):
        app, _a, _f = make_app([plain()], FakeActions(
            store=InboxStore(None), draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            before = (app.view, app.current_key, len(app.screen_stack),
                      list(app.pending))
            await pilot.press("t")
            await pilot.pause(0.2)
            self.assertTrue(any("no task launcher configured" in t
                                for t in toasts(app)), toasts(app))
            self.assertEqual((app.view, app.current_key,
                              len(app.screen_stack), list(app.pending)),
                             before)

    async def test_no_hint_and_no_menu_entry(self):
        app, _a, _f = make_app([plain()], FakeActions(
            store=InboxStore(None), draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn("t", dict(app.hints()))
            self.assertNotIn("t", [e.key for e in app.row_menu_entries()])


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class FooterMenuPaletteTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_footer_gets_a_task_hint_within_the_cap(self):
        app, _a, _f = make_app([plain()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            hints = app.hints()
            self.assertIn(("t", "task"), hints)
            self.assertLessEqual(len(hints), rowmenu.FOOTER_MAX)
            order = [k for k, _l in hints]
            self.assertEqual(order[-2:], ["⏎", "?"])
            self.assertLess(order.index("s"), order.index("t"))
            self.assertLess(order.index("t"), order.index("o"))

    async def test_t_is_a_reserved_binding(self):
        self.assertIn("t", {b.key for b in InboxApp.BINDINGS})

    async def test_the_menu_lists_start_a_task_with_its_key(self):
        app, _a, _f = make_app([plain()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entry = next(e for e in app.row_menu_entries() if e.key == "t")
            self.assertEqual(entry.label, "Start a task…")
            self.assertEqual(entry.titles, ("Start a task…",))
            await pilot.press("enter")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, RowMenu)))
            await pilot.press("t")
            self.assertTrue(await until(pilot, lambda: modal(app)))

    async def test_rows_that_cannot_start_a_task(self):
        invite = plain(2, meta={"kind": "invite"})
        alert = plain(3, source="alert", section="urgent", actions=["ack"])
        app, _a, _f = make_app([plain(1), invite, alert,
                                agent(1, source_key="elsewhere")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("t:2", "t:3"):
                row = app._rows[key]
                self.assertTrue(rowmenu.task_refusal(app, row), key)
                self.assertFalse(rowmenu.task_ok(app, row), key)
            agent_row = app._rows["ag:1"]
            self.assertIn("a task already",
                          rowmenu.task_refusal(app, agent_row))
            self.assertEqual(rowmenu.task_refusal(app, app._rows["t:1"]), "")

    async def test_the_palette_lists_the_commands(self):
        app, _a, _f = make_app([plain()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            titles = {c.title for c in app.get_system_commands(app.screen)}
            self.assertIn("Start a task…", titles)
            self.assertIn("Go to task", titles)

    async def test_help_names_the_task_key(self):
        text = " ".join(f"{k} {v}" for _t, rows in screens.HELP
                        for k, v in rows)
        self.assertIn("task_launcher", text)
        self.assertRegex(text, r"\bt\b.*task")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ModalTest(unittest.IsolatedAsyncioTestCase):
    async def open_modal(self, pilot, app):
        await settle(pilot, app)
        await pilot.press("t")
        self.assertTrue(await until(pilot, lambda: modal(app)))
        return modal(app)

    async def test_it_opens_at_once_with_local_defaults(self):
        app, acts, _f = make_app([plain()])
        acts.draft_wait = 0.6
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            form = screen.form
            self.assertEqual(form.name, "check-the-rollout-plan-1")
            self.assertEqual(form.role, "researcher")
            self.assertEqual((form.model, form.effort), ("", ""))
            self.assertEqual(form.goal, "Look into: Check the rollout plan 1")
            self.assertIn("drafting", screen.status)
            self.assertEqual(form.touched, set())      # the mount is no edit
            self.assertEqual(screen.query_one("#task-name").value, form.name)
            self.assertEqual(screen.query_one("#task-goal").value, form.goal)
            await pilot.pause(0.2)
            (call,) = acts.drafts
            self.assertEqual(call[0], "t:1")
            self.assertEqual(call[1]["name"], form.name)
            self.assertEqual(call[1]["goal"], form.goal)

    async def test_a_pr_row_starts_as_a_reviewer(self):
        app, _a, _f = make_app([plain(link="https://git.example.com/o/r/"
                                      "pull/9")])
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            self.assertEqual(screen.form.role, "reviewer")

    async def test_a_name_in_use_gets_a_suffix(self):
        app, _a, _f = make_app([
            plain(), agent(2, source_key="other", name="check-the-rollout-"
                           "plan-1")])
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            self.assertEqual(screen.form.name, "check-the-rollout-plan-1-2")

    async def test_the_draft_fills_only_untouched_fields(self):
        app, acts, _f = make_app([plain()])
        acts.draft_wait = 0.5
        acts.draft_reply = launcher.Draft(True, {
            "name": "plan-check", "role": "analyst", "model": "sonnet",
            "effort": "high", "goal": "Drafted goal", "persona": "Careful",
            "acceptance": ["A short report", "Lists the risks"],
            "template": "spec"}, ["research", "spec"], "")
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            await pilot.press("3")                     # the role chip: mine
            await pilot.press("g")                     # into the goal line
            await pilot.press("x")
            self.assertTrue(screen.form.goal.endswith("x"))
            await pilot.press("tab")                   # back to the chips
            self.assertTrue(await until(
                pilot, lambda: "draft ready" in screen.status))
            form = screen.form
            self.assertEqual(form.role, "reviewer")           # kept
            self.assertTrue(form.goal.endswith("x"))          # kept
            self.assertEqual(form.goal[:-1],
                             "Look into: Check the rollout plan 1")
            self.assertEqual((form.name, form.model, form.effort),
                             ("plan-check", "sonnet", "high"))
            self.assertEqual(form.persona, "Careful")
            self.assertEqual(form.template, "spec")
            self.assertEqual(form.acceptance,
                             ["A short report", "Lists the risks"])
            self.assertEqual(screen.query_one("#task-name").value,
                             "plan-check")
            self.assertTrue(screen.query_one("#task-persona").display)
            self.assertTrue(screen.query_one("#task-template").display)

    async def test_a_failed_draft_keeps_the_defaults_and_still_launches(self):
        app, acts, _f = make_app([plain()])
        acts.draft_reply = launcher.Draft(False, {}, [], "model timed out")
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            self.assertTrue(await until(
                pilot, lambda: "no draft: model timed out" in screen.status))
            self.assertEqual(screen.form.name, "check-the-rollout-plan-1")
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: acts.launches))

    async def test_chip_keys(self):
        app, acts, _f = make_app([plain()])
        acts.draft_reply = launcher.Draft(True, {}, ["a", "b"], "")
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            await until(pilot, lambda: "draft ready" in screen.status)
            for key in ("2", "m", "m", "e", "t", "a"):
                await pilot.press(key)
            form = screen.form
            self.assertEqual((form.role, form.model, form.effort,
                              form.template),
                             ("analyst", "sonnet", "low", "a"))
            self.assertTrue(screen.expanded)
            await pilot.press("9")                     # no such chip
            self.assertEqual(form.role, "analyst")

    async def test_enter_before_the_draft_returns_launches_the_defaults(self):
        app, acts, _f = make_app([plain()])
        acts.draft_wait = 5
        acts.draft_reply = launcher.Draft(True, {"goal": "too late"}, [], "")
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            self.assertIn("drafting", screen.status)
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: not modal(app)))
            self.assertTrue(await until(pilot, lambda: acts.launches))
            (key, fields), = acts.launches
            self.assertEqual(key, "t:1")
            self.assertEqual(fields["name"], "check-the-rollout-plan-1")
            self.assertEqual(fields["role"], "researcher")
            self.assertEqual(fields["goal"],
                             "Look into: Check the rollout plan 1")
            # the draft that was still running is stopped, and never applied
            self.assertTrue(await until(pilot, lambda: acts.draft_cancelled))
            self.assertNotIn("too late", fields["goal"])

    async def test_esc_cancels_and_launches_nothing(self):
        app, acts, _f = make_app([plain()])
        acts.draft_wait = 5
        async with app.run_test(size=SIZE) as pilot:
            await self.open_modal(pilot, app)
            await pilot.press("escape")
            self.assertTrue(await until(pilot, lambda: not modal(app)))
            await pilot.pause(0.5)
            self.assertEqual(acts.launches, [])
            self.assertEqual(list(app.pending), [])
            self.assertTrue(any("Cancelled" in t for t in toasts(app)))
            self.assertTrue(await until(pilot, lambda: acts.draft_cancelled))

    async def test_a_bad_name_or_an_empty_goal_keeps_the_modal_open(self):
        app, acts, _f = make_app([plain()])
        async with app.run_test(size=SIZE) as pilot:
            screen = await self.open_modal(pilot, app)
            screen.query_one("#task-name").value = "Bad Name"
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertIs(modal(app), screen)
            self.assertIn("a-z, 0-9", screen.form.problem())
            screen.query_one("#task-name").value = "good-name"
            screen.query_one("#task-goal").value = "  "
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertIs(modal(app), screen)
            self.assertIn("goal", screen.form.problem())
            self.assertEqual(acts.launches, [])
            screen.query_one("#task-goal").value = "Do the thing"
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: acts.launches))
            self.assertEqual(acts.launches[0][1]["name"], "good-name")
            self.assertEqual(acts.launches[0][1]["goal"], "Do the thing")

    async def test_enter_in_an_input_starts_it_too(self):
        app, acts, _f = make_app([plain()])
        async with app.run_test(size=SIZE) as pilot:
            await self.open_modal(pilot, app)
            await pilot.press("n")                     # into the name line
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: acts.launches))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class LaunchTest(unittest.IsolatedAsyncioTestCase):
    async def start(self, pilot, app):
        await settle(pilot, app)
        await pilot.press("t")
        self.assertTrue(await until(pilot, lambda: modal(app)))
        await pilot.press("enter")
        self.assertTrue(await until(pilot, lambda: not modal(app)))

    async def test_the_launch_waits_out_the_undo_window(self):
        app, acts, _f = make_app([plain()], undo_s=0.6)
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            self.assertEqual(acts.launches, [])
            self.assertEqual([p.kind for p in app.pending], ["task"])
            self.assertTrue(any("Start task check-the-rollout-plan-1" in t
                                and "u to undo" in t for t in toasts(app)))
            self.assertTrue(await until(pilot, lambda: acts.launches))
            self.assertEqual(len(acts.launches), 1)

    async def test_the_row_stays_listed(self):
        app, acts, _f = make_app([plain()], undo_s=0.4)
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            self.assertIn("i:t:1", [o.id for o in app.query_one(
                "#queue").options])
            await until(pilot, lambda: acts.launches)
            await pilot.pause(0.3)
            self.assertIn("i:t:1", [o.id for o in app.query_one(
                "#queue").options])

    async def test_undo_cancels_the_launch(self):
        app, acts, _f = make_app([plain()], undo_s=0.8)
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            await pilot.press("u")
            await pilot.pause(1.2)
            self.assertEqual(acts.launches, [])
            self.assertEqual(list(app.pending), [])
            self.assertTrue(any("Undone" in t for t in toasts(app)))

    async def test_success_toast_names_the_task(self):
        app, acts, _f = make_app([plain()])
        acts.launch_reply = Result(True, "started in a new tab\nsecond",
                                   name="plan-check")
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            self.assertTrue(await until(
                pilot, lambda: any("Task started" in t for t in toasts(app))))
            note = next(n for n in app._notifications
                        if n.title == "Task started")
            self.assertEqual(note.message, "plan-check: started in a new tab")

    async def test_a_failure_toasts_the_first_line_of_the_message(self):
        app, acts, _f = make_app([plain()])
        acts.launch_reply = Result(False, "refused: no free slot\nsecond line")
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            self.assertTrue(await until(
                pilot, lambda: any(n.title == "Task not started"
                                   for n in app._notifications)))
            note = next(n for n in app._notifications
                        if n.title == "Task not started")
            self.assertEqual(note.message, "refused: no free slot")
            self.assertEqual(note.severity, "error")
            self.assertEqual(app.busy, 0)
            self.assertIn("i:t:1", [o.id for o in app.query_one(
                "#queue").options])

    async def test_a_backend_that_raises_is_a_toast_not_a_crash(self):
        app, acts, _f = make_app([plain()])

        def boom(item, fields):
            raise RuntimeError("kaboom")
        acts.task_launch = boom
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            self.assertTrue(await until(
                pilot, lambda: any(n.title == "Task not started"
                                   for n in app._notifications)))
            self.assertTrue(app.is_running)

    async def test_a_second_t_on_the_row_is_refused_while_it_starts(self):
        app, acts, _f = make_app([plain()], undo_s=0.8)
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            await pilot.press("t")
            await pilot.pause(0.2)
            self.assertIsNone(modal(app))
            self.assertTrue(any("already being started" in t
                                for t in toasts(app)))

    async def test_the_name_taken_by_a_pending_launch_is_not_offered(self):
        app, acts, _f = make_app([plain(1), plain(2, gist="Check the "
                                                  "rollout plan 1")],
                                 undo_s=1.0)
        async with app.run_test(size=SIZE) as pilot:
            await self.start(pilot, app)
            await pilot.press("j")
            await pilot.pause(0.2)
            await pilot.press("t")
            self.assertTrue(await until(pilot, lambda: modal(app)))
            self.assertEqual(modal(app).form.name,
                             "check-the-rollout-plan-1-2")
            await pilot.press("escape")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class BadgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_origin_row_gets_the_badge(self):
        app, _a, _f = make_app([plain(1), plain(2), agent(1, "t:1")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("▶ scout-1 · researcher · in progress",
                          row_text(app, "t:1"))
            self.assertNotIn("▶", row_text(app, "t:2"))
            self.assertEqual(app._rows["t:1"].task[1].name, "scout-1")
            self.assertIsNone(app._rows["t:2"].task)

    async def test_the_badge_follows_the_card(self):
        app, _a, fetch = make_app([plain(1), agent(1, "t:1")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("in progress", row_text(app, "t:1"))
            fetch.rows = [plain(1), agent(1, "t:1", state="completed",
                                          bucket="done", link=REPORT)]
            app.poll()
            self.assertTrue(await until(
                pilot, lambda: "completed" in row_text(app, "t:1")))
            self.assertIn("▶ scout-1 · researcher · completed",
                          row_text(app, "t:1"))
            fetch.rows = [plain(1)]                      # the card is gone
            app.poll()
            self.assertTrue(await until(
                pilot, lambda: "▶" not in row_text(app, "t:1")))

    async def test_a_second_t_is_refused_while_the_card_runs(self):
        app, _a, _f = make_app([plain(1), agent(1, "t:1")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn("t", dict(app.hints()))
            await pilot.press("t")
            await pilot.pause(0.2)
            self.assertIsNone(modal(app))
            self.assertTrue(any("already running" in t for t in toasts(app)))

    async def test_a_finished_card_allows_another_task(self):
        app, _a, _f = make_app([plain(1), agent(
            1, "t:1", state="completed", bucket="done", link=REPORT)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("t")
            self.assertTrue(await until(pilot, lambda: modal(app)))
            self.assertEqual(modal(app).form.name,
                             "check-the-rollout-plan-1")      # scout-1 is not it

    async def test_the_row_menu_goes_to_the_task(self):
        app, _a, _f = make_app([plain(1), agent(1, "t:1")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entry = next(e for e in app.row_menu_entries() if e.key == ">")
            self.assertEqual(entry.label, "Go to task")
            await pilot.press("enter")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, RowMenu)))
            await pilot.press(">")
            self.assertTrue(await until(pilot, lambda: app.view == "agents"))
            self.assertEqual(app.current_key, "ag:1")
            self.assertEqual(app.current.key, "ag:1")

    async def test_no_go_to_task_without_a_card(self):
        app, _a, _f = make_app([plain(1), agent(1, "someone-else")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertNotIn(">", [e.key for e in app.row_menu_entries()])
            app.action_go_to_task()
            await pilot.pause(0.2)
            self.assertTrue(any("No task was started" in t
                                for t in toasts(app)))
            self.assertEqual(app.view, "inbox")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class OpenReportTest(unittest.IsolatedAsyncioTestCase):
    def opens(self, acts):
        return [c[1] for c in acts.calls if c[0] == "open"]

    async def test_o_opens_the_report_of_a_finished_task(self):
        app, acts, _f = make_app([plain(1), agent(
            1, "t:1", state="completed", bucket="done", link=REPORT)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("o", "open"), app.hints())
            await pilot.press("o")
            self.assertTrue(await until(pilot, lambda: self.opens(acts)))
            self.assertEqual(self.opens(acts), ["ag:1"])    # the card's link

    async def test_the_menu_names_it_and_keeps_the_source_link(self):
        app, acts, _f = make_app([plain(1), agent(
            1, "t:1", state="completed", bucket="done", link=REPORT)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entries = {e.key: e.label for e in app.row_menu_entries()}
            self.assertEqual(entries["o"], "Open task report")
            self.assertEqual(entries["O"], "Open the source link")
            await pilot.press("enter")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, RowMenu)))
            await pilot.press("O")
            self.assertTrue(await until(pilot, lambda: self.opens(acts)))
            self.assertEqual(self.opens(acts), ["t:1"])

    async def test_o_opens_the_row_while_the_task_runs(self):
        app, acts, _f = make_app([plain(1), agent(
            1, "t:1", link=REPORT)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            entries = {e.key: e.label for e in app.row_menu_entries()}
            self.assertEqual(entries["o"], "Open link")
            self.assertNotIn("O", entries)
            await pilot.press("o")
            self.assertTrue(await until(pilot, lambda: self.opens(acts)))
            self.assertEqual(self.opens(acts), ["t:1"])

    async def test_a_finished_task_without_a_report_link_opens_the_row(self):
        app, acts, _f = make_app([plain(1), agent(
            1, "t:1", state="completed", bucket="done", link=None)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("o")
            self.assertTrue(await until(pilot, lambda: self.opens(acts)))
            self.assertEqual(self.opens(acts), ["t:1"])


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class QuitTest(unittest.IsolatedAsyncioTestCase):
    async def test_quitting_in_the_undo_window_launches_nothing(self):
        app, acts, _f = make_app([plain()], undo_s=5.0)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("t")
            self.assertTrue(await until(pilot, lambda: modal(app)))
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: app.pending))
            await app.action_quit()
        self.assertEqual(acts.launches, [])
        self.assertTrue(threading.active_count() >= 1)


if __name__ == "__main__":
    unittest.main()
