"""Direct Gmail-to-Docs expansion and tolerant Gemini tab matching."""
import unittest
from unittest import mock

from workspace_daemon import drive, gmail, runner


class DriveTabMatchingTest(unittest.TestCase):
    def test_requested_tab_matches_case_insensitively(self):
        document = {
            "tabs": [
                {"title": "Quick Notes"},
                {"title": "Full Notes"},
            ]
        }
        with mock.patch.object(
            drive, "read_tab", return_value="Durable meeting content"
        ) as read:
            body, selected = drive.read_tabs(
                "doc-1",
                ["Full notes", "Transcript"],
                document=document,
            )

        self.assertEqual(selected, ["Full Notes"])
        self.assertIn("### Full Notes", body)
        read.assert_called_once_with("doc-1", "Full Notes")


class GmailLinkedDocumentTest(unittest.TestCase):
    EXPAND = {
        "kind": "drive_doc",
        "title_from_subject": r"Notes: '(?P<title>.+)'",
        "name_contains": "Notes by Gemini",
        "tabs": ["Full notes", "Transcript"],
        "on_missing": "body",
    }

    def item(self):
        return {
            "id": "message-1",
            "date": "2026-07-08",
            "body": "email stub",
            "frontmatter": {},
        }

    def test_direct_email_link_beats_title_search(self):
        links = [{
            "google_docs_id": "linked-doc",
            "href": "https://docs.google.test/document/d/linked-doc/edit",
            "text": "Open meeting notes",
        }]
        document = {
            "title": "Sales QBR – 2026/07/07 – Notes by Gemini",
            "tabs": [{"title": "Full Notes"}],
        }
        item = self.item()

        with mock.patch.object(gmail, "links", return_value=links), \
             mock.patch.object(drive, "find_doc") as find_doc, \
             mock.patch.object(drive, "info", return_value=document), \
             mock.patch.object(
                 drive,
                 "file_info",
                 return_value={"owners": ["OWNER@EXAMPLE.COM"]},
             ), \
             mock.patch.object(
                 drive,
                 "read_tabs",
                 return_value=("### Full Notes\n\nDecision", ["Full Notes"]),
             ):
            runner._expand_from_drive(
                self.EXPAND,
                item,
                "Notes: 'Sales QBR Q2' 7 Jul 2026",
                "2026-07-07",
            )

        find_doc.assert_not_called()
        self.assertEqual(item["frontmatter"]["drive_file_id"], "linked-doc")
        self.assertEqual(item["frontmatter"]["doc_lookup"], "gmail-link")
        self.assertEqual(item["frontmatter"]["doc_tabs"], ["Full Notes"])
        self.assertEqual(item["frontmatter"]["meeting_date"], "2026-07-07")
        self.assertEqual(
            item["frontmatter"]["drive_owner_emails"],
            ["owner@example.com"],
        )
        self.assertEqual(item["date"], "2026-07-07")
        self.assertEqual(item["body"], "### Full Notes\n\nDecision")

    def test_missing_email_link_keeps_title_date_search_fallback(self):
        found = {
            "id": "searched-doc",
            "name": "Weekly Sync – 2026/07/01 – Notes by Gemini",
            "web_link": "https://docs.google.test/document/d/searched-doc/edit",
        }
        document = {
            "title": found["name"],
            "tabs": [{"title": "Full notes"}],
        }
        item = self.item()

        with mock.patch.object(gmail, "links", return_value=[]), \
             mock.patch.object(drive, "find_doc", return_value=found) as find_doc, \
             mock.patch.object(drive, "info", return_value=document), \
             mock.patch.object(drive, "file_info", return_value={"owners": []}), \
             mock.patch.object(
                 drive,
                 "read_tabs",
                 return_value=("### Full notes\n\nDecision", ["Full notes"]),
             ):
            runner._expand_from_drive(
                self.EXPAND,
                item,
                "Notes: 'Weekly Sync' 1 Jul 2026",
                "2026-07-01",
            )

        find_doc.assert_called_once_with(
            "Weekly Sync",
            name_contains="Notes by Gemini",
            on_date="2026-07-01",
        )
        self.assertEqual(item["frontmatter"]["doc_lookup"], "title-date-search")

    def test_owner_lookup_failure_does_not_lose_expanded_note(self):
        links = [{
            "google_docs_id": "linked-doc",
            "href": "https://docs.google.test/document/d/linked-doc/edit",
            "text": "Open meeting notes",
        }]
        document = {
            "title": "Weekly Sync – 2026/07/01 – Notes by Gemini",
            "tabs": [{"title": "Full Notes"}],
        }
        item = self.item()

        with mock.patch.object(gmail, "links", return_value=links), \
             mock.patch.object(drive, "info", return_value=document), \
             mock.patch.object(drive, "file_info", side_effect=RuntimeError("denied")), \
             mock.patch.object(
                 drive,
                 "read_tabs",
                 return_value=("### Full Notes\n\nDecision", ["Full Notes"]),
             ), \
             mock.patch.object(runner, "log") as log:
            runner._expand_from_drive(
                self.EXPAND,
                item,
                "Notes: 'Weekly Sync' 1 Jul 2026",
                "2026-07-01",
            )

        self.assertEqual(item["body"], "### Full Notes\n\nDecision")
        self.assertNotIn("drive_owner_emails", item["frontmatter"])
        self.assertTrue(any("owner lookup failed" in call.args[0] for call in log.call_args_list))


if __name__ == "__main__":
    unittest.main()
