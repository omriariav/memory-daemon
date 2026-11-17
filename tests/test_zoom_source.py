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
        self.errors = {}
        self.calls = []
        self.kwargs = []

    def __call__(self, prompt, tools, **kwargs):
        self.calls.append(prompt)
        self.kwargs.append(kwargs)
        out = claude_bridge.ToolResults()
        for line in prompt.splitlines():
            if line[:1].isdigit() and ". {" in line:
                args = json.loads(line.split(". ", 1)[1])
                error = self.errors.get(args["meetingId"])
                if error is not None:
                    out.append({"name": TOOL, "input": args, "text": error,
                                "is_error": True})
                    continue
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
            "owner_email": "ann@example.com",
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

    def test_no_access_meeting_does_not_block_the_others(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        self.connector.errors["91234567891"] = (
            'HTTP 403 Forbidden error: {"code":2315,"message":"Permission '
            'denied. You do not have access to this summary or recording or '
            'transcript."}')
        self.connector.answers["91234567890"] = assets(summary="Recap.")
        self.at(41)
        found = self.listing()
        self.assertEqual([c["raw"]["event_id"] for c in found], ["ev1"])
        self.assertEqual(self.connector.kwargs[-1]["required"], [])
        state = self.meetings()
        self.assertEqual(state["ev2"]["last_result"], "no_access")
        self.assertEqual(state["ev2"]["status"], "tracking")
        self.assertGreater(state["ev2"]["next_check"], self.now.timestamp())

    def test_every_meeting_without_access_is_not_an_outage(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.errors["91234567890"] = (
            'HTTP 403 Forbidden error: {"code":2315,"message":"Permission '
            'denied."}')
        self.at(41)
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.meetings()["ev1"]["last_result"], "no_access")

    def test_generic_403_on_every_call_fails_the_run(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        for number in ("91234567890", "91234567891"):
            self.connector.errors[number] = "HTTP 403 Forbidden: scope revoked"
        self.at(41)
        with self.assertRaises(claude_bridge.BridgeError):
            self.listing()

    def test_expired_sign_in_is_tagged_as_an_auth_failure(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        for number in ("91234567890", "91234567891"):
            self.connector.errors[number] = (
                "This connector needs you to sign in again "
                "(run /mcp to re-authenticate)")
        self.at(41)
        with self.assertRaises(claude_bridge.BridgeError) as caught:
            self.listing()
        self.assertTrue(str(caught.exception).endswith("(auth failed)"))
        self.assertIn("no usable result", str(caught.exception))

    def test_empty_session_is_retried_once(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets(summary="Recap.")
        real = self.connector.__call__
        sessions = []

        def flaky(prompt, tools, **kwargs):
            sessions.append(prompt)
            if len(sessions) == 1:
                return claude_bridge.ToolResults()
            return real(prompt, tools, **kwargs)

        self.at(41)
        with mock.patch.object(claude_bridge, "call_tools", flaky):
            self.assertEqual(len(self.listing()), 1)
        self.assertEqual(len(sessions), 2)

    def test_results_lost_to_sleep_are_an_outage(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        self.connector.answers["91234567890"] = assets(summary="Recap.")
        self.at(41)
        with mock.patch.object(zoom_source.sleepclock.Span, "slept",
                               return_value=True):
            with self.assertRaises(claude_bridge.BridgeOutage):
                self.listing()
        self.assertEqual(self.meetings()["ev2"]["checks"], 0)

    def test_connector_outage_still_fails_the_run(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        self.connector.errors["91234567890"] = "HTTP 500 upstream timeout"
        self.at(41)
        with self.assertRaises(claude_bridge.BridgeError):
            self.listing()
        self.assertEqual(self.meetings()["ev1"]["checks"], 0)

    def test_other_error_on_one_meeting_is_retried(self):
        self.write_calendar([event(), event("ev2", number="91234567891")])
        self.listing()
        self.connector.errors["91234567891"] = "HTTP 500 upstream timeout"
        self.connector.answers["91234567890"] = assets(summary="Recap.")
        self.at(41)
        self.assertEqual(len(self.listing()), 1)
        self.assertEqual(self.meetings()["ev2"]["last_result"], "error")

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
        self.assertEqual(meta["calendar_attendee_count"], 2)
        self.assertEqual(meta["calendar_other_attendee"], "bob@example.com")
        self.assertEqual(meta["same_meeting_entry_ids"], [])
        self.assertNotIn("follows_entry_ids", meta)

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

    def test_transcript_captured_first_is_cross_referenced_not_followed(self):
        self.write_receipt("ev1", "2026-07-01-transcript")
        self.write_receipt("ev9", "2026-07-01-other", name="mila-y")
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets()
        self.at(41)
        candidate = self.listing()[0]
        item = zoom_source.fetch({}, self.source, candidate)
        self.assertEqual(item["frontmatter"]["same_meeting_entry_ids"],
                         ["2026-07-01-transcript"])
        self.assertNotIn("follows_entry_ids", item["frontmatter"])

    def test_transcript_captured_later_is_never_linked(self):
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
        with mock.patch.object(memory_sink, "_cli") as cli:
            self.at(90)
            self.listing()
        cli.assert_not_called()

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


SEARCH_TOOL = "mcp__example_zoom__search"
CONTENT_TOOL = "mcp__example_zoom__hub_get_file_content"
DOC_ID = "AbCdEfGhIjKlMnOpQrStUv"
DOC_BODY = (
    "## Quick recap\n\nThe team agreed to ship Friday. Ann's plan won.\n\n"
    "## Next steps\n\n- Bob: draft the \"launch\" note.\n" * 6)


def error_payload(content, name="Weekly sync"):
    """The schema-validation error text that still carries the content."""
    import pprint
    body = pprint.pformat({"file_content": content, "file_name": name},
                          width=60)
    return ("Invalid structured content returned by tool get_file_content: "
            "'file_link' is a required property\n\nOn instance:\n" + body)


def search_result(name="Weekly sync", when=None, doc_id=DOC_ID):
    row = {"name": name, "url": f"https://docs.example.zoom.us/doc/{doc_id}"}
    if when:
        row["modified_time"] = when
    return json.dumps({"data": [row], "has_more": False})


class HubParsingTest(unittest.TestCase):
    def test_content_from_plain_json(self):
        text = json.dumps({"file_content": "Hello", "file_link": "x",
                           "file_name": "n"})
        self.assertEqual(zoom_source.parse_hub_content(text), "Hello")

    def test_content_from_the_validation_error_payload(self):
        got = zoom_source.parse_hub_content(error_payload(DOC_BODY))
        self.assertEqual(got, DOC_BODY)

    def test_unreadable_results_are_none(self):
        for text in ("", "timed out", '{"file_content": ""}', None,
                     "'file_content': 12"):
            self.assertIsNone(zoom_source.parse_hub_content(text))

    def window(self):
        start = T0.timestamp()
        return start, start + 1800

    def match(self, text, title="Weekly sync", skipped=None):
        start, end = self.window()
        return zoom_source.hub_doc_matches(
            text, title, start, end, 7200, skipped)

    def test_doc_inside_the_window_matches(self):
        inside = search_result(when=iso(T0 + datetime.timedelta(minutes=40)))
        self.assertEqual(self.match(inside, "weekly  SYNC"), [DOC_ID])

    def test_doc_from_last_week_does_not_match(self):
        stale = search_result(when=iso(T0 - datetime.timedelta(days=7)))
        self.assertEqual(self.match(stale), [])

    def test_doc_without_a_timestamp_is_skipped_and_reported(self):
        skipped = []
        self.assertEqual(self.match(search_result(), skipped=skipped), [])
        self.assertEqual(skipped, [DOC_ID])
        naive = search_result(when="2026-07-01T10:20:00")
        self.assertEqual(self.match(naive), [])

    def test_doc_saved_within_the_grace_after_the_end_matches(self):
        end = T0 + datetime.timedelta(minutes=30)
        late = search_result(when=iso(end + datetime.timedelta(minutes=110)))
        self.assertEqual(self.match(late), [DOC_ID])
        too_late = search_result(when=iso(end + datetime.timedelta(hours=3)))
        self.assertEqual(self.match(too_late), [])
        early = search_result(when=iso(T0 - datetime.timedelta(minutes=10)))
        self.assertEqual(self.match(early), [DOC_ID])
        too_early = search_result(when=iso(T0 - datetime.timedelta(hours=2)))
        self.assertEqual(self.match(too_early), [])

    def test_timezone_aware_timestamps_compare_correctly(self):
        plus3 = datetime.timezone(datetime.timedelta(hours=3))
        inside = search_result(
            when=iso((T0 + datetime.timedelta(minutes=20)).astimezone(plus3)))
        self.assertEqual(self.match(inside), [DOC_ID])
        # The same wall clock without the offset conversion is 3h off.
        wrong = search_result(when="2026-07-01T10:20:00+03:00")
        self.assertEqual(self.match(wrong), [])
        zulu = search_result(when="2026-07-01T10:20:00Z")
        self.assertEqual(self.match(zulu), [DOC_ID])

    def test_created_time_decides_over_modified(self):
        def hit(created=None, modified=None):
            row = {"name": "Weekly sync",
                   "url": f"https://docs.example.zoom.us/doc/{DOC_ID}"}
            if created:
                row["created_time"] = iso(created)
            if modified:
                row["modified_time"] = iso(modified)
            return json.dumps({"data": [row]})
        inside = T0 + datetime.timedelta(minutes=10)
        last_week = T0 - datetime.timedelta(days=7)
        self.assertEqual(
            self.match(hit(created=last_week, modified=inside)), [])
        self.assertEqual(
            self.match(hit(created=inside, modified=last_week)), [DOC_ID])
        self.assertEqual(self.match(hit(modified=inside)), [DOC_ID])
        self.assertEqual(self.match(hit(modified=last_week)), [])
        skipped = []
        naive = json.dumps({"data": [{
            "name": "Weekly sync", "created_time": "2026-07-01T10:10:00",
            "modified_time": iso(inside),
            "url": f"https://docs.example.zoom.us/doc/{DOC_ID}"}]})
        self.assertEqual(self.match(naive, skipped=skipped), [])
        self.assertEqual(skipped, [DOC_ID])

    def test_name_valued_creator_keys_are_not_created_times(self):
        inside = iso(T0 + datetime.timedelta(minutes=10))
        last_week = iso(T0 - datetime.timedelta(days=7))
        base = {"name": "Weekly sync",
                "url": f"https://docs.example.zoom.us/doc/{DOC_ID}"}

        def hit(**extra):
            return json.dumps({"data": [dict(base, **extra)]})
        self.assertEqual(self.match(hit(
            creator="Ann Example", creator_name="Ann Example",
            modified_time=inside)), [DOC_ID])
        self.assertEqual(self.match(hit(
            created_by="Ann Example", created_time=last_week,
            modified_time=inside)), [])
        self.assertEqual(self.match(hit(
            created_by="Ann Example", created_time=inside)), [DOC_ID])
        for key in ("created_at", "createTime", "creation_date", "created"):
            self.assertEqual(self.match(hit(
                **{key: last_week, "modified_time": inside})), [], key)
        # A temporal key holding a non-date value is ignored.
        self.assertEqual(self.match(hit(
            created="yesterday", modified_time=inside)), [DOC_ID])

    def test_other_titles_and_bad_json_never_match(self):
        when = iso(T0 + datetime.timedelta(minutes=10))
        self.assertEqual(self.match(search_result("Other", when)), [])
        self.assertEqual(self.match("not json"), [])


class HubFallbackTest(unittest.TestCase):
    """Meetings with no summary asset fall back to a Hub Doc when enabled."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        zoom_source.configure(str(self.base))
        self.calendar = self.base / "next48h.json"
        self.source = {
            "kind": "zoom", "assets_tool": TOOL,
            "calendar_file": str(self.calendar), "_processed": {},
            "hub_docs": True, "hub_search_tool": SEARCH_TOOL,
            "hub_content_tool": CONTENT_TOOL,
        }
        self.connector = Connector()
        self.now = T0 - datetime.timedelta(hours=1)
        self.hub_calls = []
        self.search_text = search_result(
            when=iso(T0 + datetime.timedelta(minutes=20)))
        self.content_text = error_payload(DOC_BODY)

        def dispatch(prompt, tools, **kwargs):
            if TOOL in tools:
                return self.connector(prompt, tools, **kwargs)
            (tool,) = tools
            line = [x for x in prompt.splitlines()
                    if x[:1].isdigit() and ". {" in x][0]
            args = json.loads(line.split(". ", 1)[1])
            self.hub_calls.append((tool, args))
            out = claude_bridge.ToolResults()
            if tool == SEARCH_TOOL:
                out.append({"name": tool, "input": args,
                            "text": self.search_text, "is_error": False})
            else:
                out.append({"name": tool, "input": args,
                            "text": self.content_text, "is_error": True})
            return out

        self.patches = [
            mock.patch.object(claude_bridge, "call_tools", dispatch),
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

    def write_calendar(self, events):
        self.calendar.write_text(json.dumps({
            "events": events, "refreshed_at": iso(self.now)}))

    def at(self, minutes_after_start):
        self.now = T0 + datetime.timedelta(minutes=minutes_after_start)

    def listing(self):
        return zoom_source.candidates(dict(self.source))

    def meetings(self):
        return zoom_source.load_state()

    def ready(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets(summary=None)
        self.at(41)

    def test_hub_doc_becomes_the_summary(self):
        self.ready()
        found = self.listing()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["raw"]["source_id"], f"zoom:doc-{DOC_ID}")
        self.assertEqual(found[0]["raw"]["summary"], DOC_BODY.strip())
        self.assertEqual(
            [c[0] for c in self.hub_calls], [SEARCH_TOOL, CONTENT_TOOL])
        self.assertEqual(self.hub_calls[0][1], {"query": "Weekly sync"})
        self.assertEqual(self.hub_calls[1][1],
                         {"fileId": DOC_ID, "format": "markdown"})
        item = zoom_source.fetch({}, self.source, found[0])
        self.assertEqual(item["source_id"], f"zoom:doc-{DOC_ID}")
        self.assertIn("ship Friday", item["body"])

    def test_no_matching_doc_keeps_waiting(self):
        self.search_text = search_result("Another meeting")
        self.ready()
        self.assertEqual(self.listing(), [])
        self.assertEqual([c[0] for c in self.hub_calls], [SEARCH_TOOL])
        self.assertEqual(self.meetings()["ev1"]["last_result"],
                         "no_summary+hub_no_match")

    def test_untimed_doc_is_not_used(self):
        self.search_text = search_result()
        self.ready()
        self.assertEqual(self.listing(), [])
        self.assertEqual([c[0] for c in self.hub_calls], [SEARCH_TOOL])
        self.assertEqual(self.meetings()["ev1"]["last_result"],
                         "no_summary+hub_untimed")

    def test_two_matching_docs_are_refused(self):
        when = iso(T0 + datetime.timedelta(minutes=40))
        first = json.loads(search_result(when=when, doc_id=DOC_ID))["data"][0]
        second = json.loads(search_result(
            when=when, doc_id="ZyXwVuTsRqPoNmLkJiHgFe"))["data"][0]
        self.search_text = json.dumps({"data": [first, second]})
        self.ready()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.meetings()["ev1"]["last_result"],
                         "no_summary+hub_ambiguous")

    def test_a_real_summary_never_calls_the_hub(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.answers["91234567890"] = assets()
        self.at(41)
        found = self.listing()
        self.assertEqual(found[0]["raw"]["source_id"], "zoom:UUID-1")
        self.assertEqual(self.hub_calls, [])

    def test_no_access_meeting_can_still_use_its_hub_doc(self):
        self.write_calendar([event()])
        self.listing()
        self.connector.errors["91234567890"] = (
            'HTTP 403 {"code":2315,"message":"You do not have access to '
            'this summary"}')
        self.at(41)
        self.assertEqual(len(self.listing()), 1)

    def test_hub_grace_minutes_widens_the_window(self):
        late = T0 + datetime.timedelta(minutes=30 + 200)
        self.search_text = search_result(when=iso(late))
        self.ready()
        self.assertEqual(self.listing(), [])
        self.source["hub_grace_minutes"] = 240
        self.at(120)
        self.assertEqual(len(self.listing()), 1)

    def test_off_by_default(self):
        self.source["hub_docs"] = False
        self.ready()
        self.assertEqual(self.listing(), [])
        self.assertEqual(self.hub_calls, [])


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

    def test_hub_grace_minutes_is_bounded(self):
        self.assertEqual(self.problems(hub_grace_minutes=0), [])
        self.assertEqual(self.problems(hub_grace_minutes=720), [])
        self.assertTrue(self.problems(hub_grace_minutes=-1))
        self.assertTrue(self.problems(hub_grace_minutes=721))
        self.assertTrue(self.problems(hub_grace_minutes="2h"))

    def test_legacy_link_transcripts_key_is_still_valid(self):
        self.assertEqual(self.problems(link_transcripts=False), [])

    def test_hub_docs_need_exact_tools(self):
        self.assertEqual(self.problems(hub_docs=False), [])
        self.assertEqual(len(self.problems(hub_docs=True)), 2)
        self.assertEqual(self.problems(
            hub_docs=True, hub_search_tool="mcp__x__search",
            hub_content_tool="mcp__x__hub_get_file_content"), [])
        self.assertEqual(self.problems(
            hub_docs=True, hub_search_tool="mcp__x__search",
            hub_content_tool="mcp__x__get_file_content"), [])
        self.assertTrue(self.problems(
            hub_docs=True, hub_search_tool="mcp__x__search_meetings",
            hub_content_tool="mcp__x__hub_get_file_content"))
        self.assertTrue(self.problems(hub_docs="yes"))

    def test_rejects_other_tools_and_bad_values(self):
        self.assertTrue(self.problems(assets_tool="mcp__x__search"))
        self.assertTrue(self.problems(calendar_file=""))
        self.assertTrue(self.problems(retry_hours=0))
        self.assertTrue(self.problems(skip_declined="yes"))
        self.assertTrue(self.problems(query="x"))


if __name__ == "__main__":
    unittest.main()
