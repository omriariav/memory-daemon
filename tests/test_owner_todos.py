"""Meeting owner todos: prompt, strict validation, stable ids, backfill."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import fibery_tasks_outbound, memory_sink, owner_todos

OWNER = "me-example"
MEETING_SID = "mila:REC-1"


def meeting_extraction(actions, **extra):
    data = {
        "worthy": True, "owner_attention": False, "type": "meeting",
        "title": "Planning sync", "people": [], "tags": ["planning"],
        "follows": [], "body": "Meeting body with action items.",
    }
    if actions is not None:
        data["owner_actions"] = actions
    data.update(extra)
    return data


def make_routine(store="/store", **todos):
    block = {"enabled": True, "owner_slug": OWNER}
    block.update(todos)
    return {
        "id": "meetings",
        "analyze": {"provider": "claude", "model": "haiku"},
        "memory": {
            "store": store, "type": "note",
            "tags": ["meeting", "transcription", "mila", "work"],
            "owner_todos": block,
        },
    }


def meeting_item():
    return {
        "id": "mila:REC-1@abc", "source_id": MEETING_SID, "source_kind": "mila",
        "title": "Planning sync", "date": "2026-10-01", "body": "transcript",
        "frontmatter": {"mila_recording_id": "REC-1"},
    }


class FakeStore:
    """Fake store CLI: update in place by source id, record every call."""

    def __init__(self):
        self.entries = {}
        self.calls = []

    def cli(self, store, args, stdin_text=None, timeout=120):
        self.calls.append((args, stdin_text))
        flags = dict(zip(args[1::2], args[2::2]))
        sid = flags["--source-ids"]
        verdict = "updated" if sid in self.entries else "created"
        self.entries[sid] = {"args": args, "flags": flags, "body": stdin_text}
        eid = f"e{len(self.entries)}-{abs(hash(sid)) % 1000}"
        return mock.Mock(returncode=0, stdout=f"✓ {verdict} {eid}\n", stderr="")

    def todos(self):
        return {k: v for k, v in self.entries.items() if "#action-" in k}


class CaptureTest(unittest.TestCase):
    def setUp(self):
        memory_sink._slug_cache["/store"] = set()
        self.store = FakeStore()
        self.routine = make_routine()

    def capture(self, extraction, item=None, routine=None):
        unsafe = {"email": None, "person": None, "safe": False}
        with mock.patch.object(memory_sink, "_extract", return_value=extraction), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value=unsafe), \
                mock.patch.object(memory_sink, "_cli", side_effect=self.store.cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log") as log:
            out = memory_sink.capture(
                routine or self.routine, item or meeting_item(), "summary"
            )
        self.logged = " ".join(str(c.args[0]) for c in log.call_args_list)
        return out

    def test_owner_actions_become_todos_with_stable_ids(self):
        out = self.capture(meeting_extraction([
            {"text": "Send the revised plan to the team",
             "due": "2026-10-08", "evidence_ts": "12:03"},
            {"text": "Book the review room"},
        ]))
        self.assertEqual(out["memory_owner_todos"], 2)
        todos = self.store.todos()
        self.assertEqual(len(todos), 2)
        sid = owner_todos.action_source_id(
            MEETING_SID, "Send the revised plan to the team")
        self.assertRegex(sid, r"^mila:REC-1#action-[0-9a-f]{10}$")
        row = todos[sid]
        self.assertEqual(row["flags"]["--type"], "todo")
        self.assertEqual(row["flags"]["--title"],
                         "Send the revised plan to the team")
        self.assertEqual(row["flags"]["--people"], OWNER)
        self.assertEqual(row["flags"]["--date"], "2026-10-01")
        tags = row["flags"]["--tags"].split(",")
        self.assertIn("from-meeting", tags)
        self.assertIn("work", tags)
        self.assertNotIn("mila", tags)
        self.assertNotIn("meeting", tags)
        self.assertIn("12:03", row["body"])
        self.assertIn("due 2026-10-08", row["body"])
        self.assertIn("meeting-owner-action", row["body"])
        self.assertEqual(len(row["body"].splitlines()), 1)
        meeting = self.store.entries[MEETING_SID]
        self.assertEqual(meeting["flags"]["--type"], "meeting")

    def test_reprocessing_updates_instead_of_duplicating(self):
        self.capture(meeting_extraction([{"text": "Send the revised plan."}]))
        first = set(self.store.todos())
        out = self.capture(meeting_extraction([
            {"text": "  send the REVISED plan  "}]))
        self.assertEqual(set(self.store.todos()), first)
        self.assertEqual(out["memory_owner_todos"], 1)
        adds = [a for a, _ in self.store.calls if "#action-" in " ".join(a)]
        self.assertEqual(len(adds), 2)
        self.assertIn("updated", self.logged)

    def test_dropped_action_is_never_deleted(self):
        self.capture(meeting_extraction([{"text": "Send the revised plan"}]))
        before = dict(self.store.todos())
        self.capture(meeting_extraction([]))
        self.assertEqual(self.store.todos(), before)
        self.assertTrue(all(a[0] == "add" for a, _ in self.store.calls))
        self.assertIn("earlier todos for this meeting are kept", self.logged)

    def test_invalid_actions_save_meeting_but_no_todos(self):
        for bad in (
            "not a list", [{"text": ""}], [{"text": "x", "owner": "bob"}],
            [{"text": "x", "due": "tomorrow"}], ["plain string"],
            [{"text": "ok"}, {"text": 5}], [{"text": "y" * 500}],
        ):
            with self.subTest(bad=bad):
                self.store = FakeStore()
                out = self.capture(meeting_extraction(bad))
                self.assertEqual(out["memory"], "created")
                self.assertNotIn("memory_owner_todos", out)
                self.assertEqual(self.store.todos(), {})
                self.assertIn("invalid owner_actions", self.logged)
                self.assertNotIn(str(bad), self.logged)

    def test_missing_key_means_no_todos(self):
        out = self.capture(meeting_extraction(None))
        self.assertEqual(out["memory"], "created")
        self.assertEqual(self.store.todos(), {})

    def test_max_per_item_caps(self):
        actions = [{"text": f"Do thing number {n}"} for n in range(9)]
        out = self.capture(meeting_extraction(actions),
                           routine=make_routine(max_per_item=3))
        self.assertEqual(out["memory_owner_todos"], 3)

    def test_disabled_by_default(self):
        routine = make_routine(enabled=False)
        self.capture(meeting_extraction([{"text": "Do it"}]), routine=routine)
        self.assertEqual(self.store.todos(), {})
        del routine["memory"]["owner_todos"]
        self.capture(meeting_extraction([{"text": "Do it"}]), routine=routine)
        self.assertEqual(self.store.todos(), {})

    def test_non_meeting_entry_gets_no_todos(self):
        self.capture(meeting_extraction([{"text": "Do it"}], type="note"))
        self.assertEqual(self.store.todos(), {})

    def test_personal_meeting_todos_inherit_personal_tag(self):
        self.capture(meeting_extraction(
            [{"text": "Call the clinic back"}], personal=True,
            personal_reason="health",
        ))
        row = next(iter(self.store.todos().values()))
        self.assertIn("personal", row["flags"]["--tags"].split(","))
        meeting = self.store.entries[MEETING_SID]
        self.assertIn("personal", meeting["flags"]["--tags"].split(","))

    def test_work_meeting_todos_are_not_personal(self):
        self.capture(meeting_extraction(
            [{"text": "Send the plan"}], personal=False))
        row = next(iter(self.store.todos().values()))
        self.assertNotIn("personal", row["flags"]["--tags"].split(","))

    def test_ids_in_title_are_redacted(self):
        self.capture(meeting_extraction(
            [{"text": "Send form for SSN 123-45-6789 to payroll"}]))
        row = next(iter(self.store.todos().values()))
        self.assertNotIn("123-45-6789", row["flags"]["--title"])

    def test_owner_slug_from_verified_identity(self):
        routine = make_routine()
        del routine["memory"]["owner_todos"]["owner_slug"]
        safe = {"email": "me@example.test", "safe": True,
                "person": {"slug": "verified-me", "name": "Verified Me",
                           "resource_name": "people/1"}}
        with mock.patch.object(memory_sink, "_extract",
                               return_value=meeting_extraction(
                                   [{"text": "Send the plan"}])), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value=safe), \
                mock.patch.object(memory_sink, "_prefetch_identities"), \
                mock.patch.object(memory_sink, "_verified_source_people",
                                  return_value=([], [])), \
                mock.patch.object(memory_sink, "_verified_owner_people",
                                  return_value=([], [])), \
                mock.patch.object(memory_sink, "known_person_slugs",
                                  return_value=set()), \
                mock.patch.object(memory_sink, "_cli", side_effect=self.store.cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            memory_sink.capture(routine, meeting_item(), "summary")
        row = next(iter(self.store.todos().values()))
        self.assertEqual(row["flags"]["--people"], "verified-me")

    def test_unknown_owner_files_nothing(self):
        routine = make_routine()
        del routine["memory"]["owner_todos"]["owner_slug"]
        self.capture(meeting_extraction([{"text": "Send the plan"}]),
                     routine=routine)
        self.assertEqual(self.store.todos(), {})
        self.assertIn("owner slug unknown", self.logged)

    def test_todo_add_failure_raises_for_retry(self):
        def cli(store, args, stdin_text=None, timeout=120):
            if "#action-" in " ".join(args):
                return mock.Mock(returncode=1, stdout="", stderr="boom")
            return self.store.cli(store, args, stdin_text, timeout)

        self.store.cli  # keep the meeting add working
        with mock.patch.object(memory_sink, "_verify_written_entry",
                               return_value=None), \
                mock.patch.object(memory_sink, "_extract",
                                  return_value=meeting_extraction(
                                      [{"text": "Send the plan"}])), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value={"safe": False}), \
                mock.patch.object(memory_sink, "_cli", side_effect=cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            with self.assertRaises(RuntimeError):
                memory_sink.capture(self.routine, meeting_item(), "summary")


class PromptTest(unittest.TestCase):
    def prompt(self, routine, item):
        reply = json.dumps(meeting_extraction([]))
        with mock.patch("workspace_daemon.llm.analyze",
                        return_value=reply) as analyze:
            memory_sink._extract(routine, item, "summary", "/store",
                                 identity={"safe": False})
        return analyze.call_args.args[1]

    def test_prompt_asks_for_owner_actions_only_for_meetings(self):
        prompt = " ".join(self.prompt(make_routine(), meeting_item()).split())
        self.assertIn('"owner_actions"', prompt)
        self.assertIn("at most 5", prompt)
        self.assertIn("explicitly committed to", prompt)
        self.assertIn("Never other people's actions", prompt)
        self.assertIn("hints are candidates only", prompt)
        chat = dict(meeting_item(), source_kind="slack_mcp", frontmatter={})
        self.assertNotIn("owner_actions", self.prompt(make_routine(), chat))
        off = make_routine(enabled=False)
        self.assertNotIn("owner_actions", self.prompt(off, meeting_item()))

    def test_parse_extraction_tolerates_owner_actions(self):
        raw = json.dumps(meeting_extraction([{"text": "x"}]))
        self.assertEqual(
            memory_sink._parse_extraction(raw)["owner_actions"],
            [{"text": "x"}])
        raw = json.dumps(meeting_extraction("garbage"))
        self.assertEqual(
            memory_sink._parse_extraction(raw)["owner_actions"], "garbage")


class ConfigAndOutboundTest(unittest.TestCase):
    def test_validation(self):
        good = {"store": "/s", "owner_todos": {
            "enabled": True, "max_per_item": 5, "tag": "from-meeting"}}
        self.assertEqual(owner_todos.validate(good, "r"), [])
        for bad in (
            {"enabled": "yes"}, {"max_per_item": 0}, {"max_per_item": True},
            {"tag": "Bad Tag"}, {"surprise": 1},
        ):
            self.assertTrue(owner_todos.validate({"owner_todos": bad}, "r"), bad)
        self.assertTrue(owner_todos.validate({"owner_todos": []}, "r"))
        self.assertEqual(
            memory_sink.validate({"id": "r", "memory": good}), [])

    def test_outbound_accepts_action_source_ids(self):
        sid = owner_todos.action_source_id(MEETING_SID, "Send the plan")
        entry = {
            "id": "send-the-plan", "type": "todo", "date": "2026-10-01",
            "title": "Send the plan", "people": [OWNER],
            "tags": ["from-meeting", "auto-captured"], "source_ids": [sid],
        }
        cfg = {"owner_slug": OWNER, "outbound_since": "2026-09-01",
               "from_sources": ["mila:"]}
        self.assertTrue(fibery_tasks_outbound.eligible(entry, cfg))
        personal = dict(entry, tags=["from-meeting", "personal"])
        self.assertFalse(fibery_tasks_outbound.eligible(personal, cfg))


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        entries = self.root / "memory" / "entries"
        entries.mkdir(parents=True)
        (entries / "m1.md").write_text(
            "---\nid: planning-sync\ntype: meeting\ndate: '2026-10-01'\n"
            "title: Planning sync\ntags: [work]\npeople: [bob-example]\n"
            f"source_ids: ['{MEETING_SID}']\n---\n"
            "Action items: Me sends the plan. Bob books the room.\n"
        )
        (entries / "n1.md").write_text(
            "---\nid: a-note\ntype: note\ndate: '2026-10-01'\n"
            "title: A note\nsource_ids: ['gmail:1']\n---\nbody\n"
        )
        self.routine = make_routine(store=str(self.root))
        self.store = FakeStore()

    def run_backfill(self, entry, reply, dry_run=False):
        with mock.patch("workspace_daemon.llm.analyze",
                        return_value=reply) as analyze, \
                mock.patch.object(memory_sink, "_cli", side_effect=self.store.cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            count = memory_sink.backfill_owner_todos(
                self.routine, entry, dry_run=dry_run)
        return count, analyze

    def test_dry_run_counts_and_writes_nothing(self):
        reply = json.dumps({"owner_actions": [{"text": "Send the plan"}]})
        count, analyze = self.run_backfill("planning-sync", reply, dry_run=True)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.calls, [])
        prompt = analyze.call_args.args[1]
        self.assertIn("Me sends the plan", prompt)

    def test_real_run_uses_same_ids_as_capture(self):
        reply = json.dumps({"owner_actions": [
            {"text": "Send the plan"}, {"text": "Send the plan!"}]})
        count, _ = self.run_backfill("planning-sync", reply)
        self.assertEqual(count, 1)
        self.assertEqual(
            set(self.store.todos()),
            {owner_todos.action_source_id(MEETING_SID, "Send the plan")})
        count, _ = self.run_backfill("planning-sync", reply)
        self.assertEqual(len(self.store.todos()), 1)

    def test_personal_meeting_entry_stays_personal(self):
        path = self.root / "memory" / "entries" / "m1.md"
        path.write_text(path.read_text().replace(
            "tags: [work]", "tags: [work, personal]"))
        self.run_backfill("planning-sync", json.dumps(
            {"owner_actions": [{"text": "Send the plan"}]}))
        row = next(iter(self.store.todos().values()))
        self.assertIn("personal", row["flags"]["--tags"].split(","))

    def test_rejects_missing_non_meeting_and_invalid(self):
        good = json.dumps({"owner_actions": []})
        with self.assertRaises(RuntimeError):
            self.run_backfill("nope", good)
        with self.assertRaises(RuntimeError):
            self.run_backfill("a-note", good)
        with self.assertRaises(RuntimeError):
            self.run_backfill("planning-sync", "not json")
        with self.assertRaises(RuntimeError):
            self.run_backfill("planning-sync", json.dumps(
                {"owner_actions": [{"text": ""}]}))
        self.assertEqual(self.store.calls, [])

    def test_requires_owner_todos_block(self):
        del self.routine["memory"]["owner_todos"]
        with self.assertRaises(RuntimeError):
            self.run_backfill("planning-sync", "{}")


if __name__ == "__main__":
    unittest.main()
