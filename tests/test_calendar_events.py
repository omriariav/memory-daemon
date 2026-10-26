"""calendar_cache event diff: snapshot, new/update/cancel events, failure safety."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_calendar_cache import LIST_TOOL, NOW, cfg
from workspace_daemon import calendar_cache, claude_bridge

DAY = datetime.timedelta(days=1)


def at(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def block(event_id, start, hours=1, title="Sync", location="", rsvp="needsAction",
          organizer="alice.smith@example.com", status=None, extra=""):
    end = start + datetime.timedelta(hours=hours)
    lines = [f"- {title}", f"  Time: {at(start)} - {at(end)}",
             f"  ID: {event_id}", f"  Organizer: {organizer}",
             "  Attendees: alice.smith@example.com (accepted), "
             f"me@example.com ({rsvp}, self){extra}"]
    if location:
        lines.insert(3, f"  Location: {location}")
    if status:
        lines.append(f"  Status: {status}")
    return "\n".join(lines) + "\n"


def listing(*blocks):
    return f"Found {len(blocks)} event(s):\n\n" + "\n".join(blocks)


def soon(days=3):
    return NOW + days * DAY


class EventsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "base"
        self.base.mkdir()
        self.out = Path(tmp.name) / "out" / "next48h.txt"
        self.cfg = cfg(output=str(self.out), events_window_days=7)
        self.stream = self.base / "state" / "events.jsonl"
        self.snapshot = self.base / "state" / "calendar-snapshot.json"

    def run_at(self, text, now=NOW, **over):
        config_ = dict(self.cfg, **over)
        call = {"name": LIST_TOOL, "text": text, "is_error": False,
                "input": calendar_cache.list_args(config_, now)}
        with mock.patch.object(
            calendar_cache.claude_bridge, "call_tools", return_value=[call]
        ):
            return calendar_cache.run(config_, self.base, now=now)

    def emitted(self):
        if not self.stream.exists():
            return []
        return [json.loads(x) for x in self.stream.read_text().splitlines()]

    def seed(self, *blocks, now=NOW):
        self.run_at(listing(*blocks) if blocks else "No events found", now=now)
        self.assertEqual(self.emitted(), [])

    def test_fetch_is_one_wide_call_and_output_stays_48h(self):
        args = calendar_cache.list_args(self.cfg, NOW)
        self.assertEqual(args["time_max"], at(NOW + 7 * DAY))
        self.assertEqual(args["max_results"], 50)
        self.run_at(listing(block("far", soon(5), title="Far away"),
                            block("near", soon(1), title="Near by")))
        text = self.out.read_text()
        self.assertIn("Near by", text)
        self.assertNotIn("Far away", text)

    def test_first_run_is_silent_and_writes_snapshot(self):
        self.seed(block("a", soon(), title="Pending"))
        data = json.loads(self.snapshot.read_text())
        self.assertEqual(data["events"]["a"], {
            "start": "2026-10-23T08:30:00+00:00",
            "end": "2026-10-23T09:30:00+00:00",
            "location": "", "conference": "", "status": "confirmed",
            "my_rsvp": "needsAction", "title": "Pending",
        })
        self.assertEqual(self.snapshot.stat().st_mode & 0o777, 0o600)

    def test_pending_invite_is_new(self):
        self.seed()
        report = self.run_at(listing(block("a", soon(), title="Pending")))
        (event,) = self.emitted()
        self.assertEqual(report["emitted"], 1)
        self.assertEqual(
            (event["kind"], event["source"], event["source_id"],
             event["memory_id"], event["title"], event["people"],
             event["routine"]),
            ("new", "gcal", "gcal:a", None, "Pending", [], "calendar-cache"),
        )
        self.assertEqual(event["details"], {
            "start": "2026-10-23T08:30:00+00:00",
            "end": "2026-10-23T09:30:00+00:00",
        })

    def test_accepted_and_organized_are_not_new(self):
        self.seed()
        self.run_at(listing(
            block("acc", soon(), rsvp="accepted"),
            block("org", soon(), organizer="me@example.com", rsvp="accepted"),
            block("tent", soon(), rsvp="tentative"),
        ))
        self.assertEqual(self.emitted(), [])

    def test_time_change_is_update_with_changed(self):
        self.seed(block("a", soon(), rsvp="accepted"))
        self.run_at(listing(block("a", soon() + datetime.timedelta(hours=2),
                                  rsvp="accepted")))
        (event,) = self.emitted()
        self.assertEqual(event["kind"], "update")
        self.assertEqual(event["details"]["changed"], {
            "start": ["2026-10-23T08:30:00+00:00", "2026-10-23T10:30:00+00:00"],
            "end": ["2026-10-23T09:30:00+00:00", "2026-10-23T11:30:00+00:00"],
        })
        self.assertEqual(event["details"]["start"], "2026-10-23T10:30:00+00:00")

    def test_location_change_is_update(self):
        self.seed(block("a", soon(), rsvp="accepted", location="Room 1"))
        self.run_at(listing(block("a", soon(), rsvp="accepted",
                                  location="Room 2")))
        (event,) = self.emitted()
        self.assertEqual(event["details"]["changed"],
                         {"location": ["Room 1", "Room 2"]})

    def test_conference_link_change_is_update(self):
        self.seed(block("a", soon(), rsvp="accepted",
                        location="https://zoom.us/j/111"))
        self.run_at(listing(block("a", soon(), rsvp="accepted",
                                  location="https://zoom.us/j/222")))
        (event,) = self.emitted()
        self.assertEqual(event["details"]["changed"], {
            "conference": ["https://zoom.us/j/111", "https://zoom.us/j/222"],
        })

    def test_attendee_and_own_rsvp_changes_are_silent(self):
        self.seed(block("a", soon(), rsvp="needsAction"))
        self.run_at(listing(block(
            "a", soon(), rsvp="accepted",
            extra=", bob.jones@example.com (declined)")))
        self.assertEqual(self.emitted(), [])

    def test_status_cancelled_is_cancel_once(self):
        self.seed(block("a", soon(), rsvp="accepted"))
        text = listing(block("a", soon(), rsvp="accepted", status="cancelled"))
        self.run_at(text)
        (event,) = self.emitted()
        self.assertEqual((event["kind"], event["source_id"]),
                         ("cancel", "gcal:a"))
        self.run_at(text)
        self.assertEqual(len(self.emitted()), 1)

    def test_disappearance_in_window_is_cancel(self):
        self.seed(block("a", soon(), rsvp="accepted", title="Gone"))
        self.run_at("No events found")
        (event,) = self.emitted()
        self.assertEqual((event["kind"], event["title"]), ("cancel", "Gone"))
        self.assertEqual(event["details"]["start"],
                         "2026-10-23T08:30:00+00:00")

    def test_started_or_ended_event_vanishing_is_silent(self):
        self.seed(block("a", NOW + datetime.timedelta(hours=1),
                        rsvp="accepted"))
        self.run_at("No events found", now=NOW + datetime.timedelta(hours=3))
        self.assertEqual(self.emitted(), [])

    def test_event_beyond_a_shrunk_window_is_silent(self):
        self.seed(block("a", soon(5), rsvp="accepted"))
        self.run_at("No events found", events_window_days=3)
        self.assertEqual(self.emitted(), [])

    def test_old_invite_scrolling_into_window_is_not_new(self):
        self.seed(block("near", soon(1), rsvp="accepted"))
        self.run_at(listing(block("old", NOW + 8 * DAY)), now=NOW + 2 * DAY)
        self.assertEqual(self.emitted(), [])

    def test_declined_then_removed_is_silent(self):
        self.seed(block("a", soon(), rsvp="declined"))
        self.run_at("No events found")
        self.assertEqual(self.emitted(), [])

    def test_organizer_event_time_change_is_silent(self):
        mine = dict(organizer="me@example.com", rsvp="accepted")
        self.seed(block("a", soon(), **mine))
        self.run_at(listing(block("a", soon() + datetime.timedelta(hours=2),
                                  **mine)))
        self.assertEqual(self.emitted(), [])

    def test_organizer_event_vanishing_is_silent(self):
        self.seed(block("a", soon(), organizer="me@example.com",
                        rsvp="accepted"))
        self.run_at("No events found")
        self.assertEqual(self.emitted(), [])

    def test_declined_event_time_change_is_silent(self):
        self.seed(block("a", soon(), rsvp="declined"))
        self.run_at(listing(block("a", soon() + datetime.timedelta(hours=2),
                                  rsvp="declined")))
        self.assertEqual(self.emitted(), [])

    def test_failed_fetch_does_not_diff_or_touch_snapshot(self):
        self.seed(block("a", soon(), rsvp="accepted"))
        before = self.snapshot.read_text()
        with mock.patch.object(
            calendar_cache.claude_bridge, "call_tools",
            side_effect=claude_bridge.BridgeError("boom"),
        ):
            with self.assertRaises(claude_bridge.BridgeError):
                calendar_cache.run(self.cfg, self.base, now=NOW)
        with self.assertRaises(calendar_cache.CalendarParseError):
            self.run_at("Something odd")
        self.assertEqual(self.snapshot.read_text(), before)
        self.assertEqual(self.emitted(), [])

    def test_truncated_listing_does_not_diff(self):
        self.seed(block("a", soon(), rsvp="accepted"))
        before = self.snapshot.read_text()
        self.run_at(listing(block("b", soon(2), rsvp="accepted")),
                    max_events=1)
        self.assertEqual(self.snapshot.read_text(), before)
        self.assertEqual(self.emitted(), [])

    def test_zero_days_disables_the_diff(self):
        self.run_at(listing(block("a", soon(1))), events_window_days=0)
        self.assertFalse(self.snapshot.exists())

    def test_corrupt_snapshot_is_a_first_run(self):
        self.seed(block("a", soon(), rsvp="accepted"))
        self.snapshot.write_text("{not json")
        self.run_at("No events found")
        self.assertEqual(self.emitted(), [])
        self.assertEqual(json.loads(self.snapshot.read_text())["events"], {})


if __name__ == "__main__":
    unittest.main()
