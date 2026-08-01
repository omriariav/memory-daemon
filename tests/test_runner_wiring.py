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

from workspace_daemon import (  # noqa: E402
    actions,
    gmail,
    labels,
    llm,
    memory_sink,
    runner,
    state,
)

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
            "read_thread": gmail.read_thread,
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
        gmail.read_thread = self.saved["read_thread"]
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
                "subject": "[EXTERNAL] AW: Weekly Report DACH TEAM | 4/7/2026",
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

    def test_message_updates_use_reply_date_body_and_identity(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Re: Weekly Report DACH TEAM | 4/7/2026",
                "from": "Someone Else <reply@example.com>",
                "date": "Mon, 27 Jul 2026 15:46:29 +0000",
            },
            "body": (
                "This week's new report.\n\n"
                "On Mon, Jul 20, 2026, Someone Else\n"
                "wrote:\n"
                "> Last week's report.\n"
                "> Older details."
            ),
        }
        r = routine(
            self.vault,
            streams={
                "Weekly Report DACH": {
                    "title": "Weekly - DACH",
                    "label": "EMEA",
                    "message_updates": True,
                }
            },
        )
        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "m2", "raw": {"thread_id": "thread-1"}},
        )

        self.assertEqual(item["date"], "2026-07-27")
        self.assertEqual(item["body"], "This week's new report.")
        self.assertEqual(item["source_id"], "gmail:m2")
        self.assertEqual(item["frontmatter"]["report_date"], "2026-07-27")
        self.assertEqual(item["frontmatter"]["subject_report_date"], "2026-07-04")
        self.assertTrue(item["frontmatter"]["quoted_history_removed"])

    def test_gmail_exposes_structured_address_headers_for_identity_resolution(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Privacy decision",
                "from": '"Alice Example" <ALICE@example.com>',
                "to": "Memory Owner <owner@example.com>, Bob Example <bob@example.com>",
                "cc": "Bob Again <BOB@example.com>, Carol Example <carol@example.com>",
                "date": "Mon, 27 Jul 2026 15:46:29 +0000",
            },
            "body": "A durable privacy decision.",
        }
        r = routine(self.vault, label="EMEA")

        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "m2", "raw": {"thread_id": "thread-1"}},
        )

        self.assertEqual(
            item["frontmatter"]["source_people"],
            [
                {
                    "email": "alice@example.com",
                    "name": "Alice Example",
                    "role": "from",
                },
                {
                    "email": "owner@example.com",
                    "name": "Memory Owner",
                    "role": "to",
                },
                {
                    "email": "bob@example.com",
                    "name": "Bob Example",
                    "role": "to",
                },
                {
                    "email": "carol@example.com",
                    "name": "Carol Example",
                    "role": "cc",
                },
            ],
        )
        self.assertFalse(item["frontmatter"]["source_people_truncated"])

    def test_gmail_marks_active_self_forwarded_chat_as_followup(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Fwd: Chat with a colleague",
                "from": "Owner <owner@example.com>",
                "to": "Owner <OWNER@example.com>",
                "date": "Sat, 1 Aug 2026 17:00:00 +0000",
            },
            "labels": ["SENT", "INBOX", "STARRED"],
            "body": "Please follow up on the proposal.",
        }
        r = routine(self.vault)
        r["source"]["self_forwarded_chat_followups"] = True

        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "m2", "raw": {"thread_id": "thread-1"}},
        )

        meta = item["frontmatter"]
        self.assertTrue(meta["gmail_manual_chat_followup"])
        self.assertTrue(meta["gmail_chat_followup_managed"])
        self.assertTrue(meta["gmail_chat_followup_active"])
        self.assertEqual(meta["gmail_labels"], ["INBOX", "SENT", "STARRED"])

    def test_managed_followup_queue_is_uncapped_and_deduplicated(self):
        calls = []

        def search(query, max_results=20):
            calls.append((query, max_results))
            if query == runner.GMAIL_CHAT_FOLLOWUP_QUERY:
                return [
                    {
                        "message_id": "followup-new",
                        "thread_id": "thread-new",
                        "subject": "Fwd: Chat with a colleague",
                    },
                    {
                        "message_id": "followup-old",
                        "thread_id": "thread-old",
                        "subject": "Fwd: Chat in a project space",
                    },
                ]
            return [
                {
                    "message_id": "followup-new",
                    "thread_id": "thread-new",
                    "subject": "Fwd: Chat with a colleague",
                },
                {
                    "message_id": "ordinary",
                    "thread_id": "ordinary",
                    "subject": "A recent message",
                },
            ]

        gmail.search = search
        r = routine(self.vault, actions=[])
        r["source"].update({
            "query": "newer_than:1d",
            "max_results": 1,
            "self_forwarded_chat_followups": True,
            "actions": [],
        })
        totals = {"errors": 0}

        claims, failures = runner._collect_claims([r], totals)

        self.assertEqual(
            calls,
            [
                ("newer_than:1d", 1),
                (runner.GMAIL_CHAT_FOLLOWUP_QUERY, 0),
            ],
        )
        self.assertEqual(
            set(claims),
            {
                ("gmail", "followup-new"),
                ("gmail", "followup-old"),
                ("gmail", "ordinary"),
            },
        )
        followup_claims = claims[("gmail", "followup-new")]
        self.assertEqual(len(followup_claims), 2)
        self.assertTrue(
            any(
                claim["candidate"]["raw"].get(
                    "_gmail_chat_followup_candidate"
                )
                for claim in followup_claims
            )
        )
        self.assertEqual(failures, [])
        self.assertEqual(totals["errors"], 0)

    def test_dedicated_queue_query_handles_self_aliases(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Fwd: Chat with a colleague",
                "from": "owner-primary@example.com",
                "to": "owner-alias@example.com",
                "date": "Sat, 1 Aug 2026 17:00:00 +0000",
            },
            "labels": ["INBOX", "SENT"],
            "body": "Please follow up on the proposal.",
        }
        r = routine(self.vault)
        r["source"]["self_forwarded_chat_followups"] = True
        candidate = {
            "id": "m2",
            "raw": {
                "thread_id": "thread-1",
                "_gmail_chat_followup_candidate": True,
            },
        }

        item = runner._gmail_fetch(r, r["source"], candidate)

        self.assertTrue(item["frontmatter"]["gmail_manual_chat_followup"])
        self.assertTrue(item["frontmatter"]["gmail_chat_followup_managed"])
        self.assertTrue(item["frontmatter"]["gmail_chat_followup_active"])

    def test_gmail_marks_archived_self_forward_as_inactive(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Fwd: Chat in a project space",
                "from": "owner@example.com",
                "to": "owner@example.com",
                "date": "Sat, 1 Aug 2026 17:00:00 +0000",
            },
            "labels": ["SENT"],
            "body": "Project context.",
        }
        r = routine(self.vault)
        r["source"]["self_forwarded_chat_followups"] = True

        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "m2", "raw": {"thread_id": "thread-1"}},
        )

        self.assertTrue(item["frontmatter"]["gmail_manual_chat_followup"])
        self.assertTrue(item["frontmatter"]["gmail_chat_followup_managed"])
        self.assertFalse(item["frontmatter"]["gmail_chat_followup_active"])

    def test_automatic_chat_notification_is_not_manual_followup(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "A colleague messaged you on Google Chat",
                "from": "Google Chat <chat-noreply@example.com>",
                "to": "owner@example.com",
                "date": "Sat, 1 Aug 2026 17:00:00 +0000",
            },
            "labels": ["INBOX", "UNREAD"],
            "body": "Notification body.",
        }
        r = routine(self.vault)

        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "m2", "raw": {"thread_id": "thread-1"}},
        )

        self.assertFalse(item["frontmatter"]["gmail_manual_chat_followup"])
        self.assertFalse(item["frontmatter"]["gmail_chat_followup_managed"])
        self.assertFalse(item["frontmatter"]["gmail_chat_followup_active"])

    def test_archived_chat_followup_resolves_memory_and_ledger(self):
        processed = state.Store(self.base)
        processed.record("m1", {
            "rule_id": "wiring",
            "gmail_followup_open": True,
            "gmail_thread_id": "thread-1",
            "gmail_followup_title": "Fwd: Chat with a colleague",
            "memory_entry_id": "2026-07-31-open-follow-up",
        })
        gmail.search = lambda query, max_results=20: []
        r = routine(
            self.vault,
            memory={"store": "/store", "type": "note"},
        )
        with mock.patch.object(
            memory_sink,
            "resolve_followup",
            return_value={
                "memory": "created",
                "memory_entry_id": "2026-08-01-completed-follow-up",
            },
        ) as resolve, mock.patch.object(
            runner, "utc_now_iso", return_value="2026-08-01T17:00:00Z"
        ):
            count = runner._reconcile_gmail_chat_followups(r, processed)

        self.assertEqual(count, 1)
        resolve.assert_called_once_with(
            r,
            memory_entry_id="2026-07-31-open-follow-up",
            thread_id="thread-1",
            title="Fwd: Chat with a colleague",
        )
        record = processed.get("m1")
        self.assertFalse(record["gmail_followup_open"])
        self.assertEqual(
            record["gmail_followup_resolution_entry_id"],
            "2026-08-01-completed-follow-up",
        )

    def test_followup_remaining_in_inbox_stays_open(self):
        processed = state.Store(self.base)
        processed.record("m1", {
            "rule_id": "wiring",
            "gmail_followup_open": True,
            "gmail_thread_id": "thread-1",
            "memory_entry_id": "2026-07-31-open-follow-up",
        })
        gmail.search = lambda query, max_results=20: [{
            "message_id": "m2",
            "thread_id": "thread-1",
            "subject": "Fwd: Chat with a colleague",
        }]
        r = routine(
            self.vault,
            memory={"store": "/store", "type": "note"},
        )
        with mock.patch.object(memory_sink, "resolve_followup") as resolve:
            count = runner._reconcile_gmail_chat_followups(r, processed)

        self.assertEqual(count, 0)
        resolve.assert_not_called()
        self.assertTrue(processed.get("m1")["gmail_followup_open"])

    def test_active_followup_retries_memory_failure_on_next_run(self):
        candidate = {
            "message_id": "m1",
            "thread_id": "thread-1",
            "subject": "Fwd: Chat with a colleague",
        }
        gmail.search = lambda query, max_results=20: [candidate]
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": candidate["subject"],
                "from": "owner@example.com",
                "to": "owner@example.com",
                "date": "Sat, 1 Aug 2026 17:00:00 +0000",
            },
            "labels": ["SENT", "INBOX", "STARRED"],
            "body": "Please follow up on the proposal.",
        }
        r = routine(
            self.vault,
            memory={"store": "/store", "type": "note"},
            actions=[],
        )
        r["source"]["self_forwarded_chat_followups"] = True
        r["source"]["query"] = runner.GMAIL_CHAT_FOLLOWUP_QUERY
        outcomes = [
            RuntimeError("store unavailable"),
            {
                "memory": "created",
                "memory_entry_id": "2026-08-01-follow-up",
            },
        ]

        with mock.patch.object(
            memory_sink, "capture", side_effect=outcomes
        ) as capture:
            first = runner.run(self.base, [r])
            second = runner.run(self.base, [r])

        self.assertEqual(first["errors"], 1)
        self.assertEqual(second["errors"], 0)
        self.assertEqual(capture.call_count, 2)
        record = self.ledger()["m1"]
        self.assertNotIn("memory_error", record)
        self.assertTrue(record["gmail_followup_open"])
        self.assertEqual(
            record["memory_entry_id"], "2026-08-01-follow-up"
        )

    def test_managed_followup_upgrades_an_ordinary_ledger_record(self):
        processed = state.Store(self.base)
        processed.record("m1", {
            "rule_id": "specialized",
            "source_kind": "gmail",
            "processed_at": "2026-08-01T16:00:00Z",
            "memory_entry_id": "2026-08-01-ordinary-note",
        })
        candidate = {
            "message_id": "m1",
            "thread_id": "thread-1",
            "subject": "Fwd: Chat with a colleague",
        }
        gmail.search = lambda query, max_results=20: [candidate]
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": candidate["subject"],
                "from": "owner@example.com",
                "to": "owner@example.com",
                "date": "Sat, 1 Aug 2026 17:00:00 +0000",
            },
            "labels": ["SENT", "INBOX", "STARRED"],
            "body": "Please follow up on the proposal.",
        }
        r = routine(
            self.vault,
            memory={"store": "/store", "type": "note"},
            actions=[],
        )
        r["source"].update({
            "self_forwarded_chat_followups": True,
            "actions": [],
        })

        with mock.patch.object(
            memory_sink,
            "capture",
            return_value={
                "memory": "updated",
                "memory_entry_id": "2026-08-01-follow-up",
            },
        ) as capture:
            totals = runner.run(self.base, [r])

        self.assertEqual(totals["errors"], 0)
        capture.assert_called_once()
        record = self.ledger()["m1"]
        self.assertEqual(record["rule_id"], "wiring")
        self.assertTrue(record["gmail_followup_open"])
        self.assertTrue(record["gmail_manual_chat_followup"])
        self.assertEqual(
            record["memory_entry_id"], "2026-08-01-follow-up"
        )

    def test_followup_reconciliation_dry_run_writes_nothing(self):
        processed = state.Store(self.base, dry_run=True)
        processed.entries["m1"] = {
            "rule_id": "wiring",
            "gmail_followup_open": True,
            "gmail_thread_id": "thread-1",
            "memory_entry_id": "2026-07-31-open-follow-up",
        }
        gmail.search = lambda query, max_results=20: []
        r = routine(
            self.vault,
            memory={"store": "/store", "type": "note"},
        )
        with mock.patch.object(memory_sink, "resolve_followup") as resolve, \
             mock.patch.object(runner, "log") as log:
            count = runner._reconcile_gmail_chat_followups(
                r, processed, dry_run=True
            )

        self.assertEqual(count, 1)
        resolve.assert_not_called()
        self.assertTrue(processed.get("m1")["gmail_followup_open"])
        self.assertIn("would resolve", log.call_args.args[0])

    def test_one_broken_followup_does_not_block_other_resolutions(self):
        processed = state.Store(self.base)
        processed.record("broken", {
            "rule_id": "wiring",
            "gmail_followup_open": True,
            "gmail_thread_id": "thread-broken",
        })
        processed.record("healthy", {
            "rule_id": "wiring",
            "gmail_followup_open": True,
            "gmail_thread_id": "thread-healthy",
            "gmail_followup_title": "Fwd: Chat with a colleague",
            "memory_entry_id": "2026-07-31-open-follow-up",
        })
        gmail.search = lambda query, max_results=20: []
        r = routine(
            self.vault,
            memory={"store": "/store", "type": "note"},
        )
        with mock.patch.object(
            memory_sink,
            "resolve_followup",
            return_value={
                "memory": "created",
                "memory_entry_id": "2026-08-01-completed-follow-up",
            },
        ) as resolve:
            with self.assertRaisesRegex(
                RuntimeError, "1 follow-up.*remain unresolved"
            ):
                runner._reconcile_gmail_chat_followups(r, processed)

        resolve.assert_called_once()
        self.assertTrue(processed.get("broken")["gmail_followup_open"])
        self.assertFalse(processed.get("healthy")["gmail_followup_open"])

    def test_gmail_identity_candidates_are_capped_with_sender_first(self):
        recipients = ", ".join(
            f"Person {index} <person{index}@example.com>"
            for index in range(runner.MAX_GMAIL_SOURCE_PEOPLE + 5)
        )
        people, truncated = runner._email_source_people({
            "from": "Sender <sender@example.com>",
            "to": recipients,
        })

        self.assertEqual(len(people), runner.MAX_GMAIL_SOURCE_PEOPLE)
        self.assertEqual(people[0]["email"], "sender@example.com")
        self.assertTrue(truncated)

    def test_gmail_malformed_addresses_do_not_consume_identity_capacity(self):
        people, truncated = runner._email_source_people({
            "from": "Malformed, Sender <sender@example.com>",
        })

        self.assertEqual(
            people,
            [{
                "email": "sender@example.com",
                "name": "Sender",
                "role": "from",
            }],
        )
        self.assertFalse(truncated)

    def test_message_updates_root_message_keeps_explicit_subject_date(self):
        gmail.read_message = lambda mid: {
            "headers": {
                "subject": "Weekly Report DACH TEAM | 4/7/2026",
                "from": "Sender <sender@example.com>",
                "date": "Mon, 6 Jul 2026 15:46:29 +0000",
            },
            "body": "The original report.",
        }
        r = routine(
            self.vault,
            streams={
                "Weekly Report DACH": {
                    "title": "Weekly - DACH",
                    "label": "EMEA",
                    "message_updates": True,
                }
            },
        )
        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "thread-1", "raw": {"thread_id": "thread-1"}},
        )

        self.assertEqual(item["date"], "2026-07-04")
        self.assertEqual(item["source_id"], "gmail:thread-1")
        self.assertEqual(item["frontmatter"]["report_date"], "2026-07-04")
        self.assertNotIn("subject_report_date", item["frontmatter"])

    def test_gmail_read_thread_supplies_chronological_context_and_headers(self):
        gmail.read_message = lambda mid: self.fail("latest-message read should not run")
        gmail.read_thread = lambda thread_id: {
            "thread_id": thread_id,
            "messages": [
                {
                    "id": "m1",
                    "headers": {
                        "subject": "Proposal review",
                        "from": "Requester <requester@example.com>",
                        "to": "Leader <leader@example.com>",
                        "date": "Mon, 27 Jul 2026 10:00:00 +0000",
                    },
                    "body": "Please review the proposal by Friday.",
                },
                {
                    "id": "m2",
                    "headers": {
                        "subject": "Re: Proposal review",
                        "from": "Leader <leader@example.com>",
                        "to": "Requester <requester@example.com>",
                        "cc": "Reviewer <reviewer@example.com>",
                        "date": "Mon, 27 Jul 2026 11:00:00 +0000",
                    },
                    "body": (
                        "Reviewed and approved.\n\n"
                        "On Mon, Jul 27, 2026, Requester wrote:\n"
                        "> Please review the proposal by Friday."
                    ),
                },
            ],
        }
        r = routine(self.vault)
        r["source"]["read_thread"] = True

        item = runner._gmail_fetch(
            r,
            r["source"],
            {"id": "m2", "raw": {"thread_id": "thread-1"}},
        )

        self.assertLess(
            item["body"].index("--- Message 1 of 2 ---"),
            item["body"].index("--- Message 2 of 2 ---"),
        )
        self.assertIn("Please review the proposal by Friday.", item["body"])
        self.assertIn("Reviewed and approved.", item["body"])
        self.assertNotIn("> Please review", item["body"])
        self.assertEqual(item["title"], "Re: Proposal review")
        self.assertEqual(item["date"], "2026-07-27")
        self.assertEqual(item["frontmatter"]["email_to"],
                         "Requester <requester@example.com>")
        self.assertEqual(item["frontmatter"]["email_cc"],
                         "Reviewer <reviewer@example.com>")
        self.assertEqual(item["frontmatter"]["gmail_thread_message_count"], 2)
        self.assertEqual(item["frontmatter"]["gmail_thread_messages_included"], 2)
        self.assertFalse(item["frontmatter"]["gmail_thread_truncated"])
        self.assertEqual(
            [person["email"] for person in item["frontmatter"]["source_people"]],
            ["leader@example.com", "requester@example.com", "reviewer@example.com"],
        )

    def test_gmail_thread_context_bounds_old_messages_and_marks_partial_coverage(self):
        messages = [
            {
                "id": f"m{index}",
                "headers": {
                    "from": f"Sender {index} <sender{index}@example.com>",
                    "date": "Mon, 27 Jul 2026 11:00:00 +0000",
                },
                "body": f"Message body {index}",
            }
            for index in range(1, runner.MAX_GMAIL_THREAD_MESSAGES + 3)
        ]

        body, included, truncated = runner._gmail_thread_body(messages)

        self.assertEqual(included, runner.MAX_GMAIL_THREAD_MESSAGES)
        self.assertTrue(truncated)
        self.assertIn("Thread coverage: supplied 50 of 52 messages", body)
        self.assertNotIn("--- Message 1 of 52 ---", body)
        self.assertNotIn("--- Message 2 of 52 ---", body)
        self.assertIn("--- Message 3 of 52 ---", body)
        self.assertIn("--- Message 52 of 52 ---", body)

    def test_quote_stripping_preserves_uncorroborated_report_formatting(self):
        cases = (
            "Fresh report\nwith details",
            "Fresh intro\n>10% growth\nNext action",
            "Threshold\n_____\nNext action",
            "Section\n-----\nNext action",
        )
        for original in cases:
            with self.subTest(original=original):
                body, removed = runner._strip_quoted_history(original)
                self.assertEqual(body, original)
                self.assertFalse(removed)

    def test_quote_stripping_accepts_explicit_original_message_marker(self):
        body, removed = runner._strip_quoted_history(
            "Fresh report\n\n-----Original Message-----\nOld report"
        )
        self.assertEqual(body, "Fresh report")
        self.assertTrue(removed)

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
        self.assertEqual(
            runner._report_date_from_subject(
                "Bi Weekly Report - 1st July 2026"
            ),
            "2026-07-01",
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
