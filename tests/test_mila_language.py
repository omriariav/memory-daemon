"""Recordings transcribed in the wrong language are held and flagged once."""
import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import config, llm, mila_source

from test_mila_source import record, routine

HEBREW = "שלום זו פגישה על השקת מדינות חדשות בעברית"  # > 20 Hebrew letters
NOW = datetime.datetime(2026, 7, 27, 21, 0, tzinfo=datetime.timezone.utc)


class LanguageCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "state").mkdir()
        self.file = self.root / "recordings.json"
        patch = mock.patch.object(llm, "_base_dir", str(self.root))
        patch.start()
        self.addCleanup(patch.stop)

    def run_candidates(self, records, check=None, now=NOW, dry_run=False):
        self.file.write_text(json.dumps(records))
        source = {"recordings_file": str(self.file),
                  "calendar_timezone": "UTC"}
        if check is not False:
            source["language_check"] = check or {"expect": "he"}
        if dry_run:
            source["_dry_run"] = True
        with mock.patch.object(mila_source, "_now", return_value=now), \
                mock.patch.object(mila_source, "log") as log:
            found = mila_source.candidates(source)
        self.log = [c.args[0] for c in log.call_args_list]
        return [c["raw"]["source_id"] for c in found]

    def rec(self, rid, language, summary="", text="hello there"):
        r = record(rid, segments=[{"start": 0, "end": 2, "text": text}])
        r["language"] = language
        r["summary"] = summary
        return r

    def events(self):
        path = self.root / "state" / "events.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_english_transcript_of_a_hebrew_meeting_is_held_and_flagged_once(self):
        rec = self.rec("A", "en", summary=HEBREW)
        self.assertEqual(self.run_candidates([rec]), [])
        self.assertEqual(len(self.events()), 1)
        event = self.events()[0]
        self.assertEqual((event["source"], event["kind"]), ("daemon", "alert"))
        self.assertEqual(event["details"]["recording_id"], "A")
        self.run_candidates([rec])
        self.assertEqual(len(self.events()), 1)

    def test_matching_language_and_plain_english_pass(self):
        found = self.run_candidates([
            self.rec("HE", "he", summary=HEBREW, text=HEBREW),
            self.rec("EN", "en", summary="An English meeting"),
        ])
        self.assertEqual(sorted(found), ["mila:EN", "mila:HE"])
        self.assertEqual(self.events(), [])

    def test_hebrew_in_the_transcript_alone_counts(self):
        self.assertEqual(self.run_candidates([self.rec("T", "en", text=HEBREW)]), [])

    def test_captured_as_is_after_the_hold_with_one_warning(self):
        rec = self.rec("A", "en", summary=HEBREW)
        self.run_candidates([rec])
        later = NOW + datetime.timedelta(hours=3)
        self.assertEqual(self.run_candidates([rec], now=later), ["mila:A"])
        self.assertTrue(any("capturing as is" in line for line in self.log))
        self.run_candidates([rec], now=later)
        self.assertFalse(any("capturing as is" in line for line in self.log))

    def test_hold_counts_from_when_mila_finished(self):
        rec = self.rec("A", "en", summary=HEBREW)
        # Ended about 20:08; Mila finished it 130 minutes later.
        rec["completedAt"] = "2026-07-27T22:18:00Z"
        now = datetime.datetime(2026, 7, 27, 22, 30,
                                tzinfo=datetime.timezone.utc)
        self.assertEqual(self.run_candidates([rec], now=now), [])

    def test_hold_never_passes_the_calendar_match_horizon(self):
        rec = self.rec("A", "en", summary=HEBREW)
        rec["completedAt"] = "2026-07-28T01:00:00Z"
        now = datetime.datetime(2026, 7, 28, 1, 50,
                                tzinfo=datetime.timezone.utc)
        self.assertEqual(self.run_candidates([rec], now=now), ["mila:A"])

    def test_dry_run_neither_alerts_nor_flags(self):
        rec = self.rec("A", "en", summary=HEBREW)
        self.assertEqual(self.run_candidates([rec], dry_run=True), [])
        self.assertEqual(self.events(), [])
        self.run_candidates([rec])
        self.assertEqual(len(self.events()), 1)

    def test_an_alert_that_was_not_written_is_retried(self):
        rec = self.rec("A", "en", summary=HEBREW)
        with mock.patch.object(mila_source.events, "emit", return_value=False):
            self.run_candidates([rec])
        self.run_candidates([rec])
        self.assertEqual(len(self.events()), 1)

    def test_a_few_hebrew_names_in_english_do_not_count(self):
        summary = ("A long English summary of the meeting about pricing and "
                   "launch plans, with many English words. " * 5
                   + "שלום עליכם חברים יקרים מאוד")
        self.assertEqual(
            self.run_candidates([self.rec("E", "en", summary=summary)]),
            ["mila:E"])

    def test_retranscribed_recording_is_released(self):
        self.run_candidates([self.rec("A", "en", summary=HEBREW)])
        found = self.run_candidates([self.rec("A", "he", summary=HEBREW,
                                              text=HEBREW)])
        self.assertEqual(found, ["mila:A"])

    def test_off_without_the_setting(self):
        found = self.run_candidates([self.rec("A", "en", summary=HEBREW)],
                                    check=False)
        self.assertEqual(found, ["mila:A"])

    def test_validation(self):
        base = {"kind": "mila", "recordings_file": "/tmp/mila/recordings.json",
                "calendar_timezone": "UTC"}
        ok = dict(base, language_check={"expect": "he", "hold_minutes": 120})
        self.assertEqual(config.validate(routine("/tmp/store", ok)), [])
        for bad in ({"expect": "fr"}, {"expect": "he", "hold_minutes": 600},
                    {"expect": "he", "extra": 1}, "he"):
            problems = config.validate(
                routine("/tmp/store", dict(base, language_check=bad)))
            self.assertTrue(any("language_check" in p for p in problems), bad)


if __name__ == "__main__":
    unittest.main()
