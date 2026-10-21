"""Meeting owner todos: prompt, validation, grounding, stable ids, backfill."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import fibery_tasks_outbound, memory_sink, owner_todos

OWNER = "me-example"
NAME = "Me Example"
MEETING_SID = "mila:REC-1"
TRANSCRIPT = "\n".join([
    f"[00:12:03] {NAME}: I will send the revised plan to the team.",
    "[00:15:10] Bob Stone: I will book the review room.",
    "[00:16:00] Speaker 2: Someone should call the clinic back.",
    f"[00:17:00] {NAME}: Booking the room is on me too.",
])
TS = "00:12:03"


def meeting_extraction(actions, **extra):
    data = {
        "worthy": True, "owner_attention": True, "type": "meeting",
        "title": "Planning sync", "people": [], "tags": ["planning"],
        "follows": [], "body": "Meeting body with action items.",
    }
    if actions is not None:
        data["owner_actions"] = actions
    data.update(extra)
    return data


def act(text, ts=TS, **extra):
    return dict({"text": text, "evidence_ts": ts}, **extra)


def make_routine(store="/store", **todos):
    block = {"enabled": True, "owner_slug": OWNER, "owner_name": NAME}
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


def meeting_item(body=TRANSCRIPT):
    return {
        "id": "mila:REC-1@abc", "source_id": MEETING_SID, "source_kind": "mila",
        "title": "Planning sync", "date": "2026-10-01", "body": body,
        "frontmatter": {"mila_recording_id": "REC-1"},
    }


class FakeStore:
    """Fake store CLI: update in place by source id, record every call."""

    def __init__(self, todo_result=None):
        self.entries = {}
        self.calls = []
        self.todo_result = todo_result

    def cli(self, store, args, stdin_text=None, timeout=120):
        self.calls.append((args, stdin_text))
        flags = dict(zip(args[1::2], args[2::2]))
        sid = flags["--source-ids"]
        if self.todo_result and "#action-" in sid:
            code, text = self.todo_result
            return mock.Mock(returncode=code, stdout=text, stderr="")
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

    def capture(self, extraction, item=None, routine=None, identity=None):
        unsafe = identity or {"email": None, "person": None, "safe": False}
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
            act("Send the revised plan to the team", due="2026-10-08"),
            act("Book the review room", ts="00:17:00"),
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
        self.assertIn(TS, row["body"])
        self.assertIn("due 2026-10-08", row["body"])
        self.assertIn("meeting-owner-action", row["body"])
        self.assertEqual(len(row["body"].splitlines()), 1)
        self.assertNotIn("--force-new", row["args"])
        self.assertEqual(self.store.entries[MEETING_SID]["flags"]["--type"],
                         "meeting")

    def test_reprocessing_updates_instead_of_duplicating(self):
        self.capture(meeting_extraction([act("Send the revised plan.")]))
        first = set(self.store.todos())
        out = self.capture(meeting_extraction([
            act("  send the REVISED plan  ")]))
        self.assertEqual(set(self.store.todos()), first)
        self.assertEqual(out["memory_owner_todos"], 1)
        adds = [a for a, _ in self.store.calls if "#action-" in " ".join(a)]
        self.assertEqual(len(adds), 2)
        self.assertIn("updated", self.logged)

    def test_dropped_action_is_never_deleted(self):
        self.capture(meeting_extraction([act("Send the revised plan")]))
        before = dict(self.store.todos())
        self.capture(meeting_extraction([]))
        self.assertEqual(self.store.todos(), before)
        self.assertTrue(all(a[0] == "add" for a, _ in self.store.calls))
        self.assertIn("earlier todos for this meeting are kept", self.logged)

    def test_invalid_actions_save_meeting_but_no_todos(self):
        for bad in (
            "not a list", [{"text": ""}], [act("x", owner="bob")],
            [act("x", due="tomorrow")], ["plain string"],
            [{"text": "no timestamp"}], [act(5)], [act("y" * 500)],
        ):
            with self.subTest(bad=bad):
                self.store = FakeStore()
                out = self.capture(meeting_extraction(bad))
                self.assertEqual(out["memory"], "created")
                self.assertNotIn("memory_owner_todos", out)
                self.assertEqual(self.store.todos(), {})
                self.assertIn("owner-todos", self.logged)
                self.assertNotIn(str(bad), self.logged)

    def test_one_bad_element_keeps_the_rest(self):
        out = self.capture(meeting_extraction([
            act("Send the revised plan"), {"text": 5}, act("x", due="soon")]))
        self.assertEqual(out["memory_owner_todos"], 1)
        self.assertIn("dropped 2 invalid", self.logged)

    def test_missing_key_means_no_todos(self):
        out = self.capture(meeting_extraction(None))
        self.assertEqual(out["memory"], "created")
        self.assertEqual(self.store.todos(), {})

    def test_max_per_item_caps(self):
        actions = [act(f"Do thing number {n}") for n in range(9)]
        out = self.capture(meeting_extraction(actions),
                           routine=make_routine(max_per_item=3))
        self.assertEqual(out["memory_owner_todos"], 3)

    def test_disabled_by_default(self):
        routine = make_routine(enabled=False)
        self.capture(meeting_extraction([act("Do it")]), routine=routine)
        self.assertEqual(self.store.todos(), {})
        del routine["memory"]["owner_todos"]
        self.capture(meeting_extraction([act("Do it")]), routine=routine)
        self.assertEqual(self.store.todos(), {})

    def test_non_meeting_entry_gets_no_todos(self):
        self.capture(meeting_extraction([act("Do it")], type="note"))
        self.assertEqual(self.store.todos(), {})

    def test_owner_attention_denied_files_nothing(self):
        self.capture(meeting_extraction(
            [act("Send the revised plan")], owner_attention=False))
        self.assertEqual(self.store.todos(), {})

    def test_no_owner_marker_files_nothing(self):
        unsafe = {"email": None, "person": None, "safe": False}
        with mock.patch.object(memory_sink, "_extract", return_value=
                               meeting_extraction([act("Send the revised plan")])), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value=unsafe), \
                mock.patch.object(memory_sink, "_cli", side_effect=self.store.cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            memory_sink.capture(self.routine, meeting_item(),
                                memory_sink.NO_OWNER_ACTION_MARKER)
        self.assertEqual(self.store.todos(), {})

    def test_personal_meeting_todos_inherit_personal_tag(self):
        self.capture(meeting_extraction(
            [act("Send the revised plan")], personal=True,
            personal_reason="health",
        ))
        row = next(iter(self.store.todos().values()))
        self.assertIn("personal", row["flags"]["--tags"].split(","))
        meeting = self.store.entries[MEETING_SID]
        self.assertIn("personal", meeting["flags"]["--tags"].split(","))

    def test_work_meeting_todos_are_not_personal(self):
        self.capture(meeting_extraction(
            [act("Send the revised plan")], personal=False))
        row = next(iter(self.store.todos().values()))
        self.assertNotIn("personal", row["flags"]["--tags"].split(","))

    def test_ids_are_redacted_and_hash_uses_redacted_text(self):
        self.capture(meeting_extraction(
            [act("Send form for SSN 123-45-6789 to payroll")]))
        sid, row = next(iter(self.store.todos().items()))
        self.assertNotIn("123-45-6789", row["flags"]["--title"])
        self.assertEqual(sid, owner_todos.action_source_id(
            MEETING_SID, "Send form for SSN [ID] to payroll"))

    def test_owner_slug_and_name_from_verified_identity(self):
        routine = make_routine()
        del routine["memory"]["owner_todos"]["owner_slug"]
        del routine["memory"]["owner_todos"]["owner_name"]
        safe = {"email": "me@example.test", "safe": True,
                "person": {"slug": "verified-me", "name": "Verified Me",
                           "resource_name": "people/1"}}
        body = "[00:12:03] Verified Me: I will send the revised plan."
        with mock.patch.object(memory_sink, "_prefetch_identities"), \
                mock.patch.object(memory_sink, "_verified_source_people",
                                  return_value=([], [])), \
                mock.patch.object(memory_sink, "_verified_owner_people",
                                  return_value=([], [])), \
                mock.patch.object(memory_sink, "known_person_slugs",
                                  return_value=set()):
            self.capture(meeting_extraction([act("Send the revised plan")]),
                         item=meeting_item(body), routine=routine,
                         identity=safe)
        row = next(iter(self.store.todos().values()))
        self.assertEqual(row["flags"]["--people"], "verified-me")

    def test_unknown_owner_slug_or_name_fails_closed(self):
        for drop in ("owner_slug", "owner_name"):
            with self.subTest(drop=drop):
                self.store = FakeStore()
                routine = make_routine()
                del routine["memory"]["owner_todos"][drop]
                self.capture(meeting_extraction([act("Send the revised plan")]),
                             routine=routine)
                self.assertEqual(self.store.todos(), {})
                self.assertIn("owner slug or name unknown", self.logged)

    def test_ungrounded_timestamps_are_dropped(self):
        out = self.capture(meeting_extraction([
            act("Send the revised plan"),
            act("Invented commitment", ts="00:59:59"),
            act("Book the review room", ts="00:15:10"),   # Bob's line
            act("Call the clinic back", ts="00:16:00"),   # generic speaker
        ]))
        titles = {r["flags"]["--title"] for r in self.store.todos().values()}
        self.assertEqual(titles, {"Send the revised plan",
                                  "Call the clinic back"})
        self.assertEqual(out["memory_owner_todos"], 2)
        self.assertIn("2 ungrounded", self.logged)

    def test_timestamp_formats_are_matched(self):
        for ts in ("00:12:03", "12:03", "[00:12:03]", "00:12:03,000"):
            with self.subTest(ts=ts):
                self.assertTrue(owner_todos.grounded_in_transcript(
                    TRANSCRIPT, ts, [NAME]))
        self.assertFalse(owner_todos.grounded_in_transcript(
            TRANSCRIPT, "garbage", [NAME]))

    def test_alias_name_matches_speaker(self):
        body = "[00:01:00] M. Example: I will send the plan."
        self.assertTrue(owner_todos.grounded_in_transcript(
            body, "00:01:00", [NAME, "M. Example"]))
        self.assertFalse(owner_todos.grounded_in_transcript(
            body, "00:01:00", [NAME]))

    def test_todo_add_failure_never_raises_or_retries(self):
        self.store = FakeStore(todo_result=(1, "boom"))
        with mock.patch.object(memory_sink, "_verify_written_entry",
                               return_value=None):
            out = self.capture(meeting_extraction(
                [act("Send the revised plan")]))
        self.assertEqual(out["memory"], "created")
        self.assertEqual(out["memory_owner_todos"], 0)
        self.assertEqual(out["memory_owner_todos_error"], 1)
        self.assertNotIn("boom", self.logged)

    def test_near_duplicate_is_skipped_not_forced(self):
        self.store = FakeStore(todo_result=(
            2, "near-duplicate of weekly-report (0.95)"))
        with mock.patch.object(memory_sink, "_verify_written_entry",
                               return_value=None):
            out = self.capture(meeting_extraction(
                [act("Send the revised plan")]))
        self.assertEqual(out["memory_owner_todos"], 0)
        self.assertEqual(out["memory_owner_todos_duplicates"], 1)
        self.assertNotIn("memory_owner_todos_error", out)
        for args, _ in self.store.calls:
            self.assertNotIn("--force-new", args)

    def test_cli_exception_is_counted_not_raised(self):
        def cli(store, args, stdin_text=None, timeout=120):
            if "#action-" in " ".join(args):
                raise OSError("disk")
            return self.store.cli(store, args, stdin_text, timeout)

        with mock.patch.object(memory_sink, "_extract", return_value=
                               meeting_extraction([act("Send the revised plan")])), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value={"safe": False}), \
                mock.patch.object(memory_sink, "_cli", side_effect=cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            out = memory_sink.capture(self.routine, meeting_item(), "summary")
        self.assertEqual(out["memory_owner_todos_error"], 1)


class PromptTest(unittest.TestCase):
    def prompt(self, routine, item, **kwargs):
        reply = json.dumps(meeting_extraction([]))
        with mock.patch("workspace_daemon.llm.analyze",
                        return_value=reply) as analyze:
            memory_sink._extract(routine, item, "summary", "/store",
                                 identity={"safe": False}, **kwargs)
        return analyze.call_args.args[1]

    def test_prompt_asks_for_owner_actions_only_for_meetings(self):
        names = [NAME, "M. Example"]
        prompt = " ".join(self.prompt(
            make_routine(), meeting_item(), owner_names=names).split())
        self.assertIn('"owner_actions"', prompt)
        self.assertIn('"Me Example" / "M. Example"', prompt)
        self.assertNotIn("speaker mapping", prompt)
        self.assertIn("at most 5", prompt)
        self.assertIn("explicitly committed to", prompt)
        self.assertIn("Never other people's actions", prompt)
        self.assertIn("hints are candidates only", prompt)
        self.assertIn("(required)", prompt)
        chat = dict(meeting_item(), source_kind="slack_mcp", frontmatter={})
        self.assertNotIn("owner_actions", self.prompt(
            make_routine(), chat, owner_names=names))
        off = make_routine(enabled=False)
        self.assertNotIn("owner_actions", self.prompt(
            off, meeting_item(), owner_names=names))

    def test_no_owner_name_means_no_owner_actions_in_prompt(self):
        self.assertNotIn("owner_actions", self.prompt(
            make_routine(), meeting_item()))

    def test_existing_titles_are_offered_for_reuse(self):
        prompt = " ".join(self.prompt(
            make_routine(), meeting_item(), owner_names=[NAME],
            existing_titles=["Send the revised plan"]).split())
        self.assertIn('reuse that title verbatim', prompt)
        self.assertIn('"Send the revised plan"', prompt)

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
            "enabled": True, "max_per_item": 5, "tag": "from-meeting",
            "owner_name": NAME, "owner_names": ["M. Example"]}}
        self.assertEqual(owner_todos.validate(good, "r"), [])
        for bad in (
            {"enabled": "yes"}, {"max_per_item": 0}, {"max_per_item": True},
            {"tag": "Bad Tag"}, {"surprise": 1}, {"owner_name": ""},
            {"owner_names": "Me"}, {"owner_names": [1]},
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
        whisper = dict(entry, source_ids=["whisper:abc#action-0123456789"])
        self.assertFalse(fibery_tasks_outbound.eligible(whisper, cfg))


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
            f"Action items:\n- {NAME} sends the plan.\n- Bob books the room.\n"
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
            result = memory_sink.backfill_owner_todos(
                self.routine, entry, dry_run=dry_run)
        return result["memory_owner_todos"], analyze

    def test_dry_run_counts_and_writes_nothing(self):
        reply = json.dumps({"owner_actions": [{"text": "Send the plan"}]})
        count, analyze = self.run_backfill("planning-sync", reply, dry_run=True)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.calls, [])
        prompt = analyze.call_args.args[1]
        self.assertIn(f"{NAME} sends the plan", prompt)
        self.assertNotIn("(required)", prompt)

    def test_real_run_uses_same_ids_as_capture(self):
        reply = json.dumps({"owner_actions": [
            {"text": "Send the plan"}, {"text": "Send the plan!"}]})
        count, _ = self.run_backfill("planning-sync", reply)
        self.assertEqual(count, 1)
        self.assertEqual(
            set(self.store.todos()),
            {owner_todos.action_source_id(MEETING_SID, "Send the plan")})
        self.run_backfill("planning-sync", reply)
        self.assertEqual(len(self.store.todos()), 1)

    def test_other_peoples_actions_are_not_grounded(self):
        reply = json.dumps({"owner_actions": [
            {"text": "Book the review room"}, {"text": "Send the plan"}]})
        count, _ = self.run_backfill("planning-sync", reply)
        self.assertEqual(count, 1)
        titles = {r["flags"]["--title"] for r in self.store.todos().values()}
        self.assertEqual(titles, {"Send the plan"})

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
                {"owner_actions": "nope"}))
        self.assertEqual(self.store.calls, [])

    def test_requires_owner_todos_block_and_name(self):
        del self.routine["memory"]["owner_todos"]["owner_name"]
        with mock.patch.object(memory_sink, "_authenticated_identity",
                               return_value={"safe": False}):
            with self.assertRaises(RuntimeError):
                self.run_backfill("planning-sync", "{}")
        del self.routine["memory"]["owner_todos"]
        with self.assertRaises(RuntimeError):
            self.run_backfill("planning-sync", "{}")


if __name__ == "__main__":
    unittest.main()
