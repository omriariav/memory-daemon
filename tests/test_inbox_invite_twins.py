"""Inbox 0.8.2: the events stream's calendar rows that an invite makes
redundant are hidden. Synthetic ids and titles only; no live data."""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import invites, settings, sources
from workspace_daemon.inbox.actions import FakeActions
from workspace_daemon.inbox.store import InboxStore

HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
if HAVE_TEXTUAL:
    from workspace_daemon.inbox.app import InboxApp

NOW = time.time()
H = 3600.0


def iso(offset_h=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + offset_h * H))


def write_events(base, rows):
    path = Path(base) / "state" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def ev(source_id, kind, title, ago_h=1.0, details=None):
    return events.build("calendar-cache", kind, None, source_id, title, [],
                        ts=iso(-ago_h), details=details)


CHANGED = {"changed": {"start": ["10:00", "11:00"], "title": ["Old", "New"]}}


def marks_at(answer, at, series="aaa", event=None):
    """The marks of an RSVP given at `at` (offset hours from NOW) for one
    event (default: the series itself)."""
    store = InboxStore(None, clock=lambda: NOW + at * H)
    store.rsvp(f"invites:{series}", answer, event or series)
    return store.rsvp_series()


def row(start_h, rsvp="needsAction", title="Planning chat"):
    return {"start": iso(start_h), "end": iso(start_h + 1), "status": "confirmed",
            "my_rsvp": rsvp, "title": title, "list_title": title,
            "location": "", "conference": ""}


def write_snapshot(base, rows, body=None):
    path = Path(base) / "state" / invites.SNAPSHOT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = body if body is not None else {
        "v": 1, "fetched_at": iso(-0.1), "window_hours": 168.0, "events": rows}
    path.write_text(json.dumps(doc), encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def collect(self, marks=None, closed=()):
        res = sources.collect(settings.default_settings(), self.base, now=NOW,
                              marks=marks, closed=closed)
        return {r.name: r.items for r in res}

    def calendar(self, marks=None, closed=()):
        """Gists of the events stream's rows, as `Kind: title`."""
        return sorted(i["gist"].split(" (")[0]
                      for i in self.collect(marks, closed)["events"])


class ListedTwinTest(Base):
    def test_new_and_update_are_hidden_while_the_invite_is_listed(self):
        write_snapshot(self.base, {"aaa": row(30, title="Alpha"),
                                   "bbb": row(31, title="Beta"),
                                   "ccc": row(32, title="Gamma")})
        write_events(self.base, [ev("gcal:aaa", "new", "Alpha"),
                                 ev("gcal:bbb", "update", "Beta"),
                                 ev("gcal:ccc", "cancel", "Gamma")])
        self.assertEqual(self.calendar(), ["Cancelled: Gamma"])
        self.assertEqual(len(self.collect()["events.invites"]), 3)

    def test_the_listed_invite_shows_the_latest_change(self):
        write_snapshot(self.base, {"bbb": row(31, title="Beta"),
                                   "ddd": row(33, title="Delta")})
        write_events(self.base, [ev("gcal:bbb", "update", "Beta",
                                    details=CHANGED)])
        by = {i["id"]: i for i in self.collect()["events.invites"]}
        thread = by["invites:bbb"]["thread"]
        line = [m for m in thread if m["who"] == "changed"]
        self.assertEqual(len(line), 1)
        self.assertEqual(line[0]["text"],
                         "start: 10:00 -> 11:00; title: Old -> New")
        self.assertEqual([m["who"] for m in thread][:3],
                         ["subject", "when", "changed"])
        self.assertFalse([m for m in by["invites:ddd"]["thread"]
                          if m["who"] == "changed"])

    def test_the_changed_line_is_capped_and_the_cache_is_not_changed(self):
        write_snapshot(self.base, {"bbb": row(31)})
        many = {"changed": {f"f{n}": ["a", "b"] for n in range(5)}}
        write_events(self.base, [ev("gcal:bbb", "update", "Beta",
                                    details=many)])
        units = sources.make_units(settings.default_settings(), self.base)
        batches = [list(u.run(NOW, False)) for u in units]
        merged = sources.merge(batches)
        line = [m for r in merged for i in r.items for m in i["thread"]
                if m["who"] == "changed"]
        self.assertEqual(line[0]["text"].count("->"), 3)
        self.assertTrue(line[0]["text"].endswith("+2 more"))
        cached = [m for r in batches[1] for i in r.items for m in i["thread"]]
        self.assertFalse([m for m in cached if m["who"] == "changed"])

    def test_a_cancel_while_listed_shows(self):
        write_snapshot(self.base, {"bbb": row(31, title="Beta")})
        write_events(self.base, [ev("gcal:bbb", "cancel", "Beta")])
        self.assertEqual(self.calendar(), ["Cancelled: Beta"])

    def test_recurring_instance_ids_normalize_to_the_series(self):
        write_snapshot(self.base, {"weekly_20231116T100000Z": row(30)})
        write_events(self.base, [
            ev("gcal:weekly_20231123T100000Z", "new", "Weekly"),
            ev("gcal:weekly_20231130", "cancel", "Weekly")])
        self.assertEqual(self.calendar(), ["Cancelled: Weekly"])


class AnsweredTest(Base):
    def test_hidden_after_an_inbox_rsvp_mark_no_snapshot_needed(self):
        write_events(self.base, [ev("gcal:aaa", "new", "Alpha"),
                                 ev("gcal:ccc", "new", "Gamma")])
        self.assertEqual(self.calendar(),
                         ["New invite: Alpha", "New invite: Gamma"])
        marks = marks_at("accepted", 0)
        self.assertEqual(marks, {"aaa": {"answer": "answered", "at": NOW,
                                 "event": "aaa"}})
        self.assertEqual(self.calendar(marks), ["New invite: Gamma"])

    def test_an_update_before_the_inbox_answer_is_hidden_after_it_shows(self):
        write_events(self.base, [
            ev("gcal:aaa", "update", "Before", ago_h=3),
            ev("gcal:bbb", "update", "After", ago_h=-1),
            ev("gcal:ccc", "update", "Other series", ago_h=3)])
        marks = dict(marks_at("accepted", 0), **marks_at("accepted", 0, "bbb"))
        self.assertEqual(self.calendar(marks),
                         ["Updated: After", "Updated: Other series"])

    def test_an_update_after_an_answer_given_elsewhere_stays(self):
        write_snapshot(self.base, {"aaa": row(30, "accepted")})
        write_events(self.base, [ev("gcal:aaa", "update", "Moved")])
        self.assertEqual(self.calendar(), ["Updated: Moved"])

    def test_the_inbox_answer_time_wins_when_the_snapshot_agrees(self):
        write_snapshot(self.base, {"aaa": row(30, "accepted")})
        write_events(self.base, [ev("gcal:aaa", "update", "Seen", ago_h=3)])
        self.assertEqual(self.calendar(marks_at("accepted", 0)), [])

    def test_new_is_hidden_when_the_snapshot_shows_it_answered(self):
        write_snapshot(self.base, {"aaa": row(30, "accepted"),
                                   "bbb": row(31, "tentative"),
                                   "ccc": row(32, "declined"),
                                   "ddd": row(33, "needsAction")})
        write_events(self.base, [ev(f"gcal:{k}", "new", k)
                                 for k in ("aaa", "bbb", "ccc", "ddd", "eee")])
        self.assertEqual(self.calendar(), ["New invite: eee"])

    def test_a_recurring_series_answered_through_any_instance(self):
        write_snapshot(self.base, {"w_20231116T100000Z": row(30, "accepted"),
                                   "w_20231123T100000Z": row(40, "accepted")})
        write_events(self.base, [ev("gcal:w_20231130T100000Z", "new", "W")])
        self.assertEqual(self.calendar(), [])

    def test_a_series_with_one_instance_still_pending_is_not_answered(self):
        data = {"doc": {"events": {"w_20231116": row(30, "accepted"),
                                   "w_20231123": row(40)}}}
        self.assertEqual(invites.answered_series(data), {})

    def test_declined_meeting_hides_update_and_cancel(self):
        write_snapshot(self.base, {"ccc": row(30, "declined"),
                                   "ddd": row(31, "declined")})
        write_events(self.base, [ev("gcal:ccc", "update", "C"),
                                 ev("gcal:ddd", "cancel", "D")])
        self.assertEqual(self.calendar(), [])

    def test_declined_by_an_inbox_mark_hides_update_too(self):
        write_events(self.base, [ev("gcal:ccc", "update", "C")])
        self.assertEqual(self.calendar(marks_at("declined", -5, "ccc")), [])

    def test_accepted_or_tentative_meeting_keeps_update_and_cancel(self):
        write_snapshot(self.base, {"aaa": row(30, "accepted"),
                                   "bbb": row(31, "tentative")})
        write_events(self.base, [ev("gcal:aaa", "update", "A"),
                                 ev("gcal:bbb", "cancel", "B")])
        self.assertEqual(self.calendar(), ["Cancelled: B", "Updated: A"])

    def test_a_series_declined_in_part_keeps_its_updates(self):
        data = {"doc": {"events": {"w_20231116": row(30, "declined"),
                                   "w_20231123": row(40, "accepted")}}}
        self.assertEqual(invites.answered_series(data), {"w": "answered"})

    def test_an_unrelated_calendar_item_is_untouched(self):
        write_snapshot(self.base, {"aaa": row(30, "declined")})
        write_events(self.base, [ev("gcal:zzz", "new", "Z"),
                                 ev("gcal:yyy", "update", "Y"),
                                 ev("gcal:xxx", "cancel", "X")])
        self.assertEqual(self.calendar(),
                         ["Cancelled: X", "New invite: Z", "Updated: Y"])

    def test_an_item_that_is_not_a_calendar_row_is_untouched(self):
        write_snapshot(self.base, {"aaa": row(30, "declined")})
        write_events(self.base, [events.build(
            "mail", "new", None, "gmail:aaaaaaaa11111111", "A mail", [],
            ts=iso(-1))])
        self.assertEqual(len(self.collect()["events"]), 1)


class ReviewFixesTest(Base):
    W1, W2, W3 = "w_20231116T100000Z", "w_20231123T100000Z", "w_20231130T100000Z"

    def test_a_mark_for_another_instance_is_ignored_when_the_series_moves_on(self):
        # Instance 1 was declined; the snapshot now lists instance 2.
        write_snapshot(self.base, {self.W1: row(-100, "declined"),
                                   self.W2: row(30)})
        write_events(self.base, [
            ev(f"gcal:{self.W2}", "update", "W", details=CHANGED),
            ev("gcal:w", "cancel", "W whole series")])
        marks = marks_at("declined", -20, "w", self.W1)
        self.assertEqual(self.calendar(marks), ["Cancelled: W whole series"])
        inv = self.collect(marks)["events.invites"]
        self.assertEqual([m["who"] for i in inv for m in i["thread"]
                          if m["who"] == "changed"], ["changed"])

    def test_a_declined_mark_hides_only_its_own_instance_when_not_listed(self):
        write_snapshot(self.base, {self.W1: row(30, "declined"),
                                   self.W2: row(40, "accepted")})
        write_events(self.base, [ev(f"gcal:{self.W1}", "cancel", "One"),
                                 ev("gcal:w", "cancel", "Whole")])
        # The snapshot has an accepted row, so only the mark declines.
        marks = marks_at("declined", -5, "w", self.W1)
        self.assertEqual(self.calendar(marks), ["Cancelled: Whole"])

    def test_an_unconfirmed_decline_does_not_hide_a_cancel(self):
        write_snapshot(self.base, {"ccc": row(30)})   # still needsAction
        write_events(self.base, [ev("gcal:ccc", "cancel", "C")])
        # Answered 2h ago; the snapshot was fetched 0.1h ago: not confirmed.
        self.assertEqual(self.calendar(marks_at("declined", -2, "ccc")),
                         ["Cancelled: C"])
        # Answered after the snapshot: the decline still stands.
        self.assertEqual(self.calendar(marks_at("declined", 0, "ccc")), [])

    def test_an_acked_or_done_invite_no_longer_counts_as_listed(self):
        write_snapshot(self.base, {"aaa": row(30), "bbb": row(31),
                                   "ccc": row(32)})
        write_events(self.base, [ev("gcal:aaa", "update", "A",
                                    details=CHANGED),
                                 ev("gcal:bbb", "update", "B"),
                                 ev("gcal:ccc", "update", "C")])
        store = InboxStore(None)
        store.ack("invites:aaa")
        store.done("invites:bbb")
        store.snooze("invites:ccc", NOW + H)
        self.assertEqual(store.closed_ids(), {"invites:aaa", "invites:bbb"})
        self.assertEqual(self.calendar(closed=store.closed_ids()),
                         ["Updated: A", "Updated: B"])

    def test_an_update_of_another_instance_is_neither_hidden_nor_carried(self):
        write_snapshot(self.base, {self.W2: row(30)})
        write_events(self.base, [
            ev(f"gcal:{self.W3}", "update", "Other instance", details=CHANGED),
            ev(f"gcal:{self.W2}", "update", "Same instance")])
        self.assertEqual(self.calendar(), ["Updated: Other instance"])
        inv = self.collect()["events.invites"]
        self.assertFalse([m for i in inv for m in i["thread"]
                          if m["who"] == "changed"])

    def test_an_update_of_another_instance_is_not_seen_at_the_answer(self):
        write_snapshot(self.base, {self.W1: row(30, "accepted"),
                                   self.W2: row(40, "accepted")})
        write_events(self.base, [
            ev(f"gcal:{self.W1}", "update", "Answered", ago_h=3),
            ev(f"gcal:{self.W2}", "update", "Other", ago_h=3)])
        marks = marks_at("accepted", 0, "w", self.W1)
        self.assertEqual(self.calendar(marks), ["Updated: Other"])


class OddSnapshotTest(Base):
    def check_kept(self):
        write_events(self.base, [ev("gcal:aaa", "new", "A"),
                                 ev("gcal:bbb", "update", "B")])
        self.assertEqual(self.calendar(), ["New invite: A", "Updated: B"])

    def test_missing_snapshot(self):
        self.check_kept()

    def test_unreadable_or_odd_snapshots_do_not_crash(self):
        path = self.base / "state" / invites.SNAPSHOT_FILE
        path.parent.mkdir(parents=True)
        for text in ("{{{", "[]", "null", '{"v": 2}',
                     '{"v": 1, "fetched_at": "x", "events": []}',
                     json.dumps({"v": 1, "fetched_at": iso(-0.1),
                                 "events": {"a": 3, "b": None, 7: {},
                                            "c": {"my_rsvp": ["x"]},
                                            "d": {"my_rsvp": {}}}})):
            path.write_text(text, encoding="utf-8")
            (self.base / "state" / "events.jsonl").unlink(missing_ok=True)
            self.check_kept()

    def test_answered_series_tolerates_anything(self):
        for data in ({}, None, {"doc": None}, {"doc": {"events": []}},
                     {"error": "x"}, {"doc": {"events": {1: {}, "a": []}}}):
            self.assertEqual(invites.answered_series(data), {})


class NoMutationTest(Base):
    def test_merge_leaves_the_cached_rows_alone(self):
        write_snapshot(self.base, {"aaa": row(30, "declined")})
        write_events(self.base, [ev("gcal:aaa", "update", "A")])
        units = sources.make_units(settings.default_settings(), self.base)
        batches = [list(u.run(NOW, False)) for u in units]
        before = [[list(r.items) for r in b] for b in batches]
        merged = sources.merge(batches, sources.units_answers(units))
        self.assertEqual(merged[0].items, [])
        self.assertEqual([[list(r.items) for r in b] for b in batches], before)
        self.assertEqual(len(before[0][0]), 1)


@unittest.skipUnless(HAVE_TEXTUAL, "Textual not installed")
class AppRefreshTest(Base):
    def test_run_units_hides_twins_on_refresh_and_reeval(self):
        write_snapshot(self.base, {"aaa": row(30, "declined"),
                                   "bbb": row(31, "accepted"),
                                   "lst": row(34)})
        write_events(self.base, [ev("gcal:aaa", "update", "A"),
                                 ev("gcal:bbb", "update", "B"),
                                 ev("gcal:lst", "update", "L",
                                    details=CHANGED),
                                 ev("gcal:ccc", "new", "C"),
                                 ev("gcal:marked", "new", "M")])
        cfg = settings.default_settings()
        store = InboxStore(None)
        store.rsvp("invites:marked", "tentative", "marked")
        app = InboxApp(lambda: [], store, FakeActions(store=store),
                       settings=dict(cfg), units=sources.make_units(
                           cfg, self.base), stream_delay=0)

        def gists(results):
            return sorted(i["gist"].split(" (")[0] for r in results
                          if r.name == "events" for i in r.items)
        want = ["New invite: C", "Updated: B"]
        self.assertEqual(gists(app._run_units(None, False)), want)
        self.assertEqual(gists(app._run_units(None, False, reeval=True)), want)
        inv = [i for r in app._run_units(None, False, reeval=True)
               if r.name == "events.invites" for i in r.items]
        self.assertEqual([m["who"] for i in inv for m in i["thread"]
                          if m["who"] == "changed"], ["changed"])


if __name__ == "__main__":
    unittest.main()
