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
    {"user": "users/1", "display_name": "Jane Doe"},
    {"user": "users/2", "display_name": "John Smith"},
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
        self.assertEqual(by_sid["gchat:AAA:t1"]["id"],
                         "gchat:AAA:t1@2026-07-27T09:00:00Z")
        self.assertEqual(by_sid["gchat:AAA:t1"]["title"], "first")

    def test_empty_window(self):
        with mock.patch.object(gchat_source, "_gws",
                               return_value={"count": 0, "messages": None}):
            self.assertEqual(gchat_source.candidates({"spaces": ["spaces/AAA"]}), [])

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
        self.assertTrue(digest["id"].endswith("@2026-07-27T10:01:00Z"))

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
        with mock.patch.object(gchat_source, "_gws", return_value=messages) as gws:
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

    def test_all_spaces_recovers_space_from_message_name(self):
        messages = {"messages": [
            msg("t1", "2026-07-27T08:00:00Z", "hello"),
        ]}
        with mock.patch.object(gchat_source, "_gws", return_value=messages):
            out = gchat_source.candidates({"all_spaces": True})
        self.assertEqual(out[0]["raw"]["space"], "spaces/AAA")


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


if __name__ == "__main__":
    unittest.main()
