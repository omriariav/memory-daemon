"""Inbox 0.13.0, "simple triage": the capped footer, the row menu (Enter), tab
for the detail pane, the palette's "This item" group and the verbs-first help.

Synthetic data only. The pure part (filtering, key matching) needs no
Textual; the pilot classes skip without it."""
import importlib.util
import dataclasses
import time
import unittest
from unittest import mock

from workspace_daemon.inbox import items as itm, rowmenu, sources

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox import __version__
    from workspace_daemon.inbox.actions import FakeActions, safe_link
    from workspace_daemon.inbox.app import InboxApp, InboxCommands
    from workspace_daemon.inbox.screens import HelpScreen, RowMenu
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import ITEM_PREFIX, Queue
    from textual.command import CommandPalette
    from textual.widgets import Input

try:    # run by discovery (tests/ on sys.path) or by dotted module name
    import inbox_acts_support as support
    from test_inbox_draft import wait_for
except ImportError:
    from tests import inbox_acts_support as support
    from tests.test_inbox_draft import wait_for

SIZE = (150, 50)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
TODAY_ACTS = ("close", "snooze", "mute", "waits_on", "relay")
TASK_ACTS = ("close", "drop", "due")
REPLY_TO = {"channel": "C0EXAMPLE01", "thread_ts": "1893492000.000100",
            "message_ts": "1893492060.000200"}


def row(rid, section="fyi", source="other", actions=("ack",), **kw):
    base = dict(
        id=rid, source=source, section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender="Person " + rid, where="#room", gist="gist " + rid,
        ts=time.time() - 120, link="https://example.com/" + rid.replace(":", "-"),
        thread=[], draft=None, actions=list(actions), meta={})
    base.update(kw)
    return itm.make_item(**base)


def rows():
    """One synthetic row per kind. `cmd:` rows come from a command source
    that lists acts; the others have none."""
    return [
        row("cmd:ask", "needs_reply", "slack", ("reply", "open"),
            draft="Sure, tomorrow.", meta={"reply_to": REPLY_TO}),
        row("cmd:nodraft", "needs_reply", "slack", ("reply", "open")),
        row("cmd:wait", "waiting", "slack", ("nudge", "open"),
            draft="Any update?"),
        row("invites:evt1", "needs_reply", "calendar", ("ack",),
            meta={"kind": "invite", "event_id": "evt1",
                  "calendar_id": "primary"}),
        row("cmd:fyi", "fyi", "other", (), link=None),
        row("cmd:ack", "fyi", "calendar", ("ack", "open")),
        row("alert:disk", "fyi", "alert", ("ack",)),
        row("events:rev1", "fyi", "memory", ("ack",),
            meta={"kind": "duplicate"}),
        itm.parse_item(support.task_item(1, link="https://example.com/t1")),
        itm.parse_item(support.task_item(2, "decision")),
    ]


KIND_ROWS = {                       # kind name -> (item key, view)
    "ask": ("cmd:ask", "inbox"), "nodraft": ("cmd:nodraft", "inbox"),
    "waiting": ("cmd:wait", "inbox"),
    "rsvp": ("invites:evt1", "inbox"), "fyi": ("cmd:fyi", "feed"),
    "ack": ("cmd:ack", "feed"), "alert": ("alert:disk", "feed"),
    "review": ("events:rev1", "inbox"), "todo": ("task:1", "tasks"),
    "decision": ("task:2", "tasks"),
}

# The menu of each kind with acts on, rsvp, react, polish, draft-for-me, save,
# context, summary and one custom action (key p) all configured.
COMMON = {"y", "Y", "m", "c", "i", "I"}   # C only while the pane is open
EXPECTED = {
    # a draft the source wrote: `D` would refuse to overwrite it
    "ask": COMMON | {"a", "e", "d", "s", "P", "E", "w", "!", "x", "o", "p"},
    # no draft: nothing to send, polish or react with; `D` can write one
    "nodraft": COMMON | {"e", "d", "s", "D", "w", "!", "x", "o", "p"},
    "waiting": COMMON | {"a", "n", "e", "d", "s", "P", "w", "!", "x", "o", "p"},
    "rsvp": COMMON | {"a", "X", "T", "d", "s", "o", "p"},
    "fyi": COMMON | {"d", "s", "x", "-", "M", "w", "!", "p"},
    "ack": COMMON | {"a", "d", "s", "x", "-", "M", "w", "!", "o", "p"},
    "alert": COMMON | {"a", "d", "s", "x", "-", "M", "R", "o", "p"},
    "review": {"d", "s", "D", "o", "y", "Y"},
    "todo": {"d", "D", "s", "x", "m", "o", "y", "Y", "c", "i", "I"},
    "decision": {"d", "s", "x", "m", "y", "Y", "c", "i", "I"},
}
FOOTER_A = {"ask": "send", "nodraft": None, "waiting": "nudge", "rsvp": "accept",
            "ack": "ack", "alert": "ack", "fyi": None, "review": None,
            "todo": None, "decision": None}


if HAVE_TEXTUAL:
    class MenuActions(FakeActions):
        """FakeActions with the optional parts switched on."""
        react_configured = True
        polish_configured = True
        draft_me_configured = True
        acts_configured = True
        action_delay = 0

        def act_names(self, item):
            if not item.key.startswith(("cmd:", "task:")):
                return ()
            return TASK_ACTS if item.meta.get("kind") == "task" else TODAY_ACTS

        def act_enabled(self, item, action):
            return action in self.act_names(item)

        def react_plan(self, item):
            return item.meta.get("reply_to")

        def open_target(self, item):
            return safe_link(item.link)


    class Fixed:
        def __init__(self, found):
            self.found = found

        def __call__(self):
            return [sources.SourceResult("cmd", self.found, False, None, None)]


    def make_app(view="inbox", actions=None, undo_s=30.0, found=None, **kw):
        store = InboxStore(None)
        actions = actions or MenuActions(store=store, draft_delay=0)
        return InboxApp(Fixed(rows() if found is None else found), store,
                        actions, settings=dict(DEFAULTS), undo_s=undo_s,
                        leave_s=0, stream_delay=0, view=view, **kw,
                        today=lambda: __import__("datetime").date(2030, 1, 10))


    async def settle(pilot, app):
        await wait_for(pilot, lambda: app.polled_once and app.current_key
                       is not None, tries=200, step=0.02)
        await pilot.pause(0.2)


    async def go(pilot, app, key):
        """Select the row `key` (every row is in the app's Feed and Tasks
        views too: the view changes first when the row is not in this one)."""
        if all(i.key != key for i in app.visible()):
            for name in ("inbox", "waiting", "tasks", "feed"):
                app.set_view(name)
                await pilot.pause(0.05)
                if any(i.key == key for i in app.visible()):
                    break
        q = app.query_one("#queue", Queue)
        q.highlighted = q.get_option_index(ITEM_PREFIX + key)
        assert await wait_for(pilot, lambda: app.current_key == key), key
        q.focus()
        await pilot.pause(0.05)


    async def open_menu(pilot, app):
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: isinstance(app.screen, RowMenu))
        await pilot.pause(0.05)
        return app.screen


    def menu_keys(menu):
        return {k for e in menu.entries for k in e.keys}


    async def press_text(pilot, text):
        for ch in text:
            await pilot.press("space" if ch == " " else ch)


class PureTest(unittest.TestCase):
    def entries(self):
        run = lambda: None  # noqa: E731
        return [rowmenu.MenuEntry("d", "Done", rowmenu.G_ACT, run),
                rowmenu.MenuEntry("a", "Nudge…", rowmenu.G_ACT, run,
                                  also=("n",), detail="confirm first"),
                rowmenu.MenuEntry("p", "Add to agenda", rowmenu.G_CUSTOM, run)]

    def test_filter_matches_label_key_group_and_detail(self):
        es = self.entries()
        self.assertEqual([e.key for e in rowmenu.filter_entries(es, "")], list("dap"))
        self.assertEqual([e.key for e in rowmenu.filter_entries(es, "AGEN")], ["p"])
        self.assertEqual([e.key for e in rowmenu.filter_entries(es, "confirm")], ["a"])
        self.assertEqual([e.key for e in rowmenu.filter_entries(es, "custom")], ["p"])
        self.assertEqual(rowmenu.filter_entries(es, "nudge zzz"), [])

    def test_match_key_is_case_sensitive_and_knows_aliases(self):
        es = self.entries()
        self.assertEqual(rowmenu.match_key(es, "n").key, "a")
        self.assertEqual(rowmenu.match_key(es, "a").shown, "a / n")
        self.assertIsNone(rowmenu.match_key(es, "D"))
        self.assertIsNone(rowmenu.match_key(es, None))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class FooterTest(unittest.IsolatedAsyncioTestCase):
    async def test_at_most_seven_hints_and_the_a_label_follows_the_kind(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for kind, (key, _view) in KIND_ROWS.items():
                await go(pilot, app, key)
                hints = app.hints()
                self.assertLessEqual(len(hints), 7, (kind, hints))
                self.assertEqual(hints[-2:], [("⏎", "more"), ("?", "help")],
                                 kind)
                self.assertEqual(dict(hints).get("a"), FOOTER_A[kind], kind)
                order = [k for k, _l in hints]
                ranked = [("a", "d", "s", "o", "⏎", "?").index(k) for k in order]
                self.assertEqual(ranked, sorted(ranked), (kind, order))

    async def test_a_plain_row_has_d_s_and_o_only_with_a_link(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:fyi")             # no link
            self.assertEqual(app.hints(), [("d", "done"), ("s", "later"),
                                           ("⏎", "more"), ("?", "help")])
            await go(pilot, app, "cmd:ack")             # a link
            self.assertIn(("o", "open"), app.hints())

    async def test_a_task_shows_d_and_s_only_when_the_source_lists_them(self):
        store = InboxStore(None)
        actions = MenuActions(store=store, draft_delay=0)
        actions.act_names = lambda item: ()
        app = make_app(actions=actions)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "task:2")
            self.assertEqual(app.hints(), [("⏎", "more"), ("?", "help")])

    async def test_other_keys_are_not_in_the_footer_any_more(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            shown = {k for k, _l in app.hints()}
            for gone in ("p", "c", "i", "v", "E", "P", "w", "!", "D", "m",
                         "y", "e", "1-5", "[ ]", "j/k", "A", "S"):
                self.assertNotIn(gone, shown)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class MenuContentTest(unittest.IsolatedAsyncioTestCase):
    async def test_enter_lists_only_the_valid_actions_of_each_kind(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for kind, (key, _view) in KIND_ROWS.items():
                await go(pilot, app, key)
                menu = await open_menu(pilot, app)
                self.assertEqual(menu_keys(menu), EXPECTED[kind], kind)
                await pilot.press("escape")
                self.assertTrue(await wait_for(
                    pilot, lambda: not isinstance(app.screen, RowMenu)))

    async def test_overloaded_keys_get_distinct_labels(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            labels = {}
            for kind in ("nodraft", "todo", "review"):
                await go(pilot, app, KIND_ROWS[kind][0])
                menu = await open_menu(pilot, app)
                labels[kind] = {e.key: e.label for e in menu.entries}
                await pilot.press("escape")
                await wait_for(pilot, lambda: not isinstance(app.screen, RowMenu))
            self.assertEqual(labels["nodraft"]["D"], "Draft for me")
            self.assertEqual(labels["todo"]["D"], "Done with outcome…")
            self.assertIn("save hint", labels["review"]["D"])
            self.assertEqual(len({labels[k]["D"] for k in labels}), 3)

    async def test_the_nudge_entry_shows_both_its_keys(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:wait")
            menu = await open_menu(pilot, app)
            entry = next(e for e in menu.entries if e.key == "a")
            self.assertEqual(entry.shown, "a / n")
            self.assertEqual(entry.label, "Nudge…")

    async def test_the_custom_action_is_listed_with_its_key_and_label(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            menu = await open_menu(pilot, app)
            entry = next(e for e in menu.entries if e.key == "p")
            self.assertEqual(entry.label, "Add to agenda")
            self.assertEqual(entry.group, rowmenu.G_CUSTOM)

    async def test_nothing_unconfigured_is_listed(self):
        store = InboxStore(None)
        actions = FakeActions(store=store, draft_delay=0)   # no acts, react...
        actions.custom_actions = []
        actions.save_configured = False
        actions.context_configured = False
        actions.summary_configured = False
        app = make_app(actions=actions)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            menu = await open_menu(pilot, app)
            self.assertEqual(menu_keys(menu), {"a", "e", "d", "s", "o", "y", "Y"})

    async def test_undo_is_listed_while_something_waits_out_its_window(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:fyi")
            self.assertNotIn("u", {e.key for e in app.row_menu_entries()})
            await pilot.press("d")
            self.assertTrue(await wait_for(pilot, lambda: app.pending))
            await go(pilot, app, "cmd:ack")
            self.assertIn("u", {e.key for e in app.row_menu_entries()})


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class MenuRunsTheSameActionTest(unittest.IsolatedAsyncioTestCase):
    """The menu key and the old key end in the same state."""

    @staticmethod
    def hook(app):
        """Record every toast the app raises (the app's own registry drops
        them when they time out, which would shift an index)."""
        log, original = [], app.notify

        def notify(message, *args, **kw):
            log.append(str(message))
            return original(message, *args, **kw)
        app.notify = notify
        app.toast_log = log
        return log

    def snap(self, app, since):
        return (type(app.screen).__name__,
                sorted((p.kind, p.item.key) for p in app.pending if not p.done),
                app.toast_log[since:], app.editing)

    async def clean(self, pilot, app):
        for _ in range(3):
            if app.editing or type(app.screen).__name__ != "Screen":
                await pilot.press("escape")
                await pilot.pause(0.1)

    async def both(self, pilot, app, key, row_key):
        await go(pilot, app, row_key)
        since = len(app.toast_log)
        await pilot.press(key)
        await pilot.pause(0.6)
        alias = self.snap(app, since)
        await self.clean(pilot, app)
        await go(pilot, app, row_key)
        since = len(app.toast_log)
        await open_menu(pilot, app)
        await pilot.press(key)
        await pilot.pause(0.6)
        menu = self.snap(app, since)
        await self.clean(pilot, app)
        return alias, menu

    async def test_keys_that_open_a_modal_or_a_toast(self):
        app = make_app()
        self.hook(app)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key, row_key, screen in (
                    ("a", "cmd:ask", "ConfirmSend"), ("s", "cmd:ask", "SnoozePicker"),
                    ("!", "cmd:ask", "NoteScreen"), ("w", "cmd:ask", "ChoicePicker"),
                    ("x", "cmd:ask", "ChoicePicker"), ("p", "cmd:ask", "ChoicePicker"),
                    ("m", "cmd:ask", "ConfirmWrite"), ("y", "cmd:ask", "Screen"),
                    ("o", "cmd:ask", "Screen"), ("a", "invites:evt1", "ConfirmWrite"),
                    ("X", "invites:evt1", "ConfirmWrite"),
                    ("T", "invites:evt1", "ConfirmWrite"),
                    ("s", "task:1", "DateScreen"), ("m", "task:1", "Screen"),
                    ("x", "task:1", "NoteScreen"),
                    ("D", "task:1", "NoteScreen")):
                alias, menu = await self.both(pilot, app, key, row_key)
                self.assertEqual(alias, menu, (key, row_key))
                if screen != "DateScreen":     # the due picker is a ChoicePicker
                    self.assertEqual(alias[0], screen, (key, row_key, alias))

    async def test_edit_enters_the_editor_from_the_menu(self):
        app = make_app()
        self.hook(app)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            alias, menu = await self.both(pilot, app, "e", "cmd:ask")
            self.assertTrue(alias[3])
            self.assertEqual(alias, menu)

    async def test_done_queues_the_same_pending_write(self):
        for row_key, view in (("cmd:fyi", "inbox"), ("cmd:ask", "inbox"),
                              ("task:2", "tasks")):
            results = []
            for via_menu in (False, True):
                app = make_app()
                async with app.run_test(size=SIZE) as pilot:
                    await settle(pilot, app)
                    await go(pilot, app, row_key)
                    if via_menu:
                        await open_menu(pilot, app)
                    await pilot.press("d")
                    await pilot.pause(0.5)
                    results.append((type(app.screen).__name__,
                                    [(p.kind, p.item.key, p.act)
                                     for p in app.pending]))
            self.assertEqual(results[0], results[1], row_key)

    async def test_a_todo_done_runs_the_same_close(self):
        results = []
        for via_menu in (False, True):
            app = make_app()
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await go(pilot, app, "task:1")
                if via_menu:
                    await open_menu(pilot, app)
                await pilot.press("d")
                await pilot.pause(0.5)
                results.append([(p.kind, p.item.key, p.act) for p in app.pending])
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0][:2], ("done", "task:1"))

    async def test_copy_puts_the_same_text_on_the_clipboard(self):
        texts = []
        for via_menu in (False, True):
            app = make_app()
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await go(pilot, app, "cmd:ask")
                with mock.patch.object(app, "copy_to_clipboard") as clip:
                    if via_menu:
                        await open_menu(pilot, app)
                    await pilot.press("y")
                    await pilot.pause(0.2)
                texts.append([c.args[0] for c in clip.call_args_list])
        self.assertEqual(len(texts[0]), 1)
        self.assertEqual(texts[0], texts[1])

    async def test_a_custom_action_runs_the_same_lookup(self):
        calls = []
        for via_menu in (False, True):
            store = InboxStore(None)
            actions = MenuActions(store=store, draft_delay=0)
            app = make_app(actions=actions)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await go(pilot, app, "cmd:ask")
                if via_menu:
                    await open_menu(pilot, app)
                await pilot.press("p")
                await wait_for(pilot, lambda: type(app.screen).__name__
                               == "ChoicePicker")
                calls.append(list(actions.calls))
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][0][:2], ("choices", "Add to agenda"))

    async def test_an_entry_that_the_selection_left_does_not_run(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            await open_menu(pilot, app)
            app.current_key = "cmd:fyi"        # the row moved under the menu
            await pilot.press("d")
            await pilot.pause(0.3)
            self.assertEqual(app.pending, [])
            self.assertIn("selection changed", " ".join(
                n.message for n in app._notifications))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class OnlyWhatWorksTest(unittest.IsolatedAsyncioTestCase):
    """An entry is listed only when its action would not refuse the row."""

    def keys(self, app, it=None):
        return {e.key for e in app.row_menu_entries(it)}

    def row_of(self, app, key):
        return next(i for i in app.items if i.key == key)

    async def test_without_send_support_neither_a_nor_edit_is_listed(self):
        store = InboxStore(None)
        actions = MenuActions(store=store, draft_delay=0)
        actions.supports_send = False
        app = make_app(actions=actions)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            keys = self.keys(app)
            self.assertFalse(keys & {"a", "e"})
            self.assertNotIn("a", dict(app.hints()))
            await go(pilot, app, "cmd:ack")             # an ack needs no send
            self.assertIn("a", self.keys(app))

    async def test_draft_for_me_never_overwrites_a_draft_it_did_not_write(self):
        store = InboxStore(None)
        actions = MenuActions(store=store, draft_delay=0)
        actions.cached_draft_me = lambda it: None
        app = make_app(actions=actions)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")              # the source's own draft
            self.assertNotIn("D", self.keys(app))
            actions.cached_draft_me = lambda it: it.draft  # `D` wrote this one
            self.assertIn("D", self.keys(app))
            await go(pilot, app, "cmd:nodraft")          # no draft: `D` writes one
            actions.cached_draft_me = lambda it: None
            self.assertIn("D", self.keys(app))
            app._draft_running.add("cmd:nodraft")        # one is being written
            self.assertNotIn("D", self.keys(app))

    async def test_a_nudge_needs_a_draft_or_the_template_fallback(self):
        store = InboxStore(None)
        actions = MenuActions(store=store, draft_delay=0)
        found = [row("cmd:wait2", "waiting", "slack", ("nudge", "open"))]
        app = make_app(actions=actions, found=found, view="waiting")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:wait2")
            self.assertFalse(self.keys(app) & {"a", "n"})   # no draft, no template
            self.assertNotIn("a", dict(app.hints()))
            actions.act_nudge_text = lambda it: "Any news on this?"
            keys = {k for e in app.row_menu_entries() for k in e.keys}
            self.assertTrue({"a", "n"} <= keys)
            self.assertEqual(dict(app.hints())["a"], "nudge")
            actions.act_nudge_text = lambda it: "  "
            self.assertFalse(self.keys(app) & {"a", "n"})

    async def test_send_needs_a_non_blank_draft(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            it = self.row_of(app, "cmd:ask")
            self.assertEqual(rowmenu.approve_label(app, it), "send")
            for blank in ("", "   \n"):
                bare = dataclasses.replace(it, draft=blank)
                self.assertIsNone(rowmenu.approve_label(app, bare))
                self.assertNotIn("a", self.keys(app, bare))
            # and `a` really does refuse it
            await go(pilot, app, "cmd:nodraft")
            await pilot.press("a")
            await pilot.pause(0.2)
            self.assertIn("No draft to send",
                          " ".join(n.message for n in app._notifications))

    async def test_o_follows_what_open_would_open(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            ask = self.row_of(app, "cmd:ask")
            self.assertTrue(rowmenu.can_open(app, ask))
            for link in (None, "ftp://example.com/x", "javascript:alert(1)",
                         "https://a b"):
                self.assertFalse(rowmenu.can_open(
                    app, dataclasses.replace(ask, link=link)), link)
            task = self.row_of(app, "task:2")             # no link
            self.assertFalse(rowmenu.can_open(app, task))
            # even when a Slack open template would give a URL, a task
            # without a link says "no link"
            app.actions.open_target = lambda it: "https://example.com/t"
            self.assertFalse(rowmenu.can_open(app, task))
            self.assertTrue(rowmenu.can_open(app, ask))
            await go(pilot, app, "task:2")
            await pilot.press("o")
            await pilot.pause(0.2)
            self.assertIn("no link", " ".join(
                n.message for n in app._notifications))

    async def test_the_demo_backend_opens_item_link_only_when_valid(self):
        store = InboxStore(None)
        app = make_app(actions=FakeActions(store=store, draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            ask = self.row_of(app, "cmd:ask")
            self.assertTrue(rowmenu.can_open(app, ask))
            self.assertFalse(rowmenu.can_open(
                app, dataclasses.replace(ask, link="ftp://example.com/x")))

    async def test_refresh_context_only_while_the_pane_is_open(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            self.assertNotIn("C", self.keys(app))
            app.context_open = True
            self.assertIn("C", self.keys(app))
            app.context_open = False

    async def test_feed_rules_are_listed_on_a_saved_row(self):
        saved = [{"id": "s1", "source": "slack", "kind": "new",
                  "ts": time.time() - 600, "title": "A saved thread",
                  "link": None, "source_id": "slack:s1"}]
        app = make_app(view="feed", saved=lambda: (saved, None), saved_days=2)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            self.assertTrue(await wait_for(
                pilot, lambda: (app.current_key or "").startswith("saved:")))
            self.assertEqual(self.keys(app), {"d", "s", "y", "Y", "M"})
            await open_menu(pilot, app)
            await pilot.press("M")                      # it works there
            self.assertTrue(await wait_for(
                pilot, lambda: type(app.screen).__name__ == "RulesScreen"))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class MenuIndexTest(unittest.IsolatedAsyncioTestCase):
    async def test_equal_entries_keep_their_own_index(self):
        # two entries that compare equal must not both map to the first
        run = lambda: None  # noqa: E731
        twin = [rowmenu.MenuEntry("d", "Done", rowmenu.G_ACT, run),
                rowmenu.MenuEntry("d", "Done", rowmenu.G_ACT, run)]
        self.assertEqual(twin[0], twin[1])
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            app.push_screen(RowMenu("x", twin))
            self.assertTrue(await wait_for(pilot, lambda: isinstance(app.screen, RowMenu)))
            lst = app.screen.query_one("#rowmenu-list")
            ids = [o.id for o in lst.options if o.id is not None]
            self.assertEqual(ids, ["0", "1"])


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class MenuKeysTest(unittest.IsolatedAsyncioTestCase):
    async def test_slash_filters_and_esc_clears_then_closes(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            menu = await open_menu(pilot, app)
            all_n = len(menu.shown)
            await pilot.press("slash")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.focused, Input)))
            await press_text(pilot, "draft")
            self.assertTrue(await wait_for(pilot, lambda: len(menu.shown) < all_n))
            self.assertEqual({e.key for e in menu.shown}, {"e", "P"})
            self.assertEqual(app.pending, [])        # typing ran nothing
            await pilot.press("escape")              # clears the filter
            self.assertTrue(await wait_for(pilot, lambda: len(menu.shown) == all_n))
            self.assertIsInstance(app.screen, RowMenu)
            await pilot.press("escape")              # closes
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, RowMenu)))
            self.assertEqual(app.pending, [])

    async def test_enter_in_the_filter_then_enter_runs_the_first_match(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            menu = await open_menu(pilot, app)
            await pilot.press("slash")
            await press_text(pilot, "snooze")
            self.assertTrue(await wait_for(pilot, lambda: len(menu.shown) == 1))
            await pilot.press("enter")               # back to the list
            await pilot.press("enter")               # runs the one entry
            self.assertTrue(await wait_for(
                pilot, lambda: type(app.screen).__name__ == "SnoozePicker"))

    async def test_a_filter_with_no_match_runs_nothing_on_enter(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            menu = await open_menu(pilot, app)
            await pilot.press("slash")
            await press_text(pilot, "zzzz")
            self.assertTrue(await wait_for(pilot, lambda: not menu.shown))
            await pilot.press("enter")
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, RowMenu)
            self.assertEqual(app.pending, [])

    async def test_arrows_and_enter_run_the_highlighted_entry(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:fyi")
            menu = await open_menu(pilot, app)
            first = menu.shown[0]
            self.assertEqual(first.key, "d")
            await pilot.press("enter")
            self.assertTrue(await wait_for(pilot, lambda: app.pending))
            self.assertEqual(app.pending[0].kind, "done")

    async def test_esc_closes_without_running_anything(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            await open_menu(pilot, app)
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, RowMenu)))
            self.assertEqual(app.pending, [])

    async def test_enter_opens_the_menu_in_every_queue_view(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for view in ("inbox", "waiting", "tasks", "feed"):
                app.set_view(view)
                await pilot.pause(0.1)
                if app.current is None:
                    continue
                await open_menu(pilot, app)
                await pilot.press("escape")
                await wait_for(pilot, lambda: not isinstance(app.screen, RowMenu))

    async def test_no_row_no_menu(self):
        app = make_app(found=[])
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertFalse(isinstance(app.screen, RowMenu))


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class OldKeysTest(unittest.IsolatedAsyncioTestCase):
    """Every key bound before 0.13.0 still reaches its action."""

    async def test_every_binding_still_calls_its_action(self):
        app = make_app()
        seen = []
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for binding in InboxApp.BINDINGS:
                name = binding.action.split("(")[0]
                for key in binding.key.split(","):
                    if key in ("q", "down", "up"):
                        # q quits; the focused list takes the arrows itself
                        continue
                    with mock.patch.object(app, f"action_{name}") as fake:
                        await pilot.press(key)
                        await pilot.pause(0.02)
                    self.assertEqual(fake.call_count, 1, (key, name))
                    seen.append(key)
        self.assertGreaterEqual(len(seen), 45)

    async def test_the_reserved_letters_are_all_still_bound(self):
        from workspace_daemon.inbox import settings
        letters = set()
        for b in InboxApp.BINDINGS:
            for k in b.key.split(","):
                if len(k) == 1 and k.isalpha():
                    letters.add(k)
        self.assertEqual(letters, set(settings.RESERVED_KEYS))

    async def test_a_still_acknowledges_a_row_without_the_menu(self):
        # `a` on a row, with no menu: the same as before
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ack")
            await pilot.press("a")
            self.assertTrue(await wait_for(pilot, lambda: app.pending))
            self.assertEqual(app.pending[0].kind, "ack")

    async def test_no_nag_toast_when_an_old_key_is_used(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            await pilot.press("y")
            await pilot.pause(0.2)
            text = " ".join(n.message for n in app._notifications).lower()
            self.assertNotIn("menu", text)
            self.assertNotIn("enter", text)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class TabTest(unittest.IsolatedAsyncioTestCase):
    async def test_tab_moves_to_the_detail_pane_on_a_feed_row(self):
        app = make_app(view="feed")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ack")
            self.assertEqual(app.focused.id, "queue")
            await pilot.press("tab")
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused is not None and app.focused.id == "thread"))

    async def test_enter_on_a_feed_row_no_longer_moves_the_focus(self):
        app = make_app(view="feed")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ack")
            await open_menu(pilot, app)
            await pilot.press("escape")
            await wait_for(pilot, lambda: not isinstance(app.screen, RowMenu))
            self.assertEqual(app.focused.id, "queue")

    async def test_tab_cycles_on_the_inbox_and_comes_back(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            await pilot.press("tab")
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused.id == "thread"))
            await pilot.press("shift+tab")
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused.id == "queue"))

    async def test_tab_reaches_a_task_card(self):
        app = make_app(view="tasks")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "task:1")
            await pilot.press("tab")
            self.assertTrue(await wait_for(
                pilot, lambda: app.focused.id == "thread"))
            self.assertIn(("esc", "queue"), app.hints())


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class PaletteTest(unittest.IsolatedAsyncioTestCase):
    async def hits(self, app, query=""):
        provider = InboxCommands(app.screen_stack[0])
        out = []
        gen = provider.search(query) if query else provider.discover()
        async for hit in gen:
            out.append(hit)
        return out

    async def test_this_item_comes_first_then_the_rest(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            hits = await self.hits(app)
            texts = [h.text for h in hits]
            labels = [e.label for e in app.row_menu_entries()]
            n = len(labels)
            self.assertEqual(texts[:n], [f"This item {x}" for x in labels])
            self.assertTrue(all(not t.startswith("This item") for t in texts[n:]))
            self.assertGreater(len(texts) - n, 15)

    async def test_the_group_follows_the_selected_row(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "task:1")
            task = {h.text for h in await self.hits(app) if h.text.startswith("This item")}
            await go(pilot, app, "cmd:nodraft")
            ask = {h.text for h in await self.hits(app) if h.text.startswith("This item")}
            self.assertIn("This item Done with outcome…", task)
            self.assertNotIn("This item Done with outcome…", ask)
            self.assertIn("This item Draft for me", ask)

    async def test_settings_and_rare_toggles_stay_in_the_palette(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            texts = {h.text for h in await self.hits(app)}
            for title in ("Toggle Hebrew order", "Toggle thread order",
                          "Show audit log", "Copy section as Markdown",
                          "Show / hide resolved alerts", "Toggle dark/light theme",
                          "Row menu", "Keyboard help", "Poll now"):
                self.assertIn(title, texts)
            for key in ("b", "v", "L", "R", "ctrl+t"):
                self.assertTrue(any(b.key == key or key in b.key.split(",")
                                    for b in InboxApp.BINDINGS), key)

    async def test_no_command_is_listed_twice(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("cmd:ask", "cmd:nodraft", "cmd:wait", "invites:evt1",
                        "task:1", "alert:disk"):
                await go(pilot, app, key)
                hits = await self.hits(app)
                item = [h.text[len("This item "):] for h in hits
                        if h.text.startswith("This item")]
                rest = [h.text for h in hits if not h.text.startswith("This item")]
                self.assertFalse(set(item) & set(rest), (key, set(item) & set(rest)))
                covered = app.item_covered_titles()
                self.assertFalse(covered & set(rest), (key, covered & set(rest)))
                self.assertEqual(len(rest), len(set(rest)), key)
            # the lambda commands too: "Summary" stands for "Summarize item"
            await go(pilot, app, "cmd:ask")
            hits = await self.hits(app)
            rest = [h.text for h in hits if not h.text.startswith("This item")]
            self.assertIn("This item Summary", [h.text for h in hits])
            self.assertNotIn("Summarize item", rest)
            self.assertNotIn("Summarize item (refresh)", rest)

    async def test_commands_without_an_item_entry_stay_in_the_rest(self):
        app = make_app(found=[])
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            texts = [h.text for h in await self.hits(app)]
            for title in ("Summarize item", "Edit draft", "Mark done"):
                self.assertIn(title, texts)

    async def test_no_item_no_group(self):
        app = make_app(found=[])
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            texts = [h.text for h in await self.hits(app)]
            self.assertTrue(texts)
            self.assertFalse(any(t.startswith("This item") for t in texts))

    async def test_a_query_matches_the_group_and_the_rest(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            hits = await self.hits(app, "draft")
            texts = [h.text for h in hits]
            self.assertIn("Edit draft", texts)           # Edit draft is in both
            self.assertTrue(any(h.startswith("Edit") for h in texts))

    async def test_ctrl_p_opens_it_with_the_group_on_top(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await go(pilot, app, "cmd:ask")
            await pilot.press("ctrl+p")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, CommandPalette)))
            from textual.command import CommandList
            lst = app.screen.query_one(CommandList)
            self.assertTrue(await wait_for(pilot, lambda: lst.option_count > 10))
            first = lst.get_option_at_index(0).prompt
            self.assertIn("This item", str(first))
            await pilot.press("escape")


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class HelpTest(unittest.IsolatedAsyncioTestCase):
    async def body(self, app, pilot):
        await pilot.press("question_mark")
        assert await wait_for(pilot, lambda: isinstance(app.screen, HelpScreen))
        return str(app.screen.query_one("#help-body").render())

    async def test_help_opens_with_the_verbs_and_the_kind_table(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = await self.body(app, pilot)
            lines = [ln for ln in body.splitlines() if ln.strip()]
            self.assertEqual(lines[0].strip(), "The verbs")
            head = "\n".join(lines[:8])
            for verb in ("approve", "done", "later", "open", "more", "undo"):
                self.assertIn(verb, head)
            for kind in ("Slack ask", "Waiting", "RSVP", "Todo", "Decision",
                         "Agent card", "FYI", "Alert"):
                self.assertIn(kind, body)
            self.assertLess(body.index("What a, d and s do"),
                            body.index("Power keys (also in enter)"))
            self.assertLess(body.index("Power keys (also in enter)"),
                            body.index("Views and moving"))

    async def test_help_promises_an_outcome_on_a_slack_done_only_with_the_flag(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = await self.body(app, pilot)
            for word in ("Slack ask", "Waiting"):
                line = next(ln for ln in body.splitlines()
                            if ln.strip().startswith(word))
                self.assertIn("when the source has close_outcome", line)

    async def test_help_keeps_the_mouse_confirm_and_read_only_lines(self):
        app = make_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            body = await self.body(app, pilot)
            self.assertIn("select text in the thread or draft pane", body)
            self.assertIn("Draft in Slack d", body)
            self.assertIn("Send y", body)
            self.assertIn("Copy for Slack c", body)
            self.assertIn("read-only", body)
            self.assertIn("when the source supports it", body)

    async def test_help_still_scrolls_and_shows_the_version(self):
        app = make_app()
        async with app.run_test(size=(100, 24)) as pilot:
            await settle(pilot, app)
            await self.body(app, pilot)
            pane = app.screen.query_one("#help-scroll")
            self.assertTrue(await wait_for(pilot, lambda: pane.max_scroll_y > 0))
            before = pane.scroll_y
            await pilot.press("j")
            self.assertTrue(await wait_for(pilot, lambda: pane.scroll_y > before))
            self.assertEqual(__version__, "0.13.5")


if __name__ == "__main__":
    unittest.main()
