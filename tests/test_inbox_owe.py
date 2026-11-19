"""Inbox 0.12.3: an I OWE item's Thread pane opens with a "You owe" block.

The block holds the message that creates the obligation (the newest message
from someone else after your last reply, quoted and trimmed), the action when
the item knows one (`meta.task`, `meta.todo`, `meta.ask`, an invite's RSVP, a
drafted reply) as `To do: ...`, else `Reply to <person>`; the full thread
follows below it. Synthetic data only (invented names and text). The model
helpers run without Textual; the pane tests need it."""
import importlib.util
import time
import unittest

from workspace_daemon.inbox import items as itm, models, sources
from workspace_daemon.inbox.actions import FakeActions
from workspace_daemon.inbox.store import InboxStore

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp
    from workspace_daemon.inbox.widgets import Queue

SIZE = (150, 46)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}
MIN = 60.0
ROOT = "ROOT: kicking off the Acme onboarding review, who can take it?"
OBLIGE = ("OBLIGE: can you approve the Acme budget line?\n"
          "second quoted line\nthird quoted line\nfourth quoted line\n"
          "FIFTH-LINE-CUT")


def msg(who, ago_min, text):
    return {"who": who, "ts": time.time() - ago_min * MIN, "text": text}


def channel_thread():
    """Your old root on top, then replies; the obligation is Dana's last
    message (Ravi, a bystander, spoke after her)."""
    return [msg("You", 300, ROOT),
            msg("Dana Example", 280, "I can look at it"),
            msg("You", 200, "Thanks Dana, please share notes"),
            msg("Dana Example", 120, "Notes are in the doc"),
            msg("Dana Example", 87, OBLIGE),
            msg("Ravi Example", 80, "BYSTANDER: unrelated chatter")]


def owe_item(i=1, section="needs_reply", thread=None, **kw):
    base = dict(
        id=f"owe:{i}", source="slack", section=section,
        urgency="urgent" if section == "urgent" else "digest",
        sender="Dana Example", where="#acme-review", gist="GIST-LINE for row",
        ts=time.time() - 87 * MIN, link=f"https://example.invalid/{i}",
        thread=channel_thread() if thread is None else thread, draft=None,
        actions=["reply", "ack"], meta={})
    base.update(kw)
    return itm.make_item(**base)


def invite_item():
    return itm.make_item(
        id="invites:acme-sync", source="calendar", section="needs_reply",
        urgency="digest", sender="Dana Example", where="invite",
        gist="Acme sync - Thu 14:00-14:45 - from Dana Example",
        ts=time.time() + 20 * 3600, link=None,
        thread=[{"who": "when", "ts": time.time() + 20 * 3600,
                 "text": "Thu 14:00-14:45"}],
        draft=None, actions=["ack"],
        meta={"kind": "invite", "event_id": "ev1", "my_rsvp": "needsAction"})


class OweModelTest(unittest.TestCase):
    def view(self, **kw):
        return models.from_v1(owe_item(**kw))

    def test_the_message_is_the_senders_newest_after_your_last_reply(self):
        who, _ts, text = models.owe_message(self.view())
        self.assertEqual(who, "Dana Example")
        self.assertTrue(text.startswith("OBLIGE"))

    def test_without_a_message_from_the_sender_another_sender_counts(self):
        thread = [msg("You", 50, "Any news?"),
                  msg("Ravi Example", 10, "RAVI-ANSWER please decide")]
        who, _ts, text = models.owe_message(self.view(thread=thread))
        self.assertEqual((who, text), ("Ravi Example",
                                       "RAVI-ANSWER please decide"))

    def test_nothing_after_your_reply_falls_back_to_the_gist(self):
        thread = [msg("Dana Example", 90, "old ask"), msg("You", 5, "on it")]
        who, _ts, text = models.owe_message(self.view(thread=thread))
        self.assertEqual((who, text), ("Dana Example", "GIST-LINE for row"))

    def test_an_empty_thread_falls_back_to_the_gist(self):
        _who, _ts, text = models.owe_message(self.view(thread=[]))
        self.assertEqual(text, "GIST-LINE for row")

    def test_an_invite_is_its_gist(self):
        _who, _ts, text = models.owe_message(models.from_v1(invite_item()))
        self.assertTrue(text.startswith("Acme sync"))

    def test_the_action_comes_from_task_then_todo_then_ask(self):
        for meta, want in (
                ({"task": "Approve the line", "todo": "x", "ask": "y"},
                 "Approve the line"),
                ({"todo": "Sign the form", "ask": "y"}, "Sign the form"),
                ({"ask": "Pick a date"}, "Pick a date"),
                ({"task": "  ", "ask": "Pick a date"}, "Pick a date"),
                ({"task": 5, "ask": ["a"]}, "")):
            self.assertEqual(models.owe_action(self.view(meta=meta)), want)

    def test_a_draft_is_the_action_when_nothing_else_says(self):
        view = self.view(draft="\nYes, approved.\nSecond line")
        self.assertEqual(models.owe_action(view), "reply: Yes, approved.")

    def test_an_invite_is_an_rsvp_and_an_ack_only_item_an_ack(self):
        self.assertEqual(models.owe_action(models.from_v1(invite_item())),
                         "RSVP")
        self.assertEqual(models.owe_action(self.view(actions=["ack"])),
                         "acknowledge")
        self.assertEqual(models.owe_action(self.view()), "")

    def test_only_needs_reply_rows_that_are_not_tasks_owe(self):
        self.assertTrue(models.owes(self.view()))
        self.assertFalse(models.owes(self.view(section="waiting",
                                               actions=["nudge"])))
        self.assertFalse(models.owes(self.view(section="fyi",
                                               actions=["ack"])))
        self.assertFalse(models.owes(self.view(meta={"kind": "task"})))
        self.assertFalse(models.owes(self.view(meta={"thread_fixed": True})))


class Fixed:
    def __init__(self, rows):
        self.results = [sources.SourceResult("t", rows, False, None, None)]

    def __call__(self):
        return self.results


async def settle(pilot, app):
    for _ in range(200):
        if app.polled_once:
            break
        await pilot.pause(0.02)
    await pilot.pause(0.25)


def make_app(rows, **cfg):
    store = InboxStore(None)
    return InboxApp(Fixed(rows), store, FakeActions(store=store, draft_delay=0),
                    settings=dict(DEFAULTS, **cfg), undo_s=30, leave_s=0,
                    stream_delay=0, view="inbox")


def thread_text(app):
    return str(app.query_one("#thread-body").render())


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class OwePaneTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_block_comes_first_and_the_thread_below_it(self):
        for order in ("newest", "oldest"):
            app = make_app([owe_item()], thread_order=order)
            async with app.run_test(size=SIZE) as pilot:
                await settle(pilot, app)
                text = thread_text(app)
                self.assertIn("YOU OWE", text)
                self.assertIn("Dana Example waits on you · asked 1h27m ago",
                              text)
                block, _, thread = text.partition("─ thread ─")
                self.assertTrue(thread, "the full thread follows the block")
                self.assertTrue(text.index("YOU OWE") < text.index("OBLIGE"))
                self.assertIn("OBLIGE: can you approve the Acme budget line?",
                              block)
                # the quote is trimmed to a few lines, the cut is marked
                self.assertIn("fourth quoted line", block)
                self.assertNotIn("FIFTH-LINE-CUT", block)
                self.assertIn("…", block)
                self.assertNotIn("BYSTANDER", block)
                self.assertNotIn(ROOT, block)
                # the whole thread is still there, nothing dropped
                for part in (ROOT, "Notes are in the doc", "FIFTH-LINE-CUT",
                             "BYSTANDER"):
                    self.assertIn(part, thread)
                self.assertNotIn("← Dana Example waits on you", text)

    async def test_no_known_action_reads_reply_to_the_person(self):
        app = make_app([owe_item()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("To do: Reply to Dana Example", thread_text(app))

    async def test_the_items_task_is_the_action(self):
        app = make_app([owe_item(meta={"task": "Approve the budget line"})])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            text = thread_text(app)
            self.assertIn("To do: Approve the budget line", text)
            self.assertNotIn("Reply to Dana", text)

    async def test_a_draft_is_the_action_when_nothing_else_says(self):
        app = make_app([owe_item(draft="Yes, approved for Acme.")])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            self.assertIn("To do: reply: Yes, approved for Acme.",
                          thread_text(app))

    async def test_a_dm_quotes_the_newest_message(self):
        thread = [msg("Dana Example", 50, "DM-ONE first"),
                  msg("Dana Example", 30, "DM-TWO can you send the deck?")]
        app = make_app([owe_item(where="DM", thread=thread)])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            block = thread_text(app).partition("─ thread ─")[0]
            self.assertIn("DM-TWO can you send the deck?", block)
            self.assertNotIn("DM-ONE", block)
            self.assertIn("asked 30m ago", block)

    async def test_an_invite_says_rsvp(self):
        app = make_app([invite_item()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            text = thread_text(app)
            self.assertIn("YOU OWE", text)
            self.assertIn("Dana Example is waiting for your RSVP", text)
            self.assertRegex(text, r"starts in (19|20)h")
            self.assertIn("To do: RSVP", text)
            self.assertIn("Acme sync - Thu 14:00-14:45", text)

    async def test_other_rows_have_no_block(self):
        rows = [owe_item(1, section="waiting", actions=["nudge"],
                         ts=time.time() - 30 * 3600),
                owe_item(2, section="fyi", actions=["ack"]),
                owe_item(3, section="urgent", where="#acme-urgent")]
        app = make_app(rows)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            for _ in range(3):
                self.assertNotIn("YOU OWE", thread_text(app))
                await pilot.press("j")
                await pilot.pause(0.2)

    async def test_the_list_keeps_the_gist_as_its_second_line(self):
        app = make_app([owe_item()])
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot, app)
            queue = app.query_one(Queue)
            rows = [str(o.prompt) for o in queue.options if not o.disabled]
            self.assertTrue(any("GIST-LINE for row" in r for r in rows), rows)


if __name__ == "__main__":
    unittest.main()
