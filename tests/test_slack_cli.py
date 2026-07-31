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

    def test_rate_limit_preserves_retry_after_header(self):
        completed = mock.Mock(
            returncode=0,
            stdout='{"ok": false, "error": "ratelimited"}',
            stderr="",
        )

        def rate_limited(command, **_kwargs):
            header_path = Path(command[command.index("--dump-header") + 1])
            header_path.write_text(
                "HTTP/1.1 200 Connection established\r\n\r\n"
                "HTTP/2 429\r\nRetry-After: 17\r\n\r\n"
            )
            return completed

        with mock.patch.object(slack_cli, "token", return_value="token"), \
             mock.patch.object(
                 slack_cli.subprocess,
                 "run",
                 side_effect=rate_limited,
             ), self.assertRaises(slack_cli.SlackAPIError) as raised:
            slack_cli.slack_request("conversations.history")

        self.assertEqual(raised.exception.error, "ratelimited")
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(raised.exception.retry_after, 17)


class TimestampTest(unittest.TestCase):
    def test_python39_compatible_utc_suffix(self):
        actual = slack_cli.parse_since(
            ["--since", "2026-07-27T00:00:00Z"]
        )
        expected = datetime(
            2026, 7, 27, tzinfo=timezone.utc
        ).timestamp()
        self.assertEqual(actual, f"{expected:.6f}")


class MessageSimplificationTest(unittest.TestCase):
    def test_block_only_message_preserves_visible_text(self):
        message = {
            "ts": "1.0",
            "text": "",
            "blocks": [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": "Decision in a block"},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open"},
                    "value": "internal-action-value",
                },
            }],
        }

        simplified = slack_cli.simplify_message(message, "C1")

        self.assertIn("Decision in a block", simplified["text"])
        self.assertIn("Open", simplified["text"])
        self.assertNotIn("internal-action-value", simplified["text"])
        self.assertTrue(simplified["non_text_fallback"])

    def test_file_only_message_preserves_safe_metadata(self):
        message = {
            "ts": "1.0",
            "text": "",
            "files": [{
                "id": "F1",
                "title": "design.png",
                "mimetype": "image/png",
                "permalink": "https://example.test/file/F1",
                "url_private": "https://secret.example.test/F1",
            }],
        }

        simplified = slack_cli.simplify_message(message, "C1")

        self.assertIn("design.png (image/png)", simplified["text"])
        self.assertIn("https://example.test/file/F1", simplified["text"])
        self.assertNotIn("secret.example.test", simplified["text"])

    def test_attachment_only_message_preserves_visible_fields(self):
        message = {
            "ts": "1.0",
            "text": "",
            "attachments": [{
                "fallback": "Deployment status",
                "fields": [{"title": "State", "value": "Blocked"}],
                "actions": [{
                    "text": "Approve",
                    "value": "opaque-internal-action-value",
                }],
            }],
        }

        simplified = slack_cli.simplify_message(message, "C1")

        self.assertIn("Deployment status", simplified["text"])
        self.assertIn("State", simplified["text"])
        self.assertIn("Blocked", simplified["text"])
        self.assertIn("Approve", simplified["text"])
        self.assertNotIn("opaque-internal-action-value", simplified["text"])

    def test_unsupported_system_message_gets_visible_placeholder(self):
        simplified = slack_cli.simplify_message({
            "ts": "1.0",
            "text": "",
            "subtype": "unsupported_event",
        }, "C1")

        self.assertIn("no extractable text", simplified["text"])
        self.assertIn("unsupported_event", simplified["text"])


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

    def test_history_zero_limit_reads_every_page(self):
        responses = [
            {
                "ok": True,
                "messages": [{"ts": "2.0", "text": "two"}],
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
            slack_cli.cmd_history(["C1", "--limit", "0"])

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["count"], 2)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[0].args[1]["limit"], 200)
        self.assertEqual(api.call_args_list[1].args[1]["limit"], 200)


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

    def test_zero_limit_exhausts_channel_pagination(self):
        responses = [
            {
                "ok": True,
                "channels": [{"id": "C1", "is_member": True}],
                "response_metadata": {"next_cursor": "next"},
            },
            {
                "ok": True,
                "channels": [{"id": "D2", "is_im": True}],
                "response_metadata": {"next_cursor": ""},
            },
        ]
        stream = StringIO()
        with mock.patch.object(
            slack_cli, "slack", side_effect=responses
        ) as api, redirect_stdout(stream):
            slack_cli.cmd_channels([
                "--types", "public_channel,private_channel,im,mpim",
                "--limit", "0",
            ])

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            [channel["id"] for channel in payload["channels"]],
            ["C1", "D2"],
        )
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[0].args[1]["limit"], 200)
        self.assertEqual(api.call_args_list[1].args[1]["cursor"], "next")

    def test_channel_pagination_fails_on_repeated_cursor(self):
        responses = [
            {
                "ok": True,
                "channels": [{"id": "C1"}],
                "response_metadata": {"next_cursor": "same"},
            },
            {
                "ok": True,
                "channels": [{"id": "C2"}],
                "response_metadata": {"next_cursor": "same"},
            },
        ]
        stream = StringIO()
        with mock.patch.object(
            slack_cli, "slack", side_effect=responses
        ) as api, redirect_stdout(stream), self.assertRaises(SystemExit):
            slack_cli.cmd_channels(["--limit", "0"])

        self.assertEqual(api.call_count, 2)
        self.assertIn("repeated pagination cursor", stream.getvalue())

    def test_joined_uses_membership_scoped_api(self):
        response = {
            "ok": True,
            "channels": [{"id": "C1"}],
            "response_metadata": {"next_cursor": ""},
        }
        stream = StringIO()
        with mock.patch.object(
            slack_cli, "slack", return_value=response
        ) as api, redirect_stdout(stream):
            slack_cli.cmd_joined(["--limit", "0"])

        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["membership_scoped"])
        self.assertEqual(api.call_args.args[0], "users.conversations")


class CensusCommandTest(unittest.TestCase):
    def test_rejects_invalid_window_before_calling_slack(self):
        stream = StringIO()
        with mock.patch.object(slack_cli, "list_conversations") as listing, \
             redirect_stdout(stream), self.assertRaises(SystemExit):
            slack_cli.cmd_census(["--hours", "0"])

        self.assertIn("greater than zero", stream.getvalue())
        listing.assert_not_called()

    def test_rejects_unsafe_request_rate_before_calling_slack(self):
        stream = StringIO()
        with mock.patch.object(slack_cli, "list_conversations") as listing, \
             redirect_stdout(stream), self.assertRaises(SystemExit):
            slack_cli.cmd_census(["--requests-per-minute", "51"])

        self.assertIn("between 1 and 50", stream.getvalue())
        listing.assert_not_called()

    def test_returns_nonzero_for_fatal_conversation_coverage_error(self):
        result = {
            "cutoff_at": "2026-07-28T10:00:00Z",
            "cutoff_epoch": 10,
            "until_at": "2026-07-30T10:00:00Z",
            "until_epoch": 20,
            "inventory": [{"id": "C1"}],
            "active": [],
            "errors": [{"id": "C1", "error": "missing_scope"}],
        }
        stream = StringIO()
        with mock.patch.object(
            slack_cli, "list_conversations", return_value=[{"id": "C1"}]
        ), mock.patch(
            "workspace_daemon.slack_census.load_resumable_checkpoint",
            return_value=None,
        ), mock.patch(
            "workspace_daemon.slack_census.run",
            return_value=result,
        ), redirect_stdout(stream), self.assertRaises(SystemExit) as stopped:
            slack_cli.cmd_census([])

        self.assertEqual(stopped.exception.code, 1)
        payload = json.loads(stream.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["fatal_error_count"], 1)

    def test_stale_conversation_error_remains_visible_but_nonfatal(self):
        result = {
            "cutoff_at": "2026-07-28T10:00:00Z",
            "cutoff_epoch": 10,
            "until_at": "2026-07-30T10:00:00Z",
            "until_epoch": 20,
            "inventory": [{"id": "D1"}],
            "active": [],
            "errors": [{"id": "D1", "error": "channel_not_found"}],
        }
        stream = StringIO()
        with mock.patch.object(
            slack_cli, "list_conversations", return_value=[{"id": "D1"}]
        ), mock.patch(
            "workspace_daemon.slack_census.load_resumable_checkpoint",
            return_value=None,
        ), mock.patch(
            "workspace_daemon.slack_census.run",
            return_value=result,
        ), redirect_stdout(stream):
            slack_cli.cmd_census([])

        payload = json.loads(stream.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["fatal_error_count"], 0)

    def test_completed_checkpoint_refreshes_conversation_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            checkpoint.write_text(json.dumps({
                "version": 1,
                "cutoff_epoch": 10,
                "cutoff_at": "1970-01-01T00:00:10Z",
                "inventory": [{"id": "C-OLD"}],
                "next_index": 1,
                "active": [{"id": "C-OLD"}],
                "errors": [],
                "completed_at": "2026-07-29T10:00:00Z",
            }))
            fresh_result = {
                "cutoff_at": "2026-07-30T10:00:00Z",
                "cutoff_epoch": 10,
                "until_at": "2026-07-30T11:00:00Z",
                "until_epoch": 20,
                "inventory": [{"id": "C-NEW"}],
                "active": [],
                "errors": [],
            }
            stream = StringIO()
            with mock.patch.object(
                slack_cli,
                "list_conversations",
                return_value=[{"id": "C-NEW"}],
            ) as listing, mock.patch(
                "workspace_daemon.slack_census.run",
                return_value=fresh_result,
            ) as census, redirect_stdout(stream):
                slack_cli.cmd_census(["--checkpoint", str(checkpoint)])

            listing.assert_called_once()
            self.assertEqual(census.call_args.args[0], [{"id": "C-NEW"}])
            self.assertTrue(json.loads(stream.getvalue())["ok"])

    def test_main_renders_corrupt_checkpoint_as_json_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            checkpoint.write_text(json.dumps({
                "version": 1,
                "cutoff_epoch": 10,
                "cutoff_at": "1970-01-01T00:00:10Z",
                "inventory": [],
                "next_index": 1,
                "active": [],
                "errors": [],
            }))
            stream = StringIO()
            argv = [
                "slack_cli.py", "census", "--checkpoint", str(checkpoint)
            ]
            with mock.patch.object(slack_cli.sys, "argv", argv), \
                 redirect_stdout(stream), self.assertRaises(SystemExit):
                slack_cli.main()

            payload = json.loads(stream.getvalue())
            self.assertFalse(payload["ok"])
            self.assertIn("next_index", payload["error"])


class MentionLimitTest(unittest.TestCase):
    def test_exact_ada_limit_is_reported_as_ambiguous(self):
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps({"mentions": [{} for _ in range(100)]}),
            stderr="",
        )
        stream = StringIO()
        with mock.patch.object(
            slack_cli, "mention_user", return_value="person@example.com"
        ), mock.patch.object(
            slack_cli.subprocess, "run", return_value=completed
        ), redirect_stdout(stream):
            slack_cli.cmd_mentions(["--days", "1"])

        payload = json.loads(stream.getvalue())
        self.assertEqual(payload["count"], 100)
        self.assertEqual(payload["max_results"], 100)
        self.assertTrue(payload["limit_reached"])


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
            / "com.memory-daemon.plist.template"
        ).read_text()
        self.assertIn("<string>com.memory-daemon</string>", template)
        self.assertIn("__HOME__/.local/node-current/bin", template)
        self.assertIn("__HOME__/.local/bin", template)


if __name__ == "__main__":
    unittest.main()
