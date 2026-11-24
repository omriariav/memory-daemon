"""Date anchor in the analysis and extraction prompts."""
import datetime
import json
import unittest
from unittest import mock

from workspace_daemon import llm, memory_sink

ROUTINE = {"id": "r", "analyze": {"provider": "claude", "model": "m",
                       "instruction": "Summarize."},
           "memory": {"store": "/store", "type": "note", "extract": True},
           "sources": [{"kind": "mila", "calendar_timezone": "Europe/London"}]}
ITEM = {"id": "x", "title": "Weekly sync", "date": "2026-11-04", "body": "b",
        "frontmatter": {}}

TABLE = [
    "Thu 2026-11-05", "Fri 2026-11-06", "Sat 2026-11-07", "Sun 2026-11-08",
    "Mon 2026-11-09", "Tue 2026-11-10", "Wed 2026-11-11",
]


class DateAnchorTest(unittest.TestCase):
    def test_names_the_meeting_weekday_and_next_seven_days(self):
        text = llm.date_anchor(ROUTINE, ITEM)
        self.assertIn("dated Wednesday 2026-11-04", text)
        rows = [line for line in text.splitlines()
                if line[:3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
                and line[3] == " "]
        self.assertEqual(rows, TABLE)
        self.assertIn("ONLY from this table", text)

    def test_thursday_after_a_wednesday_meeting_is_the_5th(self):
        self.assertIn("Thu 2026-11-05", llm.date_anchor(ROUTINE, ITEM))
        self.assertNotIn("Thu 2026-11-06", llm.date_anchor(ROUTINE, ITEM))

    def test_missing_date_uses_today(self):
        item = dict(ITEM, date="")
        text = llm.date_anchor(ROUTINE, item, today=datetime.date(2026, 11, 4))
        self.assertIn("Wednesday 2026-11-04", text)

    def test_zone_comes_from_the_source_or_the_routine(self):
        self.assertEqual(str(llm.routine_zone(ROUTINE)), "Europe/London")
        self.assertEqual(
            str(llm.routine_zone({"timezone": "Asia/Jerusalem"})),
            "Asia/Jerusalem")
        self.assertIsNone(llm.routine_zone({"timezone": "Not/AZone"}))

    def test_analysis_prompt_carries_the_table(self):
        prompt = llm.build_prompt(ROUTINE, ITEM, [])
        self.assertIn("Wednesday 2026-11-04", prompt)
        for row in TABLE:
            self.assertIn(row, prompt)

    def test_extraction_prompt_carries_the_table(self):
        seen = {}

        def fake_analyze(routine, prompt, **kwargs):
            seen["prompt"] = prompt
            return json.dumps({
                "worthy": True, "owner_attention": False, "type": "note",
                "title": "T", "people": [], "tags": ["x"], "follows": [],
                "body": "b"})

        with mock.patch.object(llm, "analyze", side_effect=fake_analyze), \
                mock.patch.object(memory_sink, "known_person_slugs",
                                  return_value=set()):
            memory_sink._extract(ROUTINE, ITEM, "s", "/store")
        for row in TABLE:
            self.assertIn(row, seen["prompt"])
        self.assertIn("dated Wednesday 2026-11-04", seen["prompt"])


class SavedNoteTest(unittest.TestCase):
    def saved_item(self, **meta):
        return dict(ITEM, frontmatter=meta)

    def test_analysis_prompt_tells_the_model_the_owner_saved_it(self):
        for meta in ({"slack_saved": True}, {"saved_request": True}):
            with self.subTest(meta=meta):
                prompt = llm.build_prompt(ROUTINE, self.saved_item(**meta), [])
                self.assertIn("explicitly saved this item", prompt)
                self.assertIn("preceding context", prompt)
        for meta in ({}, {"slack_save_tag": "saved"}):
            with self.subTest(meta=meta):
                prompt = llm.build_prompt(ROUTINE, self.saved_item(**meta), [])
                self.assertNotIn("explicitly saved", prompt)

    def extraction_prompt(self, **kwargs):
        seen = {}

        def fake_analyze(routine, prompt, **kw):
            seen["prompt"] = prompt
            return json.dumps({
                "worthy": True, "owner_attention": False, "type": "note",
                "title": "T", "people": [], "tags": ["x"], "follows": [],
                "body": "b"})

        with mock.patch.object(llm, "analyze", side_effect=fake_analyze), \
                mock.patch.object(memory_sink, "known_person_slugs",
                                  return_value=set()):
            memory_sink._extract(ROUTINE, ITEM, "s", "/store", **kwargs)
        return seen["prompt"]

    def test_extraction_prompt_says_a_saved_item_is_worthy(self):
        prompt = self.extraction_prompt(saved=True)
        self.assertIn("explicitly saved this item", prompt)
        self.assertIn("Never write a title or body saying there is no durable",
                      prompt)
        self.assertNotIn("explicitly saved", self.extraction_prompt())

    def test_retry_note_is_added(self):
        prompt = self.extraction_prompt(saved=True, retry_note="Try again.")
        self.assertIn("Try again.", prompt)


if __name__ == "__main__":
    unittest.main()
