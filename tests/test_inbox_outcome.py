"""`d` asks for an outcome (inbox 0.13.1): the `close_outcome:` source flag, the
`Outcome:` prompt on a Slack ask (I OWE) or a waiting row, the model pre-fill
(never over typed text), the `close` act that carries `{"outcome": <text>}`,
and the rows that keep the plain `d`. A fake command source records the
requests; the bridge is a stub, so nothing here calls a real claude, Slack or
the network. The non-Textual classes run without Textual; the pilot tests are
skipped when it is not installed. Synthetic data only."""
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_acts_support as support  # noqa: E402

from workspace_daemon import claude_bridge  # noqa: E402
from workspace_daemon.inbox import items as itm  # noqa: E402
from workspace_daemon.inbox import outcome, settings, sources  # noqa: E402
from workspace_daemon.inbox.actions import LocalActions  # noqa: E402
from workspace_daemon.inbox.models import from_v1  # noqa: E402
from workspace_daemon.inbox.store import InboxStore  # noqa: E402

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.screens import (NoteScreen, OutcomeScreen,
                                                RowMenu)

SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def item(i, section="needs_reply", source="slack", actions=("reply",),
         **kw):
    meta = kw.pop("meta", None)
    if meta is None:
        meta = {"last_seen_ts": time.time() - 30,
                "reply_to": {"channel": support.CHANNEL,
                             "thread_ts": support.THREAD,
                             "message_ts": support.MESSAGE}}
    base = dict(
        id=f"t:{i}", source=source, section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender=f"Person {i}", where="#launch", gist=f"gist {i}",
        ts=time.time() - 60 * i, link=f"https://example.com/{i}",
        thread=[{"who": "Ada Example", "ts": time.time() - 90,
                 "text": "Can you confirm the rollout date?"}],
        draft=None, actions=list(actions), meta=meta)
    base.update(kw)
    return itm.make_item(**base)


def waiting(i=1):
    return item(i, "waiting", actions=("nudge",))


def cfg_block(source, close_outcome=True, acts=None):
    cfg = support.acts_cfg(source, acts=acts)
    cfg["src"]["close_outcome"] = close_outcome
    return cfg


def summary_cfg():
    return settings.validate({"summary": {}})["summary"]


class SettingsTest(unittest.TestCase):
    def validate(self, **over):
        src = {"name": "chat", "type": "command", "argv": ["x"],
               "acts": ["close", "snooze"]}
        src.update(over)
        return settings.validate({"sources": [src]})

    def test_close_outcome_is_off_by_default(self):
        (src,) = self.validate()["sources"]
        self.assertNotIn("close_outcome", src)

    def test_close_outcome_true_is_kept(self):
        (src,) = self.validate(close_outcome=True)["sources"]
        self.assertIs(src["close_outcome"], True)

    def test_false_is_the_same_as_omitted(self):
        (src,) = self.validate(close_outcome=False)["sources"]
        self.assertNotIn("close_outcome", src)

    def test_it_must_be_a_boolean(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            self.validate(close_outcome="yes")
        self.assertIn("close_outcome must be true or false",
                      str(ctx.exception))

    def test_it_needs_close_in_acts(self):
        with self.assertRaises(settings.SettingsError) as ctx:
            self.validate(close_outcome=True, acts=["snooze"])
        self.assertIn("needs close", str(ctx.exception))
        with self.assertRaises(settings.SettingsError):
            self.validate(close_outcome=True, acts=[])


class OutcomeModuleTest(unittest.TestCase):
    CFG = {"model": "haiku", "timeout_s": 90.0, "max_budget_usd": 0.05}

    def v1(self):
        return item(1)

    def test_the_prompt_wraps_the_thread_in_random_markers(self):
        a, b = outcome.build_prompt(self.v1()), outcome.build_prompt(self.v1())
        self.assertIn("Can you confirm the rollout date?", a)
        self.assertIn("BEGIN THREAD DATA", a)
        self.assertNotEqual(a, b)          # a fresh token per call
        self.assertIn("never call a tool", a)

    def test_the_call_has_a_short_timeout_and_one_turn(self):
        seen = {}

        def complete(prompt, **kw):
            seen.update(kw)
            return "Agreed on Friday; Ada owns the notes."
        out = outcome.run_outcome(self.CFG, "/tmp/x", self.v1(), complete)
        self.assertTrue(out.ok)
        self.assertEqual(out.text, "Agreed on Friday; Ada owns the notes.")
        self.assertEqual(seen["timeout"], outcome.TIMEOUT_S)
        self.assertEqual(seen["max_turns"], 1)
        self.assertEqual(seen["model"], "haiku")

    def test_a_short_configured_timeout_is_kept(self):
        seen = {}
        outcome.run_outcome(dict(self.CFG, timeout_s=3.0), "/tmp/x",
                            self.v1(), lambda p, **kw: seen.update(kw) or "ok")
        self.assertEqual(seen["timeout"], 3.0)

    def test_the_answer_is_one_clean_line(self):
        out = outcome.run_outcome(
            self.CFG, "/tmp/x", self.v1(),
            lambda p, **kw: '  "Done.\nShipped \x1b[31mit\x1b[0m"  ')
        self.assertEqual(out.text, "Done. Shipped it")
        long = outcome.run_outcome(self.CFG, "/tmp/x", self.v1(),
                                   lambda p, **kw: "w" * 900)
        self.assertEqual(len(long.text), 300)

    def test_failures_become_an_outcome_never_a_raise(self):
        def bridge(prompt, **kw):
            raise claude_bridge.BridgeError("timed out")

        def broken(prompt, **kw):
            raise OSError("no claude")
        self.assertFalse(outcome.run_outcome(
            self.CFG, "/tmp/x", self.v1(), bridge).ok)
        self.assertEqual(outcome.run_outcome(
            self.CFG, "/tmp/x", self.v1(), broken).detail,
            "cannot run: OSError")
        self.assertFalse(outcome.run_outcome(
            self.CFG, "/tmp/x", self.v1(), lambda p, **kw: "  ").ok)


class CapabilityTest(unittest.TestCase):
    def setUp(self):
        self.src = support.FakeSource()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.tmp.cleanup)
        self.store = InboxStore(Path(self.tmp.name))

    def actions(self, **kw):
        return LocalActions(self.store, base_dir=Path(self.tmp.name), **kw)

    def test_the_flag_needs_acts_close_and_close_outcome(self):
        row = from_v1(item(1))
        row.origin = "src"
        self.assertTrue(self.actions(
            acts=cfg_block(self.src)).close_outcome_enabled(row))
        self.assertFalse(self.actions(
            acts=cfg_block(self.src, False)).close_outcome_enabled(row))
        self.assertFalse(self.actions(
            acts=support.acts_cfg(self.src)).close_outcome_enabled(row))
        self.assertFalse(self.actions(
            acts=cfg_block(self.src, True, acts=["snooze"])
        ).close_outcome_enabled(row))
        self.assertFalse(self.actions().close_outcome_enabled(row))

    def test_another_source_is_not_opted_in(self):
        row = from_v1(item(1))
        row.origin = "other"
        self.assertFalse(self.actions(
            acts=cfg_block(self.src)).close_outcome_enabled(row))

    def test_draft_outcome_needs_a_model(self):
        row = from_v1(item(1))
        out = self.actions().draft_outcome(row)
        self.assertFalse(out.ok)
        got = self.actions(summary=summary_cfg(),
                           complete=lambda p, **k: "All settled."
                           ).draft_outcome(row)
        self.assertEqual((got.ok, got.text), (True, "All settled."))


class Fixed:
    def __init__(self, rows, name="src"):
        self.rows, self.name = rows, name

    def __call__(self):
        return [sources.SourceResult(self.name, self.rows, False, None, None)]


async def settle(pilot, app):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)


async def until(pilot, cond, tries=150, step=0.05):
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


async def type_text(pilot, text):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)


def prompt_open(app):
    return isinstance(app.screen, OutcomeScreen)


def prompt_value(app):
    return app.screen.query_one("#note-input").value


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.src = support.FakeSource()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = InboxStore(self.base)
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.tmp.cleanup)

    def actions(self, opt_in=True, **kw):
        kw.setdefault("acts", cfg_block(self.src, opt_in))
        return LocalActions(self.store, base_dir=self.base, **kw)

    def app(self, rows, actions=None, undo_s=0.15, view="inbox", **kw):
        return InboxApp(Fixed(rows), self.store, actions or self.actions(),
                        settings=dict(DEFAULTS), undo_s=undo_s, leave_s=0,
                        stream_delay=0, view=view, **kw)


class PromptTest(Base):
    async def test_d_on_a_slack_ask_opens_the_outcome_prompt(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: prompt_open(app)))
            self.assertEqual(app.screen.title_text, "Outcome:")
            self.assertEqual(self.src.requests(), [])

    async def test_d_on_a_waiting_row_opens_it_too(self):
        app = self.app([waiting()], view="waiting")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: prompt_open(app)))

    async def test_an_urgent_slack_ask_is_an_ask_too(self):
        app = self.app([item(1, "urgent")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: prompt_open(app)))

    async def test_a_source_that_did_not_opt_in_keeps_the_instant_d(self):
        app = self.app([item(1)], actions=self.actions(opt_in=False))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, self.src.requests))
            self.assertFalse(isinstance(app.screen, NoteScreen))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["args"]), ("close", {}))

    async def test_a_source_with_no_acts_keeps_the_local_d(self):
        app = self.app([item(1)], actions=LocalActions(
            self.store, base_dir=self.base))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(
                pilot, lambda: self.store.is_hidden("t:1")))
            self.assertFalse(isinstance(app.screen, NoteScreen))

    async def _plain_d(self, row, view="inbox"):
        app = self.app([row], view=view)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(
                pilot, lambda: self.store.is_hidden(row["id"])))
            self.assertFalse(isinstance(app.screen, NoteScreen))
            await until(pilot, self.src.requests)
            await until(pilot, lambda: not app.running)
            return self.src.requests()

    async def test_other_kinds_keep_the_plain_d(self):
        feed = item(2, "fyi", "other", ("ack",), meta={
            "routine": "cal", "kind": "update", "title": "Title 2"})
        for row, view in (
                (item(3, "fyi", actions=()), "feed"),              # FYI
                (feed, "feed"),                                    # feed
                (item(4, "fyi", actions=("ack",)), "feed"),        # agent card
                (item(5, "needs_reply", actions=("reply",), meta={
                    "kind": "invite", "last_seen_ts": 1.0}), "inbox"),  # RSVP
                (item(6, "fyi", "memory", ("ack",), meta={
                    "kind": "duplicate"}), "inbox")):              # review
            with self.subTest(row=row["id"]):
                self.store = InboxStore(Path(tempfile.mkdtemp(dir=self.base)))
                reqs = await self._plain_d(row, view)
                for r in reqs:
                    self.assertEqual(r["args"], {})

    async def test_a_task_keeps_its_own_d(self):
        self.src.set_items([support.task_item(1)])
        rows = [itm.parse_item(r) for r in self.src.items()]
        acts = support.acts_cfg(self.src, name="src",
                                acts=support.TASK_ACTS)
        acts["src"]["close_outcome"] = True
        app = self.app(rows, actions=self.actions(acts=acts), view="tasks")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, self.src.requests))
            self.assertFalse(prompt_open(app))
            (req,) = self.src.requests()
            self.assertEqual(req["args"], {"note": ""})

    async def test_the_row_menu_names_the_outcome(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("enter")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, RowMenu)))
            labels = [e.label for e in app.screen.entries]
            self.assertIn("Done with outcome…", labels)
            self.assertNotIn("Done", labels)


class AnswerTest(Base):
    async def test_the_act_carries_the_outcome(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await type_text(pilot, "Agreed on Friday")
            await pilot.press("enter")
            self.assertTrue(await until(pilot, self.src.requests))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"], req["args"]),
                             ("close", first,
                              {"outcome": "Agreed on Friday"}))
            self.assertTrue(await until(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            self.assertTrue(self.store.is_hidden(first))

    async def test_nothing_is_sent_inside_the_undo_window(self):
        app = self.app([item(1)], undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await type_text(pilot, "done and dusted")
            await pilot.press("enter")
            await until(pilot, lambda: not prompt_open(app))
            await pilot.pause(0.3)
            self.assertEqual(self.src.requests(), [])
            await pilot.press("u")
            self.assertTrue(await until(
                pilot, lambda: app.current is not None
                and app.current.key == "t:1"))
            await pilot.pause(0.3)
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("t:1"))

    async def test_an_empty_answer_is_refused_and_the_prompt_stays(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertTrue(prompt_open(app))
            await type_text(pilot, "   ")
            await pilot.press("enter")                 # blanks are empty too
            await pilot.pause(0.3)
            self.assertTrue(prompt_open(app))
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("t:1"))
            self.assertIn("required",
                          str(app.screen.query_one("#note-hint").render()))
            await pilot.press("escape")

    async def test_esc_cancels_and_nothing_closes(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await type_text(pilot, "never mind")
            await pilot.press("escape")
            self.assertTrue(await until(pilot, lambda: not prompt_open(app)))
            await pilot.pause(0.4)
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("t:1"))
            self.assertEqual(app.current_key, "t:1")

    async def test_a_conflict_brings_the_row_back(self):
        self.src.configure("conflict")
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await type_text(pilot, "x")
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: any(
                r["status"] == "conflict" for r in app.audit)))
            self.assertFalse(self.store.is_hidden("t:1"))


class PrefillTest(Base):
    def app_with(self, complete, rows=None):
        return self.app(rows or [item(1)], actions=self.actions(
            summary=summary_cfg(), complete=complete))

    async def test_the_draft_fills_an_untouched_prompt(self):
        calls = []

        def complete(prompt, **kw):
            calls.append(kw)
            return "Rollout moved to Friday; Ada sends the notes."
        app = self.app_with(complete)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: prompt_open(app)
                                        and prompt_value(app)))
            self.assertEqual(prompt_value(app),
                             "Rollout moved to Friday; Ada sends the notes.")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["timeout"], outcome.TIMEOUT_S)
            await pilot.press("enter")                  # enter accepts it
            self.assertTrue(await until(pilot, self.src.requests))
            self.assertEqual(self.src.requests()[0]["args"], {
                "outcome": "Rollout moved to Friday; Ada sends the notes."})

    async def test_the_prompt_opens_at_once_while_the_call_runs(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def complete(prompt, **kw):
            gate.wait(10)
            return "late text"
        app = self.app_with(complete)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: prompt_open(app)))
            self.assertEqual(prompt_value(app), "")
            self.assertIn("drafting",
                          app.screen.query_one("#note-input").placeholder)
            gate.set()
            self.assertTrue(await until(
                pilot, lambda: prompt_value(app) == "late text"))
            await pilot.press("escape")

    async def test_typed_text_is_never_overwritten(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def complete(prompt, **kw):
            gate.wait(10)
            return "the model's text"
        app = self.app_with(complete)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await type_text(pilot, "mine")
            gate.set()
            await pilot.pause(0.5)
            self.assertEqual(prompt_value(app), "mine")
            await pilot.press("enter")
            self.assertTrue(await until(pilot, self.src.requests))
            self.assertEqual(self.src.requests()[0]["args"],
                             {"outcome": "mine"})

    async def test_text_typed_then_deleted_stays_the_owners(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def complete(prompt, **kw):
            gate.wait(10)
            return "the model's text"
        app = self.app_with(complete)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await pilot.press("a")
            await pilot.press("backspace")
            gate.set()
            await pilot.pause(0.5)
            self.assertEqual(prompt_value(app), "")
            await pilot.press("escape")

    async def test_a_failed_draft_leaves_the_prompt_empty_and_editable(self):
        def complete(prompt, **kw):
            raise claude_bridge.BridgeError("timed out")
        app = self.app_with(complete)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: prompt_open(app)))
            self.assertTrue(await until(pilot, lambda: any(
                r["action"] == "outcome-draft" for r in app.audit)))
            self.assertEqual(prompt_value(app), "")
            self.assertNotIn("drafting",
                             app.screen.query_one("#note-input").placeholder)
            await type_text(pilot, "by hand")
            await pilot.press("enter")
            self.assertTrue(await until(pilot, self.src.requests))
            self.assertEqual(self.src.requests()[0]["args"],
                             {"outcome": "by hand"})

    async def test_a_draft_after_esc_is_dropped(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def complete(prompt, **kw):
            gate.wait(10)
            return "too late"
        app = self.app_with(complete)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await pilot.press("escape")
            await until(pilot, lambda: not prompt_open(app))
            gate.set()
            await pilot.pause(0.4)
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("t:1"))

    async def test_no_model_configured_means_no_call_and_an_empty_prompt(self):
        called = []
        app = self.app([item(1)], actions=self.actions(
            complete=lambda p, **k: called.append(1) or "x"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: prompt_open(app))
            await pilot.pause(0.3)
            self.assertEqual(called, [])
            self.assertEqual(prompt_value(app), "")
            await pilot.press("escape")


if __name__ == "__main__":
    unittest.main()
