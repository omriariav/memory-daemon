"""Inbox 0.12.2: a `duplicate` capture event is a row in the Inbox's REVIEW
section ("Not saved: near-duplicate of ..."), with `d` to dismiss it and `D`
to save it anyway.

Synthetic data only (invented names, ids and sources). The classes without
"UI" in their name need no Textual; the UI class skips without it. Every wait
polls a condition; a short pause stays only before a negative check."""
import importlib.util
import json
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

from workspace_daemon import events
from workspace_daemon.inbox import items as itm, sources, views
from workspace_daemon.inbox.actions import FakeActions, Result
from workspace_daemon.inbox.models import Item, Msg
from workspace_daemon.inbox.store import InboxStore

try:    # run by discovery (tests/ on sys.path) or by dotted module name
    from test_inbox_draft import wait_for
except ImportError:
    from tests.test_inbox_draft import wait_for

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp, review_cli_hint
    from workspace_daemon.inbox.screens import ConfirmWrite
    from workspace_daemon.inbox.widgets import ITEM_PREFIX, Queue

NOW = 1_700_000_000.0
MATCH = "2030-01-02-example-entry-one"
SIZE = (150, 42)
DEFAULTS = {"poll_interval_s": 300.0, "theme": "dark", "bidi_mode": "logical"}


def iso(offset_h=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(NOW + offset_h * 3600))


def dup(source_id="slack:C0EXAMPLE:100.5", title="Example sync notes",
        ago_h=1.0, duplicate_of=MATCH, actions=None, source="slack",
        routine="r"):
    details = {"untrusted": True}
    if duplicate_of:
        details["duplicate_of"] = duplicate_of
    if actions:
        details["owner_actions_not_filed"] = actions
    return events.build(routine, "duplicate", duplicate_of, source_id, title,
                        ["Ann Example"], ts=iso(-ago_h), details=details)


def new(source_id, memory_id, title, ago_h=2.0, routine="r"):
    return events.build(routine, "new", memory_id, source_id, title, [],
                        ts=iso(-ago_h))


def write_events(base, rows):
    state = Path(base) / "state"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "events.jsonl", "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class MappingTest(unittest.TestCase):
    def test_the_row_says_what_was_not_saved_and_why(self):
        item = sources.event_to_item(
            dup(actions=["Send the notes", "Book a room"]), NOW)
        self.assertEqual(itm.validate_item(item), [])
        self.assertEqual(item["source"], "memory")
        self.assertEqual(item["section"], "fyi")
        self.assertEqual(item["urgency"], "digest")
        self.assertEqual(item["actions"], ["ack"])
        self.assertEqual(item["gist"], f"Not saved: near-duplicate of {MATCH}")
        self.assertEqual(item["sender"], "Example sync notes")
        text = {m["who"]: m["text"] for m in item["thread"]}
        self.assertEqual(text["skipped"], "Example sync notes")
        self.assertEqual(text["source id"], "slack:C0EXAMPLE:100.5")
        self.assertEqual(text["matched"], MATCH)
        self.assertIn("- Send the notes", text["owner actions not filed"])
        self.assertIn("- Book a room", text["owner actions not filed"])
        meta = item["meta"]
        self.assertEqual((meta["kind"], meta["duplicate_of"]),
                         ("duplicate", MATCH))
        self.assertEqual(meta["source_id"], "slack:C0EXAMPLE:100.5")
        self.assertEqual(meta["title"], "Example sync notes")
        self.assertTrue(meta["thread_fixed"])
        # the event's memory_id is the matched entry: not this row's own
        self.assertNotIn("memory_id", meta)

    def test_every_capture_source_maps_to_a_review_row(self):
        for sid in ("gmail:18c0ffee12345678", "zoom:55501", "mila:abc",
                    "slack:C0EXAMPLE:1.5"):
            with self.subTest(sid=sid):
                item = sources.event_to_item(
                    dup(source_id=sid, source=sid.split(":")[0]), NOW)
                self.assertEqual(item["source"], "memory")
                self.assertIsNone(item["link"])

    def test_without_a_match_and_with_a_matched_title(self):
        item = sources.event_to_item(dup(duplicate_of=None), NOW)
        self.assertEqual(item["gist"],
                         "Not saved: near-duplicate of an existing entry")
        self.assertEqual(item["thread"][2]["text"], "not named")
        item = sources.event_to_item(
            dup(), NOW, match_titles={MATCH: "Earlier sync notes"})
        self.assertEqual(item["gist"],
                         "Not saved: near-duplicate of Earlier sync notes")
        self.assertIn("Earlier sync notes", item["thread"][2]["text"])

    def test_an_urgent_rule_does_not_make_it_urgent(self):
        rule = {"kind": "duplicate"}
        item = sources.event_to_item(dup(), NOW, rules=[rule])
        self.assertEqual(item["section"], "fyi")

    def test_text_from_the_event_is_cleaned(self):
        item = sources.event_to_item(
            dup(title="Line one\nline two", actions=["a\nb"]), NOW)
        self.assertEqual(itm.validate_item(item), [])
        self.assertNotIn("\n", item["sender"])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name

    def load(self):
        return sources.load_events(self.base, now=NOW)

    def test_rows_age_out_like_other_event_rows(self):
        write_events(self.base, [dup(source_id="slack:C0EXAMPLE:1.5", ago_h=1),
                                 dup(source_id="slack:C0EXAMPLE:2.5",
                                     ago_h=60)])      # past the 48 h window
        ids = [i["meta"]["source_id"] for i in self.load().items]
        self.assertEqual(ids, ["slack:C0EXAMPLE:1.5"])

    def test_one_row_per_event_id_and_per_source_id(self):
        row = dup()
        write_events(self.base, [row, row])         # the same line twice
        self.assertEqual(len(self.load().items), 1)
        write_events(self.base, [dup(ago_h=0.5)])    # a later event, same id
        (item,) = self.load().items
        self.assertEqual(item["ts"], NOW - 1800)

    def test_the_matched_entry_title_comes_from_its_capture_event(self):
        write_events(self.base, [new("slack:C0EXAMPLE:9.5", MATCH,
                                     "Earlier sync notes"), dup()])
        (item,) = [i for i in self.load().items if i["source"] == "memory"]
        self.assertIn("Earlier sync notes", item["gist"])

    def test_a_capture_row_of_the_same_source_id_stays(self):
        sid = "gmail:18c0ffee12345678"
        write_events(self.base, [new(sid, "2030-01-01-mail-one", "Mail one",
                                     ago_h=3, routine="mail"),
                                 dup(source_id=sid, source="gmail", ago_h=1,
                                     routine="mail")])
        kinds = sorted(i["source"] for i in self.load().items)
        self.assertEqual(kinds, ["email", "memory"])

    def test_a_refused_capture_is_not_the_items_memory_entry(self):
        index = sources.memory_index([dup()])
        self.assertEqual(index, {})


def row(item_dict):
    return Item(
        key=item_dict["id"], section=item_dict["section"],
        who=item_dict["sender"], where=item_dict["where"],
        ts=item_dict["ts"], gist=item_dict["gist"],
        source=item_dict["source"],
        messages=[Msg(m["who"], m["ts"], m["text"])
                  for m in item_dict["thread"]],
        actions=list(item_dict["actions"]), link=item_dict["link"],
        meta=dict(item_dict["meta"]), raw=item_dict)


class ViewTest(unittest.TestCase):
    def items(self):
        review = row(sources.event_to_item(dup(), NOW))
        other = row(itm.make_item(
            id="t:1", source="slack", section="needs_reply",
            urgency="digest", sender="Bo", where="#room", gist="g",
            ts=NOW, link=None, actions=["reply"]))
        return review, other

    def test_review_is_its_own_inbox_section_listed_only_when_present(self):
        review, other = self.items()
        shown = lambda rows: [g.title for g in views.inbox_groups(rows)  # noqa: E731
                              if g.items or g.keep_empty]
        self.assertEqual(shown([other]), ["URGENT", "I OWE", "NUDGE DUE"])
        self.assertEqual(shown([other, review]),
                         ["URGENT", "I OWE", "NUDGE DUE", "REVIEW"])
        groups = {g.key: g for g in views.inbox_groups([review, other])}
        self.assertEqual([i.key for i in groups["review"].items], [review.key])
        self.assertEqual([i.key for i in groups["owe"].items], [other.key])
        self.assertEqual(groups["urgent"].items, [])

    def test_a_feed_rule_cannot_double_list_it(self):
        review, other = self.items()
        review.feed = "inbox"          # a rule that promotes to the Inbox
        groups = {g.key: g for g in views.inbox_groups([review, other])}
        self.assertEqual(groups["urgent"].items, [])
        self.assertEqual(len(groups["review"].items), 1)
        review.feed = "show"
        feed = views.feed_view([review], NOW)
        self.assertEqual(feed.total, 0)


@dataclass
class SaveRecorder(FakeActions):
    """FakeActions that records the item and the force flag of a save."""
    saved_with: list = None

    def save(self, item, force=False):
        (self.saved_with if self.saved_with is not None
         else []).append((item.key, dict(item.raw), force))
        return Result(True, "would save to memory")


class NoSave(FakeActions):
    save_configured = False


class Fixed:
    def __init__(self, items):
        self.items = items

    def __call__(self):
        return [sources.SourceResult("events", list(self.items), False,
                                     None, None)]


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class ReviewUITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = InboxStore(Path(self.tmp.name))
        self.row = sources.event_to_item(
            dup(actions=["Send the notes"]), time.time(),
            match_titles={MATCH: "Earlier sync notes"})
        # the event is old for the test clock: shift it to "now"
        self.row["ts"] = time.time() - 60

    def app(self, actions):
        return InboxApp(Fixed([self.row]), self.store, actions,
                        settings=dict(DEFAULTS), undo_s=0.4, leave_s=0,
                        stream_delay=0, view="inbox")

    async def settle(self, pilot, app):
        self.assertTrue(await wait_for(
            pilot, lambda: app.polled_once and app.current_key == self.row["id"],
            tries=300))

    async def test_the_row_is_in_review_with_its_detail(self):
        app = self.app(FakeActions(store=self.store, draft_delay=0))
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            titles = [g.title for g in app.view_groups() if g.items]
            self.assertEqual(titles, ["REVIEW"])
            q = app.query_one("#queue", Queue)
            plain = q.get_option_at_index(q.get_option_index(
                ITEM_PREFIX + self.row["id"])).prompt.plain
            self.assertIn("Not saved: near-duplicate of Earlier sync notes",
                          plain)
            body = app.query_one("#thread-body").render().plain
            for want in ("Example sync notes", "slack:C0EXAMPLE:100.5",
                         MATCH, "Send the notes"):
                self.assertIn(want, body)
            hints = " ".join(f"{k} {v}" for k, v in app.hints())
            self.assertIn("D save anyway", hints)

    async def test_d_dismisses_it_with_a_local_mark(self):
        actions = FakeActions(store=self.store, draft_delay=0)
        app = self.app(actions)
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            await pilot.press("d")
            self.assertTrue(await wait_for(
                pilot, lambda: app.current_key is None and not app.running
                and not app.pending))
            self.assertTrue(self.store.is_hidden(self.row["id"], time.time()))
            # the only call is the local mark: no save, nothing sent
            self.assertEqual(actions.calls, [("mark_done", self.row["id"])])

    async def test_D_saves_anyway_through_the_save_command(self):
        actions = SaveRecorder(store=self.store, draft_delay=0, saved_with=[])
        app = self.app(actions)
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            await pilot.press("D")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmWrite)))
            await pilot.press("y")
            self.assertTrue(await wait_for(
                pilot, lambda: bool(actions.saved_with), tries=300))
            key, raw, force = actions.saved_with[0]
            self.assertEqual(key, self.row["id"])
            self.assertTrue(force)
            self.assertEqual(raw["meta"]["source_id"], "slack:C0EXAMPLE:100.5")
            self.assertEqual(raw["meta"]["title"], "Example sync notes")
            self.assertEqual(raw["meta"]["duplicate_of"], MATCH)
            self.assertTrue(await wait_for(
                pilot, lambda: app.current_key is None and not app.running))

    async def test_cancelling_the_confirm_saves_nothing(self):
        actions = SaveRecorder(store=self.store, draft_delay=0, saved_with=[])
        app = self.app(actions)
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            await pilot.press("D")
            self.assertTrue(await wait_for(
                pilot, lambda: isinstance(app.screen, ConfirmWrite)))
            await pilot.press("escape")
            self.assertTrue(await wait_for(
                pilot, lambda: not isinstance(app.screen, ConfirmWrite)))
            await pilot.pause(0.8)   # negative check: outlasts the undo window
            self.assertEqual(actions.saved_with, [])
            self.assertEqual(app.current_key, self.row["id"])

    async def test_without_a_save_command_D_copies_a_cli_hint(self):
        actions = NoSave(store=self.store, draft_delay=0)
        app = self.app(actions)
        async with app.run_test(size=SIZE) as pilot:
            await self.settle(pilot, app)
            with mock.patch.object(app, "copy_to_clipboard") as copied:
                await pilot.press("D")
                self.assertTrue(await wait_for(
                    pilot, lambda: copied.called))
            hint = copied.call_args.args[0]
            self.assertIn("--force-new", hint)
            self.assertIn("slack:C0EXAMPLE:100.5", hint)
            self.assertIn("Example sync notes", hint)
            toasts = [n.message for n in app._notifications]
            self.assertTrue(any("CLI hint" in t for t in toasts), toasts)
            self.assertFalse(isinstance(app.screen, ConfirmWrite))
            self.assertEqual(actions.calls, [])

    def test_the_hint_quotes_the_title(self):
        hint = review_cli_hint("slack:C0EXAMPLE:1.5", "it's; rm -rf x")
        self.assertIn("'it'\"'\"'s; rm -rf x'", hint)


if __name__ == "__main__":
    unittest.main()
