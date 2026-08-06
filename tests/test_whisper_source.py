import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import config, runner, whisper_source


MEET_BASE = (
    "2026-08-03-15-25-36-Value-reflection-product-strategy-brief"
    "---2026_02_18-13_03-IST---Recording-9abaf3"
)
LEGACY_MEET_BASE = (
    "2026-04-12-13-11-Data-Track-MGMT---bi-weekly-OKRs-update-meeting"
    "---2026_04_09-09_55-IDT--Recording"
)
FILE_BASE = "2026-08-02-09-39-40-Gil---Omri---weekly-31-7-8f8423"


def source(directory, **overrides):
    value = {
        "kind": "whisper",
        "transcriptions_dir": str(directory),
        "calendar_timezone": "Asia/Jerusalem",
        "min_quiet_seconds": 0,
        "max_results": 0,
    }
    value.update(overrides)
    return value


def routine(store, src):
    return {
        "id": "whisper-transcriptions",
        "enabled": True,
        "source": src,
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


class WhisperSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, body="שלום, החלטנו להתקדם."):
        path = self.dir / name
        path.write_text(body, encoding="utf-8")
        settled = time.time() - 3600
        os.utime(path, (settled, settled))
        return path

    def test_meet_filename_anchors_meeting_time_not_transcription_time(self):
        self.write(f"{MEET_BASE}-he.txt")
        found = whisper_source.candidates(source(self.dir))
        self.assertEqual(len(found), 1)
        raw = found[0]["raw"]
        # Transcribed Aug 3; the meeting itself was Feb 18 13:03 Israel time.
        self.assertEqual(raw["recording_start"], "2026-02-18T11:03:00Z")
        self.assertEqual(raw["origin"], "meet")
        self.assertEqual(
            raw["source_id"],
            "whisper:meet:2026-02-18T13-03:"
            "value-reflection-product-strategy-brief",
        )
        self.assertEqual(
            found[0]["title"], "Value reflection product strategy brief"
        )

    def test_meet_name_variants_converge_on_one_identity(self):
        # The same meeting, transcribed twice by different pipeline versions:
        # dashed meeting datetime vs underscored, different sanitizer output
        # for "//", en-dash before "Recording", different path hashes.
        self.write(
            "2026-07-28-13-51-08-UD-supply----UD-demand---weekly-sync"
            "---2026-07-28-12-01-IDT-–-Recording-77238a-he.txt"
        )
        self.write(
            "2026-07-28-15-54-12-UD-supply-__-UD-demand---weekly-sync"
            "---2026_07_28-12_01-IDT-–-Recording-2793a4-he.txt"
        )
        found = whisper_source.candidates(source(self.dir))
        self.assertEqual(len(found), 1)
        raw = found[0]["raw"]
        self.assertEqual(raw["origin"], "meet")
        self.assertEqual(
            raw["source_id"],
            "whisper:meet:2026-07-28T12-01:ud-supply-ud-demand-weekly-sync",
        )
        self.assertEqual(raw["recording_start"], "2026-07-28T09:01:00Z")
        # The later transcription wins as the current version.
        self.assertTrue(raw["base_name"].startswith("2026-07-28-15-54-12"))

    def test_rerun_of_same_recording_keeps_one_candidate_same_memory(self):
        self.write(f"{MEET_BASE}-he.txt")
        rerun_base = (
            "2026-08-05-10-00-00-Value-reflection-product-strategy-brief"
            "---2026_02_18-13_03-IST---Recording-0aa9fe"
        )
        self.write(f"{rerun_base}-he.txt", "תמלול מתוקן ומלא יותר.")
        found = whisper_source.candidates(source(self.dir))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["raw"]["base_name"], rerun_base)

    def test_variant_preference_and_capture_versioning(self):
        self.write(f"{LEGACY_MEET_BASE}-en.txt", "English translation.")
        self.write(f"{LEGACY_MEET_BASE}-he.txt", "עברית רגילה.")
        plain = whisper_source.candidates(source(self.dir))[0]
        self.assertTrue(
            plain["raw"]["transcript_path"].endswith("-he.txt")
        )
        self.write(
            f"{LEGACY_MEET_BASE}-he-diarized.txt", "דובר 1: עברית רגילה."
        )
        diarized = whisper_source.candidates(source(self.dir))[0]
        self.assertTrue(
            diarized["raw"]["transcript_path"].endswith("-he-diarized.txt")
        )
        # Same stable memory identity, new candidate version: the diarized
        # variant is reprocessed as a correction of the same memory.
        self.assertEqual(
            plain["raw"]["source_id"], diarized["raw"]["source_id"]
        )
        self.assertNotEqual(plain["id"], diarized["id"])

    def test_recent_writes_defer_ingestion_until_quiet(self):
        path = self.write(f"{MEET_BASE}-he.txt")
        os.utime(path)  # just written by the worker
        self.assertEqual(
            whisper_source.candidates(source(self.dir, min_quiet_seconds=300)),
            [],
        )
        settled = time.time() - 3600
        os.utime(path, (settled, settled))
        self.assertEqual(
            len(
                whisper_source.candidates(
                    source(self.dir, min_quiet_seconds=300)
                )
            ),
            1,
        )

    def test_unrelated_files_in_shared_directory_are_ignored(self):
        # Mila's live store can share the output directory.
        self.write("Data KPIs dashboard review - 30-7.txt")
        self.write("recordings.json", "[]")
        self.write("Onboarding - Limor 2026-07-30T10-07-34Z-986938.txt")
        (self.dir / f"{MEET_BASE}.m4a").write_bytes(b"\x00")
        self.assertEqual(whisper_source.candidates(source(self.dir)), [])

    def test_excluded_and_empty_outputs(self):
        self.write(f"{MEET_BASE}-he.txt", "")
        self.write(f"{FILE_BASE}-he.txt")
        found = whisper_source.candidates(
            source(self.dir, exclude_base_names=[FILE_BASE])
        )
        self.assertEqual(len(found), 1)
        self.assertIn("transcript_error", found[0]["raw"])
        with self.assertRaisesRegex(RuntimeError, "empty"):
            whisper_source.fetch({}, source(self.dir), found[0])

    def test_fetch_ranks_containing_event_and_redacts_secrets(self):
        self.write(
            f"{MEET_BASE}-he.txt",
            "Use token ghp_abcdefghijklmnopqrstuvwxyz1234567890 now",
        )
        events = [
            {
                "id": "near",
                "summary": "Unrelated sync",
                "start": "2026-02-18T14:30:00+02:00",
                "end": "2026-02-18T15:00:00+02:00",
            },
            {
                "id": "covering",
                "summary": "Value Reflection - product strategy brief",
                "start": "2026-02-18T13:00:00+02:00",
                "end": "2026-02-18T13:30:00+02:00",
                "attendees": [{"email": "omri.a@taboola.com"}],
            },
        ]
        candidate = whisper_source.candidates(source(self.dir))[0]
        with mock.patch.object(
            whisper_source, "_raw_calendar_events", return_value=events
        ):
            item = whisper_source.fetch({}, source(self.dir), candidate)
        ranked = item["_whisper_calendar_candidates"]
        self.assertEqual([event["id"] for event in ranked][0], "covering")
        self.assertNotIn("ghp_", item["body"])
        self.assertIn("REDACTED", item["body"])
        self.assertEqual(item["date"], "2026-02-18")
        meta = item["frontmatter"]
        self.assertEqual(meta["whisper_origin"], "meet")
        self.assertIsNone(meta["whisper_duration_seconds"])
        self.assertEqual(meta["calendar_candidate_count"], 2)

    def test_match_accepts_only_high_confidence_supplied_event(self):
        self.write(f"{MEET_BASE}-he.txt")
        src = source(self.dir)
        candidate = whisper_source.candidates(src)[0]
        events = [{
            "id": "EVENT",
            "summary": "Value Reflection",
            "start": "2026-02-18T13:00:00+02:00",
            "end": "2026-02-18T13:30:00+02:00",
            "organizer": {"email": "omri.a@taboola.com"},
            "attendees": [{"email": "guest@taboola.com"}],
        }]
        with mock.patch.object(
            whisper_source, "_raw_calendar_events", return_value=events
        ):
            item = whisper_source.fetch({}, src, candidate)
        rt = routine(self.dir, src)

        def matcher(payload):
            return {"content": json.dumps(payload)}

        with mock.patch.object(whisper_source, "yoetz_bin", return_value="yoetz"), \
             mock.patch.object(
                 whisper_source, "run_json",
                 return_value=matcher({
                     "matched": True, "event_id": "EVENT",
                     "confidence": "medium", "reason": "close in time",
                 }),
             ):
            accepted, match = whisper_source.match_calendar(rt, src, item)
        self.assertFalse(accepted)
        self.assertEqual(match["confidence"], "medium")

        with mock.patch.object(whisper_source, "yoetz_bin", return_value="yoetz"), \
             mock.patch.object(
                 whisper_source, "run_json",
                 return_value=matcher({
                     "matched": True, "event_id": "INVENTED",
                     "confidence": "high", "reason": "made up",
                 }),
             ), self.assertRaisesRegex(RuntimeError, "outside the supplied"):
            whisper_source.match_calendar(rt, src, item)

        with mock.patch.object(whisper_source, "yoetz_bin", return_value="yoetz"), \
             mock.patch.object(
                 whisper_source, "run_json",
                 return_value=matcher({
                     "matched": True, "event_id": "EVENT",
                     "confidence": "high", "reason": "title and time match",
                 }),
             ):
            accepted, match = whisper_source.match_calendar(rt, src, item)
        self.assertTrue(accepted)
        self.assertEqual(item["title"], "Value Reflection")
        meta = item["frontmatter"]
        self.assertEqual(meta["calendar_event_id"], "EVENT")
        self.assertEqual(
            [person["email"] for person in meta["source_people"]],
            ["omri.a@taboola.com", "guest@taboola.com"],
        )

    def test_receipts_are_current_state_per_source_identity(self):
        base_dir = self.dir / "daemon"
        item = {
            "id": "whisper:meet:2026-02-18T13-03:value@abc",
            "source_id": "whisper:meet:2026-02-18T13-03:value",
            "frontmatter": {
                "whisper_base_name": MEET_BASE,
                "whisper_content_hash": "abc",
                "whisper_recording_start": "2026-02-18T11:03:00Z",
            },
        }
        failed = whisper_source.write_receipt(
            base_dir, "failed", item, {"failure_kind": "calendar-match"}
        )
        self.assertTrue(failed.exists())
        processed = whisper_source.write_receipt(
            base_dir, "processed", item, {"memory_entry_id": "entry"}
        )
        self.assertTrue(processed.exists())
        self.assertFalse(failed.exists())
        payload = json.loads(processed.read_text())
        self.assertEqual(payload["base_name"], MEET_BASE)
        self.assertEqual(payload["status"], "processed")

    def test_duration_upgrades_matching_to_interval_overlap(self):
        self.write(f"{MEET_BASE}-he.txt")
        with mock.patch.object(
            whisper_source, "_probe_duration", return_value=1800.0
        ):
            candidate = whisper_source.candidates(source(self.dir))[0]
        raw = candidate["raw"]
        self.assertEqual(raw["duration"], 1800.0)
        self.assertEqual(raw["recording_end"], "2026-02-18T11:33:00Z")


class WhisperValidationTest(unittest.TestCase):
    def problems(self, src):
        return config.validate(routine("/tmp/store", src))

    def test_valid_source_and_invalid_fields(self):
        valid = {
            "kind": "whisper",
            "transcriptions_dir": "/somewhere/transcriptions",
            "calendar_timezone": "Asia/Jerusalem",
            "min_quiet_seconds": 120,
            "exclude_base_names": ["2026-01-01-10-00-00-old-abc123"],
            "max_results": 0,
        }
        self.assertEqual(self.problems(valid), [])
        self.assertTrue(any(
            "transcriptions_dir" in problem
            for problem in self.problems({"kind": "whisper"})
        ))
        self.assertTrue(any(
            "transcriptions_dir" in problem
            for problem in self.problems({
                "kind": "whisper", "transcriptions_dir": "relative/path",
            })
        ))
        self.assertTrue(any(
            "min_quiet_seconds" in problem
            for problem in self.problems({
                "kind": "whisper",
                "transcriptions_dir": "/somewhere",
                "min_quiet_seconds": -5,
            })
        ))
        self.assertTrue(any(
            "calendar_timezone" in problem
            for problem in self.problems({
                "kind": "whisper",
                "transcriptions_dir": "/somewhere",
                "calendar_timezone": "Neverland/Nowhere",
            })
        ))


class WhisperRunnerWiringTest(unittest.TestCase):
    def test_whisper_is_a_registered_transcript_source(self):
        self.assertIn("whisper", runner.SOURCES)
        self.assertIn("whisper", runner.TRANSCRIPT_SOURCES)
        self.assertEqual(runner._SOURCE_DEFAULT_LIMITS["whisper"], 0)
        module = runner.TRANSCRIPT_SOURCES["whisper"]
        for hook in (
            "match_calendar", "write_receipt", "dry_run_description",
            "error_receipt_item",
        ):
            self.assertTrue(callable(getattr(module, hook)))


if __name__ == "__main__":
    unittest.main()
