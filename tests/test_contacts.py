"""Exact Google Workspace directory identity resolution."""
import unittest
from unittest import mock

from workspace_daemon import contacts


class DirectoryResolutionTest(unittest.TestCase):
    def setUp(self):
        contacts.clear_cache()

    def test_accepts_one_exact_email_match_and_slugs_name(self):
        response = {
            "contacts": [
                {
                    "name": "José Example",
                    "emails": ["JOSE.EXAMPLE@EXAMPLE.COM"],
                }
            ]
        }
        with mock.patch.object(contacts, "gws_bin", return_value="/bin/gws"), \
             mock.patch.object(contacts, "run_json", return_value=response) as run:
            person = contacts.resolve_email("jose.example@example.com")

        self.assertEqual(person, {
            "email": "jose.example@example.com",
            "name": "José Example",
            "slug": "jose-example",
        })
        run.assert_called_once_with([
            "/bin/gws", "contacts", "directory-search",
            "--query", "jose.example@example.com",
            "--max", "10", "--format", "json",
        ], timeout=contacts.DIRECTORY_TIMEOUT_SECONDS)

    def test_rejects_fuzzy_result_without_exact_email(self):
        response = {
            "contacts": [
                {"name": "Different Person", "emails": ["different@example.com"]}
            ]
        }
        with mock.patch.object(contacts, "run_json", return_value=response), \
             mock.patch.object(contacts, "gws_bin", return_value="/bin/gws"):
            self.assertIsNone(contacts.resolve_email("person@example.com"))

    def test_rejects_ambiguous_exact_email(self):
        response = {
            "contacts": [
                {"name": "First", "emails": ["shared@example.com"]},
                {"name": "Second", "emails": ["shared@example.com"]},
            ]
        }
        with mock.patch.object(contacts, "run_json", return_value=response), \
             mock.patch.object(contacts, "gws_bin", return_value="/bin/gws"):
            self.assertIsNone(contacts.resolve_email("shared@example.com"))

    def test_invalid_email_never_calls_directory(self):
        with mock.patch.object(contacts, "run_json") as run:
            self.assertIsNone(contacts.resolve_email("not-an-email"))
        run.assert_not_called()

    def test_cached_by_normalized_input(self):
        response = {
            "contacts": [
                {"name": "Cache Example", "emails": ["cache@example.com"]}
            ]
        }
        with mock.patch.object(contacts, "run_json", return_value=response) as run, \
             mock.patch.object(contacts, "gws_bin", return_value="/bin/gws"):
            contacts.resolve_email(" CACHE@EXAMPLE.COM ")
            contacts.resolve_email("cache@example.com")
        self.assertEqual(run.call_count, 1)

    def test_failure_is_cached_for_the_run(self):
        with mock.patch.object(
            contacts,
            "run_json",
            side_effect=RuntimeError("directory unavailable"),
        ) as run, mock.patch.object(contacts, "gws_bin", return_value="/bin/gws"):
            with self.assertRaisesRegex(RuntimeError, "directory unavailable"):
                contacts.resolve_email("owner@example.com")
            with self.assertRaisesRegex(RuntimeError, "directory unavailable"):
                contacts.resolve_email("OWNER@EXAMPLE.COM")

        self.assertEqual(run.call_count, 1)

    def test_clear_cache_allows_retry_on_next_run(self):
        response = {
            "contacts": [
                {"name": "Owner Example", "emails": ["owner@example.com"]}
            ]
        }
        with mock.patch.object(
            contacts,
            "run_json",
            side_effect=[RuntimeError("temporary"), response],
        ) as run, mock.patch.object(contacts, "gws_bin", return_value="/bin/gws"):
            with self.assertRaisesRegex(RuntimeError, "temporary"):
                contacts.resolve_email("owner@example.com")
            contacts.clear_cache()
            self.assertEqual(
                contacts.resolve_email("owner@example.com")["slug"],
                "owner-example",
            )

        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
