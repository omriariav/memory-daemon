"""memory_sink: slug-catalog parsing, model-output validation, source-id derivation."""
import unittest
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


class SourceIdTest(unittest.TestCase):
    def test_slack_item_id_passes_through(self):
        item = {"id": "slack:C0123:1700000000.000100", "frontmatter": {}}
        self.assertEqual(memory_sink.source_id_for(item), "slack:C0123:1700000000.000100")

    def test_gmail_thread_id(self):
        item = {"id": "m1", "frontmatter": {"gmail_thread_id": "t9"}}
        self.assertEqual(memory_sink.source_id_for(item), "gmail:t9")

    def test_drive_file_id(self):
        item = {"id": "d1", "frontmatter": {"drive_file_id": "f7"}}
        self.assertEqual(memory_sink.source_id_for(item), "gdrive:f7")

    def test_no_provenance_returns_none(self):
        self.assertIsNone(memory_sink.source_id_for({"id": "x", "frontmatter": {}}))


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

    def test_no_memory_block_is_fine(self):
        self.assertEqual(memory_sink.validate({"id": "r"}), [])


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

    def _run_capture(self, extraction=None, current_user_email="me@example.com"):
        calls = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            calls["args"] = args
            calls["stdin"] = stdin_text
            return FakeResult("✓ created 2026-07-27-t\n")

        identity_patch = (
            mock.patch.object(
                memory_sink.drive,
                "current_user_email",
                side_effect=current_user_email,
            )
            if isinstance(current_user_email, Exception)
            else mock.patch.object(
                memory_sink.drive,
                "current_user_email",
                return_value=current_user_email,
            )
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
                out = memory_sink.capture(self.routine, self.item, "summary text")
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

    def test_invalid_type_falls_back_to_config(self):
        out, calls = self._run_capture(
            {"worthy": True, "type": "diary", "title": "T", "people": [],
             "tags": [], "body": "b"})
        idx = calls["args"].index("--type")
        self.assertEqual(calls["args"][idx + 1], "note")

    def test_verified_drive_owner_can_mint_new_person_slug(self):
        self.item["frontmatter"]["drive_owner_emails"] = ["owner@example.com"]
        verified = {
            "email": "owner@example.com",
            "name": "Owner Example",
            "slug": "owner-example",
        }
        with mock.patch.object(memory_sink.contacts, "resolve_email",
                               return_value=verified):
            out, calls = self._run_capture()

        idx = calls["args"].index("--people")
        self.assertEqual(calls["args"][idx + 1], "owner-example")
        self.assertEqual(out["memory_people"], ["owner-example"])

    def test_authenticated_drive_owner_is_not_added_as_a_person(self):
        self.item["frontmatter"]["drive_owner_emails"] = [
            "ME@EXAMPLE.COM",
            "owner@example.com",
        ]
        verified = {
            "email": "owner@example.com",
            "name": "Owner Example",
            "slug": "owner-example",
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

    def test_dry_run_makes_no_calls(self):
        with mock.patch.object(memory_sink, "_cli") as m, \
             mock.patch.object(memory_sink, "_extract") as e, \
             mock.patch.object(memory_sink, "log"):
            out = memory_sink.capture(self.routine, self.item, "s", dry_run=True)
        self.assertEqual(out, {"memory": "dry_run"})
        m.assert_not_called()
        e.assert_not_called()


if __name__ == "__main__":
    unittest.main()
