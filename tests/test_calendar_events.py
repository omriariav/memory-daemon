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

    def run_far(self, main_text, far_texts, now=NOW, **over):
        """Main listing answers the main session; far_texts[i] window i."""
        config_ = dict(self.cfg, **dict({"invite_lookahead_days": 21}, **over))
        windows = calendar_cache.invite_windows(config_, now)
        main_args = calendar_cache.list_args(config_, now)
        far_calls = []

        def bridge(prompt, tools, **kwargs):
            args = tools[LIST_TOOL]
            if args == main_args:
                return [{"name": LIST_TOOL, "text": main_text,
                         "is_error": False, "input": args}]
            far_calls.append(args)
            text = far_texts[windows.index(args)]
            if text is None:
                raise claude_bridge.BridgeError("session failed")
            return [{"name": LIST_TOOL, "text": text, "is_error": False,
                     "input": args}]

        with mock.patch.object(calendar_cache.claude_bridge, "call_tools",
                               side_effect=bridge):
            calendar_cache.run(config_, self.base, now=now)
        return windows, far_calls

    def test_far_windows_start_where_the_main_listing_ends(self):
        windows = calendar_cache.invite_windows(
            dict(self.cfg, invite_lookahead_days=21), NOW)
        self.assertEqual([(w["time_min"], w["time_max"]) for w in windows], [
            (at(NOW + 7 * DAY), at(NOW + 14 * DAY)),
            (at(NOW + 14 * DAY), at(NOW + 21 * DAY)),
        ])
        self.assertEqual(calendar_cache.invite_windows(self.cfg, NOW), [])

    def test_new_far_invites_are_announced_once_after_the_first_scan(self):
        old = block("old", soon(10), title="Already there")
        self.run_far(listing(), [listing(old), listing()])
        self.assertEqual(self.emitted(), [])
        new = block("new", soon(12), title="Offsite")
        answered = block("ok", soon(13), title="Answered", rsvp="accepted")
        hour = datetime.timedelta(hours=1)
        _, calls = self.run_far(listing(), [listing(old, new, answered),
                                            listing()], now=NOW + hour)
        self.assertEqual(len(calls), 2)
        (event,) = self.emitted()
        self.assertEqual((event["kind"], event["title"], event["source_id"]),
                         ("new", "Offsite", "gcal:new"))
        self.assertTrue(event["details"]["untrusted"])
        self.run_far(listing(), [listing(old, new), listing()],
                     now=NOW + 2 * hour)
        self.assertEqual(len(self.emitted()), 1)

    def test_far_scan_runs_at_most_hourly_and_backs_off_on_failure(self):
        _, first = self.run_far(listing(), [None, listing()])
        self.assertEqual(len(first), 1)
        _, soon_after = self.run_far(
            listing(), [listing(), listing()],
            now=NOW + datetime.timedelta(minutes=15))
        self.assertEqual(soon_after, [])
        _, later = self.run_far(
            listing(), [listing(), listing()],
            now=NOW + datetime.timedelta(minutes=59))
        self.assertEqual(len(later), 2)

    def test_an_incomplete_first_scan_announces_nothing_later(self):
        there = block("there", soon(16), title="Already there")
        self.run_far(listing(), [None, listing(there)])
        self.run_far(listing(), [listing(), listing(there)],
                     now=NOW + datetime.timedelta(hours=1))
        self.assertEqual(self.emitted(), [])

    def test_a_far_recurring_series_is_announced_once(self):
        self.run_far(listing(), [listing(), listing()])
        hour = datetime.timedelta(hours=1)
        series = [block(f"wk_202611{d:02d}T090000Z", soon(d - 13),
                        title="Weekly all-hands") for d in (21, 28)]
        self.run_far(listing(), [listing(series[0]), listing(series[1])],
                     now=NOW + hour)
        self.run_far(listing(), [listing(series[0]), listing(series[1])],
                     now=NOW + 2 * hour)
        self.assertEqual([e["title"] for e in self.emitted()],
                         ["Weekly all-hands"])

    def test_an_event_starting_inside_the_diff_window_is_not_announced(self):
        self.run_far(listing(), [listing(), listing()])
        spans = block("spans", soon(6) + datetime.timedelta(hours=23),
                      hours=48, title="Offsite")
        self.run_far(listing(), [listing(spans), listing()],
                     now=NOW + datetime.timedelta(hours=1))
        self.assertEqual(self.emitted(), [])

    def test_a_truncated_first_scan_records_nothing(self):
        there = block("there", soon(10), title="Already there")
        self.run_far(listing(there), [listing(there), listing()],
                     max_events=1)
        path = self.base / "state" / "calendar-invites.json"
        self.assertNotIn("seen", json.loads(path.read_text()))

    def test_a_series_the_diff_already_tracks_is_not_announced(self):
        def inst(day):
            return block(f"wk_202610{day:02d}T090000Z", soon(day - 20),
                         title="Weekly")
        # The diff sees (and announces) the first instance near-term.
        self.run_far(listing(inst(23)), [listing(), listing()])
        self.run_far(listing(inst(23)), [listing(inst(30)), listing()],
                     now=NOW + datetime.timedelta(hours=1))
        self.assertEqual(self.emitted(), [])

    def test_a_changed_range_rebaselines_silently(self):
        far = block("far", soon(25), title="Far")
        self.run_far(listing(), [listing(), listing()])
        self.run_far(listing(), [listing(), listing(), listing(far)],
                     now=NOW + datetime.timedelta(hours=1),
                     invite_lookahead_days=28)
        self.assertEqual(self.emitted(), [])

    def test_a_monthly_series_is_not_announced_again(self):
        self.run_far(listing(), [listing(), listing()])
        nov = block("mo_20261102T090000Z", soon(16), title="Monthly")
        dec = block("mo_20261202T090000Z", soon(46), title="Monthly")
        self.run_far(listing(), [listing(), listing(nov)],
                     now=NOW + datetime.timedelta(hours=1))
        later = NOW + datetime.timedelta(days=30)
        self.run_far(listing(), [listing(), listing(dec)], now=later)
        self.assertEqual(len(self.emitted()), 1)

    def test_a_far_scan_failure_never_fails_the_refresh(self):
        with mock.patch.object(calendar_cache, "scan_far_invites",
                               side_effect=RuntimeError("boom")):
            report = self.run_at(listing(block("a", soon(1), title="Near")))
        self.assertTrue(report["ok"])

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
            "list_title": "Pending",
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
            "untrusted": True,
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

    def test_title_change_is_update(self):
        self.seed(block("a", soon(), rsvp="accepted", title="Sync"))
        self.run_at(listing(block("a", soon(), rsvp="accepted",
                                  title="Sync (moved agenda)")))
        (event,) = self.emitted()
        self.assertEqual(event["kind"], "update")
        self.assertEqual(event["details"]["changed"],
                         {"title": ["Sync", "Sync (moved agenda)"]})

    def test_snapshot_without_title_is_not_a_title_change(self):
        self.seed(block("a", soon(), rsvp="accepted"))
        data = json.loads(self.snapshot.read_text())
        del data["events"]["a"]["list_title"]
        data["events"]["a"]["title"] = "Something else"
        self.snapshot.write_text(json.dumps(data))
        self.run_at(listing(block("a", soon(), rsvp="accepted")))
        self.assertEqual(self.emitted(), [])

    def mine(self, guest_rsvp, title="Mine"):
        return block("m", soon(), title=title, organizer="me@example.com",
                     rsvp="accepted",
                     extra=f", bob.jones@example.com ({guest_rsvp})")

    def test_attendee_answer_on_my_event_is_rsvp(self):
        self.seed(self.mine("needsAction"))
        self.run_at(listing(self.mine("accepted")))
        (event,) = self.emitted()
        self.assertEqual(event["kind"], "rsvp")
        self.assertEqual(event["details"]["changed"],
                         {"Bob Jones": ["needsAction", "accepted"]})

    def test_unchanged_answers_and_others_events_emit_no_rsvp(self):
        self.seed(self.mine("accepted"),
                  block("o", soon(), rsvp="accepted",
                        extra=", bob.jones@example.com (needsAction)"))
        self.run_at(listing(
            self.mine("accepted"),
            block("o", soon(), rsvp="accepted",
                  extra=", bob.jones@example.com (declined)")))
        self.assertEqual(self.emitted(), [])

    def test_added_attendee_is_not_an_rsvp_change(self):
        self.seed(block("m", soon(), title="Mine", organizer="me@example.com",
                        rsvp="accepted"))
        self.run_at(listing(self.mine("accepted")))
        self.assertEqual(self.emitted(), [])

    def test_only_my_events_store_rsvps_without_me_or_organizer(self):
        self.seed(self.mine("accepted"), block("o", soon(), rsvp="accepted"))
        data = json.loads(self.snapshot.read_text())
        self.assertEqual(data["events"]["m"]["rsvps"], {
            "alice.smith@example.com": ["Alice Smith", "accepted"],
            "bob.jones@example.com": ["Bob Jones", "accepted"],
        })
        self.assertNotIn("rsvps", data["events"]["o"])

    def test_same_names_are_labelled_by_email(self):
        two = lambda a, b: block(
            "m", soon(), title="Mine", organizer="me@example.com",
            rsvp="accepted",
            extra=f", bob.jones@example.com ({a}), bob.jones@other.test ({b})")
        self.seed(two("needsAction", "needsAction"))
        self.run_at(listing(two("accepted", "declined")))
        (event,) = self.emitted()
        self.assertEqual(event["details"]["changed"], {
            "bob.jones@example.com": ["needsAction", "accepted"],
            "bob.jones@other.test": ["needsAction", "declined"],
        })

    def test_malformed_old_entry_is_skipped_not_fatal(self):
        self.seed(self.mine("needsAction"))
        data = json.loads(self.snapshot.read_text())
        data["events"]["m"]["rsvps"]["bob.jones@example.com"] = ["Bob"]
        self.snapshot.write_text(json.dumps(data))
        self.run_at(listing(self.mine("accepted")))
        self.assertEqual(self.emitted(), [])

    def test_series_answer_is_reported_once(self):
        def inst(day, rsvp):
            return block(f"ser_2026102{day}T083000Z", soon(day - 2),
                         title="Daily", organizer="me@example.com",
                         rsvp="accepted",
                         extra=f", bob.jones@example.com ({rsvp})")
        self.seed(inst(3, "needsAction"), inst(4, "needsAction"))
        self.run_at(listing(inst(3, "accepted"), inst(4, "accepted")))
        self.assertEqual([e["kind"] for e in self.emitted()], ["rsvp"])

    def test_name_shared_with_an_unchanged_attendee_uses_email(self):
        two = lambda a: block(
            "m", soon(), title="Mine", organizer="me@example.com",
            rsvp="accepted",
            extra=f", bob.jones@example.com ({a}), bob.jones@other.test (accepted)")
        self.seed(two("needsAction"))
        self.run_at(listing(two("declined")))
        (event,) = self.emitted()
        self.assertEqual(event["details"]["changed"],
                         {"bob.jones@example.com": ["needsAction", "declined"]})

    def test_series_rename_is_reported_once(self):
        def inst(day, title):
            return block(f"ser_2026102{day}T083000Z", soon(day - 2),
                         title=title, rsvp="accepted")
        self.seed(inst(3, "Daily"), inst(4, "Daily"))
        self.run_at(listing(inst(3, "Standup"), inst(4, "Standup")))
        self.assertEqual([e["kind"] for e in self.emitted()], ["update"])

    def test_change_past_the_shown_length_is_not_an_update(self):
        long = "x" * 220
        self.seed(block("a", soon(), rsvp="accepted", title=long + "A"))
        self.run_at(listing(block("a", soon(), rsvp="accepted",
                                  title=long + "B")))
        self.assertEqual(self.emitted(), [])

    def test_owner_alias_marked_self_is_not_an_attendee(self):
        self.seed(block("m", soon(), title="Mine", organizer="me@example.com",
                        rsvp="accepted",
                        extra=", me.alias@example.com (needsAction, self)"))
        data = json.loads(self.snapshot.read_text())
        self.assertNotIn("me.alias@example.com", data["events"]["m"]["rsvps"])

    def test_series_move_is_reported_once(self):
        def inst(day, hour):
            return block(f"ser_2026102{day}T083000Z",
                         soon(day - 2) + datetime.timedelta(hours=hour),
                         title="Daily", rsvp="accepted")
        self.seed(inst(3, 0), inst(4, 0), inst(5, 0))
        self.run_at(listing(inst(3, 1), inst(4, 1), inst(5, 1)))
        self.assertEqual([e["kind"] for e in self.emitted()], ["update"])

    def test_series_answer_is_not_repeated_beside_a_single_answer(self):
        def inst(day, bob, carol):
            return block(f"ser_2026102{day}T083000Z", soon(day - 2),
                         title="Daily", organizer="me@example.com",
                         rsvp="accepted",
                         extra=f", bob.jones@example.com ({bob}), "
                               f"carol.lee@example.com ({carol})")
        self.seed(inst(3, "needsAction", "needsAction"),
                  inst(4, "needsAction", "needsAction"))
        self.run_at(listing(inst(3, "accepted", "accepted"),
                            inst(4, "accepted", "needsAction")))
        changes = [e["details"]["changed"] for e in self.emitted()]
        bob = "Bob Jones"
        self.assertEqual(sum(bob in c for c in changes), 1)

    def test_series_deletion_is_one_cancel_listing_instances(self):
        def inst(day):
            return block(f"ser_2026102{day}T083000Z", soon(day - 2),
                         title="Daily", rsvp="accepted")
        self.seed(inst(3), inst(4))
        self.run_at(listing(block("other", soon(), rsvp="accepted")))
        (event,) = self.emitted()
        self.assertEqual(event["kind"], "cancel")
        self.assertEqual(event["details"]["instances"],
                         ["ser_20261023T083000Z", "ser_20261024T083000Z"])

    def test_series_invite_is_one_new(self):
        def inst(day):
            return block(f"ser_2026102{day}T083000Z", soon(day - 2),
                         title="Daily")
        self.seed()
        self.run_at(listing(inst(3), inst(4), inst(5)))
        self.assertEqual([e["kind"] for e in self.emitted()], ["new"])

    def test_external_title_change_past_100_chars_is_not_an_update(self):
        long = "x" * 120
        ext = dict(organizer="eve@evil.test", rsvp="accepted")
        self.seed(block("a", soon(), title=long + "A", **ext))
        self.run_at(listing(block("a", soon(), title=long + "B", **ext)))
        self.assertEqual(self.emitted(), [])

    def test_unhashable_old_answer_is_skipped(self):
        self.seed(self.mine("needsAction"))
        data = json.loads(self.snapshot.read_text())
        data["events"]["m"]["rsvps"]["bob.jones@example.com"] = ["Bob", ["x"]]
        self.snapshot.write_text(json.dumps(data))
        self.run_at(listing(self.mine("accepted")))
        self.assertEqual(self.emitted(), [])

    def test_snapshot_without_rsvps_compares_nothing(self):
        self.seed(self.mine("needsAction"))
        data = json.loads(self.snapshot.read_text())
        del data["events"]["m"]["rsvps"]
        self.snapshot.write_text(json.dumps(data))
        self.run_at(listing(self.mine("accepted")))
        self.assertEqual(self.emitted(), [])

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
