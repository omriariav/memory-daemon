import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import config, llm, memory_sink, mila_source, runner, state


def record(
    recording_id,
    *,
    status="completed",
    source="meeting",
    audio="Recording 2026-07-27T19-02-44Z-ABC123.m4a",
    created="2026-07-27T19:35:32Z",
    duration=1967.0,
    title="Intro meeting",
    segments=None,
):
    return {
        "id": recording_id,
        "title": title,
        "createdAt": created,
        "duration": duration,
        "status": status,
        "source": source,
        "audioFileName": audio,
        "segments": segments or [],
    }


def routine(store, source):
    return {
        "id": "mila-transcriptions",
        "enabled": True,
        "source": source,
        "analyze": {
            "provider": "gemini",
            "model": "gemini-test",
            "instruction": "Keep durable meeting facts.",
        },
        "memory": {
            "store": str(store),
            "type": "note",
            "tags": ["meeting"],
        },
    }


class MilaSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.current = self.root / "current"
        self.legacy = self.root / "legacy"
        self.current.mkdir()
        self.legacy.mkdir()

    def write_json(self, path, records):
        path.write_text(json.dumps(records))

    def test_transcript_secrets_are_redacted_before_calendar_or_summary(self):
        candidate = {
            "id": "mila:ID@hash",
            "title": "Meeting",
            "raw": {
                "source_id": "mila:ID",
                "recording": record("ID", created="2026-07-27T19:35:32Z"),
                "recording_start": "2026-07-27T19:35:32Z",
                "recording_end": "2026-07-27T20:08:19Z",
                "content_hash": "hash",
                "transcript": "Use token ghp_abcdefghijklmnopqrstuvwxyz1234567890 now",
            },
        }
        with mock.patch.object(mila_source, "_calendar_candidates", return_value=[]):
            item = mila_source.fetch(
                {}, {"calendar_timezone": "UTC"}, candidate
            )
        self.assertNotIn("ghp_", item["body"])
        self.assertIn("REDACTED", item["body"])

    def test_manual_recording_and_new_indexed_record_are_versioned(self):
        legacy_record = record("LEGACY")
        current_record = record(
            "NEW",
            source="voiceMemo",
            audio="Voice memo.m4a",
            created="2026-07-30T08:00:00Z",
            duration=600,
            segments=[
                {"start": 0, "end": 2, "text": "A durable decision."},
            ],
        )
        excluded_record = record(
            "OLD",
            source="voiceMemo",
            audio="Old.m4a",
            segments=[{"start": 0, "end": 1, "text": "Already handled."}],
        )
        self.write_json(self.legacy / "recordings.json", [legacy_record])
        self.write_json(
            self.current / "recordings.json",
            [
                current_record,
                excluded_record,
                record("PENDING", status="pending"),
            ],
        )
        manual_srt = self.current / "manual.srt"
        manual_srt.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nHello there.\n\n"
            "2\n00:00:02,000 --> 00:00:04,000\nDecision made.\n"
        )
        source = {
            "kind": "mila",
            "recordings_file": str(self.current / "recordings.json"),
            "exclude_recording_ids": ["OLD"],
            "manual_recordings": [{
                "recording_id": "LEGACY",
                "recordings_file": str(self.legacy / "recordings.json"),
                "transcript_file": str(manual_srt),
            }],
            "max_results": 0,
        }

        candidates = mila_source.candidates(source)

        self.assertEqual(
            {candidate["raw"]["source_id"] for candidate in candidates},
            {"mila:LEGACY", "mila:NEW"},
        )
        manual = next(
            candidate
            for candidate in candidates
            if candidate["raw"]["source_id"] == "mila:LEGACY"
        )
        self.assertRegex(manual["id"], r"^mila:LEGACY@[0-9a-f]{16}$")
        self.assertIn("[00:00:00,000] Hello there.", manual["raw"]["transcript"])

        manual_srt.write_text(
            manual_srt.read_text().replace("Decision made.", "Decision corrected.")
        )
        changed = next(
            candidate
            for candidate in mila_source.candidates(source)
            if candidate["raw"]["source_id"] == "mila:LEGACY"
        )
        self.assertNotEqual(manual["id"], changed["id"])
        self.assertEqual(
            manual["raw"]["source_id"],
            changed["raw"]["source_id"],
        )

    def test_recording_metadata_change_versions_an_unchanged_transcript(self):
        metadata = self.current / "recordings.json"
        original = record(
            "META",
            source="voiceMemo",
            audio="Voice memo.m4a",
            created="2026-07-30T08:00:00Z",
            duration=600,
            title="Original title",
            segments=[{"start": 0, "text": "The same transcript."}],
        )
        self.write_json(metadata, [original])
        source = {
            "kind": "mila",
            "recordings_file": str(metadata),
            "max_results": 0,
        }
        first = mila_source.candidates(source)[0]

        corrected = dict(original, title="Corrected title", duration=660)
        self.write_json(metadata, [corrected])
        second = mila_source.candidates(source)[0]

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["raw"]["transcript"], second["raw"]["transcript"])
        self.assertEqual(first["raw"]["source_id"], second["raw"]["source_id"])

    def test_meet_export_uses_local_scheduled_start_not_import_stamp(self):
        from zoneinfo import ZoneInfo
        start, end = mila_source._recording_interval(
            record(
                "MEET",
                audio=(
                    "Tomer__Omri - PM resources - 2026_08_13 15_32 IDT – "
                    "Recording 2026-08-14T16-39-11Z-FEE10D.m4a"
                ),
                created="2026-08-14T16:39:11Z",
                duration=724.5,
            ),
            zone=ZoneInfo("Asia/Jerusalem"),
        )
        self.assertEqual(
            start,
            datetime.datetime(2026, 8, 13, 12, 32, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(
            end,
            start + datetime.timedelta(seconds=724.5),
        )

    def test_meet_export_abbreviation_wins_over_disagreeing_zone(self):
        from zoneinfo import ZoneInfo
        start, _end = mila_source._recording_interval(
            record(
                "MEET",
                audio="Sync - 2026_01_20 10_00 UTC – Recording 2026-01-21T08-00-00Z-AB.m4a",
                created="2026-01-21T08:00:00Z",
                duration=600,
            ),
            zone=ZoneInfo("Asia/Jerusalem"),
        )
        self.assertEqual(
            start,
            datetime.datetime(2026, 1, 20, 10, 0, tzinfo=datetime.timezone.utc),
        )

    def test_meeting_filename_is_start_but_voice_memo_created_at_is_start(self):
        meeting_start, meeting_end = mila_source._recording_interval(
            record("MEETING")
        )
        voice_start, _voice_end = mila_source._recording_interval(
            record(
                "VOICE",
                source="voiceMemo",
                audio="Imported 2026-07-30T10-00-00Z-ABC123.m4a",
                created="2026-07-20T08:15:00Z",
                duration=600,
            )
        )

        self.assertEqual(
            meeting_start,
            datetime.datetime(
                2026, 7, 27, 19, 2, 44,
                tzinfo=datetime.timezone.utc,
            ),
        )
        self.assertEqual(
            meeting_end,
            meeting_start + datetime.timedelta(seconds=1967),
        )
        self.assertEqual(
            voice_start,
            datetime.datetime(
                2026, 7, 20, 8, 15,
                tzinfo=datetime.timezone.utc,
            ),
        )

    def test_malformed_record_does_not_hide_valid_record(self):
        malformed = record(
            "BROKEN",
            created="not-a-timestamp",
            duration="not-a-duration",
            segments=[{"start": 0, "text": "Transcript exists."}],
        )
        valid = record(
            "VALID",
            source="voiceMemo",
            audio="Valid.m4a",
            segments=[{"start": 0, "text": "Valid transcript."}],
        )
        self.write_json(
            self.current / "recordings.json", [malformed, valid]
        )

        found = mila_source.candidates({
            "kind": "mila",
            "recordings_file": str(self.current / "recordings.json"),
            "max_results": 0,
        })

        self.assertEqual(
            {candidate["raw"]["source_id"] for candidate in found},
            {"mila:BROKEN", "mila:VALID"},
        )
        broken = next(
            candidate
            for candidate in found
            if candidate["raw"]["source_id"] == "mila:BROKEN"
        )
        self.assertIn("transcript_error", broken["raw"])
        self.assertIsNone(broken["raw"]["recording_start"])

    def test_manual_recording_must_still_be_completed(self):
        self.write_json(
            self.legacy / "recordings.json",
            [record("PENDING", status="pending")],
        )
        transcript = self.current / "manual.srt"
        transcript.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nNot ready.\n"
        )

        with self.assertRaisesRegex(RuntimeError, "is not completed"):
            mila_source.candidates({
                "kind": "mila",
                "recordings_file": str(self.current / "recordings.json"),
                "manual_recordings": [{
                    "recording_id": "PENDING",
                    "recordings_file": str(
                        self.legacy / "recordings.json"
                    ),
                    "transcript_file": str(transcript),
                }],
            })

    def test_fetch_reads_calendar_and_ranks_overlapping_event(self):
        indexed = record(
            "NEW",
            segments=[{"start": 0, "end": 1, "text": "Transcript"}],
        )
        self.write_json(self.current / "recordings.json", [indexed])
        source = {
            "kind": "mila",
            "recordings_file": str(self.current / "recordings.json"),
            "calendar_timezone": "Asia/Jerusalem",
            "calendar_window_days": 3,
            "max_calendar_candidates": 2,
            "max_results": 0,
        }
        events = [
            {
                "id": "far",
                "summary": "Nearby",
                "start": "2026-07-27T17:00:00+03:00",
                "end": "2026-07-27T17:30:00+03:00",
                "event_type": "default",
            },
            {
                "id": "overlap",
                "summary": "Intro meeting",
                "start": "2026-07-27T22:00:00+03:00",
                "end": "2026-07-27T22:35:00+03:00",
                "event_type": "default",
            },
            {
                "id": "all-day",
                "summary": "Not a meeting",
                "start": "2026-07-27",
                "end": "2026-07-28",
                "all_day": True,
            },
        ]
        candidate = mila_source.candidates(source)[0]

        with mock.patch.object(
            mila_source, "_raw_calendar_events", return_value=events
        ):
            item = mila_source.fetch({}, source, candidate)

        self.assertEqual(
            [event["id"] for event in item["_mila_calendar_candidates"]],
            ["overlap", "far"],
        )
        self.assertEqual(item["date"], "2026-07-27")

    def test_declined_event_is_still_a_candidate_but_loses_ties(self):
        indexed = record(
            "RID",
            segments=[{"start": 0, "end": 1, "text": "Transcript"}],
        )
        self.write_json(self.current / "recordings.json", [indexed])
        source = {
            "kind": "mila",
            "recordings_file": str(self.current / "recordings.json"),
            "calendar_timezone": "Asia/Jerusalem",
            "calendar_window_days": 3,
            "max_results": 0,
        }
        overlap = {
            "start": "2026-07-27T22:00:00+03:00",
            "end": "2026-07-27T22:35:00+03:00",
            "event_type": "default",
        }
        events = [
            {"id": "declined", "summary": "Weekly", "response_status": "declined", **overlap},
            {"id": "accepted", "summary": "Weekly", "response_status": "accepted", **overlap},
        ]
        candidate = mila_source.candidates(source)[0]

        with mock.patch.object(
            mila_source, "_raw_calendar_events", return_value=events
        ):
            item = mila_source.fetch({}, source, candidate)

        self.assertEqual(
            [event["id"] for event in item["_mila_calendar_candidates"]],
            ["accepted", "declined"],
        )

    def test_calendar_candidates_without_ids_are_discarded(self):
        indexed = record(
            "NEW",
            segments=[{"start": 0, "end": 1, "text": "Transcript"}],
        )
        self.write_json(self.current / "recordings.json", [indexed])
        source = {
            "kind": "mila",
            "recordings_file": str(self.current / "recordings.json"),
            "max_results": 0,
        }
        candidate = mila_source.candidates(source)[0]
        event = {
            "summary": "Missing id",
            "start": "2026-07-27T19:00:00Z",
            "end": "2026-07-27T19:30:00Z",
            "event_type": "default",
        }

        with mock.patch.object(
            mila_source, "_raw_calendar_events", return_value=[event]
        ):
            item = mila_source.fetch({}, source, candidate)

        self.assertEqual(item["_mila_calendar_candidates"], [])

    def test_match_accepts_only_high_confidence_supplied_event(self):
        source = {"match_max_output_tokens": 2048}
        item = {
            "id": "mila:R@hash",
            "title": "Meeting",
            "body": "Transcript",
            "_mila_calendar_candidates": [{
                "id": "event-1",
                "summary": "Meeting",
                "start": "2026-07-27T10:00:00+03:00",
                "end": "2026-07-27T10:30:00+03:00",
                "organizer": "organizer@example.com",
                "attendees": [{"email": "guest@example.com"}],
            }],
            "frontmatter": {
                "mila_recording_start": "2026-07-27T07:00:00Z",
                "mila_recording_end": "2026-07-27T07:30:00Z",
                "mila_duration_seconds": 1800,
            },
        }
        response = {
            "content": json.dumps({
                "matched": True,
                "event_id": "event-1",
                "confidence": "high",
                "reason": "Exact overlap and title.",
            })
        }

        with mock.patch.object(mila_source, "yoetz_bin", return_value="yoetz"), \
             mock.patch.object(mila_source, "run_json", return_value=response):
            accepted, match = mila_source.match_calendar(
                routine(self.root, source), source, item
            )

        self.assertTrue(accepted)
        self.assertEqual(match["event_id"], "event-1")
        self.assertEqual(item["title"], "Meeting")
        self.assertEqual(
            {person["email"] for person in item["frontmatter"]["source_people"]},
            {"organizer@example.com", "guest@example.com"},
        )

        response["content"] = json.dumps({
            "matched": True,
            "event_id": "invented",
            "confidence": "high",
            "reason": "Hallucinated.",
        })
        with mock.patch.object(mila_source, "yoetz_bin", return_value="yoetz"), \
             mock.patch.object(mila_source, "run_json", return_value=response), \
             self.assertRaisesRegex(RuntimeError, "outside the supplied"):
            mila_source.match_calendar(
                routine(self.root, source), source, item
            )

        response["content"] = json.dumps({
            "matched": True,
            "event_id": None,
            "confidence": "high",
            "reason": "Missing identity.",
        })
        with mock.patch.object(mila_source, "yoetz_bin", return_value="yoetz"), \
             mock.patch.object(mila_source, "run_json", return_value=response), \
             self.assertRaisesRegex(RuntimeError, "non-empty event_id"):
            mila_source.match_calendar(
                routine(self.root, source), source, item
            )

    def test_receipts_are_private_current_state_files(self):
        item = {
            "id": "mila:ID@hash",
            "source_id": "mila:ID",
            "frontmatter": {
                "mila_recording_id": "ID",
                "mila_content_hash": "hash",
                "mila_recording_start": "2026-07-27T10:00:00Z",
            },
        }
        failed = mila_source.write_receipt(
            self.root, "failed", item, {"failure_kind": "calendar-match"}
        )
        processed = mila_source.write_receipt(
            self.root, "processed", item, {"memory": "created"}
        )

        self.assertFalse(failed.exists())
        self.assertTrue(processed.exists())
        self.assertEqual(processed.stat().st_mode & 0o777, 0o600)

    def test_dry_and_wet_processing_leave_mila_files_byte_identical(self):
        legacy_record = record("LEGACY")
        self.write_json(self.legacy / "recordings.json", [legacy_record])
        self.write_json(self.current / "recordings.json", [])
        transcript = self.current / "meeting.srt"
        fallback = self.current / "meeting.txt"
        transcript.write_text(
            "1\n00:00:00,000 --> 00:00:02,000\nA decision was made.\n"
        )
        fallback.write_text("A decision was made.")
        source = {
            "kind": "mila",
            "recordings_file": str(self.current / "recordings.json"),
            "manual_recordings": [{
                "recording_id": "LEGACY",
                "recordings_file": str(self.legacy / "recordings.json"),
                "transcript_file": str(transcript),
                "fallback_file": str(fallback),
            }],
            "max_results": 0,
        }
        files = [
            self.current / "recordings.json",
            self.legacy / "recordings.json",
            transcript,
            fallback,
        ]
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in files
        }
        event = {
            "id": "event-1",
            "summary": "Intro meeting",
            "start": "2026-07-27T19:00:00Z",
            "end": "2026-07-27T19:40:00Z",
            "event_type": "default",
            "attendees": [],
        }
        match_response = {
            "content": json.dumps({
                "matched": True,
                "event_id": "event-1",
                "confidence": "high",
                "reason": "Exact overlap.",
            })
        }
        configured = routine(self.root / "memory", source)

        with mock.patch.object(
            mila_source, "_raw_calendar_events", return_value=[event]
        ), mock.patch.object(
            mila_source, "run_json", return_value=match_response
        ), mock.patch.object(
            llm, "analyze", return_value="Durable decision."
        ), mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "created", "memory_entry_id": "entry"},
        ):
            runner.run(self.root, [configured], dry_run=True)
            runner.run(self.root, [configured])

        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in files
        }
        self.assertEqual(after, before)


class MilaValidationTest(unittest.TestCase):
    def test_valid_source_and_invalid_paths(self):
        source = {
            "kind": "mila",
            "recordings_file": "/tmp/mila/recordings.json",
            "manual_recordings": [{
                "recording_id": "ID",
                "transcript_file": "/tmp/mila/meeting.srt",
            }],
            "calendar_timezone": "Asia/Jerusalem",
            "calendar_window_days": 3,
            "max_results": 0,
        }
        self.assertEqual(config.validate(routine("/tmp/store", source)), [])

        source["recordings_file"] = "relative.json"
        source["manual_recordings"][0]["transcript_file"] = "relative.srt"
        problems = config.validate(routine("/tmp/store", source))
        self.assertTrue(
            any("recordings_file must be an absolute" in p for p in problems)
        )
        self.assertTrue(
            any("transcript_file must be an absolute" in p for p in problems)
        )


class MilaRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.source = {
            "kind": "mila",
            "recordings_file": "/tmp/recordings.json",
            "max_results": 0,
        }
        self.routine = routine(self.base / "memory", self.source)
        self.candidate = {
            "id": "mila:ID@hash",
            "title": "Meeting",
            "raw": {"source_id": "mila:ID"},
        }
        self.item = {
            "id": self.candidate["id"],
            "source_id": "mila:ID",
            "source_kind": "mila",
            "title": "Meeting",
            "date": "2026-07-27",
            "body": "Transcript",
            "_mila_calendar_candidates": [{"id": "event-1"}],
            "frontmatter": {
                "mila_recording_id": "ID",
                "mila_content_hash": "hash",
                "mila_recording_start": "2026-07-27T10:00:00Z",
                "mila_recording_end": "2026-07-27T10:30:00Z",
                "mila_duration_seconds": 1800,
            },
        }

    def patched_source(self):
        return mock.patch.dict(
            runner.SOURCES,
            {"mila": (
                lambda _source: [self.candidate],
                lambda _routine, _source, _candidate: dict(
                    self.item,
                    frontmatter=dict(self.item["frontmatter"]),
                ),
            )},
        )

    def test_dry_run_never_calls_calendar_match_or_writes_state(self):
        with self.patched_source(), \
             mock.patch.object(mila_source, "match_calendar") as match:
            totals = runner.run(
                self.base, [self.routine], dry_run=True
            )

        self.assertEqual(totals["processed"], 1)
        match.assert_not_called()
        self.assertFalse((self.base / "state").exists())

    def test_low_match_is_ledgered_and_receipted_without_memory(self):
        with self.patched_source(), \
             mock.patch.object(
                 mila_source,
                 "match_calendar",
                 return_value=(False, {
                     "matched": False,
                     "event_id": None,
                     "confidence": "medium",
                     "reason": "Ambiguous.",
                 }),
             ), \
             mock.patch.object(memory_sink, "capture") as capture:
            totals = runner.run(self.base, [self.routine])

        self.assertEqual(totals["processed"], 1)
        capture.assert_not_called()
        ledger = state.load(self.base)
        self.assertTrue(ledger[self.candidate["id"]]["calendar_match_rejected"])
        receipts = list(
            (self.base / "state" / "transcriptions" / "failed").glob("*.json")
        )
        self.assertEqual(len(receipts), 1)

    def test_high_match_runs_analysis_and_writes_processed_receipt(self):
        with self.patched_source(), \
             mock.patch.object(
                 mila_source,
                 "match_calendar",
                 return_value=(True, {
                     "matched": True,
                     "event_id": "event-1",
                     "confidence": "high",
                     "reason": "Exact.",
                 }),
             ), \
             mock.patch.object(llm, "analyze", return_value="Summary"), \
             mock.patch.object(
                 memory_sink, "capture",
                 return_value={"memory": "created", "memory_entry_id": "entry"},
             ):
            totals = runner.run(self.base, [self.routine])

        self.assertEqual(totals["errors"], 0)
        receipt = next(
            (self.base / "state" / "transcriptions" / "processed").glob("*.json")
        )
        self.assertEqual(json.loads(receipt.read_text())["memory"], "created")

    def test_rejected_match_is_retried_without_transcript_change(self):
        rejected = {
            "matched": False,
            "event_id": None,
            "confidence": "medium",
            "reason": "Ambiguous.",
        }
        accepted = {
            "matched": True,
            "event_id": "event-1",
            "confidence": "high",
            "reason": "Calendar was corrected.",
        }
        with self.patched_source(), mock.patch.object(
            mila_source, "match_calendar",
            side_effect=[(False, rejected), (True, accepted)],
        ) as match, mock.patch.object(
            llm, "analyze", return_value="Summary"
        ), mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "created", "memory_entry_id": "entry"},
        ):
            first = runner.run(self.base, [self.routine])
            second = runner.run(self.base, [self.routine])

        ledger = state.load(self.base)
        self.assertEqual(first["errors"], 0)
        self.assertEqual(second["errors"], 0)
        self.assertEqual(match.call_count, 2)
        self.assertFalse(
            ledger[self.candidate["id"]].get("calendar_match_rejected")
        )

    def test_successful_new_version_resolves_rejected_old_version(self):
        rejected = {
            "matched": False,
            "event_id": None,
            "confidence": "medium",
            "reason": "Ambiguous.",
        }
        accepted = {
            "matched": True,
            "event_id": "event-1",
            "confidence": "high",
            "reason": "Calendar was corrected.",
        }
        with self.patched_source(), mock.patch.object(
            mila_source, "match_calendar", return_value=(False, rejected)
        ), mock.patch.object(memory_sink, "capture"):
            first = runner.run(self.base, [self.routine])

        old_id = self.candidate["id"]
        self.assertEqual(first["errors"], 0)
        self.candidate["id"] = "mila:ID@corrected-hash"
        self.item["id"] = self.candidate["id"]

        with self.patched_source(), mock.patch.object(
            mila_source, "match_calendar", return_value=(True, accepted)
        ), mock.patch.object(
            llm, "analyze", return_value="Summary"
        ), mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "created", "memory_entry_id": "entry"},
        ):
            second = runner.run(self.base, [self.routine])

        ledger = state.load(self.base)
        self.assertEqual(second["errors"], 0)
        self.assertNotIn(old_id, ledger)
        self.assertIn(self.candidate["id"], ledger)
        self.assertFalse(
            ledger[self.candidate["id"]].get("calendar_match_rejected")
        )
        self.assertEqual(
            list(
                (
                    self.base / "state" / "transcriptions" / "failed"
                ).glob("*.json")
            ),
            [],
        )
        self.assertEqual(
            len(
                list(
                    (
                        self.base
                        / "state"
                        / "transcriptions"
                        / "processed"
                    ).glob("*.json")
                )
            ),
            1,
        )

    def test_receipt_failure_is_isolated_and_retried_without_ledger(self):
        blocked = (
            self.base / "state" / "transcriptions" / "processed"
        )
        blocked.parent.mkdir(parents=True)
        blocked.write_text("not a directory")
        accepted = {
            "matched": True,
            "event_id": "event-1",
            "confidence": "high",
            "reason": "Exact.",
        }
        with self.patched_source(), mock.patch.object(
            mila_source, "match_calendar", return_value=(True, accepted)
        ), mock.patch.object(
            llm, "analyze", return_value="Summary"
        ), mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "created", "memory_entry_id": "entry"},
        ):
            first = runner.run(self.base, [self.routine])

        self.assertEqual(first["errors"], 1)
        self.assertNotIn(self.candidate["id"], state.load(self.base))

        blocked.unlink()
        with self.patched_source(), mock.patch.object(
            mila_source, "match_calendar", return_value=(True, accepted)
        ), mock.patch.object(
            llm, "analyze", return_value="Summary"
        ), mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "updated", "memory_entry_id": "entry"},
        ):
            second = runner.run(self.base, [self.routine])

        self.assertEqual(second["errors"], 0)
        self.assertIn(self.candidate["id"], state.load(self.base))


# --- on-device matching, connector calendar, duplicate captures -----------------

from workspace_daemon import claude_bridge, local_llm  # noqa: E402

LIST_TOOL = "mcp__example_calendar__list_events"

CALENDAR_TEXT = """Found 2 event(s):

- Alpha // Beta - intro
  Time: 2026-07-27T22:00:00+03:00 - 2026-07-27T22:35:00+03:00
  ID: evt-intro
  Organizer: host@example.com
  Attendees: host@example.com (accepted), me@example.com (accepted, self), room-1@resource.calendar.google.com (accepted)

- Focus block
  Time: 2026-07-27T22:00:00+03:00 - 2026-07-27T22:30:00+03:00
  ID: evt-focus
  Organizer: me@example.com
  Attendees: me@example.com (accepted, self)
"""

LOCAL_MATCH = {
    "provider": "ollama",
    "model": "example-model",
    "base_url": "http://localhost:11434",
}


def event(event_id, summary, attendees=True, start="2026-07-27T19:00:00Z",
          end="2026-07-27T19:35:00Z"):
    return {
        "id": event_id, "summary": summary, "start": start, "end": end,
        "event_type": "default",
        "attendees": (
            [{"email": "guest@example.com"},
             {"email": "me@example.com", "self": True}]
            if attendees else [{"email": "me@example.com", "self": True}]
        ),
    }


def match_item(title="Alpha -- Beta - intro", events=(), body="Transcript"):
    return {
        "id": "mila:R@hash", "source_id": "mila:R", "title": title,
        "date": "2026-07-27", "body": body,
        "_mila_calendar_candidates": list(events),
        "frontmatter": {
            "mila_recording_start": "2026-07-27T19:02:44Z",
            "mila_recording_end": "2026-07-27T19:32:44Z",
            "mila_duration_seconds": 1800,
        },
    }


def local_source(**extra):
    return {"kind": "mila", "match": dict(LOCAL_MATCH), **extra}


class LocalMatchTest(unittest.TestCase):
    def setUp(self):
        mila_source._reset_run()
        self.routine = routine("/tmp/store", local_source())

    def match(self, item, source=None):
        return mila_source.match_calendar(
            self.routine, source or local_source(), item
        )

    def test_normalized_titles_ignore_separators_case_and_diacritics(self):
        self.assertTrue(mila_source.titles_match(
            "Alpha -- Beta - intro", "ALPHA // beta – Intro"
        ))
        self.assertTrue(mila_source.titles_match("Café | sync", "cafe sync"))
        self.assertTrue(mila_source.titles_match(
            "פגישה -- סיכום",
            "פגישה // סיכום",
        ))
        self.assertFalse(mila_source.titles_match("Alpha sync", "Beta review"))
        self.assertFalse(mila_source.titles_match("", ""))

    def test_title_and_time_rule_accepts_without_any_model(self):
        item = match_item(events=[
            event("evt-intro", "Alpha // Beta - intro"),
            event("evt-other", "Unrelated weekly"),
        ])
        with mock.patch.object(local_llm, "chat_json") as chat:
            accepted, match = self.match(item)
        chat.assert_not_called()
        self.assertTrue(accepted)
        self.assertEqual(match["event_id"], "evt-intro")
        self.assertEqual(match["confidence"], "high")
        self.assertEqual(item["frontmatter"]["calendar_event_id"], "evt-intro")

    def test_sole_overlapping_event_with_other_attendee_is_accepted(self):
        item = match_item(title="New Recording", events=[event("only", "Planning")])
        with mock.patch.object(local_llm, "chat_json") as chat:
            accepted, match = self.match(item)
        chat.assert_not_called()
        self.assertTrue(accepted)
        self.assertEqual(match["reason"], "sole overlapping event")

    def test_solo_block_is_not_auto_accepted(self):
        item = match_item(
            title="New Recording",
            events=[event("solo", "Focus", attendees=False)],
        )
        with mock.patch.object(
            local_llm, "chat_json",
            return_value={"matched": False, "event_id": None,
                          "confidence": "low", "reason": "x"},
        ) as chat:
            accepted, _match = self.match(item)
        chat.assert_called_once()
        self.assertFalse(accepted)

    def test_ambiguous_candidates_go_to_local_model(self):
        item = match_item(title="Weekly", body="שלום לכולם",
                          events=[event("a", "Weekly"), event("b", "Weekly")])
        reply = {"matched": True, "event_id": "b", "confidence": "high",
                 "reason": "transcript text that must never be kept"}
        with mock.patch.object(local_llm, "chat_json", return_value=reply) as chat:
            accepted, match = self.match(item)
        self.assertTrue(accepted)
        self.assertEqual(match["event_id"], "b")
        self.assertNotIn("transcript text", json.dumps(match))
        self.assertNotIn("transcript text", json.dumps(item["frontmatter"]))
        args = chat.call_args[0]
        self.assertEqual(args[0], "http://localhost:11434")
        self.assertEqual(args[1], "example-model")
        self.assertIn("שלום", args[2][0]["content"])
        self.assertIs(args[3], transcripts_schema())

    def test_local_model_cannot_select_an_unsupplied_event(self):
        item = match_item(events=[event("a", "Weekly"), event("b", "Weekly")],
                          title="Weekly")
        reply = {"matched": True, "event_id": "invented",
                 "confidence": "high", "reason": ""}
        with mock.patch.object(local_llm, "chat_json", return_value=reply):
            accepted, match = self.match(item)
        self.assertFalse(accepted)
        self.assertEqual(match["confidence"], "low")

    def test_medium_confidence_is_not_accepted(self):
        item = match_item(events=[event("a", "Weekly"), event("b", "Weekly")],
                          title="Weekly")
        reply = {"matched": True, "event_id": "a", "confidence": "medium",
                 "reason": ""}
        with mock.patch.object(local_llm, "chat_json", return_value=reply):
            accepted, _match = self.match(item)
        self.assertFalse(accepted)

    def test_ollama_down_leaves_unmatched_and_never_uses_a_cloud_model(self):
        item = match_item(events=[event("a", "Weekly"), event("b", "Weekly")],
                          title="Weekly")
        with mock.patch.object(
            local_llm, "chat_json",
            side_effect=local_llm.LocalLLMUnavailable("down"),
        ), mock.patch.object(mila_source, "run_json") as yoetz, \
             mock.patch.object(claude_bridge, "complete") as claude:
            accepted, match = self.match(item)
        self.assertFalse(accepted)
        self.assertFalse(match["matched"])
        yoetz.assert_not_called()
        claude.assert_not_called()

    def test_chat_json_maps_network_failure_to_unavailable(self):
        import urllib.error
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("refused")
        with mock.patch(
            "urllib.request.build_opener", return_value=opener
        ), self.assertRaises(local_llm.LocalLLMUnavailable):
            local_llm.chat_json("http://localhost:1", "m", [], {})

    def test_chat_json_sends_schema_temperature_and_utf8(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"message": {"content": '{"ok": true}'}}).encode()

        opener = mock.Mock()
        opener.open.return_value = Response()
        schema = {"type": "object"}
        with mock.patch("urllib.request.build_opener", return_value=opener):
            result = local_llm.chat_json(
                "http://localhost:11434/", "m",
                [{"role": "user", "content": "שלום"}],
                schema, timeout=5,
            )
        self.assertEqual(result, {"ok": True})
        request = opener.open.call_args[0][0]
        self.assertEqual(request.full_url, "http://localhost:11434/api/chat")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["format"], schema)
        self.assertEqual(body["options"]["temperature"], 0)
        self.assertFalse(body["stream"])
        self.assertIn("שלום", request.data.decode("utf-8"))

    def test_chat_json_rejects_malformed_reply_without_echoing_it(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"message": {"content": "SECRET not json"}}).encode()

        opener = mock.Mock()
        opener.open.return_value = Response()
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(local_llm.LocalLLMError) as ctx:
                local_llm.chat_json("http://localhost:1", "m", [], {})
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, local_llm.LocalLLMUnavailable)


def transcripts_schema():
    from workspace_daemon import transcripts
    return transcripts.MATCH_SCHEMA


def connector_source(**extra):
    return {
        "kind": "mila",
        "calendar": {
            "provider": "connector", "list_tool": LIST_TOOL,
            "calendar_id": "primary", "owner_email": "me@example.com",
        },
        "calendar_timezone": "Asia/Jerusalem",
        "calendar_match_hours": 6,
        "match": dict(LOCAL_MATCH),
        **extra,
    }


def fake_call_tools(text=CALENDAR_TEXT):
    def call(prompt, tools, **kwargs):
        return claude_bridge.ToolResults([
            {"name": name, "input": dict(args), "text": text, "is_error": False}
            for name, args in tools.items()
        ])
    return mock.Mock(side_effect=call)


class ConnectorCalendarTest(unittest.TestCase):
    def setUp(self):
        mila_source._reset_run()
        self.start = datetime.datetime(
            2026, 7, 27, 19, 2, 44, tzinfo=datetime.timezone.utc
        )
        self.end = self.start + datetime.timedelta(minutes=30)

    def test_one_pinned_read_only_call_returns_normalized_events(self):
        bridge = fake_call_tools()
        with mock.patch.object(claude_bridge, "call_tools", bridge):
            events = mila_source._raw_calendar_events(
                connector_source(), self.start, self.end
            )
        bridge.assert_called_once()
        tools = bridge.call_args[0][1]
        self.assertEqual(list(tools), [LIST_TOOL])
        self.assertEqual(tools[LIST_TOOL], {
            "calendar_id": "primary",
            "time_min": "2026-07-27T13:00:00Z",
            "time_max": "2026-07-28T02:00:00Z",
            "max_results": 250,
        })
        self.assertEqual(bridge.call_args[1]["required"], [LIST_TOOL])
        by_id = {e["id"]: e for e in events}
        intro = by_id["evt-intro"]
        self.assertEqual(intro["summary"], "Alpha // Beta - intro")
        self.assertEqual(intro["organizer"], "host@example.com")
        self.assertEqual(
            [a["email"] for a in intro["attendees"]],
            ["host@example.com", "me@example.com"],  # room dropped
        )
        self.assertTrue(intro["attendees"][1]["self"])
        self.assertEqual(intro["response_status"], "accepted")

    def test_recordings_in_one_window_cost_one_call(self):
        bridge = fake_call_tools()
        with mock.patch.object(claude_bridge, "call_tools", bridge):
            first = mila_source._raw_calendar_events(
                connector_source(), self.start, self.end
            )
            second = mila_source._raw_calendar_events(
                connector_source(),
                self.start + datetime.timedelta(seconds=26),
                self.end - datetime.timedelta(minutes=2),
            )
        self.assertEqual(bridge.call_count, 1)
        self.assertIs(first, second)

    def test_distant_recording_makes_a_second_call(self):
        bridge = fake_call_tools()
        with mock.patch.object(claude_bridge, "call_tools", bridge):
            mila_source._raw_calendar_events(
                connector_source(), self.start, self.end
            )
            mila_source._raw_calendar_events(
                connector_source(),
                self.start + datetime.timedelta(days=2),
                self.end + datetime.timedelta(days=2),
            )
        self.assertEqual(bridge.call_count, 2)

    def test_unrecognised_connector_text_fails_loudly(self):
        with mock.patch.object(
            claude_bridge, "call_tools", fake_call_tools("garbage")
        ), self.assertRaisesRegex(RuntimeError, "not recognised"):
            mila_source._raw_calendar_events(
                connector_source(), self.start, self.end
            )

    def test_dry_run_never_calls_the_connector(self):
        bridge = fake_call_tools()
        with mock.patch.object(claude_bridge, "call_tools", bridge):
            events = mila_source._raw_calendar_events(
                connector_source(_dry_run=True), self.start, self.end
            )
        self.assertEqual(events, [])
        bridge.assert_not_called()

    def test_gws_remains_the_default_provider(self):
        with mock.patch.object(
            mila_source, "run_json", return_value={"events": [{"id": "x"}]}
        ) as run, mock.patch.object(
            mila_source, "gws_bin", return_value="gws"
        ), mock.patch.object(claude_bridge, "call_tools") as bridge:
            events = mila_source._raw_calendar_events(
                {"calendar_timezone": "UTC"}, self.start, self.end
            )
        self.assertEqual(events, [{"id": "x"}])
        bridge.assert_not_called()
        self.assertIn("calendar", run.call_args[0][0])


class DuplicateCaptureTest(unittest.TestCase):
    def setUp(self):
        mila_source._reset_run()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.recordings = self.base / "recordings.json"
        segment = [{"start": 0, "end": 1, "text": "שלום"}]
        self.recordings.write_text(json.dumps([
            record("MEMO", source="voiceMemo", audio="Memo.m4a",
                   created="2026-07-27T19:03:10Z", duration=1780,
                   title="Alpha -- Beta - intro", segments=segment),
            record("SYS", source="systemAudio", duration=1800,
                   title="Alpha -- Beta - intro", segments=segment),
            record("RUNNING", status="running", source="systemAudio",
                   title="Alpha -- Beta - intro", segments=segment),
        ]))
        self.source = connector_source(
            recordings_file=str(self.recordings), max_results=0,
        )
        self.routine = routine(self.base / "memory", self.source)
        self.routine["analyze"] = {
            "provider": "claude", "model": "sonnet",
            "instruction": "Keep durable meeting facts.",
        }

    def run_once(self):
        with mock.patch.object(
            claude_bridge, "call_tools", self.bridge
        ), mock.patch.object(
            llm, "analyze", return_value="Durable decision."
        ) as analyze, mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "created", "memory_entry_id": "entry"},
        ) as capture, mock.patch.object(
            local_llm, "chat_json"
        ) as chat:
            runner.run(self.base, [self.routine])
        chat.assert_not_called()
        return analyze, capture

    def test_running_recording_is_skipped_until_completed(self):
        found = mila_source.candidates(self.source)
        self.assertEqual(
            {c["raw"]["source_id"] for c in found}, {"mila:MEMO", "mila:SYS"}
        )
        data = json.loads(self.recordings.read_text())
        data[2]["status"] = "completed"
        self.recordings.write_text(json.dumps(data))
        self.assertIn(
            "mila:RUNNING",
            {c["raw"]["source_id"] for c in mila_source.candidates(self.source)},
        )

    def test_preferred_capture_comes_first(self):
        order = [c["raw"]["source_id"] for c in mila_source.candidates(self.source)]
        self.assertEqual(order, ["mila:SYS", "mila:MEMO"])

    def test_longest_wins_when_neither_is_system_audio(self):
        data = json.loads(self.recordings.read_text())
        data[1]["source"] = "voiceMemo"
        data[1]["createdAt"] = "2026-07-27T19:02:44Z"
        data[1]["duration"] = 1500
        self.recordings.write_text(json.dumps(data))
        order = [c["raw"]["source_id"] for c in mila_source.candidates(self.source)]
        self.assertEqual(order, ["mila:MEMO", "mila:SYS"])

    def test_two_captures_of_one_meeting_yield_one_memory(self):
        self.bridge = fake_call_tools()
        analyze, capture = self.run_once()

        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(self.bridge.call_count, 1)  # one window, one read
        self.assertEqual(
            capture.call_args[0][1]["source_id"], "mila:SYS"
        )
        ledger = state.load(self.base)
        by_source = {entry["source_id"]: entry for entry in ledger.values()}
        self.assertEqual(by_source["mila:MEMO"]["duplicate_of"], "mila:SYS")
        self.assertNotIn("duplicate_of", by_source["mila:SYS"])
        self.assertEqual(by_source["mila:SYS"]["calendar_event_id"], "evt-intro")
        self.assertNotIn("calendar_match_rejected", by_source["mila:MEMO"])
        processed = list(
            (self.base / "state" / "transcriptions" / "processed").glob("*.json")
        )
        self.assertEqual(len(processed), 2)
        self.assertFalse(
            list((self.base / "state" / "transcriptions" / "failed").glob("*.json"))
        )

        # A later run leaves both settled.
        mila_source._reset_run()
        analyze, capture = self.run_once()
        analyze.assert_not_called()
        capture.assert_not_called()

    def test_later_import_of_same_meeting_is_a_duplicate_across_runs(self):
        data = json.loads(self.recordings.read_text())
        self.recordings.write_text(json.dumps([data[1]]))  # only SYS first
        self.bridge = fake_call_tools()
        self.run_once()
        self.recordings.write_text(json.dumps([data[0], data[1]]))  # MEMO arrives
        mila_source._reset_run()
        analyze, capture = self.run_once()
        analyze.assert_not_called()
        capture.assert_not_called()
        ledger = state.load(self.base)
        memo = next(e for e in ledger.values() if e["source_id"] == "mila:MEMO")
        self.assertEqual(memo["duplicate_of"], "mila:SYS")

    def test_ollama_down_is_unmatched_then_retried_next_run(self):
        data = json.loads(self.recordings.read_text())
        self.recordings.write_text(json.dumps([data[1]]))
        twin = CALENDAR_TEXT.replace("Focus block", "Alpha // Beta - intro")
        self.bridge = fake_call_tools(twin)

        def run(chat):
            mila_source._reset_run()
            with mock.patch.object(
                claude_bridge, "call_tools", self.bridge
            ), mock.patch.object(
                llm, "analyze", return_value="Durable decision."
            ), mock.patch.object(
                memory_sink, "capture",
                return_value={"memory": "created", "memory_entry_id": "e"},
            ) as capture, mock.patch.object(
                local_llm, "chat_json", chat
            ), mock.patch.object(claude_bridge, "complete") as cloud:
                runner.run(self.base, [self.routine])
            cloud.assert_not_called()
            return capture

        down = mock.Mock(side_effect=local_llm.LocalLLMUnavailable("down"))
        capture = run(down)
        capture.assert_not_called()
        entry = next(iter(state.load(self.base).values()))
        self.assertTrue(entry["calendar_match_rejected"])

        up = mock.Mock(return_value={
            "matched": True, "event_id": "evt-focus",
            "confidence": "high", "reason": "x",
        })
        capture = run(up)
        capture.assert_called_once()
        entry = next(iter(state.load(self.base).values()))
        self.assertNotIn("calendar_match_rejected", entry)
        self.assertEqual(entry["calendar_event_id"], "evt-focus")

    def test_non_overlapping_recordings_of_one_event_are_not_duplicates(self):
        mila_source._reset_run()
        long_event = event(
            "e", "Alpha // Beta - intro", end="2026-07-27T22:00:00Z"
        )
        first = match_item(events=[long_event])
        second = match_item(events=[long_event])
        second["source_id"] = "mila:LATER"
        second["frontmatter"]["mila_recording_start"] = "2026-07-27T21:00:00Z"
        second["frontmatter"]["mila_recording_end"] = "2026-07-27T21:20:00Z"
        second["frontmatter"]["mila_duration_seconds"] = 1200
        r = routine("/tmp/store", local_source())
        self.assertTrue(mila_source.match_calendar(r, local_source(), first)[0])
        self.assertTrue(mila_source.match_calendar(r, local_source(), second)[0])


class MilaConfigTest(unittest.TestCase):
    def problems(self, **source_extra):
        source = {"kind": "mila", "recordings_file": "/tmp/mila/recordings.json",
                  **source_extra}
        return config.validate(routine("/tmp/store", source))

    def test_connector_and_ollama_config_is_valid(self):
        self.assertEqual(self.problems(
            calendar={"provider": "connector", "list_tool": LIST_TOOL,
                      "calendar_id": "primary", "owner_email": "me@example.com"},
            match=dict(LOCAL_MATCH),
            max_transcript_chars=20000,
        ), [])

    def test_list_tool_must_be_a_read_only_list_name(self):
        for bad in (
            "mcp__example_calendar__create_event",
            "mcp__example_calendar__delete_event",
            "mcp__example_calendar__update_events",
            "list_events",
            "mcp__example_calendar__get_event",
            None,
        ):
            problems = self.problems(
                calendar={"provider": "connector", "list_tool": bad}
            )
            self.assertTrue(
                any("list_tool" in p for p in problems), msg=str(bad)
            )
        self.assertTrue(mila_source.valid_list_tool(
            "mcp__example-cal_1__list_events"
        ))

    def test_gws_remains_valid_default(self):
        self.assertEqual(self.problems(calendar={"provider": "gws"}), [])
        self.assertTrue(any(
            "provider" in p for p in self.problems(calendar={"provider": "x"})
        ))

    def test_match_requires_local_base_url_and_known_provider(self):
        self.assertTrue(any("localhost" in p for p in self.problems(
            match={"provider": "ollama", "model": "m",
                   "base_url": "http://example.com:11434"}
        )))
        self.assertTrue(any("match.provider" in p for p in self.problems(
            match={"provider": "gemini", "model": "m"}
        )))
        self.assertTrue(any("match.model" in p for p in self.problems(
            match={"provider": "ollama"}
        )))

    def test_claude_analyze_needs_a_match_block_for_mila(self):
        source = {"kind": "mila", "recordings_file": "/tmp/mila/recordings.json"}
        configured = routine("/tmp/store", source)
        configured["analyze"] = {"provider": "claude", "model": "sonnet",
                                 "instruction": "x"}
        self.assertTrue(any(
            "claude is not supported" in p for p in config.validate(configured)
        ))
        source["match"] = dict(LOCAL_MATCH)
        self.assertEqual(config.validate(configured), [])

    def test_transcript_bound_is_validated_and_applied(self):
        self.assertTrue(any("max_transcript_chars" in p
                            for p in self.problems(max_transcript_chars=5)))
        text = "ש" * 1000 + "MIDDLE" + "ת" * 1000
        bounded = mila_source._bounded(text, 1000)
        self.assertLess(len(bounded), len(text))
        self.assertIn("truncated", bounded)
        self.assertTrue(bounded.startswith("ש"))
        self.assertTrue(bounded.endswith("ת"))
        self.assertEqual(mila_source._bounded("short", 1000), "short")


class ImportTimeScenarioTest(unittest.TestCase):
    """One meeting captured twice; the second copy's createdAt is import time."""

    E1 = ("E1", "X // Y - intro", "2026-07-27T08:00:00Z", "2026-07-27T08:30:00Z")
    E2 = ("E2", "X / Z - intro", "2026-07-27T09:00:00Z", "2026-07-27T09:30:00Z")

    def setUp(self):
        mila_source._reset_run()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.recordings = self.base / "recordings.json"
        seg = [{"start": 0, "end": 1, "text": "שלום"}]
        self.records = [
            record("A", source="voiceMemo", audio="A.m4a",
                   created="2026-07-27T08:02:49Z", duration=1712,
                   title="X -- Y - intro", segments=seg),
            record("B", source="systemAudio", audio="B.m4a",
                   created="2026-07-27T08:56:36Z", duration=1712,
                   title="X -- Y - intro", segments=seg),
        ]
        self.recordings.write_text(json.dumps(self.records))
        self.source = connector_source(
            recordings_file=str(self.recordings), max_results=0,
            calendar_timezone="UTC",
        )
        self.routine = routine(self.base / "memory", self.source)
        self.routine["analyze"] = {"provider": "claude", "model": "sonnet",
                                   "instruction": "Keep durable facts."}
        lines = ["Found 2 event(s):\n"]
        for eid, title, start, end in (self.E1, self.E2):
            lines.append(
                f"- {title}\n  Time: {start} - {end}\n  ID: {eid}\n"
                "  Organizer: host@example.com\n"
                "  Attendees: host@example.com (accepted), "
                "me@example.com (accepted, self)\n"
            )
        self.bridge = fake_call_tools("\n".join(lines))

    def events(self):
        return [event(eid, title, start=start, end=end)
                for eid, title, start, end in (self.E1, self.E2)]

    def item(self, rid, start, end, duration=1712):
        it = match_item(title="X -- Y - intro", events=self.events())
        it["source_id"] = f"mila:{rid}"
        it["frontmatter"].update(
            mila_recording_start=start, mila_recording_end=end,
            mila_duration_seconds=duration,
        )
        return it

    def test_title_beats_a_sole_overlap_with_another_event(self):
        # B's computed interval sits on E2, but its title names E1.
        item = self.item("B", "2026-07-27T09:02:00Z", "2026-07-27T09:30:30Z")
        r = routine("/tmp/store", local_source())
        with mock.patch.object(local_llm, "chat_json") as chat:
            accepted, match = mila_source.match_calendar(r, local_source(), item)
        chat.assert_not_called()
        self.assertTrue(accepted)
        self.assertEqual(match["event_id"], "E1")

    def test_title_match_accepts_even_when_interval_misses_the_event(self):
        item = self.item("B", "2026-07-27T11:00:00Z", "2026-07-27T11:28:00Z")
        r = routine("/tmp/store", local_source())
        accepted, match = mila_source.match_calendar(r, local_source(), item)
        self.assertTrue(accepted)
        self.assertEqual(match["event_id"], "E1")

    def test_sole_overlap_is_refused_when_titles_conflict(self):
        item = match_item(
            title="Budget review",
            events=[event("only", "Planning offsite")],
        )
        with mock.patch.object(
            local_llm, "chat_json",
            return_value={"matched": False, "event_id": None,
                          "confidence": "low", "reason": ""},
        ) as chat:
            accepted, _ = mila_source.match_calendar(
                routine("/tmp/store", local_source()), local_source(), item
            )
        chat.assert_called_once()
        self.assertFalse(accepted)

    def test_generic_title_still_accepts_sole_overlap(self):
        for title in ("New recording", "Voice memo 12", ""):
            mila_source._reset_run()
            item = match_item(title=title, events=[event("only", "Planning")])
            accepted, match = mila_source.match_calendar(
                routine("/tmp/store", local_source()), local_source(), item
            )
            self.assertTrue(accepted, msg=title)
            self.assertEqual(match["reason"], "sole overlapping event")

    def test_copies_collapse_in_either_processing_order(self):
        a = ("A", "2026-07-27T08:02:49Z", "2026-07-27T08:31:21Z")
        b = ("B", "2026-07-27T08:28:04Z", "2026-07-27T08:56:36Z")
        r = routine("/tmp/store", local_source())
        for first, second in ((a, b), (b, a)):
            mila_source._reset_run()
            one = self.item(*first)
            two = self.item(*second)
            self.assertTrue(
                mila_source.match_calendar(r, local_source(), one)[0]
            )
            accepted, match = mila_source.match_calendar(
                r, local_source(), two
            )
            self.assertFalse(accepted)
            self.assertEqual(match["duplicate_of"], f"mila:{first[0]}")
            self.assertEqual(match["event_id"], "E1")

    def test_end_to_end_one_memory_for_both_copies(self):
        with mock.patch.object(
            claude_bridge, "call_tools", self.bridge
        ), mock.patch.object(
            llm, "analyze", return_value="Durable decision."
        ) as analyze, mock.patch.object(
            memory_sink, "capture",
            return_value={"memory": "created", "memory_entry_id": "e"},
        ) as capture, mock.patch.object(local_llm, "chat_json") as chat:
            runner.run(self.base, [self.routine])
        chat.assert_not_called()
        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(capture.call_args[0][1]["source_id"], "mila:B")
        ledger = {e["source_id"]: e for e in state.load(self.base).values()}
        self.assertEqual(ledger["mila:A"]["duplicate_of"], "mila:B")
        self.assertEqual(ledger["mila:B"]["calendar_event_id"], "E1")

    def test_same_title_length_and_day_group_without_interval_overlap(self):
        order = [c["raw"]["source_id"]
                 for c in mila_source.candidates(self.source)]
        self.assertEqual(order, ["mila:B", "mila:A"])
        self.records[1]["duration"] = 1720  # more than 3s apart: not copies
        self.recordings.write_text(json.dumps(self.records))
        traits = [
            mila_source._traits(c, datetime.timezone.utc)
            for c in mila_source.candidates(self.source)
        ]
        self.assertFalse(mila_source._copies(
            traits[0], traits[1], datetime.timezone.utc
        ))
