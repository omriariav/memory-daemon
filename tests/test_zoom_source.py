"""Zoom summaries: discovery from the calendar cache, back-off, capture, links.

Fixtures follow the connector's real assets text (one JSON object) and the
calendar_cache JSON twin. All names, ids and hosts are placeholders.
"""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import claude_bridge, config, memory_sink, zoom_source

TOOL = "mcp__example_zoom__get_meeting_assets"
T0 = datetime.datetime(2026, 7, 1, 10, 0, tzinfo=datetime.timezone.utc)


def iso(moment):
    return moment.isoformat()


def event(event_id="ev1", number="91234567890", start=T0, minutes=30,
          rsvp="accepted", url=None, title="Weekly sync"):
    return {
        "event_id": event_id, "title": title, "all_day": False,
        "start": iso(start), "end": iso(start + datetime.timedelta(minutes=minutes)),
        "conference_url": url or f"https://example.zoom.us/j/{number}",
        "my_rsvp": rsvp,
        "organizer": {"email": "ann@example.com", "name": "Ann Example"},
        "attendees": [
            {"email": "ann@example.com", "name": "Ann Example", "organizer": True},
            {"email": "Bob@Example.com", "name": "Bob Example"},
        ],
    }


def assets(number="91234567890", start=T0, summary="## Decisions\n- Ship it",
           permission=True, has_summary=True, uuid="UUID-1"):
    out = {
        "meeting_number": int(number), "meeting_uuid": uuid,
        "topic": "Weekly sync", "start_time": iso(start),
        "end_time": iso(start + datetime.timedelta(minutes=30)),
    }
    if summary is not None:
        out["meeting_summary"] = {
            "has_permission": permission, "has_summary": has_summary,
            "summary_markdown": summary,
        }
    return json.dumps(out)


class Connector:
    """Fake call_tools: answers each meetingId from a script."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def __call__(self, prompt, tools, **kwargs):
        self.calls.append(prompt)
        out = claude_bridge.ToolResults()
        for line in prompt.splitlines():
            if line[:1].isdigit() and ". {" in line:
                args = json.loads(line.split(". ", 1)[1])
                text = self.answers.get(args["meetingId"])
                if text is not None:
                    out.append({"name": TOOL, "input": args, "text": text,
                                "is_error": False})
        return out


class ZoomSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        zoom_source.configure(str(self.base))
        self.calendar = self.base / "next48h.json"
        self.source = {
            "kind": "zoom", "assets_tool": TOOL,
            "calendar_file": str(self.calendar), "_processed": {},
        }
        self.connector = Connector()
        self.now = T0 - datetime.timedelta(hours=1)
        self.patches = [
            mock.patch.object(claude_bridge, "call_tools", self.connector),
            mock.patch.object(zoom_source, "_now",
                              lambda: self.now.timestamp()),
            mock.patch.object(zoom_source, "log"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        zoom_source.configure(None)
        self.tmp.cleanup()

    def write_calendar(self, events, refreshed=None):
        self.calendar.write_text(json.dumps({
            "events": events, "refreshed_at": iso(refreshed or self.now),
        }))

    def at(self, minutes_after_start):
        self.now = T0 + datetime.timedelta(minutes=minutes_after_start)

    def listing(self, **extra):
        return zoom_source.candidates(dict(self.source, **extra))

    def meetings(self):
        return zoom_source.load_state()

    def test_meeting_number_from_join_links(self):
        hosts = ["zoom.us"]
        self.assertEqual(zoom_source.meeting_number(
            "https://example.zoom.us/j/91234567890?pwd=x", hosts), "91234567890")
        self.assertEqual(zoom_source.meeting_number(
            "https://zoom.us/w/91234567890", hosts), "91234567890")
        self.assertIsNone(zoom_source.meeting_number(
            "https://example.zoom.us/my/someone", hosts))
        self.assertIsNone(zoom_source.meeting_number(
            "https://evilzoom.us/j/91234567890", hosts))
        self.assertIsNone(zoom_source.meeting_number(
            "https://meet.example.com/abc-defg-hij", hosts))

    def test_tracks_zoom_meetings_and_waits_for_the_end(self):
        self.write_calendar([
            event(),
            event("ev2", url="https://meet.example.com/abc-defg-hij"),
            event("ev3", number="91234567891", rsvp="declined"),
        ])
        self.assertEqual(self.listing(), [])
        self.assertEqual(list(self.meetings()), ["ev1"])
        record = self.meetings()["ev1"]
        self.assertEqual(record["number"], "91234567890")
        self.assertEqual(
            [p["email"] for p in record["people"]],
            ["ann@example.com", "bob@example.com"],
        )
        self.at(35)  # ended 5 minutes ago: before the first check
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.connector.calls, [])

    def test_no_summary_backs_off_then_expires(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets(summary=None)
        self.at(41)
        self.assertEqual(self.listing(), [])
        self.assertEqual(len(self.connector.calls), 1)
        record = self.meetings()["ev1"]
        self.assertEqual((record["checks"], record["last_result"]),
                         (1, "no_summary"))
        self.at(50)  # inside the 20-minute back-off
        self.listing()
        self.assertEqual(len(self.connector.calls), 1)
        self.at(62)
        self.listing()
        self.assertEqual(len(self.connector.calls), 2)
        self.at(30 + 24 * 60 + 1)
        self.listing()
        self.assertEqual(self.meetings()["ev1"]["status"], "expired")
        calls = len(self.connector.calls)
        self.at(30 + 30 * 60)
        self.listing()
        self.assertEqual(len(self.connector.calls), calls)

    def test_no_permission_and_other_instance_are_not_captured(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        self.connector.answers["91234567890"] = assets(permission=False)
        self.connector.answers["91234567891"] = assets(
            number="91234567891", start=T0 - datetime.timedelta(days=7))
        self.at(41)
        self.assertEqual(self.listing(), [])
        state = self.meetings()
        self.assertEqual(state["ev1"]["last_result"], "no_permission")
        self.assertEqual(state["ev2"]["last_result"], "other_instance")

    def test_summary_becomes_a_candidate_and_settles_on_the_ledger(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets()
        self.at(41)
        found = self.listing()
        self.assertEqual(len(found), 1)
        candidate = found[0]
        self.assertRegex(candidate["id"], r"^zoom:UUID-1@[0-9a-f]{12}$")
        self.assertEqual(candidate["raw"]["source_id"], "zoom:UUID-1")

        item = zoom_source.fetch({}, self.source, candidate)
        self.assertEqual(item["source_id"], "zoom:UUID-1")
        self.assertEqual(item["source_kind"], "zoom")
        self.assertEqual(item["date"], "2026-07-01")
        self.assertIn("Ship it", item["body"])
        meta = item["frontmatter"]
        self.assertEqual(meta["calendar_event_id"], "ev1")
        self.assertEqual(len(meta["source_people"]), 2)
        self.assertEqual(meta["follows_entry_ids"], [])

        # Not settled yet (a failed capture): offered again next run.
        self.source["_processed"] = {
            candidate["id"]: {"memory_error": "boom"}}
        self.at(42)
        self.assertEqual(len(self.listing()), 1)
        self.source["_processed"] = {candidate["id"]: {
            "memory": "created", "memory_entry_id": "2026-07-01-weekly-sync"}}
        self.at(43)
        self.assertEqual(self.listing(), [])
        record = self.meetings()["ev1"]
        self.assertEqual(record["status"], "captured")
        self.assertEqual(record["memory_entry_id"], "2026-07-01-weekly-sync")
        calls = len(self.connector.calls)
        self.at(300)
        self.listing()
        self.assertEqual(len(self.connector.calls), calls)

    def write_receipt(self, event_id, entry_id, name="mila-x"):
        directory = self.base / "state" / "transcriptions" / "processed"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.json").write_text(json.dumps({
            "calendar_match": {"event_id": event_id, "matched": True},
            "memory": "created", "memory_entry_id": entry_id,
        }))

    def test_transcript_captured_first_is_followed(self):
        self.write_receipt("ev1", "2026-07-01-transcript")
        self.write_receipt("ev9", "2026-07-01-other", name="mila-y")
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets()
        self.at(41)
        candidate = self.listing()[0]
        item = zoom_source.fetch({}, self.source, candidate)
        self.assertEqual(item["frontmatter"]["follows_entry_ids"],
                         ["2026-07-01-transcript"])

    def test_transcript_captured_later_is_linked_once(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets()
        self.at(41)
        candidate = self.listing()[0]
        zoom_source.fetch({}, self.source, candidate)
        self.source["_processed"] = {candidate["id"]: {
            "memory": "created", "memory_entry_id": "2026-07-01-zoom"}}
        self.source["_memory_store"] = "/store"
        self.at(45)
        self.listing()
        self.write_receipt("ev1", "2026-07-01-transcript")
        result = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(memory_sink, "_cli",
                               return_value=result) as cli:
            self.at(90)
            self.listing()
            self.at(120)
            self.listing()
        cli.assert_called_once_with(
            "/store", ["link", "2026-07-01-zoom", "--follows",
                       "2026-07-01-transcript"])

    def test_dry_run_calls_nothing_and_writes_nothing(self):
        self.write_calendar([event()])
        self.at(41)
        self.assertEqual(self.listing(_dry_run=True), [])
        self.assertEqual(self.connector.calls, [])
        self.assertFalse((self.base / "state" / "zoom-meetings.json").exists())

    def test_meeting_listed_without_its_link_is_kept(self):
        self.write_calendar([event()])
        self.listing()
        self.write_calendar([dict(event(), conference_url="")])
        self.listing()
        self.assertEqual(list(self.meetings()), ["ev1"])

    def test_cancelled_upcoming_meeting_is_dropped(self):
        self.write_calendar([event()])
        self.listing()
        self.write_calendar([])
        self.listing()
        self.assertEqual(self.meetings(), {})


class ZoomConfigTest(unittest.TestCase):
    def routine(self, **source):
        base = {"kind": "zoom", "assets_tool": TOOL,
                "calendar_file": "~/cal.json"}
        base.update(source)
        return {
            "id": "z", "source": base,
            "analyze": {"provider": "claude", "model": "sonnet"},
            "memory": {"store": "/store", "type": "meeting"},
        }

    def problems(self, **source):
        r = self.routine(**source)
        return config._validate_source(r, r["source"], "source")

    def test_valid(self):
        self.assertEqual(self.problems(retry_hours=12), [])

    def test_rejects_other_tools_and_bad_values(self):
        self.assertTrue(self.problems(assets_tool="mcp__x__search"))
        self.assertTrue(self.problems(calendar_file=""))
        self.assertTrue(self.problems(retry_hours=0))
        self.assertTrue(self.problems(skip_declined="yes"))
        self.assertTrue(self.problems(query="x"))


if __name__ == "__main__":
    unittest.main()
