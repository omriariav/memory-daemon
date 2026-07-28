"""End-to-end wiring tests for runner.run().

There was no coverage of runner.py at all, which is how a plain NameError
(`catalog` referenced in _process but never passed to it) reached review with
green CI: dry-runs return before that line, and the label tests exercise the
Catalog class in isolation. These drive the real run loop with only the
outermost I/O — Gmail, the LLM — stubbed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace_daemon import actions, gmail, labels, llm, runner  # noqa: E402

LABELS = ["EMEA", "CHANNELS", "ECN"]


def routine(vault, **extra):
    r = {
        "id": "wiring", "enabled": True,
        "source": {"kind": "gmail", "query": "in:inbox", "max_results": 5},
        "analyze": {"provider": "gemini", "model": "m", "instruction": "go"},
        "output": {"vault_dir": str(vault), "slug_prefix": "note"},
        "actions": ["apply_label", "archive"],
    }
    r.update(extra)
    return r


class RunnerWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.vault = self.base / "vault"
        (self.base / "state").mkdir()
        self.applied = []

        self.saved = {
            "search": gmail.search, "read": gmail.read_message,
            "user_labels": gmail.user_labels, "lab_gmail": labels.gmail,
            "analyze": llm.analyze, "handlers": dict(actions._HANDLERS),
        }
        gmail.search = lambda q, m=20: [
            {"message_id": "m1", "thread_id": "m1", "subject": "Weekly Report DACH TEAM"}
        ]
        gmail.read_message = lambda mid: {
            "headers": {"subject": "Weekly Report DACH TEAM",
                        "from": "Markus <markus.f@example.com>",
                        "date": "Sun, 19 Jul 2026 15:46:29 +0000"},
            "body": "pixel integration is delayed",
        }
        gmail.user_labels = lambda: list(LABELS)
        labels.gmail = gmail
        llm.analyze = lambda r, p: "a summary"
        actions._HANDLERS["archive"] = lambda mid: self.applied.append("archive")
        gmail.apply_label = lambda mid, label: self.applied.append(f"label:{label}")

    def tearDown(self):
        gmail.search = self.saved["search"]
        gmail.read_message = self.saved["read"]
        gmail.user_labels = self.saved["user_labels"]
        labels.gmail = self.saved["lab_gmail"]
        llm.analyze = self.saved["analyze"]
        actions._HANDLERS.clear()
        actions._HANDLERS.update(self.saved["handlers"])
        self.tmp.cleanup()

    def ledger(self):
        path = self.base / "state" / "processed.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def test_static_label_routine_runs_clean(self):
        """The regression: _process referenced an undefined `catalog`."""
        totals = runner.run(self.base, [routine(self.vault, label="EMEA")])
        self.assertEqual(totals["errors"], 0, "a static-label routine must not error")
        self.assertEqual(totals["processed"], 1)
        self.assertIn("label:EMEA", self.applied)
        self.assertEqual(self.ledger()["m1"]["gmail_label_applied"], "EMEA")

    def test_streams_label_routine_runs_clean(self):
        streams = {"Weekly Report DACH": {"title": "Weekly - DACH", "label": "EMEA"}}
        r = routine(self.vault, streams=streams)
        r["output"]["filename_template"] = "{title}-{date}"
        totals = runner.run(self.base, [r])
        self.assertEqual(totals["errors"], 0)
        self.assertIn("label:EMEA", self.applied)
        note = next(self.vault.glob("*.md"))
        self.assertEqual(note.name, "weekly-dach-2026-07-19.md",
                         "the stream title should drive the filename")
        self.assertIn("stream: Weekly - DACH", note.read_text())

    def test_stream_subject_date_beats_later_reply_date(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Re: Weekly Report DACH TEAM | 4/7/2026",
                "from": "Someone Else <reply@example.com>",
                "date": "Mon, 27 Jul 2026 15:46:29 +0000",
            },
            "body": "pixel integration is delayed",
        }
        streams = {
            "Weekly Report DACH": {
                "title": "Weekly - DACH",
                "label": "EMEA",
            }
        }
        r = routine(self.vault, streams=streams)
        r["output"]["filename_template"] = "{title}-{date}"

        totals = runner.run(self.base, [r])

        self.assertEqual(totals["errors"], 0)
        note = next(self.vault.glob("*.md"))
        self.assertEqual(note.name, "weekly-dach-2026-07-04.md")
        self.assertIn("report_date: '2026-07-04'", note.read_text())

    def test_non_stream_email_keeps_header_date(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Unrelated 4/7/2026",
                "from": "sender@example.com",
                "date": "Mon, 27 Jul 2026 15:46:29 +0000",
            },
            "body": "message",
        }
        r = routine(self.vault, label="EMEA")
        r["output"]["filename_template"] = "{title}-{date}"

        totals = runner.run(self.base, [r])

        self.assertEqual(totals["errors"], 0)
        note = next(self.vault.glob("*.md"))
        self.assertTrue(note.name.endswith("-2026-07-27.md"))

    def test_subject_report_date_parser_is_conservative(self):
        self.assertEqual(
            runner._report_date_from_subject("Weekly | 02/07/26"),
            "2026-07-02",
        )
        self.assertEqual(
            runner._report_date_from_subject("as of 27-07-2026"),
            "2026-07-27",
        )
        self.assertIsNone(
            runner._report_date_from_subject("week 27")
        )
        self.assertIsNone(
            runner._report_date_from_subject("Weekly | 39/19/2026")
        )

    def test_dry_run_names_configured_label(self):
        messages = []
        with mock.patch.object(runner, "log", side_effect=messages.append):
            totals = runner.run(
                self.base,
                [routine(self.vault, label="EMEA")],
                dry_run=True,
            )

        self.assertEqual(totals["errors"], 0)
        action_line = next(
            message for message in messages if "would apply:" in message
        )
        self.assertIn("apply_label 'EMEA'", action_line)
        self.assertNotIn("<llm-chosen>", action_line)

    def test_pick_label_routine_runs_clean(self):
        llm.analyze = lambda r, p: "a summary\nLABEL: CHANNELS"
        r = routine(self.vault)
        r["analyze"]["pick_label"] = True
        totals = runner.run(self.base, [r])
        self.assertEqual(totals["errors"], 0)
        self.assertIn("label:CHANNELS", self.applied)

    def test_a_mistyped_label_fails_the_routine_once_not_every_item(self):
        totals = runner.run(self.base, [routine(self.vault, label="NOT-A-REAL-LABEL")])
        self.assertEqual(totals["errors"], 1, "one routine-level failure, not one per item")
        self.assertEqual(totals["processed"], 0)
        self.assertEqual(self.ledger(), {}, "nothing should be ledgered")

    def test_an_empty_catalog_fails_fast_rather_than_refetching_per_item(self):
        """An empty catalog must fail the routine once, not once per item.

        Needs MORE THAN ONE candidate: with a single item the buggy guard and
        the fixed one are indistinguishable, since there is nothing to multiply.
        """
        calls = []
        gmail.user_labels = lambda: (calls.append(1), [])[1]
        gmail.search = lambda q, m=20: [
            {"message_id": f"m{i}", "thread_id": f"m{i}",
             "subject": "Weekly Report DACH TEAM"}
            for i in range(4)
        ]
        totals = runner.run(self.base, [routine(self.vault, label="EMEA")])
        self.assertEqual(totals["errors"], 1,
                         "one routine-level failure, not one per item")
        self.assertEqual(totals["processed"], 0)
        self.assertLessEqual(len(calls), 2,
                             f"refetch storm over 4 items: {len(calls)} catalog fetches")

    def test_dry_run_writes_nothing_at_all(self):
        runner.run(self.base, [routine(self.vault, label="EMEA")], dry_run=True)
        self.assertEqual(self.ledger(), {})
        self.assertFalse(self.vault.exists(), "no notes in a dry run")
        self.assertFalse(labels.cache_file(self.base).exists(),
                         "dry-run promises no state write, including the label cache")
        self.assertEqual(self.applied, [], "no Gmail mutations in a dry run")

    def test_label_cache_is_written_and_reused_across_runs(self):
        calls = []
        gmail.user_labels = lambda: (calls.append(1), list(LABELS))[1]
        runner.run(self.base, [routine(self.vault, label="EMEA")])
        self.assertTrue(labels.cache_file(self.base).exists())
        gmail.search = lambda q, m=20: [
            {"message_id": "m2", "thread_id": "m2", "subject": "Weekly Report DACH TEAM"}
        ]
        runner.run(self.base, [routine(self.vault, label="EMEA")])
        self.assertEqual(len(calls), 1, "the second run should reuse the cached catalog")


if __name__ == "__main__":
    unittest.main()
