"""memory_sink: slug-catalog parsing, model-output validation, source-id derivation."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import memory_sink

# Verbatim `slugs list --kind person` output from personal-memory (2026-07).
# The count column broke the original parser: it returned an empty set, and the
# validation layer then dropped every person the model attributed. Pinned here
# so a format drift fails loudly instead of silently un-peopling entries.
SLUGS_OUTPUT = """\
   1  dana-magen  (last seen 2026-07-26)
   1  helin-lee  (last seen 2026-07-26)
  12  limor-miller  (last seen 2026-07-26)
   3  yakov-rosenberg  (last seen 2026-07-26)
7 person slugs across 6 entries
"""


class FakeResult:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class SlugCatalogTest(unittest.TestCase):
    def setUp(self):
        memory_sink._slug_cache.clear()

    def test_parses_count_prefixed_rows(self):
        with mock.patch.object(memory_sink, "_cli", return_value=FakeResult(SLUGS_OUTPUT)):
            slugs = memory_sink.known_person_slugs("/store")
        self.assertEqual(
            slugs, {"dana-magen", "helin-lee", "limor-miller", "yakov-rosenberg"}
        )

    def test_summary_line_is_not_a_slug(self):
        with mock.patch.object(memory_sink, "_cli", return_value=FakeResult(SLUGS_OUTPUT)):
            slugs = memory_sink.known_person_slugs("/store")
        self.assertNotIn("person", slugs)
        self.assertNotIn("7", slugs)

    def test_cli_failure_yields_empty_set(self):
        with mock.patch.object(memory_sink, "_cli", return_value=FakeResult("", returncode=1)):
            self.assertEqual(memory_sink.known_person_slugs("/store"), set())

    def test_cached_per_store(self):
        with mock.patch.object(memory_sink, "_cli", return_value=FakeResult(SLUGS_OUTPUT)) as m:
            memory_sink.known_person_slugs("/store")
            memory_sink.known_person_slugs("/store")
        self.assertEqual(m.call_count, 1)

    def test_extraction_catalog_includes_verified_new_source_people(self):
        memory_sink._slug_cache["/store"] = {"existing-person"}
        verified = [{
            "email": "new.person@example.com",
            "name": "New Person",
            "slug": "new-person",
            "resource_name": "people/new-person",
        }]
        response = (
            '{"worthy":true,"owner_attention":false,"type":"note","title":"T",'
            '"people":["new-person"],"tags":["context"],"follows":[],"body":"b"}'
        )
        routine = {
            "analyze": {"provider": "gemini", "model": "m"},
        }
        with mock.patch("workspace_daemon.llm.analyze", return_value=response) as analyze:
            memory_sink._extract(
                routine,
                {"title": "T", "date": "2026-07-28"},
                "New Person made a decision.",
                "/store",
                verified_people=verified,
            )
        prompt = analyze.call_args.args[1]
        self.assertIn("New Person -> new-person", prompt)
        self.assertIn("new-person", prompt)

    def test_extraction_policy_keeps_concrete_pending_requests(self):
        response = (
            '{"worthy":true,"owner_attention":true,"type":"todo",'
            '"title":"Review roadmap","people":[],"tags":["roadmap"],'
            '"follows":[],"body":"Review the roadmap."}'
        )
        routine = {
            "analyze": {"provider": "gemini", "model": "m"},
        }
        with mock.patch("workspace_daemon.llm.analyze", return_value=response) as analyze:
            memory_sink._extract(
                routine,
                {"title": "Review request", "date": "2026-07-30"},
                "The product lead was asked to review the roadmap.",
                "/store",
            )

        prompt = " ".join(analyze.call_args.args[1].split())
        self.assertIn("concrete pending action/request", prompt)
        self.assertIn("worthy as a todo while it remains unresolved", prompt)
        self.assertIn("need not already have been accepted or started", prompt)
        self.assertIn("Preserve any stated deadline, but do not require one", prompt)
        self.assertIn('"owner_attention": boolean', prompt)
        self.assertIn("A third party's deadline, commitment, or unresolved work", prompt)
        self.assertIn(memory_sink.NO_OWNER_ACTION_MARKER, prompt)
        self.assertIn('use "todo" or "pending-decision" only when owner_attention is true', prompt)


class SourceIdTest(unittest.TestCase):
    def test_slack_item_id_passes_through(self):
        item = {"id": "slack:C0123:1700000000.000100", "frontmatter": {}}
        self.assertEqual(memory_sink.source_id_for(item), "slack:C0123:1700000000.000100")

    def test_gmail_thread_id(self):
        item = {"id": "m1", "frontmatter": {"gmail_thread_id": "t9"}}
        self.assertEqual(memory_sink.source_id_for(item), "gmail:t9")

    def test_gmail_followup_has_distinct_stable_source_id(self):
        item = {"id": "m1", "frontmatter": {"gmail_thread_id": "t9"}}
        self.assertEqual(
            memory_sink.followup_source_id_for(item),
            "gmail:t9:followup-open",
        )

    def test_drive_file_id(self):
        item = {"id": "d1", "frontmatter": {"drive_file_id": "f7"}}
        self.assertEqual(memory_sink.source_id_for(item), "gdrive:f7")

    def test_no_provenance_returns_none(self):
        self.assertIsNone(memory_sink.source_id_for({"id": "x", "frontmatter": {}}))

    def test_ambiguous_write_verification_requires_the_exact_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            entry_dir = Path(directory) / "memory" / "entries" / "2026" / "08"
            entry_dir.mkdir(parents=True)
            path = entry_dir / "durable.md"
            path.write_text(
                "---\n"
                "id: durable\n"
                "date: 2026-08-02\n"
                "type: note\n"
                "title: Durable update\n"
                "people: [jane-doe]\n"
                "tags: [auto-captured, product]\n"
                "source_ids: [gmail:thread-1]\n"
                "---\n"
                "The exact durable body.\n"
            )
            self.assertEqual(
                memory_sink._verify_written_entry(
                    directory, "gmail:thread-1", "note", "Durable update",
                    ["jane-doe"], ["auto-captured"],
                    "The exact durable body.",
                ),
                "durable",
            )
            self.assertIsNone(
                memory_sink._verify_written_entry(
                    directory, "gmail:thread-1", "note", "Durable update",
                    ["jane-doe"], ["auto-captured"], "stale body",
                )
            )


class ValidateTest(unittest.TestCase):
    def test_relative_store_rejected(self):
        probs = memory_sink.validate({"id": "r", "memory": {"store": "relative/path"}})
        self.assertTrue(any("absolute" in p for p in probs))

    def test_unknown_type_rejected(self):
        probs = memory_sink.validate({"id": "r", "memory": {"store": "/s", "type": "diary"}})
        self.assertTrue(any("diary" in p for p in probs))

    def test_valid_block_passes(self):
        self.assertEqual(
            memory_sink.validate({"id": "r", "memory": {"store": "/s", "type": "note",
                                                        "tags": ["a"]}}),
            [],
        )

    def test_operator_confirmed_source_ids_must_be_non_empty_strings(self):
        for invalid in (
            "gmail:t1",
            [""],
            [None],
            [" gmail:t1 "],
            ["gmail:"],
            ["bogus"],
            ["unknown:t1"],
        ):
            with self.subTest(value=invalid):
                probs = memory_sink.validate({
                    "id": "r",
                    "memory": {
                        "store": "/s",
                        "operator_confirmed_source_ids": invalid,
                    },
                })
                self.assertTrue(
                    any("operator_confirmed_source_ids" in p for p in probs),
                    probs,
                )

        self.assertEqual(
            memory_sink.validate({
                "id": "r",
                "memory": {
                    "store": "/s",
                    "operator_confirmed_source_ids": ["gmail:thread-1"],
                },
            }),
            [],
        )

    def test_no_memory_block_is_fine(self):
        self.assertEqual(memory_sink.validate({"id": "r"}), [])


class ConnectorStateTest(unittest.TestCase):
    def routine(self):
        return {
            "id": "sweep",
            "analyze": {
                "instruction_from_connector": "gchat",
                "connector_sweep": True,
            },
            "memory": {"store": "/store"},
        }

    def test_marks_connector_at_scan_start(self):
        with mock.patch.object(
            memory_sink, "_cli", return_value=FakeResult("✓ marked\n")
        ) as cli:
            changed = memory_sink.mark_connector_pulled(
                self.routine(), "2026-07-29T10:00:00Z"
            )
        self.assertTrue(changed)
        cli.assert_called_once_with(
            "/store",
            [
                "connectors", "mark-pulled", "gchat",
                "--at", "2026-07-29T10:00:00Z",
            ],
        )

    def test_dry_run_reports_but_does_not_write(self):
        with mock.patch.object(memory_sink, "_cli") as cli, \
             mock.patch.object(memory_sink, "log") as log:
            changed = memory_sink.mark_connector_pulled(
                self.routine(), "2026-07-29T10:00:00Z", dry_run=True
            )
        self.assertTrue(changed)
        cli.assert_not_called()
        self.assertIn("would mark connector", log.call_args.args[0])

    def test_cli_failure_is_a_hard_error(self):
        failed = FakeResult("bad connector state", returncode=1)
        with mock.patch.object(memory_sink, "_cli", return_value=failed):
            with self.assertRaisesRegex(
                RuntimeError, "mark-pulled failed"
            ):
                memory_sink.mark_connector_pulled(
                    self.routine(), "2026-07-29T10:00:00Z"
                )

    def test_inline_routine_does_not_touch_connector_state(self):
        routine = {
            "id": "specialized",
            "analyze": {"instruction": "Keep reports."},
            "memory": {"store": "/store"},
        }
        with mock.patch.object(memory_sink, "_cli") as cli:
            self.assertFalse(
                memory_sink.mark_connector_pulled(
                    routine, "2026-07-29T10:00:00Z"
                )
            )
        cli.assert_not_called()

    def test_connector_prompt_without_sweep_declaration_does_not_mark(self):
        routine = self.routine()
        routine["analyze"].pop("connector_sweep")
        with mock.patch.object(memory_sink, "_cli") as cli:
            self.assertFalse(
                memory_sink.mark_connector_pulled(
                    routine, "2026-07-29T10:00:00Z"
                )
            )
        cli.assert_not_called()


class CaptureValidationTest(unittest.TestCase):
    """Model output is validated: unknown slugs dropped, bad type falls back."""

    def setUp(self):
        memory_sink._slug_cache.clear()
        memory_sink.contacts.clear_cache()
        memory_sink._slug_cache["/store"] = {"jane-doe"}
        self.routine = {"id": "r", "memory": {"store": "/store", "type": "note",
                                              "extract": False}}
        self.item = {"id": "slack:C1:1.0", "title": "t", "date": "2026-07-27",
                     "frontmatter": {}}

    def _run_capture(
        self,
        extraction=None,
        current_user_email="me@example.com",
        summary="summary text",
    ):
        calls = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            calls["args"] = args
            calls["stdin"] = stdin_text
            return FakeResult("✓ created 2026-07-27-t\n")

        identity = (
            {"email": None, "person": None, "safe": False}
            if isinstance(current_user_email, Exception)
            else {
                "email": current_user_email,
                "person": {
                    "email": current_user_email,
                    "name": "Memory Owner",
                    "slug": "memory-owner",
                    "resource_name": "people/memory-owner",
                },
                "safe": True,
            }
        )
        identity_patch = mock.patch.object(
            memory_sink, "_authenticated_identity", return_value=identity
        )
        patches = [mock.patch.object(memory_sink, "_cli", side_effect=fake_cli),
                   identity_patch,
                   mock.patch.object(memory_sink.subprocess, "run",
                                     return_value=FakeResult())]
        if extraction is not None:
            self.routine["memory"]["extract"] = True
            patches.append(mock.patch.object(memory_sink, "_extract",
                                             return_value=extraction))
        with mock.patch.object(memory_sink, "log"):
            for p in patches:
                p.start()
            try:
                out = memory_sink.capture(self.routine, self.item, summary)
            finally:
                for p in patches:
                    p.stop()
        return out, calls

    def test_unknown_people_dropped_and_tagged(self):
        out, calls = self._run_capture(
            {"worthy": True, "type": "decision", "title": "T",
             "people": ["jane-doe", "invented-person"], "tags": ["x"], "body": "b"})
        idx = calls["args"].index("--people")
        self.assertEqual(calls["args"][idx + 1], "jane-doe")
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertIn(memory_sink.AUTO_TAG, tags)
        self.assertEqual(out["memory"], "created")

    def test_follows_restricted_to_offered_related_entries(self):
        self.item["frontmatter"]["related_memory_entries"] = [
            {"id": "2026-08-03-provide-bullets", "type": "todo",
             "date": "2026-08-03", "title": "Provide bullets"},
        ]
        out, calls = self._run_capture(
            {"worthy": True, "type": "achievement", "title": "T",
             "people": [], "tags": ["x"],
             "follows": ["2026-08-03-provide-bullets", "invented-entry"],
             "body": "b"})
        idx = calls["args"].index("--follows")
        self.assertEqual(calls["args"][idx + 1], "2026-08-03-provide-bullets")
        self.assertEqual(out["memory"], "created")

    def test_follows_dropped_entirely_without_related_context(self):
        out, calls = self._run_capture(
            {"worthy": True, "type": "note", "title": "T",
             "people": [], "tags": ["x"],
             "follows": ["invented-entry"], "body": "b"})
        self.assertNotIn("--follows", calls["args"])
        self.assertEqual(out["memory"], "created")

    def test_invalid_type_falls_back_to_config(self):
        out, calls = self._run_capture(
            {"worthy": True, "type": "diary", "title": "T", "people": [],
             "tags": [], "body": "b"})
        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")

    def test_third_party_todo_is_downgraded_when_owner_attention_is_false(self):
        outcome, calls = self._run_capture({
            "worthy": True,
            "owner_attention": False,
            "type": "todo",
            "title": "External delivery",
            "people": [],
            "tags": [],
            "body": "Another person owns the delivery.",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn(memory_sink.NO_OWNER_ACTION_TAG, tags)

    def test_slack_todo_without_ownership_evidence_is_downgraded(self):
        # Regression: a channel discussion between two other people about a
        # relevant domain must not become the owner's task even when the
        # extraction model affirms owner attention on topical relevance.
        self.item["frontmatter"]["slack_owner_evidence"] = []
        outcome, calls = self._run_capture({
            "worthy": True,
            "owner_attention": True,
            "type": "todo",
            "title": "Decide OKR share for another team's experiment",
            "people": ["jane-doe"],
            "tags": ["okr"],
            "body": "Two colleagues discussed what to share in the OKR review.",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")
        people = calls["args"][calls["args"].index("--people") + 1]
        self.assertEqual(people, "jane-doe")
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn(memory_sink.NO_OWNER_ACTION_TAG, tags)

    def test_slack_pending_decision_without_evidence_is_downgraded(self):
        self.item["frontmatter"]["slack_owner_evidence"] = []
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": True,
            "type": "pending-decision",
            "title": "Open choice owned elsewhere",
            "people": [],
            "tags": [],
            "body": "Someone else must decide.",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")

    def test_slack_evidence_must_be_real_strings(self):
        self.item["frontmatter"]["slack_owner_evidence"] = [3, "", None]
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": True,
            "type": "todo",
            "title": "Junk evidence",
            "people": [],
            "tags": [],
            "body": "b",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")

    def test_slack_todo_with_ownership_evidence_stays_todo(self):
        self.item["frontmatter"]["slack_owner_evidence"] = ["mentioned"]
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": True,
            "type": "todo",
            "title": "Owner was asked directly",
            "people": [],
            "tags": [],
            "body": "The memory owner was mentioned and asked to decide.",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "todo")
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertNotIn(memory_sink.NO_OWNER_ACTION_TAG, tags)

    def test_slack_evidence_does_not_override_owner_attention_denial(self):
        self.item["frontmatter"]["slack_owner_evidence"] = ["mentioned"]
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": False,
            "type": "todo",
            "title": "Mentioned but not assigned",
            "people": [],
            "tags": [],
            "body": "The owner was mentioned as FYI only.",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")

    def test_slack_note_without_evidence_is_not_tagged(self):
        self.item["frontmatter"]["slack_owner_evidence"] = []
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": False,
            "type": "note",
            "title": "Plain context",
            "people": [],
            "tags": ["context"],
            "body": "b",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertNotIn(memory_sink.NO_OWNER_ACTION_TAG, tags)

    def test_actionable_type_requires_explicit_boolean_owner_attention(self):
        invalid_values = [
            ("missing", None),
            ("null", None),
            ("zero", 0),
            ("one", 1),
            ("true-string", "true"),
            ("false-string", "false"),
            ("list", []),
            ("object", {}),
        ]
        for label, value in invalid_values:
            with self.subTest(owner_attention=label):
                extraction = {
                    "worthy": True,
                    "type": "todo",
                    "title": "Uncertain owner",
                    "people": [],
                    "tags": [],
                    "body": "Ownership was not classified reliably.",
                }
                if label != "missing":
                    extraction["owner_attention"] = value
                _, calls = self._run_capture(extraction)

                idx = calls["args"].index("--type")
                self.assertEqual(calls["args"][idx + 1], "note")

    def test_fyi_marker_downgrades_pending_decision_without_new_field(self):
        _, calls = self._run_capture(
            {
                "worthy": True,
                "type": "pending-decision",
                "title": "External choice",
                "people": [],
                "tags": [],
                "body": "Another person owns the choice.",
            },
            summary=(
                f"{memory_sink.NO_OWNER_ACTION_MARKER}\n"
                "Another person must make the decision."
            ),
        )

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")

    def test_owner_todo_remains_todo_when_owner_attention_is_true(self):
        outcome, calls = self._run_capture({
            "worthy": True,
            "owner_attention": True,
            "type": "todo",
            "title": "Owner follow-up",
            "people": [],
            "tags": [],
            "body": "The memory owner must follow up.",
        })

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "todo")

    def test_quoted_fyi_marker_does_not_downgrade_owner_todo(self):
        _, calls = self._run_capture(
            {
                "worthy": True,
                "owner_attention": True,
                "type": "todo",
                "title": "Owner follow-up",
                "people": [],
                "tags": [],
                "body": "The memory owner must follow up.",
            },
            summary=(
                f'The policy text says "{memory_sink.NO_OWNER_ACTION_MARKER}" '
                "but the memory owner explicitly accepted this follow-up."
            ),
        )

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "todo")

    def test_extract_false_configured_todo_does_not_require_new_field(self):
        self.routine["memory"]["type"] = "todo"
        _, calls = self._run_capture()

        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "todo")

    def test_active_self_forwarded_chat_is_always_a_distinct_todo(self):
        self.item["frontmatter"].update({
            "gmail_thread_id": "thread-1",
            "gmail_manual_chat_followup": True,
            "gmail_chat_followup_managed": True,
            "gmail_chat_followup_active": True,
            "gmail_followup_predecessor_entry_id": "original-entry",
        })
        outcome, calls = self._run_capture({
            "worthy": False,
            "owner_attention": False,
            "type": "note",
            "title": "Follow up on the discussion",
            "people": [],
            "tags": [],
            "body": "The owner intentionally queued this discussion.",
        })

        args = calls["args"]
        self.assertEqual(args[args.index("--type") + 1], "todo")
        self.assertEqual(
            args[args.index("--source-ids") + 1],
            "gmail:thread-1:followup-open",
        )
        self.assertEqual(
            args[args.index("--follows") + 1],
            "original-entry",
        )
        self.assertIn("--force-new", args)
        tags = args[args.index("--tags") + 1]
        self.assertIn("gmail-followup", tags)
        self.assertIn(
            "manually forwarded this Chat conversation",
            calls["stdin"],
        )
        self.assertEqual(
            outcome["memory_source_id"],
            "gmail:thread-1:followup-open",
        )

    def test_active_chat_followup_survives_source_not_worthy_sentinel(self):
        self.item["frontmatter"].update({
            "gmail_thread_id": "thread-1",
            "gmail_manual_chat_followup": True,
            "gmail_chat_followup_managed": True,
            "gmail_chat_followup_active": True,
        })
        _, calls = self._run_capture(
            {
                "worthy": False,
                "owner_attention": False,
                "type": "note",
                "title": "Follow up on the queued Chat message",
                "people": [],
                "tags": [],
                "body": "Follow up on the source-linked conversation.",
            },
            summary="NOT MEMORY-WORTHY",
        )

        args = calls["args"]
        self.assertEqual(args[args.index("--type") + 1], "todo")
        self.assertIn("--force-new", args)

    def test_archived_self_forward_is_not_forced_to_todo(self):
        self.item["frontmatter"].update({
            "gmail_thread_id": "thread-1",
            "gmail_manual_chat_followup": True,
            "gmail_chat_followup_managed": True,
            "gmail_chat_followup_active": False,
        })
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": False,
            "type": "note",
            "title": "Discussion context",
            "people": [],
            "tags": [],
            "body": "The discussion remains useful context.",
        })

        args = calls["args"]
        self.assertEqual(args[args.index("--type") + 1], "note")
        self.assertNotIn("--force-new", args)

    def test_unmanaged_self_forward_is_not_forced_to_todo(self):
        self.item["frontmatter"].update({
            "gmail_thread_id": "thread-1",
            "gmail_manual_chat_followup": True,
            "gmail_chat_followup_managed": False,
            "gmail_chat_followup_active": True,
        })
        _, calls = self._run_capture({
            "worthy": True,
            "owner_attention": False,
            "type": "note",
            "title": "Discussion context",
            "people": [],
            "tags": [],
            "body": "The discussion remains useful context.",
        })

        args = calls["args"]
        self.assertEqual(args[args.index("--type") + 1], "note")
        self.assertNotIn("--force-new", args)

    def test_active_followup_requires_store_entry_id(self):
        self.item["frontmatter"].update({
            "gmail_thread_id": "thread-1",
            "gmail_manual_chat_followup": True,
            "gmail_chat_followup_managed": True,
            "gmail_chat_followup_active": True,
        })
        with mock.patch.object(
            memory_sink, "_cli", return_value=FakeResult("capture completed\n")
        ), mock.patch.object(memory_sink, "_commit_store") as commit:
            with self.assertRaisesRegex(RuntimeError, "returned no entry id"):
                memory_sink.capture(self.routine, self.item, "summary text")

        commit.assert_called_once_with("/store", "memory: r auto-capture")

    def test_nonzero_add_is_accepted_only_after_exact_disk_verification(self):
        with mock.patch.object(
            memory_sink, "_cli", return_value=FakeResult("late failure", returncode=2)
        ), mock.patch.object(memory_sink, "_commit_store") as commit, \
             mock.patch.object(
                 memory_sink, "_verify_written_entry",
                 return_value="2026-08-02-verified-entry",
             ) as verify:
            outcome = memory_sink.capture(
                self.routine, self.item, "summary text"
            )

        self.assertEqual(outcome["memory"], "verified")
        self.assertEqual(
            outcome["memory_entry_id"], "2026-08-02-verified-entry"
        )
        commit.assert_called_once_with("/store", "memory: r auto-capture")
        verify.assert_called_once()

    def test_verified_drive_owner_can_mint_new_person_slug(self):
        self.item["frontmatter"]["drive_owner_emails"] = ["owner@example.com"]
        verified = {
            "email": "owner@example.com",
            "name": "Owner Example",
            "slug": "owner-example",
            "resource_name": "people/owner-example",
        }
        with mock.patch.object(memory_sink.contacts, "resolve_email",
                               return_value=verified):
            out, calls = self._run_capture()

        idx = calls["args"].index("--people")
        self.assertEqual(calls["args"][idx + 1], "owner-example")
        self.assertEqual(out["memory_people"], ["owner-example"])

    def test_verified_source_participant_can_mint_new_person_slug_when_selected(self):
        self.item["frontmatter"]["source_people"] = [{
            "email": "new.person@example.com",
            "name": "Untrusted Header Name",
            "role": "from",
        }]
        verified = {
            "email": "new.person@example.com",
            "name": "Directory Person",
            "slug": "directory-person",
            "resource_name": "people/directory-person",
        }
        with mock.patch.object(
            memory_sink.contacts, "resolve_email", return_value=verified
        ):
            out, calls = self._run_capture({
                "worthy": True,
                "type": "decision",
                "title": "T",
                "people": ["directory-person"],
                "tags": [],
                "body": "b",
            })

        idx = calls["args"].index("--people")
        self.assertEqual(calls["args"][idx + 1], "directory-person")
        self.assertEqual(out["memory_people"], ["directory-person"])

    def test_verified_source_participant_is_candidate_not_automatic_link(self):
        self.item["frontmatter"]["source_people"] = [{
            "email": "observer@example.com",
            "name": "Observer",
        }]
        verified = {
            "email": "observer@example.com",
            "name": "Observer",
            "slug": "observer",
            "resource_name": "people/observer",
        }
        with mock.patch.object(
            memory_sink.contacts, "resolve_email", return_value=verified
        ):
            out, calls = self._run_capture({
                "worthy": True,
                "type": "decision",
                "title": "T",
                "people": [],
                "tags": [],
                "body": "b",
            })

        self.assertNotIn("--people", calls["args"])
        self.assertEqual(out["memory_people"], [])

    def test_authenticated_user_is_not_a_source_person_candidate(self):
        self.item["frontmatter"]["source_people"] = [
            {"email": "me@example.com", "name": "Me"},
            {"email": "other@example.com", "name": "Other"},
        ]
        verified = {
            "email": "other@example.com",
            "name": "Other",
            "slug": "other",
            "resource_name": "people/other",
        }
        with mock.patch.object(
            memory_sink.contacts, "resolve_email", return_value=verified
        ) as resolve, mock.patch.object(
            memory_sink.drive, "current_user_email", return_value="me@example.com"
        ):
            people, unresolved = memory_sink._verified_source_people(
                self.item,
                "r",
                {
                    "email": "me@example.com",
                    "person": {
                        "email": "me@example.com",
                        "name": "Me",
                        "slug": "me",
                        "resource_name": "people/me",
                    },
                    "safe": True,
                },
            )

        resolve.assert_called_once_with("other@example.com")
        self.assertEqual(people, [verified])
        self.assertEqual(unresolved, [])

    def test_existing_authenticated_slug_is_removed_from_final_people(self):
        memory_sink._slug_cache["/store"].add("memory-owner")
        out, calls = self._run_capture({
            "worthy": True,
            "type": "decision",
            "title": "T",
            "people": ["memory-owner"],
            "tags": [],
            "body": "b",
        })

        self.assertNotIn("--people", calls["args"])
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory_people"], [])

    def test_source_alias_resolving_to_authenticated_person_is_excluded(self):
        self.item["frontmatter"]["source_people"] = [{
            "email": "owner-alias@example.com",
            "name": "Memory Owner Alias",
        }]
        same_person = {
            "email": "owner-alias@example.com",
            "name": "Memory Owner",
            "slug": "memory-owner",
            "resource_name": "people/memory-owner",
        }
        with mock.patch.object(
            memory_sink.contacts, "resolve_email", return_value=same_person
        ) as resolve:
            out, calls = self._run_capture({
                "worthy": True,
                "type": "decision",
                "title": "T",
                "people": ["memory-owner"],
                "tags": [],
                "body": "b",
            })

        resolve.assert_called_once_with("owner-alias@example.com")
        self.assertNotIn("--people", calls["args"])
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory_people"], [])

    def test_same_slug_different_directory_person_is_unresolved(self):
        self.item["frontmatter"]["source_people"] = [{
            "email": "different.owner@example.com",
            "name": "Same Display Name",
        }]
        different_person = {
            "email": "different.owner@example.com",
            "name": "Memory Owner",
            "slug": "memory-owner",
            "resource_name": "people/different-person",
        }
        with mock.patch.object(
            memory_sink.contacts, "resolve_email", return_value=different_person
        ):
            out, calls = self._run_capture({
                "worthy": True,
                "type": "decision",
                "title": "T",
                "people": ["memory-owner"],
                "tags": [],
                "body": "b",
            })

        self.assertNotIn("--people", calls["args"])
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory_people"], [])

    def test_extract_false_skips_source_participant_directory_lookups(self):
        self.item["frontmatter"]["source_people"] = [{
            "email": "observer@example.com",
            "name": "Observer",
        }]
        with mock.patch.object(memory_sink.contacts, "resolve_email") as resolve:
            out, calls = self._run_capture()

        resolve.assert_not_called()
        self.assertNotIn("--people", calls["args"])
        self.assertEqual(out["memory_people"], [])

    def test_source_participant_resolution_is_defensively_capped(self):
        self.item["frontmatter"]["source_people"] = [
            {"email": f"person{index}@example.com", "name": f"Person {index}"}
            for index in range(memory_sink.MAX_SOURCE_PEOPLE + 5)
        ]

        def resolve(email):
            local = email.split("@", 1)[0]
            return {
                "email": email,
                "name": local,
                "slug": local,
                "resource_name": f"people/{local}",
            }

        with mock.patch.object(
            memory_sink.contacts, "resolve_email", side_effect=resolve
        ) as directory:
            out, calls = self._run_capture({
                "worthy": True,
                "type": "decision",
                "title": "T",
                "people": [],
                "tags": [],
                "body": "b",
            })

        self.assertEqual(directory.call_count, memory_sink.MAX_SOURCE_PEOPLE)
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory"], "created")

    def test_unresolved_source_participant_is_tagged_for_repair(self):
        self.item["frontmatter"]["source_people"] = [{
            "email": "unknown@example.com",
            "name": "Unknown",
        }]
        with mock.patch.object(
            memory_sink.contacts, "resolve_email", return_value=None
        ):
            out, calls = self._run_capture({
                "worthy": True,
                "type": "decision",
                "title": "T",
                "people": [],
                "tags": [],
                "body": "b",
            })

        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory"], "created")

    def test_authenticated_drive_owner_is_not_added_as_a_person(self):
        self.item["frontmatter"]["drive_owner_emails"] = [
            "ME@EXAMPLE.COM",
            "owner@example.com",
        ]
        verified = {
            "email": "owner@example.com",
            "name": "Owner Example",
            "slug": "owner-example",
            "resource_name": "people/owner-example",
        }
        with mock.patch.object(
            memory_sink.contacts,
            "resolve_email",
            return_value=verified,
        ) as resolve:
            out, calls = self._run_capture()

        resolve.assert_called_once_with("owner@example.com")
        idx = calls["args"].index("--people")
        self.assertEqual(calls["args"][idx + 1], "owner-example")
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertNotIn("people-unmapped", tags)
        self.assertEqual(out["memory_people"], ["owner-example"])

    def test_identity_failure_skips_owner_enrichment_and_marks_for_repair(self):
        self.item["frontmatter"]["drive_owner_emails"] = ["owner@example.com"]
        with mock.patch.object(memory_sink.contacts, "resolve_email") as resolve:
            out, calls = self._run_capture(
                current_user_email=RuntimeError("Drive unavailable"),
            )

        resolve.assert_not_called()
        self.assertNotIn("--people", calls["args"])
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory"], "created")

    def test_unresolved_drive_owner_is_tagged_without_failing_capture(self):
        self.item["frontmatter"]["drive_owner_emails"] = ["owner@example.com"]
        with mock.patch.object(memory_sink.contacts, "resolve_email",
                               return_value=None):
            out, calls = self._run_capture()

        self.assertNotIn("--people", calls["args"])
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory"], "created")

    def test_directory_failure_is_tagged_without_failing_capture(self):
        self.item["frontmatter"]["drive_owner_emails"] = ["owner@example.com"]
        with mock.patch.object(
            memory_sink.contacts,
            "resolve_email",
            side_effect=RuntimeError("directory unavailable"),
        ):
            out, calls = self._run_capture()

        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("people-unmapped", tags)
        self.assertEqual(out["memory"], "created")

    def test_directory_failure_shells_out_once_across_captures(self):
        self.item["frontmatter"]["drive_owner_emails"] = ["owner@example.com"]
        with mock.patch.object(
            memory_sink.contacts,
            "run_json",
            side_effect=RuntimeError("directory unavailable"),
        ) as directory, mock.patch.object(
            memory_sink.contacts,
            "gws_bin",
            return_value="/bin/gws",
        ):
            first, _ = self._run_capture()
            second, _ = self._run_capture()

        self.assertEqual(first["memory"], "created")
        self.assertEqual(second["memory"], "created")
        self.assertEqual(directory.call_count, 1)

    def test_not_worthy_skips_store(self):
        out, calls = self._run_capture(
            {"worthy": False, "type": "note", "title": "T", "people": [],
             "tags": [], "body": "b"})
        self.assertEqual(out, {"memory": "skipped_not_worthy"})
        self.assertEqual(calls, {})

    def test_source_not_worthy_sentinel_skips_extraction_and_store(self):
        with mock.patch.object(memory_sink, "_extract") as extract, \
             mock.patch.object(memory_sink, "_cli") as cli, \
             mock.patch.object(memory_sink.contacts, "resolve_email") as resolve, \
             mock.patch.object(memory_sink, "log"):
            out = memory_sink.capture(
                self.routine,
                self.item,
                "  NOT MEMORY-WORTHY\n",
            )

        self.assertEqual(out, {"memory": "skipped_not_worthy"})
        extract.assert_not_called()
        cli.assert_not_called()
        resolve.assert_not_called()

    def test_operator_confirmation_overrides_both_worthiness_vetoes(self):
        self.routine["memory"]["operator_confirmed_source_ids"] = [
            "slack:C1:1.0"
        ]
        with mock.patch.object(
            memory_sink,
            "_operator_confirmed_summary",
            return_value="Durable product capability context.",
        ) as summarize:
            out, calls = self._run_capture(
                {
                    "worthy": False,
                    "owner_attention": False,
                    "type": "note",
                    "title": "Capability context",
                    "people": [],
                    "tags": ["product"],
                    "body": "The capability changed in a durable way.",
                },
                summary="NOT MEMORY-WORTHY",
            )

        summarize.assert_called_once_with(self.routine, self.item)
        self.assertEqual(out["memory"], "created")
        self.assertEqual(
            calls["stdin"], "The capability changed in a durable way."
        )
        tags = calls["args"][calls["args"].index("--tags") + 1]
        self.assertIn("operator-confirmed", tags.split(","))

    def test_operator_confirmation_is_exact_source_id_only(self):
        self.routine["memory"]["operator_confirmed_source_ids"] = [
            "slack:C1:different"
        ]
        with mock.patch.object(memory_sink, "_operator_confirmed_summary") as summarize:
            out, calls = self._run_capture(summary="NOT MEMORY-WORTHY")

        self.assertEqual(out, {"memory": "skipped_not_worthy"})
        self.assertEqual(calls, {})
        summarize.assert_not_called()

    def test_operator_confirmed_summary_keeps_normal_source_metadata(self):
        self.item.update({
            "source_kind": "gmail",
            "body": "The product behavior changed.",
        })
        self.item["frontmatter"].update({
            "email_from": "Product Owner <owner@example.com>",
            "email_to": "Memory Owner <memory@example.com>",
            "gmail_thread_message_count": 9,
            "gmail_thread_messages_included": 5,
            "gmail_thread_truncated": True,
        })
        with mock.patch(
            "workspace_daemon.llm.analyze",
            return_value="Durable product context.",
        ) as analyze:
            summary = memory_sink._operator_confirmed_summary(
                self.routine, self.item
            )

        self.assertEqual(summary, "Durable product context.")
        prompt = analyze.call_args.args[1]
        self.assertIn("From: Product Owner <owner@example.com>", prompt)
        self.assertIn("To: Memory Owner <memory@example.com>", prompt)
        self.assertIn("Messages in supplied thread: 5 of 9", prompt)
        self.assertIn("Coverage warning:", prompt)

    def test_dry_run_makes_no_calls(self):
        with mock.patch.object(memory_sink, "_cli") as m, \
             mock.patch.object(memory_sink, "_extract") as e, \
             mock.patch.object(memory_sink, "log"):
            out = memory_sink.capture(self.routine, self.item, "s", dry_run=True)
        self.assertEqual(out, {"memory": "dry_run"})
        m.assert_not_called()
        e.assert_not_called()

    def test_operator_confirmation_dry_run_makes_no_calls(self):
        self.routine["memory"]["operator_confirmed_source_ids"] = [
            "slack:C1:1.0"
        ]
        with mock.patch.object(
            memory_sink, "_operator_confirmed_summary"
        ) as summarize, mock.patch.object(
            memory_sink, "_extract"
        ) as extract, mock.patch.object(
            memory_sink, "_cli"
        ) as cli, mock.patch(
            "workspace_daemon.llm.analyze"
        ) as analyze, mock.patch.object(memory_sink, "log"):
            out = memory_sink.capture(
                self.routine,
                self.item,
                "NOT MEMORY-WORTHY",
                dry_run=True,
            )

        self.assertEqual(out, {"memory": "dry_run"})
        summarize.assert_not_called()
        extract.assert_not_called()
        cli.assert_not_called()
        analyze.assert_not_called()

    def test_active_followup_dry_run_reports_forced_todo(self):
        self.item["frontmatter"].update({
            "gmail_thread_id": "thread-1",
            "gmail_manual_chat_followup": True,
            "gmail_chat_followup_managed": True,
            "gmail_chat_followup_active": True,
        })
        with mock.patch.object(memory_sink, "_cli") as cli, \
             mock.patch.object(memory_sink, "log") as log:
            out = memory_sink.capture(
                self.routine, self.item, "summary", dry_run=True
            )

        self.assertEqual(out, {"memory": "dry_run"})
        cli.assert_not_called()
        self.assertIn("type=todo", log.call_args.args[0])
        self.assertIn("active Chat follow-up", log.call_args.args[0])


class FollowupResolutionTest(unittest.TestCase):
    def test_resolution_creates_idempotent_timeline_successor(self):
        calls = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            calls.update({"store": store, "args": args, "body": stdin_text})
            return FakeResult("✓ created 2026-08-01-completed-follow-up\n")

        routine = {
            "id": "gmail-sweep",
            "memory": {"store": "/store", "type": "note"},
        }
        with mock.patch.object(memory_sink, "_cli", side_effect=fake_cli), \
             mock.patch.object(
                 memory_sink.subprocess, "run", return_value=FakeResult()
             ):
            outcome = memory_sink.resolve_followup(
                routine,
                memory_entry_id="2026-07-31-open-follow-up",
                thread_id="thread-1",
                title="Fwd: Chat with a colleague",
                completed_on="2026-08-01",
            )

        args = calls["args"]
        self.assertEqual(args[args.index("--type") + 1], "note")
        self.assertEqual(
            args[args.index("--source-ids") + 1],
            "gmail:thread-1:followup-completed",
        )
        self.assertEqual(
            args[args.index("--follows") + 1],
            "2026-07-31-open-follow-up",
        )
        self.assertIn("--force-new", args)
        self.assertIn("no longer in the Gmail Inbox", calls["body"])
        self.assertEqual(
            outcome["memory_entry_id"],
            "2026-08-01-completed-follow-up",
        )

    def test_resolution_requires_store_entry_id(self):
        routine = {
            "id": "gmail-sweep",
            "memory": {"store": "/store", "type": "note"},
        }
        with mock.patch.object(
            memory_sink, "_cli", return_value=FakeResult("completed\n")
        ), mock.patch.object(memory_sink.subprocess, "run") as commit:
            with self.assertRaisesRegex(RuntimeError, "returned no entry id"):
                memory_sink.resolve_followup(
                    routine,
                    memory_entry_id="2026-07-31-open-follow-up",
                    thread_id="thread-1",
                    title="Fwd: Chat with a colleague",
                    completed_on="2026-08-01",
                )

        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
