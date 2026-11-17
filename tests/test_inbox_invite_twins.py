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


def ev(source_id, kind, title, ago_h=1.0):
    return events.build("calendar-cache", kind, None, source_id, title, [],
                        ts=iso(-ago_h))


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

    def collect(self, marks=None):
        res = sources.collect(settings.default_settings(), self.base, now=NOW,
                              marks=marks)
        return {r.name: r.items for r in res}

    def calendar(self, marks=None):
        """Gists of the events stream's rows, as `Kind: title`."""
        return sorted(i["gist"].split(" (")[0]
                      for i in self.collect(marks)["events"])


class ListedTwinTest(Base):
    def test_new_is_hidden_while_its_invite_is_listed_update_stays(self):
        write_snapshot(self.base, {"aaa": row(30, title="Alpha"),
                                   "bbb": row(31, title="Beta")})
        write_events(self.base, [ev("gcal:aaa", "new", "Alpha"),
                                 ev("gcal:bbb", "update", "Beta"),
                                 ev("gcal:bbb", "new", "Beta", ago_h=5)])
        self.assertEqual(self.calendar(), ["Updated: Beta"])
        self.assertEqual(len(self.collect()["events.invites"]), 2)

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
        store = InboxStore(None)
        store.rsvp("invites:aaa", "accepted", "aaa")
        self.assertEqual(store.rsvp_series(), {"aaa": "answered"})
        self.assertEqual(self.calendar(store.rsvp_series()),
                         ["New invite: Gamma"])

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
        store = InboxStore(None)
        store.rsvp("invites:ccc", "declined", "ccc")
        self.assertEqual(self.calendar(store.rsvp_series()), [])

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
                                   "bbb": row(31, "accepted")})
        write_events(self.base, [ev("gcal:aaa", "update", "A"),
                                 ev("gcal:bbb", "update", "B"),
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


if __name__ == "__main__":
    unittest.main()
