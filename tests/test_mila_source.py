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
