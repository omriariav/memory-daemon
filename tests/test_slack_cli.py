"""Built-in Slack CLI configuration and DM-discovery behavior."""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from workspace_daemon import slack_cli, slack_source


class ConfigTest(unittest.TestCase):
    def test_config_override_supplies_user_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slack.json"
            path.write_text(json.dumps({"user_token": "xoxp-test"}))
            with mock.patch.dict(
                os.environ,
                {"MEMORY_DAEMON_SLACK_CONFIG": str(path)},
                clear=False,
            ):
                self.assertEqual(slack_cli.token(), "xoxp-test")

    def test_mentions_identity_can_come_from_private_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slack.json"
            path.write_text(json.dumps({
                "user_token": "xoxp-test",
                "mention_user": "person@example.com",
            }))
            with mock.patch.dict(
                os.environ,
                {
                    "MEMORY_DAEMON_SLACK_CONFIG": str(path),
                    "MEMORY_DAEMON_SLACK_MENTION_USER": "",
                },
                clear=False,
            ), mock.patch.object(slack_cli, "slack") as api:
                self.assertEqual(
                    slack_cli.mention_user(),
                    "person@example.com",
                )
                api.assert_not_called()

    def test_mentions_identity_is_resolved_for_authenticated_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "slack.json"
            path.write_text(json.dumps({"user_token": "xoxp-test"}))
            responses = [
                {"ok": True, "user_id": "U123"},
                {
                    "ok": True,
                    "user": {"profile": {"email": "person@example.com"}},
                },
            ]
            with mock.patch.dict(
                os.environ,
                {
                    "MEMORY_DAEMON_SLACK_CONFIG": str(path),
                    "MEMORY_DAEMON_SLACK_MENTION_USER": "",
                },
                clear=False,
            ), mock.patch.object(slack_cli, "slack", side_effect=responses):
                self.assertEqual(
                    slack_cli.mention_user(),
                    "person@example.com",
                )

    def test_default_config_is_outside_repository(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(Path, "home", return_value=Path("/Users/test")):
            self.assertEqual(
                slack_cli.config_path(),
                Path("/Users/test/.config/memory-daemon/slack.json"),
            )


class TransportTest(unittest.TestCase):
    def test_bearer_token_is_sent_on_stdin_not_in_process_arguments(self):
        completed = mock.Mock(
            returncode=0,
            stdout='{"ok": true}',
            stderr="",
        )
        with mock.patch.object(slack_cli, "token", return_value="secret-token"), \
             mock.patch.object(
                 slack_cli.subprocess,
                 "run",
                 return_value=completed,
             ) as run:
            slack_cli.slack("auth.test")
        command = run.call_args.args[0]
        self.assertNotIn("secret-token", " ".join(command))
        self.assertEqual(command[-2:], ["-H", "@-"])
        self.assertEqual(
            run.call_args.kwargs["input"],
            "Authorization: Bearer secret-token\n",
        )


class TimestampTest(unittest.TestCase):
    def test_python39_compatible_utc_suffix(self):
        actual = slack_cli.parse_since(
            ["--since", "2026-07-27T00:00:00Z"]
        )
        expected = datetime(
            2026, 7, 27, tzinfo=timezone.utc
        ).timestamp()
        self.assertEqual(actual, f"{expected:.6f}")


class PaginationTest(unittest.TestCase):
    def test_history_follows_cursor_until_requested_limit(self):
        responses = [
            {
                "ok": True,
                "messages": [
                    {"ts": "3.0", "text": "three"},
                    {"ts": "2.0", "text": "two"},
                ],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "ok": True,
                "messages": [{"ts": "1.0", "text": "one"}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        stream = StringIO()
        with mock.patch.object(
            slack_cli,
            "slack",
            side_effect=responses,
        ) as api, redirect_stdout(stream):
            slack_cli.cmd_history(["C1", "--limit", "3"])
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["count"], 3)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(
            api.call_args_list[1].args[1]["cursor"],
            "next",
        )
        self.assertEqual(
            api.call_args_list[1].args[1]["limit"],
            1,
        )

    def test_replies_reads_every_page(self):
        responses = [
            {
                "ok": True,
                "messages": [{"ts": "1.0", "text": "root"}],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "ok": True,
                "messages": [
                    {
                        "ts": "2.0",
                        "thread_ts": "1.0",
                        "text": "reply",
                    },
                ],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        stream = StringIO()
        with mock.patch.object(
            slack_cli,
            "slack",
            side_effect=responses,
        ) as api, redirect_stdout(stream):
            slack_cli.cmd_replies(["C1", "1.0"])
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["count"], 2)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(
            api.call_args_list[1].args[1]["cursor"],
            "next",
        )


class DirectConversationTest(unittest.TestCase):
    def test_channel_listing_preserves_dm_identity_fields(self):
        response = {
            "ok": True,
            "channels": [{
                "id": "D123",
                "user": "U456",
                "is_im": True,
                "is_mpim": False,
            }],
            "response_metadata": {"next_cursor": ""},
        }
        stream = StringIO()
        with mock.patch.object(slack_cli, "slack", return_value=response), \
             redirect_stdout(stream):
            slack_cli.cmd_channels(["--types", "im"])
        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["channels"][0]["user"], "U456")
        self.assertTrue(payload["channels"][0]["is_im"])


class SourceCommandTest(unittest.TestCase):
    def test_source_defaults_to_repository_module(self):
        completed = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(slack_source, "SLACK_CLI", None), \
             mock.patch.object(slack_source.subprocess, "run", return_value=completed) as run:
            slack_source._cli(["auth-test"])
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                slack_source.sys.executable,
                "-m",
                "workspace_daemon.slack_cli",
                "auth-test",
            ],
        )
        self.assertEqual(run.call_args.kwargs["cwd"], str(slack_source.REPO_DIR))


class LaunchdTemplateTest(unittest.TestCase):
    def test_path_includes_user_local_bin_for_ada(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "launchd"
            / "com.workspace-daemon.plist.template"
        ).read_text()
        self.assertIn("__HOME__/.local/bin", template)


if __name__ == "__main__":
    unittest.main()
