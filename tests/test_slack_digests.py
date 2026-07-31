"""Hybrid Ada/private Slack digest behavior."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import config, slack_source


class AdaDigestTest(unittest.TestCase):
    SUMMARY = {
        "success": True,
        "channel_name": "public-project",
        "message_count": 100,
        "time_period": "last 30 days",
        "active_users": [],
        "important_links": ["https://example.test/spec"],
        "key_threads": [{
            "permalink": "https://example.test/thread",
            "reply_count": 3,
            "text_preview": "Decision with context",
            "timestamp": "1783922952.468549",
            "user": "Ada User",
        }],
        "top_messages": [{
            "permalink": "https://example.test/message",
            "reply_count": 0,
            "text": "API token: super-secret",
            "timestamp": "1784700404.877219",
            "user": "Another User",
        }],
    }

    def test_public_channel_becomes_one_timestamped_curated_candidate(self):
        with mock.patch.object(
            slack_source, "_ada_summary", return_value=self.SUMMARY
        ), mock.patch.object(slack_source, "utc_now_iso", return_value="2026-07-27T12:00:00Z"):
            candidates = slack_source.candidates({
                "ada_channels": ["CPUBLIC"],
                "ada_days": 30,
            })
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(
            candidate["raw"]["source_id"],
            "slack:CPUBLIC:digest:2026-07-27",
        )
        item = slack_source.fetch({}, candidate)
        self.assertEqual(item["frontmatter"]["slack_capture_mode"], "ada-channel-summary")
        self.assertTrue(item["frontmatter"]["message_limit_reached"])
        self.assertIn("[2026-", item["body"])
        self.assertIn("[REDACTED]", item["body"])
        self.assertNotIn("super-secret", item["body"])


class PrivateDigestTest(unittest.TestCase):
    HISTORY = {
        "ok": True,
        "messages": [
            {
                "source_id": "slack:CPRIVATE:1784700460.0",
                "ts": "1784700460.0",
                "user": "U2",
                "text": "activation code: 123456",
                "reply_count": 0,
            },
            {
                "source_id": "slack:CPRIVATE:1784700404.0",
                "ts": "1784700404.0",
                "user": "U1",
                "text": "root",
                "reply_count": 1,
            },
        ],
    }

    def test_private_messages_are_grouped_by_day_and_threads_expanded(self):
        with mock.patch.object(
            slack_source, "_cli", return_value=self.HISTORY
        ):
            candidates = slack_source.candidates({
                "private_channels": ["CPRIVATE"],
                "hours": 720,
                "max_results": 100,
            })
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertIn(":digest:", candidate["raw"]["source_id"])

        thread = {
            "ok": True,
            "messages": [
                {"ts": "1784700404.0", "user": "U1", "text": "root"},
                {"ts": "1784700410.0", "user": "U2", "text": "reply"},
            ],
        }
        whois = {
            "ok": True,
            "users": {
                "U1": {
                    "real_name": "One",
                    "email": "one@example.com",
                },
                "U2": {
                    "real_name": "Two",
                    "email": "two@example.com",
                },
            },
        }
        with mock.patch.object(
            slack_source, "_cli", side_effect=[thread, whois]
        ):
            item = slack_source.fetch({}, candidate)
        self.assertEqual(item["frontmatter"]["message_count"], 3)
        self.assertEqual(
            item["frontmatter"]["slack_capture_mode"],
            "private-daily-digest",
        )
        self.assertIn("One: root", item["body"])
        self.assertIn("Two: reply", item["body"])
        self.assertIn("[REDACTED]", item["body"])
        self.assertNotIn("123456", item["body"])
        self.assertEqual(item["frontmatter"]["source_people"], [
            {"name": "One", "email": "one@example.com"},
            {"name": "Two", "email": "two@example.com"},
        ])

    def test_textless_unthreaded_activity_gets_a_safe_placeholder(self):
        history = {
            "ok": True,
            "messages": [{
                "source_id": "slack:CPRIVATE:1783496058.862689",
                "ts": "1783496058.862689",
                "user": "U1",
                "text": "",
                "reply_count": 0,
            }],
        }
        with mock.patch.object(
            slack_source, "_cli", return_value=history
        ), mock.patch.object(slack_source, "log") as log:
            candidates = slack_source.candidates({
                "private_channels": ["CPRIVATE"],
                "hours": 720,
                "max_results": 100,
            })

        self.assertEqual(len(candidates), 1)
        whois = {
            "ok": True,
            "users": {
                "U1": {
                    "real_name": "One",
                    "email": "one@example.com",
                },
            },
        }
        with mock.patch.object(
            slack_source, "_cli", return_value=whois
        ):
            item = slack_source.fetch({}, candidates[0])
        self.assertIn("no extractable text", item["body"])
        self.assertIn("retaining activity", log.call_args.args[0])

    def test_textless_root_with_replies_is_expanded_and_fetched(self):
        history = {
            "ok": True,
            "messages": [{
                "source_id": "slack:CPRIVATE:1783496058.862689",
                "ts": "1783496058.862689",
                "latest_reply": "1783496158.862689",
                "user": "U1",
                "text": "",
                "reply_count": 1,
            }],
        }
        with mock.patch.object(
            slack_source, "_cli", return_value=history
        ):
            candidates = slack_source.candidates({
                "private_channels": ["CPRIVATE"],
                "hours": 720,
                "max_results": 100,
            })

        thread = {
            "ok": True,
            "messages": [
                history["messages"][0],
                {
                    "source_id": "slack:CPRIVATE:1783496058.862689",
                    "thread_ts": "1783496058.862689",
                    "ts": "1783496158.862689",
                    "user": "U2",
                    "text": "durable reply content",
                    "reply_count": 0,
                },
            ],
        }
        whois = {
            "ok": True,
            "users": {
                "U2": {
                    "real_name": "Two",
                    "email": "two@example.com",
                },
            },
        }
        with mock.patch.object(
            slack_source, "_cli", side_effect=[thread, whois]
        ):
            item = slack_source.fetch({}, candidates[0])

        self.assertIn("Two: durable reply content", item["body"])

    def test_catch_up_finds_old_root_reply_and_preserves_cutover(self):
        root = {
            "source_id": "slack:CDIRECT:1785225600.0",
            "ts": "1785225600.0",          # 08:00, before cutover
            "latest_reply": "1785232800.0",  # 10:00, after cursor
            "user": "U1",
            "text": "pre-cutover root",
            "reply_count": 2,
        }
        history = {
            "ok": True,
            "messages": [
                root,
                {
                    "source_id": "slack:CDIRECT:1785228600.0",
                    "ts": "1785228600.0",  # 08:50, before cursor
                    "user": "U3",
                    "text": "earlier post-cutover same-day context",
                    "reply_count": 0,
                },
                {
                    "source_id": "slack:CDIRECT:1785226800.0",
                    "ts": "1785226800.0",  # 08:20, before cutover
                    "user": "U4",
                    "text": "must stay in legacy coverage",
                    "reply_count": 0,
                },
                {
                    "source_id": "slack:CDIRECT:1785227400.0",
                    "ts": "1785227400.0",  # exactly at the 08:30 cutover
                    "user": "U5",
                    "text": "exact boundary stays in legacy coverage",
                    "reply_count": 0,
                },
            ],
        }
        thread = {
            "ok": True,
            "messages": [
                root,
                {
                    "source_id": root["source_id"],
                    "thread_ts": root["ts"],
                    "ts": "1785228300.0",  # 08:45, after cutover
                    "user": "U2",
                    "text": "earlier reply in the recurring day",
                },
                {
                    "source_id": root["source_id"],
                    "thread_ts": root["ts"],
                    "ts": "1785232800.0",  # 10:00, after cursor
                    "user": "U2",
                    "text": "new reply to an old root",
                },
            ],
        }
        source = {
            "kind": "slack",
            "direct_channels": ["CDIRECT"],
            "max_results": 0,
            "catch_up": True,
            "_since": "2026-07-28T09:00:00Z",
            "_catch_up_boundary": "2026-07-28T08:30:00Z",
            "reply_roots_after": "2026-06-28T08:00:00Z",
        }

        with mock.patch.object(
            slack_source, "_cli", side_effect=[history, thread]
        ) as cli:
            candidates = slack_source.candidates(source)

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertRegex(
            candidate["id"],
            r"^slack:CDIRECT:daily:2026-07-28@1785232800\.0:[0-9a-f]{16}$",
        )
        self.assertEqual(
            candidate["raw"]["source_id"],
            "slack:CDIRECT:daily:2026-07-28",
        )
        texts = [message["text"] for message in candidate["raw"]["messages"]]
        self.assertEqual(texts, [
            "earlier reply in the recurring day",
            "earlier post-cutover same-day context",
            "new reply to an old root",
        ])
        self.assertNotIn("pre-cutover root", texts)
        self.assertNotIn("must stay in legacy coverage", texts)
        self.assertNotIn("exact boundary stays in legacy coverage", texts)
        self.assertEqual(
            cli.call_args_list[0].args[0],
            [
                "history", "CDIRECT",
                "--since", "2026-06-28T08:00:00Z",
                "--limit", "0",
            ],
        )
        self.assertEqual(
            cli.call_args_list[1].args[0],
            ["replies", "CDIRECT", "1785225600.0"],
        )

    def test_catch_up_retains_textless_unthreaded_activity(self):
        history = {
            "ok": True,
            "messages": [{
                "source_id": "slack:CDIRECT:1785232800.0",
                "ts": "1785232800.0",
                "user": "U1",
                "text": "",
                "subtype": "unsupported_event",
                "reply_count": 0,
            }],
        }
        source = {
            "kind": "slack",
            "direct_channels": ["CDIRECT"],
            "max_results": 0,
            "catch_up": True,
            "_since": "2026-07-28T09:00:00Z",
            "_catch_up_boundary": "2026-07-28T08:30:00Z",
            "reply_roots_after": "2026-06-28T08:00:00Z",
        }

        with mock.patch.object(
            slack_source, "_cli", return_value=history
        ), mock.patch.object(slack_source, "log") as log:
            candidates = slack_source.candidates(source)

        self.assertEqual(len(candidates), 1)
        messages = candidates[0]["raw"]["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIn("no extractable text", messages[0]["text"])
        self.assertIn("unsupported_event", messages[0]["text"])
        self.assertIn("retaining activity", log.call_args.args[0])

    def test_catch_up_removes_membership_events_before_digesting(self):
        membership = {
            "source_id": "slack:CDIRECT:1785232800.0",
            "ts": "1785232800.0",
            "user": "U1",
            "text": "<@U1> has joined the channel",
            "subtype": "channel_join",
            "reply_count": 0,
        }
        durable = {
            "source_id": "slack:CDIRECT:1785232860.0",
            "ts": "1785232860.0",
            "user": "U2",
            "text": "Decision: use the safer rollout.",
            "reply_count": 0,
        }
        source = {
            "kind": "slack",
            "direct_channels": ["CDIRECT"],
            "max_results": 0,
            "catch_up": True,
            "_since": "2026-07-28T09:00:00Z",
            "_catch_up_boundary": "2026-07-28T08:30:00Z",
            "reply_roots_after": "2026-06-28T08:00:00Z",
        }

        with mock.patch.object(
            slack_source,
            "_cli",
            return_value={"ok": True, "messages": [membership, durable]},
        ):
            candidates = slack_source.candidates(source)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            [row["text"] for row in candidates[0]["raw"]["messages"]],
            ["Decision: use the safer rollout."],
        )

        with mock.patch.object(
            slack_source,
            "_cli",
            return_value={"ok": True, "messages": [membership]},
        ):
            self.assertEqual(slack_source.candidates(source), [])

    def test_wider_root_floor_changes_version_when_latest_is_unchanged(self):
        latest = {
            "source_id": "slack:CDIRECT:1785232800.0",
            "ts": "1785232800.0",  # 10:00, activates the day
            "user": "U1",
            "text": "latest message",
            "reply_count": 0,
        }
        omitted = {
            "source_id": "slack:CDIRECT:1785228300.0",
            "ts": "1785228300.0",  # 08:45, older context now in scope
            "user": "U2",
            "text": "newly included older context",
            "reply_count": 0,
        }
        base = {
            "kind": "slack",
            "direct_channels": ["CDIRECT"],
            "max_results": 0,
            "catch_up": True,
            "_since": "2026-07-28T09:30:00Z",
            "_catch_up_boundary": "2026-07-28T08:30:00Z",
        }

        with mock.patch.object(
            slack_source, "_cli", return_value={"ok": True, "messages": [latest]}
        ):
            narrow = slack_source.candidates({
                **base,
                "reply_roots_after": "2026-07-28T09:00:00Z",
            })[0]
        with mock.patch.object(
            slack_source, "_cli",
            return_value={"ok": True, "messages": [latest, omitted]},
        ):
            wider = slack_source.candidates({
                **base,
                "reply_roots_after": "2026-06-28T08:00:00Z",
            })[0]

        self.assertEqual(
            narrow["raw"]["source_id"], wider["raw"]["source_id"]
        )
        self.assertTrue(
            narrow["id"].startswith(
                "slack:CDIRECT:daily:2026-07-28@1785232800.0:"
            )
        )
        self.assertTrue(
            wider["id"].startswith(
                "slack:CDIRECT:daily:2026-07-28@1785232800.0:"
            )
        )
        self.assertNotEqual(narrow["id"], wider["id"])

    def test_new_reply_versions_an_existing_daily_digest(self):
        history = {
            "ok": True,
            "messages": [{
                "source_id": "slack:CDIRECT:1785225600.0",
                "ts": "1785225600.0",
                "latest_reply": "1785232800.0",
                "user": "U1",
                "text": "thread root",
                "reply_count": 1,
            }],
        }
        with mock.patch.object(slack_source, "_cli", return_value=history):
            candidate = slack_source.candidates({
                "direct_channels": ["CDIRECT"],
                "hours": 26,
                "max_results": 30,
            })[0]

        self.assertEqual(
            candidate["id"],
            "slack:CDIRECT:digest:2026-07-28@1785232800.0",
        )
        self.assertEqual(
            candidate["raw"]["source_id"],
            "slack:CDIRECT:digest:2026-07-28",
        )


class ActiveConversationSweepTest(unittest.TestCase):
    def _checkpoint(self, path):
        path.write_text(json.dumps({
            "version": 1,
            "cutoff_epoch": 1785225600,
            "cutoff_at": "2026-07-28T08:00:00Z",
            "until_epoch": 1785398400,
            "until_at": "2026-07-30T08:00:00Z",
            "inventory": [
                {"id": "CACTIVE"},
                {"id": "COWNED"},
                {"id": "DSTALE"},
            ],
            "next_index": 3,
            "active": [
                {
                    "id": "CACTIVE",
                    "name": "active",
                    "type": "public_channel",
                    "latest_ts": "1785390000.0",
                },
                {
                    "id": "COWNED",
                    "name": "domain-owned",
                    "type": "private_channel",
                    "latest_ts": "1785391000.0",
                },
            ],
            "errors": [
                {"id": "DSTALE", "error": "channel_not_found"},
            ],
            "completed_at": "2026-07-30T08:01:00Z",
        }))

    def test_fresh_census_feeds_direct_sweep_and_excludes_owned_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            history = {
                "ok": True,
                "messages": [{
                    "source_id": "slack:CACTIVE:1785390000.0",
                    "ts": "1785390000.0",
                    "user": "U1",
                    "text": "message from either direction",
                    "reply_count": 0,
                }],
            }
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 48,
                    "refresh_every": "1d",
                    "requests_per_minute": 40,
                },
                "max_results": 0,
                "catch_up": True,
                "_since": "2026-07-30T07:00:00Z",
                "_catch_up_boundary": "2026-07-28T08:00:00Z",
                "reply_roots_after": "2026-07-28T08:00:00Z",
                "_exclude_channels": ["COWNED"],
            }

            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-30T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli", return_value=history
            ) as cli:
                candidates = slack_source.candidates(source)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["raw"]["source_id"],
            "slack:CACTIVE:daily:2026-07-30",
        )
        self.assertEqual(
            cli.call_args_list[0].args[0],
            [
                "history", "CACTIVE",
                "--since", "2026-07-28T08:00:00Z",
                "--limit", "0",
            ],
        )

    def test_stale_census_preview_does_not_pass_checkpoint_to_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            census = {
                "ok": True,
                "window_hours": 48,
                "cutoff_at": "2026-07-30T10:00:00Z",
                "active": [],
                "errors": [],
            }
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 48,
                    "refresh_every": "1h",
                    "requests_per_minute": 40,
                },
                "_dry_run": True,
            }
            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-31T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli", return_value=census
            ) as cli:
                effective = slack_source._with_active_conversations(source)

        args = cli.call_args.args[0]
        self.assertEqual(args[:3], ["census", "--hours", "48"])
        self.assertNotIn("--checkpoint", args)
        self.assertEqual(effective["direct_channels"], [])

    def test_consume_only_sweep_fails_on_stale_cache_without_census_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 48,
                    "refresh_every": "1h",
                    "requests_per_minute": 40,
                    "refresh_if_stale": False,
                },
                "_dry_run": True,
            }
            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-31T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli"
            ) as cli, self.assertRaisesRegex(
                RuntimeError, "consume-only sweep"
            ):
                slack_source._with_active_conversations(source)

        cli.assert_not_called()

    def test_future_completed_timestamp_is_not_treated_as_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            data = json.loads(checkpoint.read_text())
            data["completed_at"] = "2026-08-01T09:00:00Z"
            checkpoint.write_text(json.dumps(data))
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 48,
                    "refresh_every": "1d",
                    "requests_per_minute": 40,
                },
                "_dry_run": True,
            }
            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-31T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli",
                return_value={
                    "ok": True,
                    "window_hours": 48,
                    "cutoff_at": "2026-07-29T09:00:00Z",
                    "active": [],
                    "errors": [],
                },
            ) as cli:
                slack_source._with_active_conversations(source)

        self.assertEqual(cli.call_args.args[0][0], "census")

    def test_changed_window_invalidates_otherwise_fresh_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 24,
                    "refresh_every": "1d",
                    "requests_per_minute": 40,
                },
                "_dry_run": True,
            }
            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-30T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli",
                return_value={
                    "ok": True,
                    "window_hours": 24,
                    "cutoff_at": "2026-07-29T09:00:00Z",
                    "active": [],
                    "errors": [],
                },
            ) as cli:
                slack_source._with_active_conversations(source)

        self.assertEqual(
            cli.call_args.args[0][:3],
            ["census", "--hours", "24"],
        )

    def test_snapshot_boundary_controls_freshness_not_completion_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            data = json.loads(checkpoint.read_text())
            data["completed_at"] = "2026-07-31T08:59:00Z"
            checkpoint.write_text(json.dumps(data))
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 48,
                    "refresh_every": "1d",
                    "requests_per_minute": 40,
                },
                "_dry_run": True,
            }
            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-31T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli",
                return_value={
                    "ok": True,
                    "window_hours": 48,
                    "cutoff_at": "2026-07-29T09:00:00Z",
                    "active": [],
                    "errors": [],
                },
            ) as cli:
                slack_source._with_active_conversations(source)

        self.assertEqual(cli.call_args.args[0][0], "census")

    def test_refreshed_census_window_is_revalidated_before_use(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "census.json"
            self._checkpoint(checkpoint)
            source = {
                "kind": "slack",
                "active_conversations": {
                    "checkpoint": str(checkpoint),
                    "hours": 48,
                    "refresh_every": "1h",
                    "requests_per_minute": 40,
                },
                "_dry_run": True,
            }
            with mock.patch.object(
                slack_source, "utc_now_iso",
                return_value="2026-07-31T09:00:00Z",
            ), mock.patch.object(
                slack_source, "_cli",
                return_value={
                    "ok": True,
                    "window_hours": 1,
                    "cutoff_at": "2026-07-31T08:00:00Z",
                    "active": [],
                    "errors": [],
                },
            ), self.assertRaisesRegex(
                RuntimeError, "requested 48h, received 1h"
            ):
                slack_source._with_active_conversations(source)


class MentionOwnershipTest(unittest.TestCase):
    def test_mentions_skip_channels_owned_by_another_routine(self):
        mentions = {
            "ok": True,
            "mentions": [
                {
                    "source_id": "slack:COWNED:100.0",
                    "channel_id": "COWNED",
                    "ts": "100.0",
                    "text": "already in the domain digest",
                },
                {
                    "source_id": "slack:CEXTERNAL:200.0",
                    "channel_id": "CEXTERNAL",
                    "ts": "200.0",
                    "text": "keep this external mention",
                },
            ],
        }
        with mock.patch.object(slack_source, "_cli", return_value=mentions):
            candidates = slack_source.candidates({
                "include_mentions": True,
                "hours": 24,
                "_exclude_mention_channels": ["COWNED"],
            })

        self.assertEqual(
            [candidate["raw"]["channel"] for candidate in candidates],
            ["CEXTERNAL"],
        )

    def test_catch_up_fails_closed_when_ada_mentions_hit_the_limit(self):
        with mock.patch.object(slack_source, "_cli", return_value={
            "ok": True,
            "limit_reached": True,
            "mentions": [],
        }), mock.patch.object(
            slack_source, "utc_now_iso", return_value="2026-07-28T10:00:00Z"
        ):
            with self.assertRaisesRegex(RuntimeError, "result limit"):
                slack_source.candidates({
                    "kind": "slack",
                    "include_mentions": True,
                    "catch_up": True,
                    "_since": "2026-07-28T09:00:00Z",
                })

    def test_fixed_window_mentions_also_fail_closed_at_ada_limit(self):
        with mock.patch.object(slack_source, "_cli", return_value={
            "ok": True,
            "limit_reached": True,
            "mentions": [],
        }):
            with self.assertRaisesRegex(RuntimeError, "result limit"):
                slack_source.candidates({
                    "kind": "slack",
                    "include_mentions": True,
                    "hours": 24,
                })

    def test_catch_up_mention_boundary_is_exclusive(self):
        mentions = {
            "ok": True,
            "limit_reached": False,
            "mentions": [
                {
                    "source_id": "slack:C1:1785229200.0",
                    "channel_id": "C1",
                    "ts": "1785229200.0",
                    "text": "exactly at cursor",
                },
                {
                    "source_id": "slack:C2:1785229260.0",
                    "channel_id": "C2",
                    "ts": "1785229260.0",
                    "text": "after cursor",
                },
            ],
        }
        with mock.patch.object(
            slack_source, "_cli", return_value=mentions
        ), mock.patch.object(
            slack_source, "utc_now_iso", return_value="2026-07-28T10:00:00Z"
        ):
            candidates = slack_source.candidates({
                "kind": "slack",
                "include_mentions": True,
                "catch_up": True,
                "_since": "2026-07-28T09:00:00Z",
            })

        self.assertEqual(
            [candidate["raw"]["channel"] for candidate in candidates],
            ["C2"],
        )


class HybridValidationTest(unittest.TestCase):
    def routine(self, source):
        return {
            "id": "hybrid",
            "source": source,
            "analyze": {
                "provider": "gemini",
                "model": "model",
                "instruction": "Keep durable decisions and commitments only.",
            },
            "memory": {"store": "/tmp/memory", "type": "note"},
        }

    def test_explicit_public_and_private_lists_are_valid(self):
        problems = config.validate(self.routine({
            "kind": "slack",
            "ada_channels": ["CPUBLIC"],
            "private_channels": ["CPRIVATE"],
            "ada_days": 30,
        }))
        self.assertEqual(problems, [])

    def test_same_channel_cannot_use_both_ingestion_paths(self):
        problems = config.validate(self.routine({
            "kind": "slack",
            "ada_channels": ["CSAME"],
            "private_channels": ["CSAME"],
        }))
        self.assertTrue(any("appears in both" in problem for problem in problems))

    def test_ada_days_range_is_validated(self):
        problems = config.validate(self.routine({
            "kind": "slack",
            "ada_channels": ["CPUBLIC"],
            "ada_days": 91,
        }))
        self.assertTrue(any("ada_days" in problem for problem in problems))

    def test_direct_slack_catch_up_is_uncapped_and_rejects_ada_summaries(self):
        valid = config.validate(self.routine({
            "kind": "slack",
            "direct_channels": ["CDIRECT"],
            "include_mentions": True,
            "max_results": 0,
            "catch_up": True,
            "catch_up_overlap": "1h",
            "catch_up_after": "2026-07-28T08:00:00Z",
            "reply_roots_after": "2026-06-28T08:00:00Z",
        }))
        invalid = config.validate(self.routine({
            "kind": "slack",
            "ada_channels": ["CPUBLIC"],
            "max_results": 100,
            "catch_up": True,
            "catch_up_overlap": "soon",
            "catch_up_after": "yesterday",
        }))

        self.assertEqual(valid, [])
        self.assertTrue(any("direct Slack reads" in p for p in invalid))
        self.assertTrue(any("requires max_results: 0" in p for p in invalid))
        self.assertTrue(any("catch_up_overlap must look like" in p for p in invalid))
        self.assertTrue(any("quoted RFC3339" in p for p in invalid))

    def test_reply_root_floor_must_cover_the_bootstrap_boundary(self):
        problems = config.validate(self.routine({
            "kind": "slack",
            "direct_channels": ["CDIRECT"],
            "max_results": 0,
            "catch_up": True,
            "catch_up_after": "2026-07-28T08:00:00Z",
            "reply_roots_after": "2026-07-29T08:00:00Z",
        }))
        self.assertTrue(any("must not be later" in p for p in problems))

    def test_active_conversation_catch_up_configuration_is_validated(self):
        valid = config.validate(self.routine({
            "kind": "slack",
            "active_conversations": {
                "checkpoint": "state/slack-census.json",
                "hours": 48,
                "refresh_every": "1d",
                "requests_per_minute": 40,
            },
            "max_results": 0,
            "catch_up": True,
            "catch_up_after": "2026-07-28T08:00:00Z",
            "reply_roots_after": "2026-07-28T08:00:00Z",
        }))
        invalid = config.validate(self.routine({
            "kind": "slack",
            "active_conversations": {
                "hours": 0,
                "refresh_every": "sometimes",
                "requests_per_minute": 80,
            },
        }))

        self.assertEqual(valid, [])
        self.assertTrue(any("checkpoint is required" in p for p in invalid))
        self.assertTrue(any(".hours must be positive" in p for p in invalid))
        self.assertTrue(any(".refresh_every" in p for p in invalid))
        self.assertTrue(any(".requests_per_minute" in p for p in invalid))

        non_overlapping = config.validate(self.routine({
            "kind": "slack",
            "active_conversations": {
                "checkpoint": "state/slack-census.json",
                "hours": 1,
                "refresh_every": "1d",
            },
        }))
        self.assertTrue(
            any("requires catch_up: true" in p for p in non_overlapping)
        )
        self.assertTrue(
            any("must not exceed its hours window" in p for p in non_overlapping)
        )


if __name__ == "__main__":
    unittest.main()
