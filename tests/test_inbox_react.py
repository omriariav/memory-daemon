"""Slack reactions (inbox 0.6.0): the `react:` settings, emoji validation, the
pinned call and its outcomes, and the Textual flow (picker, confirm, undo,
quit), plus the key-map invariants for the three 0.6.0 keys.

The bridge is mocked: nothing here calls a real claude, Slack or the network.
The non-Textual classes run without Textual."""
import importlib.util
import threading
import time
import unittest
from pathlib import Path

from workspace_daemon import claude_bridge, claude_bridge_hook
from workspace_daemon.inbox import items as itm
from workspace_daemon.inbox import models, settings, sources, writes
from workspace_daemon.inbox.actions import LocalActions
from workspace_daemon.inbox.store import InboxStore

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import (ChoicePicker, ConfirmWrite,
                                                HelpScreen)

ROOT = Path(__file__).resolve().parent.parent
TOOL = "mcp__slack_connector__add_reaction"
REACT_BLOCK = {"tool": TOOL, "args": {"channel": "ch", "timestamp": "ts",
                                      "name": "nm"}}
CHANNEL, THREAD, MESSAGE = "C0EXAMPLE01", "1893492000.000100", "1893492060.000200"
SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def cfg(**over):
    return settings.validate({"react": dict(REACT_BLOCK, **over)})["react"]


def row(i=1, reply_to=None, meta=None, **kw):
    meta = dict(meta) if meta is not None else (
        {} if reply_to is False else {"reply_to": reply_to or {
            "channel": CHANNEL, "thread_ts": THREAD, "message_ts": MESSAGE}})
    base = dict(
        id=f"t:{i}", source="slack", section="needs_reply", urgency="digest",
        sender=f"Person {i}", where="#launch", gist=f"gist {i}",
        ts=time.time() - 60 * i, link=f"https://example.com/{i}",
        thread=[], draft="a draft", actions=["reply"], meta=meta)
    base.update(kw)
    return itm.make_item(**base)


def good_call(calls=None, text="Reaction added"):
    def call(prompt, tools, **kw):
        if calls is not None:
            calls.append((prompt, tools, kw))
        (name, pins), = tools.items()
        return [{"name": name, "input": dict(pins), "text": text,
                 "is_error": False}]
    return call


class ReactSettingsTest(unittest.TestCase):
    def problems(self, block):
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate({"react": block})
        return " | ".join(ctx.exception.problems)

    def test_off_by_default_and_the_defaults(self):
        self.assertIsNone(settings.default_settings()["react"])
        out = cfg()
        self.assertEqual(out["emojis"], ["eyes", "white_check_mark", "+1", "pray"])
        self.assertEqual((out["timeout_s"], out["model"], out["mark_done"]),
                         (60.0, "haiku", True))

    def test_emoji_names(self):
        self.assertEqual(cfg(emojis=["a", "b_c", "+1", "x-y", "9"])["emojis"],
                         ["a", "b_c", "+1", "x-y", "9"])
        self.assertEqual(cfg(emojis=["a" * 64])["emojis"], ["a" * 64])
        for bad in (["Eyes"], [":eyes:"], ["a b"], [""], ["a" * 65], [5], [None],
                    ["eyes\n"], ["é"], ["a::skin-tone-2"]):
            self.assertIn("react.emojis", self.problems(dict(
                REACT_BLOCK, emojis=bad)), repr(bad))
        for bad in ([], "eyes", {"a": 1}, None, [f"e{i}" for i in range(10)]):
            self.assertIn("react.emojis", self.problems(dict(
                REACT_BLOCK, emojis=bad)), repr(bad))
        self.assertIn("twice", self.problems(dict(REACT_BLOCK, emojis=["a", "a"])))

    def test_other_problems(self):
        good = dict(REACT_BLOCK)
        for bad, text in [
            ({**good, "tool": "Bash"}, "react.tool"),
            ({k: v for k, v in good.items() if k != "args"}, "react.args"),
            ({**good, "args": {"channel": "a", "timestamp": "b"}}, "react.args.name"),
            ({**good, "args": {"channel": "a", "timestamp": "a", "name": "n"}},
             "3 different"),
            ({**good, "timeout_s": 29}, "react.timeout_s"),
            ({**good, "timeout_s": 301}, "react.timeout_s"),
            ({**good, "mark_done": "yes"}, "react.mark_done"),
            ({**good, "model": ""}, "react.model"),
            ({**good, "bogus": 1}, "unknown key 'bogus'"),
            (["x"], "react must be a mapping"),
        ]:
            with self.subTest(bad=bad):
                self.assertIn(text, self.problems(bad))
        self.assertFalse(cfg(mark_done=False)["mark_done"])


class ReactTargetTest(unittest.TestCase):
    def test_message_ts_else_thread_ts(self):
        both = {"reply_to": {"channel": CHANNEL, "thread_ts": THREAD,
                             "message_ts": MESSAGE}}
        self.assertEqual(writes.react_target(both), (CHANNEL, MESSAGE))
        only_thread = {"reply_to": {"channel": CHANNEL, "thread_ts": THREAD}}
        self.assertEqual(writes.react_target(only_thread), (CHANNEL, THREAD))
        for meta in ({"reply_to": {"channel": CHANNEL}}, {},
                     {"reply_to": {"channel": CHANNEL, "message_ts": "bad"}},
                     {"reply_to": {"channel": "bad", "message_ts": MESSAGE}}):
            self.assertIsNone(writes.react_target(meta), repr(meta))

    def test_emoji_validation(self):
        for ok in ("eyes", "+1", "white_check_mark", "a-b", "x" * 64):
            self.assertTrue(writes.valid_emoji(ok), ok)
        for bad in ("", "Eyes", ":eyes:", "a b", "x" * 65, "eyes\n", None, 5,
                    "ey;es", "👍"):
            self.assertFalse(writes.valid_emoji(bad), repr(bad))


class RunReactTest(unittest.TestCase):
    def test_pins_are_exactly_channel_timestamp_and_name(self):
        calls = []
        out = writes.run_react(cfg(), "/base", CHANNEL, MESSAGE, "eyes",
                               call_tools=good_call(calls))
        self.assertTrue(out.ok)
        (prompt, tools, kw), = calls
        self.assertEqual(tools, {TOOL: {"ch": CHANNEL, "ts": MESSAGE, "nm": "eyes"}})
        self.assertEqual((kw["attempts"], kw["exact_args"]), (1, True))
        self.assertEqual(kw["timeout"], 60.0)
        for needed in (TOOL, CHANNEL, MESSAGE, "eyes"):
            self.assertIn(needed, prompt)

    def test_the_hook_denies_extra_arguments(self):
        pins = writes.react_pins(cfg(), CHANNEL, MESSAGE, "eyes")
        extra = {"tool_name": TOOL, "tool_input": dict(pins[TOOL], skin_tone=3)}
        self.assertFalse(claude_bridge_hook.decide(extra, pins, True)[0])
        other = {"tool_name": TOOL, "tool_input": dict(pins[TOOL], nm="pray")}
        self.assertFalse(claude_bridge_hook.decide(other, pins, True)[0])

    def test_invalid_targets_and_names_never_reach_the_bridge(self):
        called = []
        for args in (("bad", MESSAGE, "eyes"), (CHANNEL, "1.2", "eyes"),
                     (CHANNEL, MESSAGE, "Eyes"), (CHANNEL, MESSAGE, ":eyes:"),
                     (CHANNEL, None, "eyes"), (CHANNEL, MESSAGE, "")):
            out = writes.run_react(cfg(), "/b", *args,
                                   call_tools=lambda *a, **k: called.append(1))
            self.assertFalse(out.ok, args)
        self.assertEqual(called, [])

    def test_failure_and_timeout_messages_and_no_retry(self):
        n = []

        def timeout(*a, **k):
            n.append(1)
            raise claude_bridge.BridgeError("claude run timed out after 60.0s")
        out = writes.run_react(cfg(), "/b", CHANNEL, MESSAGE, "eyes",
                               call_tools=timeout)
        self.assertFalse(out.ok)
        self.assertIn("reaction may have been sent, check Slack", out.detail)
        self.assertEqual(len(n), 1)
        out = writes.run_react(
            cfg(), "/b", CHANNEL, MESSAGE, "eyes",
            call_tools=good_call(text='{"ok": false, "error": "already_reacted"}'))
        self.assertFalse(out.ok)
        self.assertIn("no reaction added", out.detail)
        out = writes.run_react(cfg(), "/b", CHANNEL, MESSAGE, "eyes",
                               call_tools=lambda *a, **k: [])
        self.assertFalse(out.ok)

    def test_spend_is_logged_under_the_react_tag(self):
        seen = []

        def call(prompt, tools, **kw):
            seen.append(claude_bridge.current_tag())
            return good_call()(prompt, tools, **kw)
        writes.run_react(cfg(), "/b", CHANNEL, MESSAGE, "eyes", call_tools=call)
        self.assertEqual(seen, ["inbox-react"])


class LocalActionsReactTest(unittest.TestCase):
    def setUp(self):
        self.store = InboxStore(None)
        self.item = models.from_v1(row())

    def actions(self, call=None, **kw):
        return LocalActions(self.store, react=cfg(**kw), base_dir="/base",
                            call_tools=call or good_call())

    def test_not_configured(self):
        a = LocalActions(self.store, base_dir="/b")
        self.assertFalse(a.react_configured)
        self.assertIn("not configured", a.react(self.item, "eyes").detail)

    def test_success_and_the_mark_done_flag(self):
        acts = self.actions()
        res = acts.react(self.item, "eyes")
        self.assertTrue(res.ok)
        self.assertIn(":eyes:", res.detail)
        self.assertTrue(acts.react_mark_done)
        self.assertFalse(self.actions(mark_done=False).react_mark_done)
        self.assertEqual(acts.react_emojis, ["eyes", "white_check_mark", "+1", "pray"])

    def test_only_a_configured_valid_emoji_on_a_valid_target(self):
        called = []
        acts = self.actions(lambda *a, **k: called.append(1))
        self.assertFalse(acts.react(self.item, "tada").ok)       # not configured
        self.assertFalse(acts.react(self.item, "Eyes").ok)
        bare = models.from_v1(row(2, reply_to=False))
        res = acts.react(bare, "eyes")
        self.assertFalse(res.ok)
        self.assertIn("message_ts or thread_ts", res.detail)
        self.assertEqual(called, [])


class SuggestedReactionTest(unittest.TestCase):
    def test_only_a_valid_emoji_name_counts(self):
        self.assertEqual(writes.suggested_reaction({"suggested_reaction": "tada"}),
                         "tada")
        for bad in ("Tada", ":tada:", "a b", "", "x" * 65, "tada\n", 5, None,
                    ["tada"]):
            self.assertIsNone(writes.suggested_reaction(
                {"suggested_reaction": bad}), repr(bad))
        self.assertIsNone(writes.suggested_reaction({}))
        self.assertIsNone(writes.suggested_reaction(None))

    def test_a_suggestion_outside_the_list_is_ignored_by_default(self):
        acts = LocalActions(InboxStore(None), react=cfg(), base_dir="/b",
                            call_tools=good_call())
        extra = models.from_v1(row(2, meta={
            "reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
            "suggested_reaction": "tada"}))
        self.assertEqual(acts.react_choices(extra),
                         (["eyes", "white_check_mark", "+1", "pray"], None))
        res = acts.react(extra, "tada")                    # and not allowed
        self.assertFalse(res.ok)
        self.assertIn("not one of the configured", res.detail)
        inlist = models.from_v1(row(1, meta={
            "reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
            "suggested_reaction": "pray"}))
        self.assertEqual(acts.react_choices(inlist)[1], "pray")  # in the list: kept

    def test_allow_suggested_is_an_opt_in_bool(self):
        self.assertFalse(cfg()["allow_suggested"])
        self.assertTrue(cfg(allow_suggested=True)["allow_suggested"])
        for bad in ("yes", 1, None):
            with self.assertRaises(settings.SettingsError) as ctx:
                settings.validate({"react": dict(REACT_BLOCK, allow_suggested=bad)})
            self.assertIn("react.allow_suggested", str(ctx.exception))

    def test_with_allow_suggested_the_suggestion_is_listed_once_and_allowed(self):
        acts = LocalActions(InboxStore(None), react=cfg(allow_suggested=True),
                            base_dir="/b", call_tools=good_call())
        inlist = models.from_v1(row(1, meta={
            "reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
            "suggested_reaction": "pray"}))
        emojis, picked = acts.react_choices(inlist)
        self.assertEqual((emojis, picked),
                         (["eyes", "white_check_mark", "+1", "pray"], "pray"))
        extra = models.from_v1(row(2, meta={
            "reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
            "suggested_reaction": "tada"}))
        emojis, picked = acts.react_choices(extra)
        self.assertEqual((emojis[0], len(emojis), picked), ("tada", 5, "tada"))
        self.assertTrue(acts.react(extra, "tada").ok)           # allowed for this item
        self.assertFalse(acts.react(inlist, "tada").ok)         # not for another
        bad = models.from_v1(row(3, meta={
            "reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
            "suggested_reaction": "Not Valid"}))
        self.assertEqual(acts.react_choices(bad),
                         (["eyes", "white_check_mark", "+1", "pray"], None))


# --------------------------------------------------------------- Textual ---

def worker_threads():
    return [t for t in threading.enumerate()
            if t.name in ("inbox-polish", "inbox-summary", "inbox-context-fetch",
                          "inbox-action-choices", "inbox-action-prepare",
                          "inbox-precheck")]


class WorkerCleanup:
    """A thread of the app under test must be gone when the test ends."""

    async def asyncTearDown(self):
        deadline = time.monotonic() + 15
        for t in worker_threads():
            t.join(max(0.1, deadline - time.monotonic()))
        self.assertEqual([t.name for t in worker_threads()], [],
                         "a worker thread outlived its test")


class Fixed:
    def __init__(self, rows):
        self.results = [sources.SourceResult("t", rows, False, None, None)]

    def __call__(self):
        return self.results


async def wait_for(pilot, cond, tries=120, step=0.05):
    for _ in range(tries):
        if cond():
            return True
        await pilot.pause(step)
    return cond()


async def settle(pilot, app):
    await wait_for(pilot, lambda: app.polled_once, tries=200, step=0.02)
    await pilot.pause(0.25)


def toasts(app):
    return [f"{n.title} {n.message}".strip() for n in app._notifications]


def keys(app):
    return [i.key for i in app.items]


def make_app(rows, actions, store, undo_s=0.15):
    return InboxApp(Fixed(rows), store, actions, settings=dict(DEFAULTS),
                    undo_s=undo_s, leave_s=0, stream_delay=0)


def real(store, call=None, **kw):
    return LocalActions(store, react=cfg(**kw), base_dir="/base",
                        call_tools=call or good_call())


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ReactFlowTest(WorkerCleanup, unittest.IsolatedAsyncioTestCase):
    async def to_confirm(self, pilot, app, pick="enter"):
        await settle(pilot, app)
        await pilot.press("E")
        self.assertTrue(await wait_for(
            pilot, lambda: isinstance(app.screen, ChoicePicker)))
        await pilot.press(pick)
        self.assertTrue(await wait_for(
            pilot, lambda: isinstance(app.screen, ConfirmWrite)))

    async def test_not_configured_says_so(self):
        store = InboxStore(None)
        app = make_app([row(1)], LocalActions(store), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, ChoicePicker)
            self.assertTrue(any("React not configured" in t for t in toasts(app)))
            self.assertNotIn("react", " ".join(label for _k, label in app.hints()))

    async def test_an_item_without_a_message_says_so(self):
        store = InboxStore(None)
        app = make_app([row(1, reply_to={"channel": CHANNEL})], real(store), store)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.1)
            self.assertNotIsInstance(app.screen, ChoicePicker)
            self.assertTrue(any("no message to react to" in t for t in toasts(app)))
            self.assertNotIn(("E", "react"), app.hints())

    async def test_the_picker_lists_the_configured_emojis_with_quick_keys(self):
        store = InboxStore(None)
        app = make_app([row(1)], real(store, emojis=["tada", "+1", "pray"]),
                       store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn(("E", "react"), app.hints())
            await pilot.press("E")
            await pilot.pause(0.2)
            picker = app.screen
            self.assertIsInstance(picker, ChoicePicker)
            self.assertEqual([c["id"] for c in picker.choices],
                             ["tada", "+1", "pray"])
            self.assertEqual([c["label"] for c in picker.choices],
                             [":tada:", ":+1:", ":pray:"])
            await pilot.press("2")                       # quick key
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, ConfirmWrite)
            self.assertEqual(dict(app.screen.rows)["Reaction"], ":+1:")

    async def test_a_suggested_reaction_is_preselected_but_needs_confirmation(self):
        store = InboxStore(None)
        calls = []
        meta = {"reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
                "suggested_reaction": "white_check_mark"}
        app = make_app([row(1, meta=meta)], real(store, good_call(calls)), store,
                       undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.2)
            picker = app.screen
            self.assertIsInstance(picker, ChoicePicker)
            lst = picker.query_one("#picker-list")
            self.assertEqual(picker.choices[lst.highlighted]["id"], "white_check_mark")
            self.assertEqual(
                [c["detail"] for c in picker.choices],
                ["", "suggested", "", ""])
            self.assertEqual(calls, [])                  # nothing yet
            await pilot.press("enter")                   # picks the highlighted one
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, ConfirmWrite)   # still must confirm
            self.assertEqual(dict(app.screen.rows)["Reaction"], ":white_check_mark:")
            self.assertEqual(calls, [])
            await pilot.pause(0.15)
            self.assertEqual(app.screen.focused.id, "cancel")
            await pilot.press("y")
            self.assertTrue(await wait_for(pilot, lambda: calls))
            self.assertEqual(calls[0][1][TOOL]["nm"], "white_check_mark")

    async def test_a_suggestion_outside_the_list_is_ignored_by_default(self):
        store = InboxStore(None)
        meta = {"reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
                "suggested_reaction": "tada"}
        app = make_app([row(1, meta=meta)], real(store), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.2)
            picker = app.screen
            self.assertEqual([c["id"] for c in picker.choices],
                             ["eyes", "white_check_mark", "+1", "pray"])
            self.assertIsNone(picker.selected)
            self.assertEqual({c["detail"] for c in picker.choices}, {""})
            await pilot.press("escape")

    async def test_with_allow_suggested_a_suggestion_outside_the_list_is_offered_first(self):
        store = InboxStore(None)
        meta = {"reply_to": {"channel": CHANNEL, "message_ts": MESSAGE},
                "suggested_reaction": "tada"}
        app = make_app([row(1, meta=meta)], real(store, allow_suggested=True),
                       store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.2)
            picker = app.screen
            self.assertEqual(picker.choices[0]["id"], "tada")
            self.assertEqual(picker.query_one("#picker-list").highlighted, 0)
            await pilot.press("escape")

    async def test_without_a_suggestion_nothing_is_marked(self):
        store = InboxStore(None)
        app = make_app([row(1)], real(store), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.2)
            self.assertEqual({c["detail"] for c in app.screen.choices}, {""})
            self.assertIsNone(app.screen.selected)
            await pilot.press("escape")

    async def test_esc_in_the_picker_sends_nothing(self):
        store = InboxStore(None)
        calls = []
        app = make_app([row(1)], real(store, good_call(calls)), store, undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("E")
            await pilot.pause(0.2)
            await pilot.press("escape")
            await pilot.pause(0.4)
            self.assertEqual((app.pending, calls), ([], []))
            self.assertNotIsInstance(app.screen, (ChoicePicker, ConfirmWrite))

    async def test_full_flow_confirm_undo_window_call_then_done(self):
        store = InboxStore(None)
        calls = []
        app = make_app([row(1), row(2)], real(store, good_call(calls)), store,
                       undo_s=0.4)
        async with app.run_test(size=SIZE) as pilot:
            await self.to_confirm(pilot, app, "1")
            modal = app.screen
            await pilot.pause(0.15)
            self.assertEqual(modal.focused.id, "cancel")        # Cancel default
            rows = dict(modal.rows)
            self.assertEqual((rows["Reaction"], rows["Time stamp"], rows["Where"]),
                             (":eyes:", MESSAGE, "#launch"))
            self.assertEqual(calls, [])
            await pilot.press("y")
            await pilot.pause(0.1)
            [p] = app.pending
            self.assertEqual((p.kind, p.choice, p.hide, p.mark_done),
                             ("react", "eyes", False, True))
            self.assertIn("t:1", keys(app))                      # not hidden yet
            self.assertEqual(calls, [])                          # undo window
            self.assertTrue(await wait_for(pilot, lambda: calls))
            (_p, tools, kw), = calls
            self.assertEqual(tools, {TOOL: {"ch": CHANNEL, "ts": MESSAGE,
                                            "nm": "eyes"}})
            self.assertEqual((kw["attempts"], kw["exact_args"]), (1, True))
            self.assertTrue(await wait_for(
                pilot, lambda: any(x.kind == "done" for x in app.pending)))
            self.assertTrue(await wait_for(pilot, lambda: store.is_hidden("t:1")))
            self.assertEqual(keys(app), ["t:2"])
            audit = {r["action"]: r for r in app.audit}
            self.assertEqual(audit["react"]["status"], "committed")
            self.assertEqual(audit["done"]["detail"], "after react")

    async def test_a_thread_only_item_reacts_to_the_thread_root(self):
        store = InboxStore(None)
        calls = []
        app = make_app([row(1, reply_to={"channel": CHANNEL, "thread_ts": THREAD})],
                       real(store, good_call(calls)), store, undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await self.to_confirm(pilot, app)
            await pilot.press("y")
            self.assertTrue(await wait_for(pilot, lambda: calls))
            self.assertEqual(calls[0][1][TOOL]["ts"], THREAD)

    async def test_mark_done_false_keeps_the_row(self):
        store = InboxStore(None)
        calls = []
        app = make_app([row(1)], real(store, good_call(calls), mark_done=False),
                       store, undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await self.to_confirm(pilot, app)
            await pilot.press("y")
            self.assertTrue(await wait_for(pilot, lambda: calls))
            await wait_for(pilot, lambda: any(
                "Reaction sent" in t for t in toasts(app)))
            await pilot.pause(0.6)
            self.assertIn("t:1", keys(app))
            self.assertFalse(store.is_hidden("t:1"))
            self.assertEqual([x for x in app.pending if x.kind == "done"], [])

    async def test_cancel_undo_and_quit_inside_the_window_send_nothing(self):
        for how in ("cancel", "undo", "quit"):
            store = InboxStore(None)
            calls = []
            app = make_app([row(1)], real(store, good_call(calls)), store,
                           undo_s=30)
            async with app.run_test(size=SIZE) as pilot:
                await self.to_confirm(pilot, app)
                if how == "cancel":
                    await pilot.press("escape")
                else:
                    await pilot.press("y")
                    await pilot.pause(0.1)
                    await pilot.press("u" if how == "undo" else "q")
                await pilot.pause(0.3)
                self.assertEqual(calls, [], how)
                self.assertEqual(bool(app._exit), how == "quit")
            self.assertFalse(store.is_hidden("t:1"))
            if how == "quit":
                self.assertEqual(
                    [r["status"] for r in app.audit if r["action"] == "react"],
                    ["cancelled on quit"])

    async def test_a_failed_react_keeps_the_row_and_never_retries(self):
        store = InboxStore(None)
        calls = []

        def boom(prompt, tools, **kw):
            calls.append(1)
            raise claude_bridge.BridgeError("tool returned an error: " + TOOL
                                            + " (auth failed)")
        app = make_app([row(1)], real(store, boom), store, undo_s=0.1)
        async with app.run_test(size=SIZE) as pilot:
            await self.to_confirm(pilot, app)
            await pilot.press("y")
            self.assertTrue(await wait_for(
                pilot, lambda: any("auth failed" in t for t in toasts(app))))
            await pilot.pause(0.5)
            self.assertEqual(len(calls), 1)
            self.assertIn("t:1", keys(app))
            self.assertEqual([x for x in app.pending if x.kind == "done"], [])
            self.assertEqual(next(r for r in app.audit
                                  if r["action"] == "react")["status"], "failed")

    async def test_a_running_react_counts_in_the_quit_guard_and_blocks_a_second(self):
        store = InboxStore(None)
        gate, started = threading.Event(), threading.Event()

        def slow(prompt, tools, **kw):
            started.set()
            gate.wait(10)
            return good_call()(prompt, tools, **kw)
        app = make_app([row(1)], real(store, slow), store, undo_s=0.15)
        try:
            async with app.run_test(size=SIZE) as pilot:
                await self.to_confirm(pilot, app)
                await pilot.press("y")
                self.assertTrue(await wait_for(pilot, started.is_set))
                await pilot.press("E")
                await pilot.pause(0.1)
                self.assertTrue(any("react already in progress" in t
                                    for t in toasts(app)))
                await pilot.press("q")
                await pilot.pause(0.2)
                self.assertFalse(app._exit)
                self.assertTrue(any("1 write(s) still running" in t
                                    for t in toasts(app)))
                gate.set()
                await wait_for(pilot, lambda: not app.running)
                await pilot.press("q")
                await pilot.pause(0.2)
                self.assertTrue(app._exit)
        finally:
            gate.set()

    async def test_footer_help_and_palette_list_the_new_keys(self):
        store = InboxStore(None)
        app = make_app([row(1)], real(store), store, undo_s=30)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            titles = {c.title for c in app.get_system_commands(app.screen)}
            self.assertIn("React to message", titles)
            self.assertIn("Polish draft", titles)
            await pilot.press("question_mark")
            await pilot.pause(0.1)
            self.assertIsInstance(app.screen, HelpScreen)
            body = str(app.screen.query_one("#help-body").render())
            self.assertIn("react to the message", body)
            self.assertIn("polish the draft", body)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class KeyMapInvariantTest(unittest.TestCase):
    """E (react) and P (polish) are now the inbox's own keys: reserved, bound
    once, not offered to `actions:`, and every earlier key unchanged."""

    def bound(self):
        out = []
        for b in InboxApp.BINDINGS:
            out += b.key.split(",")
        return out

    def test_new_keys_are_bound_once_to_their_actions(self):
        bound = self.bound()
        self.assertEqual(len(bound), len(set(bound)), "a key is bound twice")
        actions = {k: b.action for b in InboxApp.BINDINGS for k in b.key.split(",")}
        self.assertEqual((actions["E"], actions["P"]), ("react", "polish"))
        for action in ("react", "polish"):
            self.assertTrue(hasattr(InboxApp, f"action_{action}"), action)

    def test_reserved_keys_are_exactly_the_bound_letters(self):
        letters = {k for k in self.bound() if len(k) == 1 and k.isalpha()}
        self.assertEqual(letters, set(settings.RESERVED_KEYS))
        self.assertTrue({"E", "P"} <= settings.RESERVED_KEYS)
        self.assertFalse({"E", "P"} & set(settings.ACTION_KEY_CHOICES))

    def test_actions_cannot_take_the_new_keys(self):
        for key in "EP":
            with self.assertRaises(settings.SettingsError) as ctx:
                settings.validate({"actions": [{
                    "key": key, "label": "x", "argv": ["x"]}]})
            self.assertIn("already used by the inbox", str(ctx.exception))

    def test_the_free_keys_are_the_documented_ones(self):
        self.assertEqual("".join(settings.ACTION_KEY_CHOICES),
                         "BDFHJKMNOQUVWZhlptwxz")
        text = (ROOT / "docs" / "inbox.md").read_text()
        self.assertIn("`h l p t w x z`", text)
        self.assertIn("`B D F H J K M N O Q U V W Z`", text)
        self.assertNotIn("`B D E F H J K M N O P Q U V W Z`", text)

    def test_every_earlier_key_still_does_what_it_did(self):
        actions = {k: b.action for b in InboxApp.BINDINGS for k in b.key.split(",")}
        for key, action in {
                "a": "approve", "d": "done", "s": "snooze", "n": "nudge",
                "e": "edit", "m": "save_memory", "c": "context", "C": "context_refresh",
                "i": "summary(False)", "I": "summary(True)", "u": "undo",
                "q": "quit", "L": "audit", "X": "rsvp_decline",
                "T": "rsvp_tentative", "v": "toggle_thread_order",
                "y": "copy_item", "Y": "copy_section", "S": "saved",
                "A": "toggle_all", "R": "toggle_resolved"}.items():
            self.assertEqual(actions[key], action, key)

    def test_the_example_names_the_new_blocks_with_placeholder_tools(self):
        example = (ROOT / "inbox.example.yaml").read_text()
        for block in ("# send:", "# react:", "# polish:"):
            self.assertIn(block, example)
        self.assertNotIn("B D E F H J K M N O P Q U V W Z", example)


if __name__ == "__main__":
    unittest.main()
