"""Built-in Slack CLI configuration and DM-discovery behavior."""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
