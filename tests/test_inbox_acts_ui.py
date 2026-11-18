"""Pilot tests for the acts (inbox 0.10.0): d / s / x / w / ! / D / n / L, the
undo window, the retry, a conflict, the shadow marker, the health strip, the
done reaction, synthetic rows, and the guarantee that a source with no
`acts:` behaves as before. A fake command source script records the requests.
Skipped when Textual is not installed. Nothing here calls Slack or claude."""
import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_acts_support as support  # noqa: E402

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None

if HAVE_TEXTUAL:
    from workspace_daemon.inbox import acts, items as itm, settings, sources
    from workspace_daemon.inbox.actions import LocalActions
    from workspace_daemon.inbox.app import InboxApp, CONFLICT_TOAST
    from workspace_daemon.inbox.screens import (AuditScreen, ChoicePicker,
                                                ConfirmSend, ConfirmWrite,
                                                NoteScreen, OpsScreen)
    from workspace_daemon.inbox.store import InboxStore
    from workspace_daemon.inbox.widgets import HealthStrip

SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
TOOL = "mcp__slack_connector__add_reaction"
REACT_BLOCK = {"tool": TOOL, "args": {"channel": "ch", "timestamp": "ts",
                                      "name": "nm"}}


def item(i, section="needs_reply", source="slack", actions=("reply",),
         reply_to=True, **kw):
    meta = kw.pop("meta", None)
    if meta is None:
        meta = {"last_seen_ts": time.time() - 30}
        if reply_to:
            meta["reply_to"] = {"channel": support.CHANNEL,
                                "thread_ts": support.THREAD,
                                "message_ts": support.MESSAGE}
    base = dict(
        id=f"t:{i}", source=source, section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender=f"Person {i}", where="#launch", gist=f"gist {i}",
        ts=time.time() - 60 * i, link=f"https://example.com/{i}", thread=[],
        draft=None, actions=list(actions), meta=meta)
    base.update(kw)
    return itm.make_item(**base)


def feed_item(i, routine="cal", kind="update"):
    return item(i, "fyi", "other", ("ack",), meta={
        "routine": routine, "kind": kind, "title": f"Title {i}"})


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
        except Exception:                # not built yet; the last try raises
            ok = False
        if ok:
            await pilot.pause()          # run what the change queued (compose)
            return True
        await pilot.pause(step)
    return cond()


def toasts(app):
    return [n.message for n in app._notifications]


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.src = support.FakeSource()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = InboxStore(self.base)
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.tmp.cleanup)

    def actions(self, acts_on=True, **kw):
        if acts_on:
            kw.setdefault("acts", support.acts_cfg(self.src))
        return LocalActions(self.store, base_dir=self.base, **kw)

    def app(self, rows, actions=None, undo_s=0.15, name="src", **kw):
        actions = actions or self.actions()
        kw.setdefault("settings", dict(DEFAULTS))
        return InboxApp(Fixed(rows, name), self.store, actions, undo_s=undo_s,
                        leave_s=0, stream_delay=0, **kw)


class SendTest(Base):
    async def test_done_closes_in_the_source_after_the_undo_window(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            await until(pilot, lambda: self.store.is_hidden(first))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"], req["args"]),
                             ("close", first, {}))
            self.assertTrue(self.store.is_hidden(first))
            self.assertTrue(await until(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            self.assertTrue(any("✓ synced" in t for t in toasts(app)))
            self.assertEqual(app.act_pending, 0)

    async def test_snooze_sends_the_chosen_time(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            before = time.time()
            await pilot.press("s")
            await pilot.pause(0.2)
            await pilot.press("1")                 # in 1 hour
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            (req,) = self.src.requests()
            self.assertEqual(req["action"], "snooze")
            self.assertAlmostEqual(req["args"]["until"], before + 3600, delta=30)
            self.assertIsNotNone(self.store.snoozed_until("t:1"))

    async def test_undo_cancels_before_anything_is_sent(self):
        app = self.app([item(1)], undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await pilot.pause(0.2)
            await pilot.press("u")
            # negative check: it must not happen
            await pilot.pause(0.4)
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("t:1"))
            self.assertEqual(self.actions_pending(app), 0)
            self.assertEqual(app.actions.ops_rows(), [])

    def actions_pending(self, app):
        return app.actions.pending_count()

    async def test_the_worker_never_blocks_the_ui(self):
        self.src.configure("ok", sleep=1.5)
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            # the act is now running
            self.assertTrue(await until(pilot, lambda: app.running))
            started = time.monotonic()
            await pilot.press("j")                  # the UI still answers
            # negative check: a timing bound, not a state wait
            await pilot.pause(0.05)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(await until(pilot, lambda: not app.running,
                                        tries=100))

    async def test_a_conflict_puts_the_row_back_and_says_so(self):
        self.src.configure("conflict")
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: any(
                r["status"] == "conflict" for r in app.audit)))
            # negative check: it must not happen
            await pilot.pause(0.2)
            self.assertFalse(self.store.is_hidden(first))
            self.assertIn(first, [i.key for i in app.items])
            self.assertIn(CONFLICT_TOAST, toasts(app))
            self.assertEqual(len(self.src.requests()), 1)      # not retried
            self.assertEqual(app.act_pending, 0)

    async def test_a_failure_keeps_the_mark_and_shows_not_synced(self):
        self.src.configure("fail")
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: app.act_pending == 1))
            self.assertTrue(self.store.is_hidden(first))
            strip = app.query_one("#strip", HealthStrip)
            self.assertTrue(await until(pilot, lambda: strip.display))
            self.assertIn("1 not synced",
                          strip.render_strip(app, app.pal, 120).plain)

    async def test_a_refresh_retries_with_the_same_op_id(self):
        self.src.configure("fail")
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: app.act_pending == 1))
            self.src.configure("ok")
            await pilot.press("r")                  # too soon: 5 minutes
            # negative check: it must not happen
            await pilot.pause(0.6)
            self.assertEqual(len(self.src.requests()), 1)
            with mock.patch.object(acts, "RETRY_EVERY_S", 0):
                await pilot.press("r")
                self.assertTrue(await until(pilot, lambda: app.act_pending == 0))
            ids = [r["op_id"] for r in self.src.requests()]
            self.assertEqual(len(ids), 2)
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(len(self.src.applied()), 1)
            self.assertTrue(any("synced" in t for t in toasts(app)))

    async def test_a_conflict_on_retry_brings_the_row_back(self):
        self.src.configure("fail")
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: app.act_pending == 1))
            self.src.configure("conflict")
            with mock.patch.object(acts, "RETRY_EVERY_S", 0):
                await pilot.press("r")
                self.assertTrue(await until(pilot, lambda: app.act_pending == 0))
            # negative check: it must not happen
            await pilot.pause(0.3)
            self.assertFalse(self.store.is_hidden(first))
            self.assertIn(CONFLICT_TOAST, toasts(app))

    async def test_shadow_shows_a_marker_and_keeps_the_mark(self):
        self.src.configure("shadow")
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: app.act_shadow))
            self.assertTrue(self.store.is_hidden(first))
            strip = app.query_one("#strip", HealthStrip)
            self.assertTrue(await until(pilot, lambda: strip.display))
            self.assertIn("shadow", strip.render_strip(app, app.pal, 120).plain)
            self.assertTrue(any(t.startswith("shadow:") for t in toasts(app)))
            self.src.configure("ok")                 # the marker follows the
            await pilot.press("d")                   # last response
            await until(pilot, lambda: not app.act_shadow)
            self.assertFalse(app.act_shadow)

    async def test_quitting_inside_the_window_keeps_the_mark_and_the_op(self):
        app = self.app([item(1), item(2)], undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await pilot.pause(0.2)
            await pilot.press("q")
            # negative check: the quit must not wait for the act
            await pilot.pause(0.5)
        self.assertEqual(self.src.requests(), [])        # the app never blocks
        self.assertTrue(InboxStore(self.base).is_hidden(first))
        self.assertEqual(acts.PendingOps(self.base).count(), 1)


class PickerTest(Base):
    def thread_item(self):
        return item(1, thread=[
            {"who": "Ada Example", "ts": time.time() - 50, "text": "hi"},
            {"who": "You", "ts": time.time() - 40, "text": "yo"},
            {"who": "Bo Sample", "ts": time.time() - 30, "text": "ok"},
            {"who": "Ada Example", "ts": time.time() - 20, "text": "so?"}])

    async def test_w_lists_me_the_thread_people_and_nobody(self):
        app = self.app([self.thread_item()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("w")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ChoicePicker)))
            self.assertEqual([c["label"] for c in app.screen.choices],
                             ["Me", "Ada Example", "Bo Sample",
                              "Nobody (FYI)"])
            await pilot.press("3")                   # Bo Sample
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["args"]),
                             ("waits_on", {"who": "Bo Sample"}))
            self.assertFalse(self.store.is_hidden("t:1"))   # no local mark

    async def test_w_me_and_nobody(self):
        app = self.app([self.thread_item()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key, who in (("1", "me"), ("4", "nobody")):
                await pilot.press("w")
                await pilot.pause(0.2)
                await pilot.press(key)
                self.assertTrue(await until(pilot, lambda: len(
                    self.src.requests()) == (1 if who == "me" else 2)))
                # negative check: no further request may follow
                await pilot.pause(0.3)
            self.assertEqual([r["args"] for r in self.src.requests()],
                             [{"who": "me"}, {"who": "nobody"}])

    async def test_w_is_cancelled_by_undo(self):
        app = self.app([self.thread_item()], undo_s=5)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("w")
            await pilot.pause(0.2)
            await pilot.press("1")
            await pilot.pause(0.2)
            await pilot.press("u")
            # negative check: it must not happen
            await pilot.pause(0.3)
            self.assertEqual(self.src.requests(), [])

    async def test_x_offers_the_conversation_mute_outside_the_feed(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("x")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ChoicePicker)))
            self.assertEqual([c["label"] for c in app.screen.choices], [
                "Mute this conversation: 1 day",
                "Mute this conversation: 1 week",
                "Mute this conversation: untrack"])
            before = time.time()
            await pilot.press("2")
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["id"]), ("mute", first))
            self.assertAlmostEqual(req["args"]["until"], before + 7 * 86400,
                                   delta=30)
            self.assertIsNotNone(self.store.snoozed_until(first))

    async def test_x_untrack_sends_a_null_until_and_hides_the_row(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("x")
            await pilot.pause(0.2)
            await pilot.press("3")
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            self.assertEqual(self.src.requests()[0]["args"], {"until": None})
            await until(pilot, lambda: self.store.is_hidden(first))
            self.assertTrue(self.store.is_hidden(first))

    async def test_x_in_the_feed_keeps_the_feed_rule_options_first(self):
        app = self.app([feed_item(1, "cal", "update")], view="feed")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ChoicePicker)))
            labels = [c["label"] for c in app.screen.choices]
            self.assertEqual(labels[:3], ["This kind from cal",
                                          "Everything from cal",
                                          "Titles like this"])
            self.assertEqual(labels[3:], [
                "Mute this conversation: 1 day",
                "Mute this conversation: 1 week",
                "Mute this conversation: untrack"])
            await pilot.press("2")                   # a Feed rule: the old path
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ChoicePicker)))
            self.assertEqual(app.screen.title_text, "Mute: for how long")
            await pilot.press("1")
            self.assertTrue(await until(
                pilot, lambda: len(self.store.feed_rules()) == 1))
            self.assertEqual(self.src.requests(), [])

    async def test_bang_relays_with_a_note(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("exclamation_mark")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, NoteScreen)))
            await pilot.press(*"look at  this", "enter")
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            (req,) = self.src.requests()
            self.assertEqual((req["action"], req["args"]),
                             ("relay", {"note": "look at this"}))
            self.assertFalse(self.store.is_hidden("t:1"))

    async def test_bang_with_no_note_and_esc(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("exclamation_mark")
            await pilot.pause(0.2)
            await pilot.press("escape")
            # negative check: it must not happen
            await pilot.pause(0.3)
            self.assertEqual(self.src.requests(), [])
            await pilot.press("exclamation_mark")
            await pilot.pause(0.2)
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            self.assertEqual(self.src.requests()[0]["args"], {"note": ""})

    async def test_an_act_the_source_did_not_list_says_so(self):
        app = self.app([item(1)], actions=self.actions(
            acts=support.acts_cfg(self.src, acts=["close"])))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("w", "exclamation_mark"):
                await pilot.press(key)
                # negative check: it must not happen
                await pilot.pause(0.1)
                self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(any("does not list" in t for t in toasts(app)))
            await pilot.press("x")
            # negative check: it must not happen
            await pilot.pause(0.1)
            self.assertEqual(len(app.screen_stack), 1)   # not in the Feed
            self.assertEqual(self.src.requests(), [])


class DraftAndNudgeTest(Base):
    def summary(self):
        return settings.validate({"summary": {}})["summary"]

    async def test_D_fills_the_draft_pane_and_never_sends(self):
        sent = []
        complete_calls = []

        def complete(prompt, **kw):
            complete_calls.append(kw)
            return "Thanks, I will look today."
        app = self.app([item(1, thread=[{"who": "Ada", "ts": time.time() - 9,
                                         "text": "Can you review?"}])],
                       actions=self.actions(
                           summary=self.summary(), complete=complete,
                           copy=True, call_tools=lambda *a, **k: sent.append(a)))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.current.draft, "")
            await pilot.press("D")
            self.assertTrue(await until(pilot, lambda: app.current.draft))
            self.assertEqual(app.current.draft, "Thanks, I will look today.")
            self.assertEqual(sent, [])
            self.assertEqual(self.src.requests(), [])
            self.assertFalse(self.store.is_hidden("t:1"))
            self.assertEqual(len(complete_calls), 1)
            self.assertEqual(complete_calls[0]["max_turns"], 1)
            self.assertTrue(self.store.has_draft("t:1"))
            # D again, with its own draft in the pane, writes a new one.
            await pilot.press("D")
            await until(pilot, lambda: len(complete_calls) == 2)
            self.assertEqual(len(complete_calls), 2)

    async def test_D_uses_the_kept_draft_after_a_restart(self):
        from workspace_daemon.inbox.models import from_v1
        calls = []
        a = self.actions(summary=self.summary(),
                         complete=lambda p, **k: calls.append(1) or "kept text")
        raw = item(1)
        seen = from_v1(raw)
        seen.origin = "src"
        a.draft_for_me(seen)                       # made in an earlier session
        app = self.app([raw], actions=a)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.current.draft, "kept text")   # on load
            self.assertEqual(len(calls), 1)

    async def test_D_does_not_overwrite_a_draft_from_the_source(self):
        calls = []
        app = self.app([item(1, draft="the source's draft")],
                       actions=self.actions(
                           summary=self.summary(),
                           complete=lambda p, **k: calls.append(1) or "x"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("D")
            self.assertTrue(await until(
                pilot, lambda: app.current.draft == "the source's draft"))
            self.assertEqual(calls, [])

    async def test_D_needs_summary_or_polish_and_a_replyable_row(self):
        app = self.app([item(1), item(2, "fyi", "other", ("ack",))])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("D")
            self.assertTrue(await until(
                pilot, lambda: any("summary: or polish:" in t for t in toasts(app))))
            self.assertEqual(app.current.draft, "")

    async def test_D_failure_is_a_toast_and_leaves_the_row_alone(self):
        def complete(prompt, **kw):
            raise RuntimeError("boom")
        app = self.app([item(1)], actions=self.actions(
            summary=self.summary(), complete=complete))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("D")
            self.assertTrue(await until(pilot, lambda: any(
                "Draft for me failed" in t for t in toasts(app))))
            self.assertEqual(app.current.draft, "")

    def waiting(self):
        return item(1, "waiting", "slack", ("nudge",), sender="Ada Example",
                    gist="the rollout plan")

    async def test_n_with_no_draft_falls_back_to_the_template(self):
        app = self.app([self.waiting()], view="waiting",
                       actions=self.actions(copy=True))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("n")
            # the normal path
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ConfirmSend)))
            self.assertEqual(app.screen.text,
                             "Hi Ada, any news on the rollout plan?")
            self.assertEqual(app.screen.verb, "Send nudge")
            await pilot.press("escape")
            # negative check: it must not happen
            await pilot.pause(0.2)
            self.assertEqual(self.src.requests(), [])
            # Cancelled: the template is not left behind as the row's draft.
            self.assertEqual(app.current.draft, "")

    async def test_a_cancelled_n_template_does_not_block_D(self):
        complete_calls = []
        app = self.app([self.waiting()], view="waiting",
                       actions=self.actions(
                           copy=True, summary=self.summary(),
                           complete=lambda p, **k: complete_calls.append(1)
                           or "A real draft"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("n")
            await pilot.pause(0.3)
            await pilot.press("escape")
            self.assertTrue(await until(
                pilot, lambda: app.current.draft == ""))
            await pilot.press("D")
            self.assertTrue(await until(pilot, lambda: app.current.draft))
            self.assertEqual(app.current.draft, "A real draft")
            self.assertEqual(len(complete_calls), 1)

    async def test_n_uses_the_configured_template(self):
        app = self.app([self.waiting()], view="waiting",
                       actions=self.actions(
                           copy=True, nudge_template="Ping {who}: {topic}"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("n")
            self.assertTrue(await until(
                pilot, lambda: app.screen.text == "Ping Ada Example: the rollout plan"))

    async def test_n_with_a_stored_draft_sends_that_draft(self):
        row = item(1, "waiting", "slack", ("nudge",), draft="Stored nudge text")
        app = self.app([row], view="waiting", actions=self.actions(copy=True))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("n")
            self.assertTrue(await until(
                pilot, lambda: app.screen.text == "Stored nudge text"))

    async def test_n_without_acts_is_not_due_as_before(self):
        app = self.app([self.waiting()], view="waiting",
                       actions=self.actions(acts_on=False, copy=True))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("n")
            # negative check: it must not happen
            await pilot.pause(0.3)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(any("Nudge not due yet" in t for t in toasts(app)))


class DoneReactionTest(Base):
    def react_actions(self, calls, done_reaction, **kw):
        def call(prompt, tools, **k):
            calls.append(tools)
            (name, pins), = tools.items()
            return [{"name": name, "input": dict(pins),
                     "text": "Reaction added", "is_error": False}]
        return self.actions(
            react=settings.validate({"react": REACT_BLOCK})["react"],
            done_reaction=done_reaction, call_tools=call, **kw)

    async def test_done_reaction_on_reacts_after_the_window(self):
        calls = []
        # A long undo window: the in-window check runs as soon as the done is
        # pending, so a slow machine cannot let the window close under it.
        app = self.app([item(1), item(2)], undo_s=2.0,
                       actions=self.react_actions(calls, "white_check_mark"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: app.pending))
            self.assertEqual(calls, [])              # still inside the window
            self.assertTrue(await until(pilot, lambda: calls))
            (pins,) = calls
            self.assertEqual(pins[TOOL]["nm"], "white_check_mark")
            self.assertEqual(pins[TOOL]["ts"], support.MESSAGE)
            self.assertEqual(self.src.requests()[0]["action"], "close")
            await until(pilot, lambda: any("Reacted" in t for t in toasts(app)))

    async def test_done_reaction_off_never_reacts(self):
        calls = []
        app = self.app([item(1), item(2)],
                       actions=self.react_actions(calls, None))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: self.src.requests())
            # negative check: it must not happen
            await pilot.pause(0.5)
            self.assertEqual(calls, [])

    async def test_a_row_with_no_message_is_skipped(self):
        calls = []
        app = self.app([item(1, reply_to=False), item(2)],
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.current_key, "t:1")
            await pilot.press("d")
            await until(pilot, lambda: self.src.requests())
            # negative check: it must not happen
            await pilot.pause(0.5)
            self.assertEqual(calls, [])

    async def test_a_thread_only_row_gets_no_reaction(self):
        """Without reply_to.message_ts the reaction would land on the root
        post of someone else's thread."""
        calls = []
        thread_only = item(1, meta={"reply_to": {
            "channel": support.CHANNEL, "thread_ts": support.THREAD}})
        app = self.app([thread_only, item(2)],
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertEqual(app.current_key, "t:1")
            await pilot.press("d")
            await until(pilot, lambda: self.src.requests())
            # negative check: it must not happen
            await pilot.pause(0.5)
            self.assertEqual(calls, [])
            self.assertEqual(self.src.requests()[0]["action"], "close")

    async def test_a_shadow_op_gets_no_reaction_and_says_so(self):
        self.src.configure("shadow")
        calls = []
        app = self.app([item(1), item(2)],
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: any(
                "reaction skipped (shadow)" in t for t in toasts(app))))
            self.assertEqual(calls, [])

    async def test_an_unsynced_op_gets_no_reaction_and_says_so(self):
        self.src.configure("fail")
        calls = []
        app = self.app([item(1), item(2)],
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: any(
                "reaction skipped (not synced)" in t for t in toasts(app))))
            self.assertEqual(calls, [])

    async def test_a_conflict_says_the_reaction_was_skipped(self):
        self.src.configure("conflict")
        calls = []
        app = self.app([item(1)], actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: any(
                "reaction skipped (conflict)" in t for t in toasts(app))))
            self.assertEqual(calls, [])

    async def test_quitting_inside_the_window_skips_the_reaction(self):
        calls = []
        app = self.app([item(1), item(2)], undo_s=5,
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(app, "exit") as leave:
                await pilot.press("d")
                await pilot.pause(0.2)
                await pilot.press("q")
                await pilot.pause(0.3)
            message = leave.call_args.kwargs["message"]
            self.assertIn("done reaction(s) skipped", message)
        self.assertEqual(calls, [])

    async def test_quitting_while_the_act_runs_warns_about_the_reaction(self):
        self.src.configure("ok", sleep=2)
        calls = []
        app = self.app([item(1), item(2)],
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: app.running)
            await pilot.pause(0.3)
            await pilot.press("q")                  # asks first
            await pilot.pause(0.2)
            with mock.patch.object(app, "exit") as leave:
                await pilot.press("q")
                await pilot.pause(0.3)
            message = leave.call_args.kwargs["message"]
            self.assertIn("may already have reached the source", message)
            self.assertIn("done reaction (:eyes:) may already have been "
                          "posted", message)
            await until(pilot, lambda: not app.running, tries=100)

    async def test_undo_cancels_the_reaction_too(self):
        calls = []
        app = self.app([item(1)], undo_s=5,
                       actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await pilot.pause(0.2)
            await pilot.press("u")
            # negative check: it must not happen
            await pilot.pause(0.3)
            self.assertEqual((calls, self.src.requests()), ([], []))

    async def test_a_conflict_means_no_reaction(self):
        self.src.configure("conflict")
        calls = []
        app = self.app([item(1)], actions=self.react_actions(calls, "eyes"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: any(
                r["status"] == "conflict" for r in app.audit))
            # negative check: it must not happen
            await pilot.pause(0.3)
            self.assertEqual(calls, [])

    async def test_a_failed_reaction_does_not_undo_the_done(self):
        def call(prompt, tools, **k):
            raise RuntimeError("down")
        app = self.app([item(1), item(2)], actions=self.actions(
            react=settings.validate({"react": REACT_BLOCK})["react"],
            done_reaction="eyes", call_tools=call))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await until(pilot, lambda: any(
                "Reaction failed" in t for t in toasts(app)))
            self.assertTrue(self.store.is_hidden(first))

    async def test_done_reaction_works_with_acts_off(self):
        calls = []
        app = self.app([item(1), item(2)], actions=self.react_actions(
            calls, "eyes", acts_on=False))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: calls))
            self.assertEqual(self.src.requests(), [])


class ReviewFixesUITest(Base):
    async def test_a_conflict_does_not_set_the_shadow_marker(self):
        self.src.configure("conflict")
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: any(
                r["status"] == "conflict" for r in app.audit))
            self.assertFalse(app.act_shadow)

    async def test_the_shadow_marker_comes_back_after_a_restart(self):
        self.src.configure("shadow")
        a = self.actions()
        from workspace_daemon.inbox.models import from_v1
        it = from_v1(item(1))
        it.origin = "src"
        a.send_act(it, "close", {})                  # an earlier session
        app = self.app([item(2)], actions=self.actions())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertTrue(app.act_shadow)
            strip = app.query_one("#strip", HealthStrip)
            self.assertTrue(await until(pilot, lambda: strip.display))
            self.assertIn("shadow", strip.render_strip(app, app.pal, 120).plain)

    async def test_a_lost_op_is_an_error_toast(self):
        self.src.configure("fail")
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            with mock.patch.object(acts.PendingOps, "add",
                                   side_effect=OSError("disk full")):
                await pilot.press("d")
                self.assertTrue(await until(pilot, lambda: any(
                    "never got it" in t for t in toasts(app))))
            self.assertEqual(app.act_pending, 0)

    async def test_an_undone_row_does_not_offer_u_again(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            await pilot.press("L")
            await pilot.pause(0.3)
            await pilot.press("u")
            await pilot.pause(0.3)
            await pilot.press("y")
            self.assertTrue(await until(pilot, lambda: len(
                self.src.requests()) == 2))
            # negative check: it must not happen
            await pilot.pause(0.5)
            self.assertEqual(len(app.screen_stack), 1)
            await pilot.press("L")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            close_row = next(r for r in app.screen.rows
                             if r["action"] == "close")
            self.assertTrue(close_row.get("undone"))
            await pilot.press("u")                   # on the newest row (the
            # negative check: it must not happen
            await pilot.pause(0.2)                   # reopen) or the close:
            self.assertIsInstance(app.screen, OpsScreen)     # never a confirm
            await pilot.press("j", "u")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            self.assertEqual(len(self.src.requests()), 2)

    async def test_quit_waits_for_a_running_draft(self):
        import threading
        release = threading.Event()

        def complete(prompt, **kw):
            release.wait(10)
            return "late"
        app = self.app([item(1)], actions=self.actions(
            summary=settings.validate({"summary": {}})["summary"],
            complete=complete))
        try:
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                await pilot.press("D")
                await until(pilot, lambda: app._draft_running)
                await pilot.press("q")
                # negative check: it must not happen
                await pilot.pause(0.2)
                self.assertFalse(app._quitting)           # asked first
                self.assertTrue(any("draft for me still running" in t
                                    for t in toasts(app)))
                release.set()
                await pilot.pause(0.5)
        finally:
            release.set()

    async def test_quit_waits_for_a_running_inverse(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("d")
            await until(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit))
            self.src.configure("ok", sleep=1.5)
            await pilot.press("L")
            await pilot.pause(0.3)
            await pilot.press("u")
            await pilot.pause(0.3)
            await pilot.press("y")
            await until(pilot, lambda: app._inverse_running)
            await pilot.press("q")
            # negative check: it must not happen
            await pilot.pause(0.2)
            self.assertFalse(app._quitting)
            self.assertTrue(any("undo in the source still running" in t
                                for t in toasts(app)))
            await until(pilot, lambda: not app._inverse_running, tries=100)

    async def test_update_hints_survives_a_missing_bar(self):
        app = self.app([item(1)])
        # Before mount (as during teardown) there is no #hints to find.
        app.update_hints()

    async def test_the_d_hint_needs_draft_me_configured(self):
        app = self.app([item(1)])                  # acts on, no summary/polish
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            keys = {k for k, _label in app.hints()}
            self.assertNotIn("D", keys)


class AuditTest(Base):
    async def test_L_lists_the_ops_and_u_sends_the_inverse(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            self.assertTrue(await until(pilot, lambda: any(
                r["status"] == "synced" for r in app.audit)))
            await pilot.press("L")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            (row,) = app.screen.rows
            self.assertEqual((row["action"], row["id"], row["result"]),
                             ("close", first, "ok"))
            await pilot.press("u")
            # a confirm
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ConfirmWrite)))
            self.assertEqual(len(self.src.requests()), 1)
            await pilot.press("y")
            self.assertTrue(await until(pilot, lambda: len(
                self.src.requests()) == 2))
            self.assertEqual(self.src.requests()[1]["action"], "reopen")
            await until(pilot, lambda: not self.store.is_hidden(first))
            self.assertFalse(self.store.is_hidden(first))
            self.assertTrue(await until(pilot, lambda: first in [
                i.key for i in app.items]))

    async def test_u_on_a_row_without_an_inverse_says_so(self):
        app = self.app([item(1), item(2)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("exclamation_mark")
            await pilot.pause(0.2)
            await pilot.press("enter")
            self.assertTrue(await until(pilot, lambda: self.src.requests()))
            await pilot.pause(0.3)
            await pilot.press("L")
            await pilot.pause(0.3)
            await pilot.press("u")
            # negative check: it must not happen
            await pilot.pause(0.2)
            self.assertIsInstance(app.screen, OpsScreen)         # no confirm
            self.assertEqual(len(self.src.requests()), 1)

    async def test_esc_closes_and_a_opens_the_session_log(self):
        app = self.app([item(1)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("L")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            await pilot.press("a")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, AuditScreen)))
            await pilot.press("escape")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, OpsScreen)))
            await pilot.press("escape")
            self.assertTrue(await until(
                pilot, lambda: len(app.screen_stack) == 1))


class SyntheticRowsTest(Base):
    async def test_a_digest_row_never_acts(self):
        rows = [feed_item(n, "sheet-watch", "change") for n in (1, 2)]
        cfg = dict(DEFAULTS, feed={"window_h": 48.0, "rules": [
            {"routine": "sheet-watch", "action": "digest"}]})
        app = self.app(rows, view="feed", settings=cfg)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertTrue(app.current.key.startswith("digest:"))
            for key in ("w", "exclamation_mark", "D"):
                await pilot.press(key)
                # negative check: it must not happen
                await pilot.pause(0.1)
                self.assertEqual(len(app.screen_stack), 1, key)
            await pilot.press("x")                   # a rule, never a mute act
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, ChoicePicker)))
            self.assertFalse(any("conversation" in c["label"]
                                 for c in app.screen.choices))
            await pilot.press("escape")
            await pilot.pause(0.2)

            await pilot.press("d")                   # a local dismiss still works
            await until(pilot, lambda: all(
                self.store.is_hidden(r["id"]) for r in rows))
            # negative check: it must not happen
            await pilot.pause(0.4)
            self.assertEqual(self.src.requests(), [])
            self.assertEqual(app.actions.ops_rows(), [])

    async def test_a_saved_row_never_acts(self):
        saved = [{"id": "s1", "source": "slack", "kind": "new",
                  "ts": time.time() - 600, "title": "A saved thread",
                  "link": None, "source_id": "slack:s1"}]
        app = self.app([feed_item(1)], saved=lambda: (saved, None),
                       saved_days=2)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("S")
            self.assertTrue(await until(
                pilot, lambda: app.current is not None and app.current.key.startswith("saved:")))
            row = app.current
            self.assertTrue(row.key.startswith("saved:"))
            row.origin = "src"          # even claiming a source that lists acts
            self.assertEqual(app.actions.act_names(row), ())
            for key in ("w", "exclamation_mark", "D", "x"):
                await pilot.press(key)
                # negative check: it must not happen
                await pilot.pause(0.1)
                self.assertEqual(len(app.screen_stack), 1, key)
            await pilot.press("d")                   # local only
            # negative check: it must not happen
            await pilot.pause(0.5)
            self.assertEqual(self.src.requests(), [])
            self.assertEqual(app.actions.ops_rows(), [])


class ActsOffTest(Base):
    """No `acts:` on the source: today's behaviour."""

    def plain(self):
        return self.actions(acts_on=False)

    async def test_d_is_a_local_mark_and_nothing_is_written(self):
        app = self.app([item(1), item(2)], actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            first = app.current_key
            await pilot.press("d")
            await until(pilot, lambda: self.store.is_hidden(first))
            self.assertTrue(await until(
                pilot, lambda: self.store.is_hidden(first)))
            self.assertEqual(self.src.requests(), [])
            self.assertEqual(app.act_pending, 0)
            self.assertFalse(app.act_shadow)
            state = self.base / "state"
            self.assertEqual(sorted(p.name for p in state.iterdir()
                                    if "ops" in p.name), [])
            self.assertEqual(app.running, [])
            self.assertEqual([r["status"] for r in app.audit
                              if r["action"] == "done"], ["committed"])

    async def test_snooze_is_local_only(self):
        app = self.app([item(1)], actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("s")
            await pilot.pause(0.2)
            await pilot.press("1")
            await until(pilot, lambda: self.store.snoozed_until("t:1"))
            self.assertTrue(await until(
                pilot, lambda: self.store.snoozed_until("t:1") is not None))
            self.assertEqual(self.src.requests(), [])

    async def test_the_new_keys_only_say_acts_are_off(self):
        app = self.app([item(1), item(2)], actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for key in ("w", "exclamation_mark"):
                await pilot.press(key)
                # negative check: it must not happen
                await pilot.pause(0.1)
                self.assertEqual(len(app.screen_stack), 1, key)
            self.assertTrue(any("Acts are not enabled" in t
                                for t in toasts(app)))
            self.assertEqual(app.pending, [])
            self.assertEqual(self.store.marks, {})
            self.assertEqual(self.src.requests(), [])

    async def test_x_outside_the_feed_says_so_as_before(self):
        app = self.app([item(1)], actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            # negative check: it must not happen
            await pilot.pause(0.2)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertTrue(any("x and - work on a row of the Feed" in t
                                for t in toasts(app)))

    async def test_x_in_the_feed_has_only_the_feed_rule_options(self):
        app = self.app([feed_item(1, "cal", "update")], view="feed",
                       actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            await pilot.press("x")
            self.assertTrue(await until(
                pilot, lambda: [c["id"] for c in app.screen.choices] == ["kind", "routine", "title"]))

    async def test_L_is_the_session_audit_log_and_there_is_no_strip(self):
        app = self.app([item(1)], actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertFalse(app.query_one("#strip", HealthStrip).display)
            await pilot.press("L")
            self.assertTrue(await until(
                pilot, lambda: isinstance(app.screen, AuditScreen)))

    async def test_hints_do_not_mention_the_new_keys(self):
        app = self.app([item(1)], actions=self.plain())
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            keys = {k for k, _label in app.hints()}
            self.assertFalse(keys & {"w", "!", "D"})

    async def test_hints_name_the_new_keys_when_acts_are_on(self):
        app = self.app([item(1)], actions=self.actions(
            summary=settings.validate({"summary": {}})["summary"]))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            keys = {k for k, _label in app.hints()}
            self.assertTrue({"w", "!", "D"} <= keys)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class KeyMapTest(unittest.TestCase):
    def test_new_keys_are_bound_once_and_reserved(self):
        keys = []
        for b in InboxApp.BINDINGS:
            keys += b.key.split(",")
        self.assertEqual(len(keys), len(set(keys)))
        actions = {k: b.action for b in InboxApp.BINDINGS
                   for k in b.key.split(",")}
        self.assertEqual(actions["w"], "waits_on")
        self.assertEqual(actions["D"], "draft_me")
        self.assertEqual(actions["exclamation_mark"], "relay")
        self.assertEqual(actions["L"], "audit")
        self.assertEqual(actions["x"], "mute")
        letters = {k for k in keys if len(k) == 1 and k.isalpha()}
        self.assertEqual(letters, set(settings.RESERVED_KEYS))
        for action in ("waits_on", "draft_me", "relay"):
            self.assertTrue(hasattr(InboxApp, f"action_{action}"))


if __name__ == "__main__":
    unittest.main()
