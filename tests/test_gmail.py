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
        ], timeout=600)

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
        ], timeout=120)

    def test_explicit_timeout_overrides_both_defaults(self):
        for limit in (0, 25):
            with mock.patch.object(
                gmail, "run_json", return_value={"threads": []}
            ) as run_json, mock.patch.object(
                gmail, "gws_bin", return_value="gws"
            ):
                gmail.search("in:inbox", limit, timeout=900)
            self.assertEqual(run_json.call_args.kwargs, {"timeout": 900})

    def test_listing_timeout_still_raises(self):
        import subprocess
        with mock.patch.object(
            gmail, "run_json",
            side_effect=subprocess.TimeoutExpired(["gws"], 1),
        ), mock.patch.object(gmail, "gws_bin", return_value="gws"):
            with self.assertRaises(subprocess.TimeoutExpired):
                gmail.search("in:inbox", 0)

    def test_read_thread_uses_thread_endpoint(self):
        with mock.patch.object(
            gmail, "run_json", return_value={"messages": []}
        ) as run_json, mock.patch.object(
            gmail, "gws_bin", return_value="gws"
        ):
            gmail.read_thread("thread-1")

        run_json.assert_called_once_with([
            "gws", "gmail", "thread", "thread-1", "--format", "json",
        ])


if __name__ == "__main__":
    unittest.main()
