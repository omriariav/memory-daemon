"""gchat_source: thread grouping, version-aware ids, rendering; slack id versioning."""
import unittest
from unittest import mock

from workspace_daemon import gchat_source, memory_sink, slack_source


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


class CandidatesTest(unittest.TestCase):
    def setUp(self):
        gchat_source._member_cache.clear()

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


class FetchTest(unittest.TestCase):
    def setUp(self):
        gchat_source._member_cache.clear()

    def _candidate(self):
        with mock.patch.object(gchat_source, "_gws", return_value=MESSAGES):
            out = gchat_source.candidates({"spaces": ["spaces/AAA"]})
        return next(c for c in out if c["raw"]["source_id"] == "gchat:AAA:t1")

    def test_renders_conversation_with_names(self):
        cand = self._candidate()
        with mock.patch.object(gchat_source, "_gws", return_value=MEMBERS):
            item = gchat_source.fetch({}, cand)
        self.assertEqual(item["source_id"], "gchat:AAA:t1")
        self.assertIn("Jane Doe: first", item["body"])
        self.assertIn("John Smith: reply", item["body"])
        self.assertEqual(item["date"], "2026-07-27")
        self.assertEqual(item["frontmatter"]["message_count"], 2)

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
