"""gchat_source: thread grouping, version-aware ids, rendering; slack id versioning."""
import unittest
from unittest import mock

from workspace_daemon import config, gchat_source, memory_sink, slack_source


def msg(thread, ts, text, sender="users/1"):
    return {"thread": f"spaces/AAA/threads/{thread}", "create_time": ts,
            "text": text, "sender": sender, "name": f"spaces/AAA/messages/{thread}.x"}


MESSAGES = {
    "messages": [
        msg("t1", "2026-07-27T08:00:00Z", "first"),
        msg("t1", "2026-07-27T09:00:00Z", "reply", sender="users/2"),
        msg("t2", "2026-07-27T08:30:00Z", "solo thread"),
    ],
    "count": 3,
}
MEMBERS = [
    {
        "user": "users/1",
        "display_name": "Jane Doe",
        "email": "jane@example.com",
    },
    {
        "user": "users/2",
        "display_name": "John Smith",
        "email": "john@example.com",
    },
]
SPACE = {
    "name": "spaces/AAA",
    "display_name": "",
    "type": "DIRECT_MESSAGE",
}


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        gchat_source._member_cache.clear()
        gchat_source._space_cache.clear()

    def test_groups_by_thread_with_versioned_ids(self):
        with mock.patch.object(gchat_source, "_gws", return_value=MESSAGES):
            out = gchat_source.candidates({"spaces": ["spaces/AAA"], "hours": 26})
        by_sid = {c["raw"]["source_id"]: c for c in out}
        self.assertEqual(set(by_sid), {"gchat:AAA:t1", "gchat:AAA:t2"})
        # candidate id carries the LATEST message time -> new reply = new candidate
        self.assertTrue(by_sid["gchat:AAA:t1"]["id"].startswith(
            "gchat:AAA:t1@2026-07-27T09:00:00Z:"
        ))
        self.assertEqual(by_sid["gchat:AAA:t1"]["title"], "first")

    def test_empty_window(self):
        with mock.patch.object(gchat_source, "_gws",
                               return_value={"count": 0, "messages": None}):
            self.assertEqual(gchat_source.candidates({"spaces": ["spaces/AAA"]}), [])

    def test_attachment_only_message_is_captured_without_url_or_body(self):
        message = msg("file", "2026-07-27T08:00:00Z", "")
        message["attachments"] = [{
            "content_name": "roadmap.pdf",
            "content_type": "application/pdf",
            "download_uri": "https://secret.example/file",
        }]
        with mock.patch.object(
            gchat_source, "_gws", return_value={"messages": [message]}
        ):
            candidate = gchat_source.candidates({"spaces": ["spaces/AAA"]})[0]
        self.assertIn("roadmap.pdf", candidate["title"])
        with mock.patch.object(gchat_source, "_member_context", return_value={
            "names": {}, "members": [], "people": {},
        }), mock.patch.object(gchat_source, "_space_context", return_value={}):
            item = gchat_source.fetch({}, candidate)
        self.assertIn("[Attachment: roadmap.pdf (application/pdf)]", item["body"])
        self.assertNotIn("secret.example", item["body"])

    def test_edit_changes_candidate_version_even_when_create_time_is_stable(self):
        original = {"messages": [msg("t1", "2026-07-27T08:00:00Z", "draft")]}
        edited = {"messages": [{
            **msg("t1", "2026-07-27T08:00:00Z", "final decision"),
            "last_update_time": "2026-07-27T09:00:00Z",
        }]}
        with mock.patch.object(gchat_source, "_gws", side_effect=[original, edited]):
            before = gchat_source.candidates({"spaces": ["spaces/AAA"]})[0]
            after = gchat_source.candidates({"spaces": ["spaces/AAA"]})[0]
        self.assertNotEqual(before["id"], after["id"])
        self.assertIn("@2026-07-27T09:00:00Z:", after["id"])

    def test_daily_batches_split_long_gaps_into_stable_sessions(self):
        messages = {"messages": [
            msg("one", "2026-07-27T08:00:00Z", "morning topic"),
            msg("two", "2026-07-27T08:30:00Z", "morning follow-up"),
            msg("three", "2026-07-27T14:00:00Z", "unrelated afternoon topic"),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            candidates = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
                "session_gap_minutes": 120,
            })
        self.assertEqual(len(candidates), 2)
        self.assertEqual(
            sum(":session:" in row["raw"]["source_id"] for row in candidates),
            1,
        )

    def test_zero_max_explicit_space_paginates_exhaustively(self):
        with mock.patch.object(
            gchat_source, "_gws", return_value={"messages": []}
        ) as gws:
            gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "hours": 26,
                "max_results": 0,
            })

        gws.assert_called_once_with([
            "chat", "messages", "spaces/AAA",
            "--after", mock.ANY,
            "--raw", "--all",
        ])

    def test_positive_max_explicit_space_caps_a_single_raw_page(self):
        with mock.patch.object(
            gchat_source, "_gws", return_value={"messages": []}
        ) as gws:
            gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "hours": 26,
                "max_results": 50,
            })

        gws.assert_called_once_with([
            "chat", "messages", "spaces/AAA",
            "--after", mock.ANY,
            "--raw", "--max", "50",
        ])

    def test_daily_batches_unthreaded_messages_but_keeps_real_threads(self):
        messages = {"messages": [
            msg("t1", "2026-07-27T08:00:00Z", "thread root"),
            msg("t1", "2026-07-27T09:00:00Z", "thread reply"),
            msg("solo1", "2026-07-27T10:00:00Z", "first sentence"),
            msg("solo2", "2026-07-27T10:01:00Z", "second sentence"),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            out = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_unthreaded": "daily",
            })
        by_sid = {candidate["raw"]["source_id"]: candidate for candidate in out}
        self.assertEqual(
            set(by_sid),
            {"gchat:AAA:t1", "gchat:AAA:day:2026-07-27"},
        )
        digest = by_sid["gchat:AAA:day:2026-07-27"]
        self.assertEqual(len(digest["raw"]["messages"]), 2)
        self.assertIn("@2026-07-27T10:01:00Z:", digest["id"])

    def test_daily_message_batch_has_one_stable_space_day_identity(self):
        first_messages = {"messages": [
            msg("t1", "2026-07-27T08:00:00Z", "thread root"),
            msg("t1", "2026-07-27T09:00:00Z", "thread reply"),
            msg("solo1", "2026-07-27T10:00:00Z", "first sentence"),
        ]}
        updated_messages = {"messages": [
            *first_messages["messages"],
            msg("solo2", "2026-07-27T10:01:00Z", "second sentence"),
        ]}
        later_window = {"messages": [updated_messages["messages"][-1]]}
        with mock.patch.object(gchat_source, "_gws", side_effect=[
            first_messages, first_messages,
            later_window, updated_messages,
        ]) as gws:
            first = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
            })[0]
            updated = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
            })[0]

        self.assertEqual(
            first["raw"]["source_id"],
            "gchat:AAA:daily:2026-07-27",
        )
        self.assertEqual(first["raw"]["source_id"], updated["raw"]["source_id"])
        self.assertNotEqual(first["id"], updated["id"])
        self.assertEqual(len(updated["raw"]["messages"]), 4)
        self.assertIn("@2026-07-27T10:01:00Z:", updated["id"])
        # The second discovery saw only the newest slice, but the final
        # candidate was rebuilt from an exhaustive UTC-day history call.
        self.assertEqual(
            gws.call_args_list[-1],
            mock.call([
                "chat", "messages", "spaces/AAA",
                "--after", "2026-07-26T23:59:59.999999Z",
                "--raw", "--all",
            ], timeout=300),
        )

    def test_daily_message_batch_cutover_excludes_legacy_content(self):
        messages = {"messages": [
            msg("old", "2026-07-27T08:00:00Z", "legacy thread"),
            msg("boundary", "2026-07-27T09:00:00Z", "already covered"),
            msg("new", "2026-07-27T10:00:00Z", "new capture"),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            out = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
                "batch_messages_after": "2026-07-27T09:00:00Z",
            })

        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0]["raw"]["source_id"],
            "gchat:AAA:daily:2026-07-27",
        )
        self.assertEqual(
            [message["text"] for message in out[0]["raw"]["messages"]],
            ["new capture"],
        )

    def test_daily_message_batch_cutover_preserves_nanosecond_precision(self):
        messages = {"messages": [
            msg(
                "boundary",
                "2026-07-27T09:00:00.123456789Z",
                "already covered",
            ),
            msg(
                "later",
                "2026-07-27T09:00:00.123456790Z",
                "one nanosecond later",
            ),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            out = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
                "batch_messages_after": "2026-07-27T09:00:00.123456789Z",
            })

        self.assertEqual(len(out), 1)
        self.assertEqual(
            [message["text"] for message in out[0]["raw"]["messages"]],
            ["one nanosecond later"],
        )

    def test_daily_message_batch_system_event_does_not_advance_version(self):
        messages = {"messages": [
            msg("content", "2026-07-27T08:00:00Z", "durable update"),
            msg("system", "2026-07-27T09:00:00Z", ""),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            candidate = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
            })[0]

        self.assertIn("@2026-07-27T08:00:00Z:", candidate["id"])
        self.assertEqual(len(candidate["raw"]["messages"]), 1)

    def test_daily_message_batch_drops_empty_system_messages(self):
        messages = {"messages": [
            msg("system", "2026-07-27T08:00:00Z", ""),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            out = gchat_source.candidates({
                "spaces": ["spaces/AAA"],
                "batch_messages": "daily",
            })
        self.assertEqual(out, [])

    def test_all_spaces_uses_recent_and_groups_each_space(self):
        messages = {
            "messages": [
                {
                    **msg("t1", "2026-07-27T08:00:00Z", "from one"),
                    "space": "spaces/AAA",
                    "space_display_name": "One",
                    "space_type": "SPACE",
                },
                {
                    **msg("t2", "2026-07-27T09:00:00Z", "from two"),
                    "space": "spaces/BBB",
                    "space_display_name": "",
                    "space_type": "DIRECT_MESSAGE",
                },
            ],
        }
        with mock.patch.object(
            gchat_source, "_gws", return_value=messages
        ) as gws, mock.patch.object(gchat_source, "log") as log:
            out = gchat_source.candidates({
                "all_spaces": True,
                "hours": 168,
                "max_results": 0,
                "max_per_space": 0,
            })

        gws.assert_called_once_with([
            "chat", "recent",
            "--since", "168h",
            "--max", "0",
            "--max-per-space", "0",
        ], timeout=300)
        self.assertEqual(
            {candidate["raw"]["space"] for candidate in out},
            {"spaces/AAA", "spaces/BBB"},
        )
        self.assertEqual(gchat_source._space_cache["spaces/AAA"]["display_name"], "One")
        coverage = log.call_args.args[0]
        self.assertIn(
            "discovered_space_ids=['spaces/AAA', 'spaces/BBB']",
            coverage,
        )
        self.assertIn(
            "considered_space_ids=['spaces/AAA', 'spaces/BBB']",
            coverage,
        )

    def test_all_spaces_uses_exact_catch_up_cursor(self):
        with mock.patch.object(
            gchat_source, "_gws",
            return_value={"messages": []},
        ) as gws:
            out = gchat_source.candidates({
                "all_spaces": True,
                "_since": "2026-07-25T09:00:00Z",
                "hours": 1,
                "max_results": 0,
                "max_per_space": 0,
            })

        self.assertEqual(out, [])
        gws.assert_called_once_with([
            "chat", "recent",
            "--since", "2026-07-25T09:00:00Z",
            "--max", "0",
            "--max-per-space", "0",
        ], timeout=300)

    def test_all_spaces_recovers_space_from_message_name(self):
        messages = {"messages": [
            msg("t1", "2026-07-27T08:00:00Z", "hello"),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            out = gchat_source.candidates({"all_spaces": True})
        self.assertEqual(out[0]["raw"]["space"], "spaces/AAA")

    def test_all_spaces_excludes_reserved_domain_spaces(self):
        messages = {
            "messages": [
                {
                    **msg("t1", "2026-07-27T08:00:00Z", "owned"),
                    "space": "spaces/AAA",
                },
                {
                    **msg("t2", "2026-07-27T09:00:00Z", "fallback"),
                    "space": "spaces/BBB",
                },
            ],
        }
        with mock.patch.object(
            gchat_source, "_gws", return_value=messages
        ), mock.patch.object(gchat_source, "log") as log:
            out = gchat_source.candidates({
                "all_spaces": True,
                "_exclude_spaces": ["spaces/AAA"],
            })
        self.assertEqual(
            {candidate["raw"]["space"] for candidate in out},
            {"spaces/BBB"},
        )
        coverage = log.call_args.args[0]
        self.assertIn(
            "excluded_active_space_ids=['spaces/AAA']",
            coverage,
        )
        self.assertIn(
            "considered_space_ids=['spaces/BBB']",
            coverage,
        )


def raw_msg(thread, ts, text, sender="users/1", **extra):
    return {
        "name": f"spaces/AAA/messages/{thread}.x",
        "createTime": ts,
        "text": text,
        "sender": {"name": sender, "type": "HUMAN"},
        "thread": {"name": f"spaces/AAA/threads/{thread}"},
        "space": {"name": "spaces/AAA"},
        **extra,
    }


class RawNormalizationTest(unittest.TestCase):
    def setUp(self):
        gchat_source._member_cache.clear()
        gchat_source._space_cache.clear()

    def test_raw_message_flattens_to_module_shape(self):
        flat = gchat_source._normalize_raw_message(raw_msg(
            "t1", "2026-08-04T07:30:00Z", "done",
            lastUpdateTime="2026-08-04T07:31:00Z",
        ))
        self.assertEqual(flat["create_time"], "2026-08-04T07:30:00Z")
        self.assertEqual(flat["last_update_time"], "2026-08-04T07:31:00Z")
        self.assertEqual(flat["sender"], "users/1")
        self.assertEqual(flat["thread"], "spaces/AAA/threads/t1")
        self.assertEqual(flat["space"], "spaces/AAA")

    def test_flattened_message_passes_through(self):
        message = msg("t1", "2026-08-04T07:30:00Z", "hello")
        self.assertIs(gchat_source._normalize_raw_message(message), message)

    def test_reactions_and_quoted_context_are_normalized(self):
        flat = gchat_source._normalize_raw_message(raw_msg(
            "t1", "2026-08-04T07:30:00Z", "done",
            emojiReactionSummaries=[
                {"emoji": {"unicode": "♥️"}, "reactionCount": 1},
            ],
            quotedMessageMetadata={
                "quotedMessageSnapshot": {
                    "text": "can you add some bullets to this slide?",
                },
            },
        ))
        self.assertEqual(flat["reactions"], [{"emoji": "♥️", "count": 1}])
        self.assertEqual(
            flat["quoted_message"],
            {"text": "can you add some bullets to this slide?"},
        )

    def test_raw_attachment_metadata_survives_normalization(self):
        flat = gchat_source._normalize_raw_message(raw_msg(
            "t1", "2026-08-04T07:30:00Z", "",
            attachment=[{
                "contentName": "roadmap.pdf",
                "contentType": "application/pdf",
                "downloadUri": "https://secret.example/file",
            }],
        ))
        content = gchat_source._message_content(flat)
        self.assertIn("roadmap.pdf (application/pdf)", content)
        self.assertNotIn("secret.example", content)

    def test_single_dict_attachment_is_normalized_like_a_list(self):
        flat = gchat_source._normalize_raw_message(raw_msg(
            "t1", "2026-08-04T07:30:00Z", "",
            attachment={
                "contentName": "notes.txt",
                "contentType": "text/plain",
            },
        ))
        self.assertIn(
            "notes.txt (text/plain)", gchat_source._message_content(flat)
        )

    def test_new_reaction_changes_candidate_version(self):
        plain = {"messages": [raw_msg("t1", "2026-08-04T07:30:00Z", "done")]}
        hearted = {"messages": [raw_msg(
            "t1", "2026-08-04T07:30:00Z", "done",
            emojiReactionSummaries=[
                {"emoji": {"unicode": "♥️"}, "reactionCount": 1},
            ],
        )]}
        source = {"spaces": ["spaces/AAA"], "batch_messages": "daily"}
        with mock.patch.object(gchat_source, "_gws",
                               side_effect=[plain, plain, hearted, hearted]):
            before = gchat_source.candidates(source)[0]
            after = gchat_source.candidates(source)[0]
        self.assertEqual(
            before["raw"]["source_id"], after["raw"]["source_id"]
        )
        self.assertNotEqual(before["id"], after["id"])

    def test_fetch_renders_reactions_and_quoted_context(self):
        messages = {"messages": [raw_msg(
            "t1", "2026-08-04T07:30:00Z", "done",
            emojiReactionSummaries=[
                {"emoji": {"unicode": "♥️"}, "reactionCount": 2},
            ],
            quotedMessageMetadata={
                "quotedMessageSnapshot": {
                    "text": "can you add some bullets to this slide?",
                },
            },
        )]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            candidate = gchat_source.candidates({"spaces": ["spaces/AAA"]})[0]
        with mock.patch.object(gchat_source, "_member_context", return_value={
            "names": {}, "members": [], "people": {},
        }), mock.patch.object(gchat_source, "_space_context", return_value={}):
            item = gchat_source.fetch({}, candidate)
        self.assertIn(
            '[in reply to: "can you add some bullets to this slide?"]',
            item["body"],
        )
        self.assertIn("[reactions: ♥️ x2]", item["body"])
        self.assertEqual(item["frontmatter"]["reaction_count"], 2)


class ConfigTest(unittest.TestCase):
    @staticmethod
    def routine(source):
        return {
            "id": "gchat-sweep",
            "enabled": True,
            "source": {"kind": "gchat", **source},
            "analyze": {
                "provider": "gemini",
                "model": "gemini/example",
                "instruction": "Keep durable facts.",
            },
            "memory": {"store": "/tmp/memory", "type": "note"},
        }

    def test_all_spaces_accepts_unlimited_recent_results(self):
        problems = config.validate(self.routine({
            "all_spaces": True,
            "max_results": 0,
            "max_per_space": 0,
        }))
        self.assertEqual(problems, [])

    def test_gchat_requires_exactly_one_scope(self):
        missing = config.validate(self.routine({}))
        both = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "all_spaces": True,
        }))
        self.assertTrue(any("exactly one" in problem for problem in missing))
        self.assertTrue(any("exactly one" in problem for problem in both))

    def test_max_per_space_is_only_for_all_space_sweeps(self):
        problems = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "max_per_space": 0,
        }))
        self.assertTrue(any("requires `all_spaces: true`" in problem for problem in problems))

    def test_gchat_batch_modes_are_mutually_exclusive(self):
        problems = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "batch_unthreaded": "daily",
            "batch_messages": "daily",
        }))
        self.assertTrue(any("set batch_unthreaded or batch_messages" in p for p in problems))

    def test_batch_messages_after_requires_daily_mode_and_rfc3339(self):
        without_mode = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "batch_messages_after": "2026-07-27T09:00:00Z",
        }))
        malformed = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "batch_messages": "daily",
            "batch_messages_after": "yesterday",
        }))
        iso_space = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "batch_messages": "daily",
            "batch_messages_after": "2026-07-27 09:00:00+00:00",
        }))
        compact_offset = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "batch_messages": "daily",
            "batch_messages_after": "2026-07-27T09:00:00+0000",
        }))
        valid = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "batch_messages": "daily",
            "batch_messages_after": "2026-07-27T09:00:00Z",
        }))

        self.assertTrue(any("requires batch_messages: daily" in p for p in without_mode))
        self.assertTrue(any("quoted RFC3339" in p for p in malformed))
        self.assertTrue(any("quoted RFC3339" in p for p in iso_space))
        self.assertTrue(any("quoted RFC3339" in p for p in compact_offset))
        self.assertEqual(valid, [])

    def test_catch_up_requires_uncapped_all_space_daily_batch(self):
        valid = config.validate(self.routine({
            "all_spaces": True,
            "batch_messages": "daily",
            "catch_up": True,
            "catch_up_overlap": "1h",
            "max_results": 0,
            "max_per_space": 0,
        }))
        invalid = config.validate(self.routine({
            "spaces": ["spaces/AAA"],
            "catch_up": True,
            "catch_up_overlap": "soon",
            "max_results": 50,
        }))

        self.assertEqual(valid, [])
        self.assertTrue(any("requires all_spaces: true" in p for p in invalid))
        self.assertTrue(any("requires batch_messages: daily" in p for p in invalid))
        self.assertTrue(any("requires max_results: 0" in p for p in invalid))
        self.assertTrue(any("requires max_per_space: 0" in p for p in invalid))
        self.assertTrue(any("catch_up_overlap must look like" in p for p in invalid))


class FetchTest(unittest.TestCase):
    def setUp(self):
        gchat_source._member_cache.clear()
        gchat_source._space_cache.clear()

    def _candidate(self):
        with mock.patch.object(gchat_source, "_gws", return_value=MESSAGES):
            out = gchat_source.candidates({"spaces": ["spaces/AAA"]})
        return next(c for c in out if c["raw"]["source_id"] == "gchat:AAA:t1")

    def test_renders_conversation_with_names(self):
        cand = self._candidate()
        with mock.patch.object(
            gchat_source, "_gws",
            side_effect=lambda args: SPACE if args[1] == "get-space" else MEMBERS,
        ):
            item = gchat_source.fetch({}, cand)
        self.assertEqual(item["source_id"], "gchat:AAA:t1")
        self.assertIn("Jane Doe: first", item["body"])
        self.assertIn("John Smith: reply", item["body"])
        self.assertIn("[2026-07-27T08:00:00Z]", item["body"])
        self.assertEqual(item["date"], "2026-07-27")
        self.assertEqual(item["frontmatter"]["message_count"], 2)
        self.assertEqual(
            item["frontmatter"]["latest_message_at"],
            "2026-07-27T09:00:00Z",
        )
        self.assertEqual(item["frontmatter"]["gchat_space_type"], "DIRECT_MESSAGE")
        self.assertEqual(
            item["frontmatter"]["gchat_space_members"], ["Jane Doe", "John Smith"]
        )
        self.assertEqual(item["frontmatter"]["gchat_space_member_count"], 2)
        self.assertEqual(
            item["frontmatter"]["source_people"],
            [
                {
                    "email": "jane@example.com",
                    "name": "Jane Doe",
                    "role": "gchat-member",
                },
                {
                    "email": "john@example.com",
                    "name": "John Smith",
                    "role": "gchat-member",
                },
            ],
        )

    def test_large_space_exposes_only_actual_senders_as_identity_candidates(self):
        cand = self._candidate()
        members = [
            {
                "user": f"users/{index}",
                "display_name": f"Person {index}",
                "email": f"person{index}@example.com",
            }
            for index in range(25)
        ]
        space = {
            "name": "spaces/AAA",
            "display_name": "Large room",
            "type": "SPACE",
        }
        with mock.patch.object(
            gchat_source, "_gws",
            side_effect=lambda args: space if args[1] == "get-space" else members,
        ):
            item = gchat_source.fetch({}, cand)
        self.assertEqual(
            item["frontmatter"]["source_people"],
            [
                {
                    "email": "person1@example.com",
                    "name": "Person 1",
                    "role": "gchat-member",
                },
                {
                    "email": "person2@example.com",
                    "name": "Person 2",
                    "role": "gchat-member",
                },
            ],
        )

    def test_large_space_keeps_count_without_copying_roster_into_item(self):
        cand = self._candidate()
        members = [
            {"user": f"users/{index}", "display_name": f"Person {index}"}
            for index in range(25)
        ]
        space = {
            "name": "spaces/AAA",
            "display_name": "Large room",
            "type": "SPACE",
        }
        with mock.patch.object(
            gchat_source, "_gws",
            side_effect=lambda args: space if args[1] == "get-space" else members,
        ):
            item = gchat_source.fetch({}, cand)
        self.assertEqual(item["frontmatter"]["gchat_space_members"], [])
        self.assertEqual(item["frontmatter"]["gchat_space_member_count"], 25)

    def test_member_lookup_failure_keeps_ids(self):
        cand = self._candidate()
        with mock.patch.object(gchat_source, "_gws", side_effect=RuntimeError("nope")), \
             mock.patch.object(gchat_source, "log"):
            item = gchat_source.fetch({}, cand)
        self.assertIn("users/1: first", item["body"])

    def test_sink_uses_stable_source_id(self):
        cand = self._candidate()
        with mock.patch.object(gchat_source, "_gws", return_value=MEMBERS):
            item = gchat_source.fetch({}, cand)
        self.assertEqual(memory_sink.source_id_for(item), "gchat:AAA:t1")

    def test_redacts_verification_code_before_rendering(self):
        messages = {"messages": [
            msg(
                "t1",
                "2026-07-27T08:00:00Z",
                "Account verification code: 29423921",
            ),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            candidate = gchat_source.candidates({"spaces": ["spaces/AAA"]})[0]
        with mock.patch.object(
            gchat_source,
            "_gws",
            side_effect=lambda args: SPACE if args[1] == "get-space" else MEMBERS,
        ):
            item = gchat_source.fetch({}, candidate)
        self.assertNotIn("29423921", item["title"])
        self.assertNotIn("29423921", item["body"])
        self.assertIn("[REDACTED]", item["body"])


class SlackVersionedIdTest(unittest.TestCase):
    """Bug 2 regression: replies must produce a new candidate id, same source id."""

    HISTORY_V1 = {"ok": True, "messages": [
        {"source_id": "slack:C1:100.0", "ts": "100.0", "text": "root"},
    ]}
    HISTORY_V2 = {"ok": True, "messages": [
        {"source_id": "slack:C1:100.0", "ts": "105.0", "text": "a reply"},
        {"source_id": "slack:C1:100.0", "ts": "100.0", "text": "root"},
    ]}

    def _candidates(self, history):
        with mock.patch.object(slack_source, "_cli", return_value=history):
            return slack_source.candidates({"channels": ["C1"]})

    def test_reply_changes_candidate_id_not_source_id(self):
        v1 = self._candidates(self.HISTORY_V1)[0]
        v2 = self._candidates(self.HISTORY_V2)[0]
        self.assertNotEqual(v1["id"], v2["id"])                    # ledger reprocesses
        self.assertEqual(v1["raw"]["source_id"], v2["raw"]["source_id"])  # store updates
        self.assertEqual(v2["id"], "slack:C1:100.0@105.0")

    def test_fetch_exposes_stable_source_id(self):
        cand = self._candidates(self.HISTORY_V2)[0]
        thread = {"ok": True, "messages": [
            {"ts": "100.0", "user": "U1", "text": "root"},
            {"ts": "105.0", "user": "U2", "text": "a reply"},
        ]}
        whois = {"ok": True, "users": {"U1": {"real_name": "A"}, "U2": {"real_name": "B"}}}
        with mock.patch.object(slack_source, "_cli",
                               side_effect=[thread, whois]):
            item = slack_source.fetch({}, cand)
        self.assertEqual(item["source_id"], "slack:C1:100.0")
        self.assertEqual(memory_sink.source_id_for(item), "slack:C1:100.0")

    def test_fetch_dates_updated_thread_by_latest_reply(self):
        root = "1780835023.379039"   # 2026-06-07
        latest = "1783425442.544349"  # 2026-07-07
        candidate = {
            "id": f"slack:C1:{root}@{latest}",
            "title": "a later decision",
            "raw": {
                "channel": "C1",
                "anchor": root,
                "source_id": f"slack:C1:{root}",
                "mode": "thread",
            },
        }
        thread = {"ok": True, "messages": [
            {"ts": latest, "user": "U2", "text": "decision"},
            {"ts": root, "user": "U1", "text": "root"},
        ]}
        whois = {
            "ok": True,
            "users": {
                "U1": {"real_name": "A"},
                "U2": {"real_name": "B"},
            },
        }
        with mock.patch.object(
            slack_source, "_cli", side_effect=[thread, whois]
        ):
            item = slack_source.fetch({}, candidate)

        self.assertEqual(item["date"], "2026-07-07")
        self.assertEqual(
            item["frontmatter"]["first_message_at"],
            "2026-06-07T12:23:43.379Z",
        )
        self.assertEqual(
            item["frontmatter"]["latest_message_at"],
            "2026-07-07T11:57:22.544Z",
        )


if __name__ == "__main__":
    unittest.main()
