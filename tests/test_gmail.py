"""Gmail source adapter tests."""
import unittest
from unittest import mock

from workspace_daemon import gmail


class SearchTest(unittest.TestCase):
    def test_zero_max_requests_all_pages(self):
        with mock.patch.object(
            gmail, "run_json", return_value={"threads": []}
        ) as run_json, mock.patch.object(
            gmail, "gws_bin", return_value="gws"
        ):
            gmail.search("is:unread", 0)

        run_json.assert_called_once_with([
            "gws", "gmail", "list", "--query", "is:unread",
            "--all", "--format", "json",
        ])

    def test_positive_max_remains_bounded(self):
        with mock.patch.object(
            gmail, "run_json", return_value={"threads": []}
        ) as run_json, mock.patch.object(
            gmail, "gws_bin", return_value="gws"
        ):
            gmail.search("in:inbox", 25)

        run_json.assert_called_once_with([
            "gws", "gmail", "list", "--query", "in:inbox",
            "--max", "25", "--format", "json",
        ])


if __name__ == "__main__":
    unittest.main()
