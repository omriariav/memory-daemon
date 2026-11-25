"""Meeting owner todos: prompt, validation, grounding, stable ids, backfill."""
import fcntl
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import (action_review_queue, fibery_tasks_outbound,
                              memory_sink, owner_todos)

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
        self.ids = {}
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
        eid = self.ids.setdefault(sid, f"e{len(self.ids) + 1}")
        return mock.Mock(returncode=0, stdout=f"✓ {verdict} {eid}\n", stderr="")

    def todos(self):
        return {k: v for k, v in self.entries.items() if "#action-" in k}

    def reviews(self):
        return {k: v for k, v in self.entries.items() if ":review" in k}


class CaptureHarness(unittest.TestCase):
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


class CaptureTest(CaptureHarness):

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
            [act(5)], [act("y" * 500)],
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

    def test_entry_type_does_not_gate_the_filer(self):
        for etype in ("decision", "note", "meeting"):
            with self.subTest(etype):
                self.setUp()
                out = self.capture(meeting_extraction(
                    [act("Do it")], type=etype))
                self.assertEqual(out["memory_owner_todos"], 1)
                self.assertEqual(len(self.store.todos()), 1)

    def test_non_meeting_source_gets_no_todos(self):
        chat = dict(meeting_item(), source_kind="slack_mcp", frontmatter={})
        self.capture(meeting_extraction([act("Do it")], type="meeting"),
                     item=chat)
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
            act("Call the clinic back", ts="00:16:00"),   # unnamed, owner mapped
        ]))
        titles = {r["flags"]["--title"] for r in self.store.todos().values()}
        self.assertEqual(titles, {"Send the revised plan"})
        self.assertEqual(out["memory_owner_todos"], 1)
        self.assertIn("3 ungrounded", self.logged)

    def test_timestamp_formats_are_matched(self):
        for ts in ("00:12:03", "12:03", "[00:12:03]", "00:12:03,000"):
            with self.subTest(ts=ts):
                self.assertTrue(owner_todos.grounded_in_transcript(
                    TRANSCRIPT, ts, [NAME]))
        self.assertFalse(owner_todos.grounded_in_transcript(
            TRANSCRIPT, "garbage", [NAME]))

    def test_owner_mapped_requires_owner_line(self):
        body = (f"[00:00:05] {NAME}: I will send it.\n"
                "[00:00:09] SPEAKER_01: Please also fix the build.")
        self.assertTrue(owner_todos.grounded_in_transcript(
            body, "00:00:05", [NAME]))
        self.assertFalse(owner_todos.grounded_in_transcript(
            body, "00:00:09", [NAME]))

    def test_owner_mapped_in_recording_metadata_rejects_raw_keys(self):
        body = ("[00:00:05] SPEAKER_00: I will send it.\n"
                "[00:00:09] SPEAKER_01: Please also fix the build.")
        self.assertTrue(owner_todos.grounded_in_transcript(
            body, "00:00:09", [NAME]))   # owner unmapped: unnamed passes
        self.assertFalse(owner_todos.grounded_in_transcript(
            body, "00:00:09", [NAME], mapped_names=[NAME]))
        self.assertFalse(owner_todos.grounded_in_transcript(
            body, "00:00:05", [NAME], mapped_names=[NAME]))

    def test_owner_unmapped_fallback(self):
        body = ("[00:00:05] Bob Stone: I will send it.\n"
                "[00:00:09] SPEAKER_01: Sure.\n"
                "[00:00:11] No label here")
        self.assertFalse(owner_todos.grounded_in_transcript(
            body, "00:00:05", [NAME]))
        self.assertTrue(owner_todos.grounded_in_transcript(
            body, "00:00:09", [NAME]))
        self.assertTrue(owner_todos.grounded_in_transcript(
            body, "00:00:11", [NAME]))

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
        self.assertIn("leave the key out when no transcript line carries one",
                      prompt)
        self.assertIn("never 0", prompt)
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


def read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read().splitlines()


MIXED = [
    act("Send the revised plan to the team", due="2026-10-08"),
    act("Book the review room", ts="00:15:10"),
    {"text": "Call the clinic back", "due": "soon"},
]


class ReviewModeTest(CaptureHarness):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = os.path.join(self.tmp.name, "q", "candidates.jsonl")
        self.routine = make_routine(mode="review", review_queue=self.queue)

    def rows(self):
        try:
            with open(self.queue, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh.read().splitlines()]
        except FileNotFoundError:
            return []

    def test_one_review_todo_with_every_candidate(self):
        out = self.capture(meeting_extraction(MIXED, type="decision"))
        self.assertEqual(self.store.todos(), {})
        self.assertEqual(list(self.store.reviews()), ["mila:REC-1:review"])
        row = self.store.reviews()["mila:REC-1:review"]
        flags = row["flags"]
        self.assertEqual(flags["--type"], "todo")
        self.assertEqual(flags["--title"], "Review action items: Planning sync")
        self.assertEqual(flags["--people"], OWNER)
        self.assertEqual(flags["--date"], "2026-10-01")
        tags = flags["--tags"].split(",")
        for tag in ("from-meeting", "needs-review", "auto-captured"):
            self.assertIn(tag, tags)
        self.assertNotIn("--force-new", row["args"])
        body = row["body"]
        self.assertIn(f"From meeting {out['memory_entry_id']}", body)
        self.assertIn("due 2026-10-01", body)
        for text in ("Send the revised plan to the team",
                     "Book the review room", "Call the clinic back"):
            self.assertIn(text, body)
        self.assertIn("[unverified: ungrounded]", body)
        self.assertIn("[invalid: bad due date]", body)
        self.assertIn('line at timestamp: "Bob Stone: I will book the review '
                      'room."', body)
        self.assertNotIn('quote: "Bob', body)
        self.assertIn('quote: "Me Example: I will send the revised plan to '
                      'the team."', body)
        self.assertEqual(out["memory_owner_review"], 1)
        self.assertEqual(out["memory_owner_review_queue"], "added")

    def test_queue_row_fields(self):
        out = self.capture(meeting_extraction(MIXED, type="note"))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(list(row), [
            "v", "meeting_entry_id", "review_todo_id", "source_id",
            "source_kind", "recording_id", "title", "date", "at",
            "candidates"])
        self.assertEqual(row["v"], 1)
        self.assertEqual(row["meeting_entry_id"], out["memory_entry_id"])
        self.assertEqual(row["review_todo_id"], out["memory_owner_review_id"])
        self.assertEqual(row["source_id"], MEETING_SID)
        self.assertEqual(row["source_kind"], "mila")
        self.assertEqual(row["recording_id"], "REC-1")
        self.assertEqual(row["title"], "Planning sync")
        self.assertEqual(row["date"], "2026-10-01")
        self.assertRegex(row["at"], r"^\d{4}-\d\d-\d\dT")
        valid, ungrounded, invalid = row["candidates"]
        self.assertEqual(valid, {
            "text": "Send the revised plan to the team",
            "quote": "Me Example: I will send the revised plan to the team.",
            "due": "2026-10-08", "owner": OWNER, "status": "valid",
            "reason": None})
        self.assertEqual(ungrounded["status"], "ungrounded")
        self.assertIsNone(ungrounded["quote"])
        self.assertEqual(ungrounded["line_at_ts"],
                         "Bob Stone: I will book the review room.")
        self.assertTrue(ungrounded["reason"])
        self.assertNotIn("due", ungrounded)
        self.assertEqual(invalid, {
            "text": "Call the clinic back", "quote": None, "owner": OWNER,
            "status": "invalid", "reason": "bad due date"})

    def test_rerun_is_idempotent(self):
        self.capture(meeting_extraction(MIXED))
        first = self.rows()
        entries = dict(self.store.entries)
        out = self.capture(meeting_extraction(MIXED))
        self.assertEqual(self.rows()[0]["candidates"], first[0]["candidates"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(set(self.store.entries), set(entries))
        self.assertEqual(len(self.store.reviews()), 1)
        self.assertEqual(out["memory_owner_review_queue"], "unchanged")
        # a changed extraction replaces the row, never adds one
        out = self.capture(meeting_extraction([act("Send the revised plan")]))
        self.assertEqual(out["memory_owner_review_queue"], "replaced")
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["candidates"]), 1)

    def test_no_actions_files_nothing(self):
        for actions in ([], None):
            with self.subTest(actions):
                self.setUp()
                out = self.capture(meeting_extraction(actions))
                self.assertEqual(self.store.reviews(), {})
                self.assertEqual(self.store.todos(), {})
                self.assertFalse(os.path.exists(self.queue))
                self.assertNotIn("memory_owner_review", out)
                self.assertIn("0 candidates", self.logged)

    def test_non_list_files_nothing(self):
        self.capture(meeting_extraction("garbage"))
        self.assertEqual(self.store.reviews(), {})
        self.assertEqual(self.rows(), [])

    def test_only_invalid_candidates_still_reach_review(self):
        self.capture(meeting_extraction([{"text": "No date", "due": "soon"}]))
        self.assertEqual(len(self.store.reviews()), 1)
        self.assertEqual(self.rows()[0]["candidates"][0]["status"], "invalid")

    def test_gate_is_source_and_owner_attention(self):
        chat = dict(meeting_item(), source_kind="slack_mcp", frontmatter={})
        self.capture(meeting_extraction(MIXED), item=chat)
        self.capture(meeting_extraction(MIXED, owner_attention=False))
        self.assertEqual(self.store.reviews(), {})
        self.assertEqual(self.rows(), [])

    def test_unknown_owner_fails_closed(self):
        del self.routine["memory"]["owner_todos"]["owner_name"]
        self.capture(meeting_extraction(MIXED))
        self.assertEqual(self.store.reviews(), {})
        self.assertFalse(os.path.exists(self.queue))

    def test_logs_carry_no_candidate_text(self):
        self.capture(meeting_extraction(MIXED))
        for secret in ("revised plan", "review room", "clinic",
                       "I will send", "Bob Stone"):
            self.assertNotIn(secret, self.logged)
        self.assertIn("valid=1", self.logged)
        self.assertIn("ungrounded=1", self.logged)
        self.assertIn("invalid=1", self.logged)

    def test_max_per_item_caps_candidates(self):
        self.routine["memory"]["owner_todos"]["max_per_item"] = 2
        self.capture(meeting_extraction(MIXED))
        self.assertEqual(len(self.rows()[0]["candidates"]), 2)

    def test_default_queue_lives_in_the_state_dir(self):
        del self.routine["memory"]["owner_todos"]["review_queue"]
        with mock.patch.object(memory_sink, "llm_base_dir",
                               return_value=self.tmp.name):
            self.capture(meeting_extraction(MIXED))
        default = os.path.join(
            self.tmp.name, "state", "meeting-action-candidates.jsonl")
        self.assertTrue(os.path.exists(default))

    def test_missing_queue_path_still_files_the_todo(self):
        del self.routine["memory"]["owner_todos"]["review_queue"]
        with mock.patch.object(memory_sink, "llm_base_dir", return_value=None):
            out = self.capture(meeting_extraction(MIXED))
        self.assertEqual(len(self.store.reviews()), 1)
        self.assertEqual(out["memory_owner_review_queue"], "no_path")

    def test_todo_failure_writes_no_queue_row(self):
        real = self.store.cli

        def failing(store, args, stdin_text=None, timeout=120):
            flags = dict(zip(args[1::2], args[2::2]))
            if flags["--source-ids"].endswith(":review"):
                return mock.Mock(returncode=1, stdout="", stderr="boom")
            return real(store, args, stdin_text, timeout)

        self.store.cli = failing
        with mock.patch.object(memory_sink, "_verify_written_entry",
                               return_value=None):
            out = self.capture(meeting_extraction(MIXED))
        self.assertEqual(out["memory_owner_review_error"], 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(out["memory"], "created")

    def test_same_second_collision_quotes_the_owner_line(self):
        for lines in (
            ["[00:20:00] Bob Stone: Not me, I am busy.",
             f"[00:20:00] {NAME}: I will file the report."],
            [f"[00:20:00] {NAME}: I will file the report.",
             "[00:20:00] Bob Stone: Not me, I am busy."],
        ):
            with self.subTest(lines[0][:20]):
                self.setUp()
                item = meeting_item("\n".join(lines))
                self.capture(meeting_extraction(
                    [act("File the report", ts="00:20:00")]), item=item)
                cand, = self.rows()[0]["candidates"]
                self.assertEqual(cand["status"], "valid")
                self.assertEqual(
                    cand["quote"], f"{NAME}: I will file the report.")
                self.assertNotIn("line_at_ts", cand)

    def test_invalid_candidate_with_a_timestamp_gets_line_at_ts(self):
        self.capture(meeting_extraction([
            {"text": "Odd one", "evidence_ts": TS, "due": "soon"}]))
        cand, = self.rows()[0]["candidates"]
        self.assertEqual(cand["status"], "invalid")
        self.assertIsNone(cand["quote"])
        self.assertEqual(
            cand["line_at_ts"],
            f"{NAME}: I will send the revised plan to the team.")

    def test_valid_candidates_are_never_capped_away(self):
        body = "\n".join(
            f"[00:0{n}:00] {NAME}: I will do thing {n}." for n in range(1, 4))
        self.routine["memory"]["owner_todos"]["max_per_item"] = 2
        actions = [{"text": "Flagged first", "due": "soon"}] + [
            act(f"Do thing {n}", ts=f"00:0{n}:00") for n in range(1, 4)]
        self.capture(meeting_extraction(actions), item=meeting_item(body))
        cands = self.rows()[0]["candidates"]
        self.assertEqual([c["status"] for c in cands], ["valid"] * 3)
        self.assertIn("dropped 1 flagged", self.logged)
        # with room left the flagged tail fills it, valid first
        self.routine["memory"]["owner_todos"]["max_per_item"] = 4
        self.capture(meeting_extraction(actions), item=meeting_item(body))
        self.assertEqual([c["status"] for c in self.rows()[0]["candidates"]],
                         ["valid"] * 3 + ["invalid"])

    def test_near_duplicate_refusal_still_writes_the_row(self):
        real = self.store.cli

        def refusing(store, args, stdin_text=None, timeout=120):
            flags = dict(zip(args[1::2], args[2::2]))
            if flags["--source-ids"].endswith(":review"):
                return mock.Mock(returncode=2, stdout="",
                                 stderr="x this looks like a near-duplicate of:")
            return real(store, args, stdin_text, timeout)

        self.store.cli = refusing
        with mock.patch.object(memory_sink, "_verify_written_entry",
                               return_value=None):
            out = self.capture(meeting_extraction(MIXED))
        self.assertEqual(out["memory_owner_review"], 0)
        row, = self.rows()
        self.assertIsNone(row["review_todo_id"])
        self.assertEqual(len(row["candidates"]), 3)

    def test_u2028_in_a_title_cannot_split_the_row(self):
        item = dict(meeting_item(), title="Sync\u2028one\u0085two\u2029")
        extraction = meeting_extraction(
            MIXED, title="Sync\u2028one\u0085two\u2029")
        self.capture(extraction, item=item)
        with open(self.queue, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        self.assertEqual(len(raw.split("\n")), 2)  # one row + trailing ""
        self.assertEqual(len(raw.splitlines()), 1)
        self.assertEqual(self.rows()[0]["title"],
                         "Sync\u2028one\u0085two\u2029")
        self.capture(extraction, item=item)
        self.assertEqual(len(self.rows()), 1)

    def test_hand_logged_review_todo_is_not_overwritten(self):
        def guard(store, source_id, etype, rid=None):
            return (etype == "todo", ["hand-todo"] if etype == "todo" else [])

        with mock.patch.object(memory_sink, "capture_guard",
                               side_effect=guard):
            self.capture(meeting_extraction(MIXED))
        self.assertEqual(list(self.store.reviews()),
                         ["mila:REC-1:review:auto"])


class FileModeTest(CaptureHarness):
    def test_default_mode_is_file_and_unchanged(self):
        self.assertEqual(owner_todos.mode(self.routine), "file")
        with tempfile.TemporaryDirectory() as tmp:
            queue = os.path.join(tmp, "q.jsonl")
            routine = make_routine(review_queue=queue)
            out = self.capture(meeting_extraction(
                [act("Send the revised plan."), act("Book it", ts="00:15:10"),
                 {"text": "no stamp"}], type="decision"), routine=routine)
            self.assertFalse(os.path.exists(queue))
        self.assertEqual(out["memory_owner_todos"], 1)
        self.assertEqual(len(self.store.todos()), 1)
        self.assertEqual(self.store.reviews(), {})
        self.assertNotIn("memory_owner_review", out)

    def test_explicit_file_mode(self):
        routine = make_routine(mode="file")
        out = self.capture(meeting_extraction([act("Do it")], type="note"),
                           routine=routine)
        self.assertEqual(out["memory_owner_todos"], 1)

    def test_unknown_mode_falls_back_to_file(self):
        self.assertEqual(owner_todos.mode(make_routine(mode="x")), "file")


BAD_TS = (0, 0.0, "0", "00:00", "0:00:00", "[00:00:00]", "00:00:00,000",
          "garbage", "", None, True, float("nan"), [], {}, "125", -5, 0.4,
          86400, 1e12, float("inf"))


class UnusableTimestampTest(CaptureHarness):
    """An unusable evidence_ts (a recorder's own `timestamp_seconds: 0`
    copied by the model) is dropped; the row is kept."""

    def test_usable_ts(self):
        for ts in BAD_TS:
            with self.subTest(ts=ts):
                self.assertIsNone(owner_todos.usable_ts(ts))
        for given, want in (("00:12:03", "00:12:03"), (" 12:03 ", "12:03"),
                            ("[00:12:03]", "00:12:03"), (" [12:03] ", "12:03"),
                            ("75:30", "75:30"), (86399, "23:59:59"),
                            ("00:12:03,000", "00:12:03,000"),
                            (723, "00:12:03"), (723.9, "00:12:03"),
                            (3725, "01:02:05")):
            with self.subTest(ts=given):
                self.assertEqual(owner_todos.usable_ts(given), want)

    def test_validate_keeps_the_row_without_the_timestamp(self):
        for ts in BAD_TS:
            with self.subTest(ts=ts):
                rows, dropped = owner_todos.validate_actions(
                    [{"text": "Send it", "evidence_ts": ts}])
                self.assertEqual((rows, dropped), ([{"text": "Send it"}], 0))
        rows, _ = owner_todos.validate_actions(
            [{"text": "Send it"}, act("Do it", ts=723)])
        self.assertEqual(rows, [{"text": "Send it"},
                                {"text": "Do it", "evidence_ts": "00:12:03"}])

    def test_classify_keeps_the_row_valid(self):
        for ts in BAD_TS:
            with self.subTest(ts=ts):
                rows = owner_todos.classify_actions(
                    [{"text": "Send it", "evidence_ts": ts, "due": "2026-10-08"}])
                self.assertEqual(rows, [{
                    "text": "Send it", "due": "2026-10-08",
                    "status": "valid", "reason": None}])

    def test_other_refusals_are_unchanged(self):
        bad = [
            ({"text": "", "evidence_ts": 0}, "missing text"),
            ({"text": "x" * 500, "evidence_ts": 0}, "text too long"),
            ({"text": "x", "evidence_ts": 0, "due": "soon"}, "bad due date"),
            ({"text": "x", "evidence_ts": 0, "owner": "bob"}, "unexpected field"),
            ("plain", "not an object"),
        ]
        for row, reason in bad:
            with self.subTest(reason=reason):
                self.assertEqual(owner_todos.validate_actions([row]), ([], 1))
                got = owner_todos.classify_actions([row])[0]
                self.assertEqual((got["status"], got["reason"]),
                                 ("invalid", reason))
                self.assertNotIn(reason, got["text"])

    def test_ground_without_ts_is_owner_only(self):
        ok = owner_todos.ground_without_ts
        self.assertEqual(
            ok(TRANSCRIPT, "Send the revised plan to the team", [NAME]),
            (True, f"{NAME}: I will send the revised plan to the team."))
        # Bob's line and the unnamed speaker never ground an owner action
        self.assertEqual(ok("[00:15:10] Bob Stone: I will book the review "
                            "room.", "Book the review room", [NAME]),
                         (False, None))
        self.assertEqual(ok(TRANSCRIPT, "Call the clinic back", [NAME]),
                         (False, None))
        self.assertEqual(ok(TRANSCRIPT, "Do", [NAME]), (False, None))
        self.assertEqual(ok(TRANSCRIPT, "Book it", [NAME]), (False, None))
        self.assertEqual(ok("", "Send the plan", [NAME]), (False, None))

    def test_ground_without_ts_matches_whole_name_words_only(self):
        ok = owner_todos.ground_without_ts
        line = "[00:01:00] {}: I will send the revised plan to the team."
        for label, want in (("Dan", True), ("Dan Cohen", True), ("dan", True),
                            ("Cohen, Dan", True), ("Daniel", False),
                            ("Dana Lee", False), ("Abdan", False)):
            with self.subTest(label=label):
                self.assertEqual(
                    ok(line.format(label), "Send the revised plan to the team",
                       ["Dan"])[0], want)
        self.assertTrue(ok("[00:01:00] \u05d3\u05e0: send the plan today",
                           "Send the plan today", ["\u05d3\u05e0"])[0])
        self.assertFalse(ok("[00:01:00] \u05d3\u05e0\u05d9: send the plan "
                            "today", "Send the plan today", ["\u05d3\u05e0"])[0])

    def test_ground_without_ts_returns_the_best_owner_line(self):
        body = "\n".join([
            f"[00:01:00] {NAME}: I will send the plan.",
            f"[00:02:00] {NAME}: I will send the revised plan to the team.",
        ])
        self.assertEqual(
            owner_todos.ground_without_ts(
                body, "Send the revised plan to the team", [NAME]),
            (True, f"{NAME}: I will send the revised plan to the team."))

    def test_a_missed_timestamp_falls_back_to_the_text(self):
        for ts in ("00:12:04", "75:30", 723.9 + 60, "01:59:59"):
            with self.subTest(ts=ts):
                self.setUp()
                out = self.capture(meeting_extraction([
                    act("Send the revised plan to the team", ts=ts),
                    act("Call the clinic back", ts="00:16:00")]))
                self.assertEqual(out["memory_owner_todos"], 1)
                (row,) = self.store.todos().values()
                self.assertEqual(row["flags"]["--title"],
                                 "Send the revised plan to the team")
                self.assertIn("1 ungrounded", self.logged)

    def test_file_mode_files_a_zero_timestamp_row_grounded_by_text(self):
        for ts in (0, "0", "00:00", "garbage", None):
            with self.subTest(ts=ts):
                self.setUp()
                out = self.capture(meeting_extraction([
                    act("Send the revised plan to the team", ts=ts),
                    act("Call the clinic back", ts=0),     # unnamed speaker
                ]))
                self.assertEqual(out["memory_owner_todos"], 1)
                (row,) = self.store.todos().values()
                self.assertEqual(row["flags"]["--title"],
                                 "Send the revised plan to the team")
                self.assertNotIn(" at ", row["body"])
                self.assertIn("1 ungrounded", self.logged)

    def test_file_mode_positive_seconds_ground_like_a_timestamp(self):
        out = self.capture(meeting_extraction([
            act("Send the revised plan to the team", ts=723)]))
        self.assertEqual(out["memory_owner_todos"], 1)
        (row,) = self.store.todos().values()
        self.assertIn("at 00:12:03", row["body"])


class ZeroTimestampReviewTest(CaptureHarness):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = os.path.join(self.tmp.name, "q.jsonl")
        self.routine = make_routine(mode="review", review_queue=self.queue)

    def rows(self):
        with open(self.queue, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh.read().splitlines()]

    def test_meeting_of_only_zero_timestamp_items_is_filed_for_review(self):
        out = self.capture(meeting_extraction([
            act("Send the revised plan to the team", ts=0),
            act("Booking the room is on me too", ts="0:00:00"),
            act("Call the clinic back", ts=0.0),
            act("Order the lunch", ts="garbage"),
        ]))
        self.assertEqual(out["memory_owner_review"], 1)
        self.assertEqual(out["memory_owner_review_candidates"], 4)
        body = self.store.reviews()["mila:REC-1:review"]["body"]
        self.assertNotIn("[invalid", body)
        self.assertNotIn("bad timestamp", body)
        self.assertEqual(body.count("[unverified: no timestamp]"), 2)
        self.assertNotIn("[unverified: ungrounded]", body)
        self.assertIn('quote: "Me Example: I will send the revised plan to '
                      'the team."', body)
        cands = self.rows()[0]["candidates"]
        self.assertEqual([c["status"] for c in cands],
                         ["valid", "valid", "ungrounded", "ungrounded"])
        self.assertEqual(cands[0]["quote"],
                         "Me Example: I will send the revised plan to the team.")
        self.assertEqual(cands[2]["reason"], owner_todos.NO_TS_REASON)
        self.assertNotIn("evidence_ts", cands[2])
        self.assertNotIn("line_at_ts", cands[2])
        self.assertNotIn("bad timestamp", self.logged)

    def test_review_marks_a_text_match_and_keeps_a_timestamp_match_plain(self):
        out = self.capture(meeting_extraction([
            act("Send the revised plan to the team", ts="00:12:04"),   # missed
            act("Booking the room is on me too", ts="00:17:00"),       # exact
            act("Call the clinic back", ts="00:17:30"),                # no line
        ]))
        self.assertEqual(out["memory_owner_review"], 1)
        body = self.store.reviews()["mila:REC-1:review"]["body"]
        lines = [l for l in body.splitlines() if l.startswith("- ")]
        self.assertIn("[matched by text]", lines[0])
        self.assertNotIn("[matched by text]", lines[1])
        self.assertIn("[unverified: ungrounded]", lines[2])
        cands = self.rows()[0]["candidates"]
        self.assertEqual([c["status"] for c in cands],
                         ["valid", "valid", "ungrounded"])
        self.assertEqual(cands[0]["reason"], owner_todos.TEXT_MATCH_REASON)
        self.assertIsNone(cands[1]["reason"])
        self.assertEqual(cands[0]["quote"], "Me Example: I will send the "
                         "revised plan to the team.")

    def test_file_mode_nothing_grounded_files_no_todo_without_error(self):
        out = self.capture(meeting_extraction(
            [act("Call the clinic back", ts=0)]),
            routine=make_routine())
        self.assertEqual(self.store.todos(), {})
        self.assertNotIn("memory_owner_todos", out)
        self.assertIn("1 ungrounded", self.logged)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state", "q.jsonl")

    def row(self, meeting, **extra):
        return action_review_queue.build_row(
            meeting_entry_id=meeting, review_todo_id="t-" + meeting,
            source_id="mila:" + meeting, source_kind="mila", recording_id=None,
            title="T", date="2026-10-01", at="2026-10-01T00:00:00Z",
            candidates=extra.get("candidates", []))

    def test_permissions_and_lock_file(self):
        action_review_queue.upsert(self.path, self.row("m1"))
        for path in (self.path, self.path + ".lock"):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        # an existing loose file is tightened
        os.chmod(self.path, 0o644)
        self.assertEqual(
            action_review_queue.upsert(self.path, self.row("m1")), "unchanged")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # a rewrite keeps 0600
        action_review_queue.upsert(
            self.path, self.row("m1", candidates=[{"text": "x"}]))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_unicode_line_separators_cannot_split_a_row(self):
        row = self.row("m1")
        row["title"] = "a\u2028b\u2029c\u0085d"
        action_review_queue.upsert(self.path, row)
        with open(self.path, encoding="utf-8", newline="") as fh:
            raw = fh.read()
        self.assertEqual(len(raw.splitlines()), 1)
        self.assertEqual(raw.count("\n"), 1)
        self.assertEqual(json.loads(raw)["title"], row["title"])
        self.assertEqual(action_review_queue.upsert(self.path, row),
                         "unchanged")
        row["candidates"] = [{"text": "x"}]
        self.assertEqual(action_review_queue.upsert(self.path, row),
                         "replaced")
        self.assertEqual(len(read_lines(self.path)), 1)

    def test_new_folder_is_private_and_loose_lock_is_tightened(self):
        deep = os.path.join(self.tmp.name, "custom", "q.jsonl")
        action_review_queue.upsert(deep, self.row("m1"))
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(deep)).st_mode), 0o700)
        os.chmod(deep + ".lock", 0o644)
        action_review_queue.upsert(deep, self.row("m2"))
        self.assertEqual(stat.S_IMODE(os.stat(deep + ".lock").st_mode), 0o600)
        self.assertTrue(action_review_queue.has_row(deep, "m2"))
        self.assertFalse(action_review_queue.has_row(deep, "m3"))
        self.assertFalse(action_review_queue.has_row(deep + ".none", "m1"))

    def test_no_recording_id_key_when_unknown(self):
        self.assertNotIn("recording_id", self.row("m1"))

    def test_dedupe_replace_and_skip(self):
        up = action_review_queue.upsert
        self.assertEqual(up(self.path, self.row("m1")), "added")
        self.assertEqual(up(self.path, self.row("m2")), "added")
        self.assertEqual(up(self.path, self.row("m1")), "unchanged")
        self.assertEqual(
            up(self.path, self.row("m1", candidates=[{"text": "x"}])),
            "replaced")
        lines = read_lines(self.path)
        ids = [json.loads(line)["meeting_entry_id"] for line in lines]
        self.assertEqual(ids, ["m1", "m2"])

    def test_writer_waits_for_the_lock(self):
        os.makedirs(os.path.dirname(self.path))
        lock = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        done = threading.Event()

        def write():
            action_review_queue.upsert(self.path, self.row("m1"))
            done.set()

        thread = threading.Thread(target=write)
        thread.start()
        try:
            self.assertFalse(done.wait(0.3))
            self.assertFalse(os.path.exists(self.path))
        finally:
            os.close(lock)  # releases the flock
        thread.join(5)
        self.assertTrue(done.is_set())
        self.assertEqual(len(read_lines(self.path)), 1)

    def test_concurrent_writers_never_corrupt_or_duplicate(self):
        def write(n):
            for _ in range(2):
                action_review_queue.upsert(self.path, self.row(f"m{n % 10}"))

        threads = [threading.Thread(target=write, args=(n,))
                   for n in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        ids = [json.loads(line)["meeting_entry_id"]
               for line in read_lines(self.path)]
        self.assertEqual(sorted(ids), sorted({f"m{n}" for n in range(10)}))

    def test_malformed_lines_are_kept(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as fh:
            fh.write("not json\n")
        action_review_queue.upsert(self.path, self.row("m1"))
        action_review_queue.upsert(
            self.path, self.row("m1", candidates=[{"text": "x"}]))
        lines = read_lines(self.path)
        self.assertEqual(lines[0], "not json")
        self.assertEqual(len(lines), 2)


class ReviewHelpersTest(unittest.TestCase):
    def test_validation_of_new_keys(self):
        ok = {"owner_todos": {"mode": "review", "review_queue": "q.jsonl"}}
        self.assertEqual(owner_todos.validate(ok, "r"), [])
        self.assertEqual(owner_todos.validate(
            {"owner_todos": {"mode": "file"}}, "r"), [])
        for bad in ({"mode": "both"}, {"mode": True}, {"review_queue": ""},
                    {"review_queue": 3}):
            self.assertTrue(owner_todos.validate({"owner_todos": bad}, "r"),
                            bad)

    def test_classify_actions(self):
        rows = owner_todos.classify_actions([
            {"text": "Do A", "evidence_ts": "00:01:00"},
            {"text": "do a!", "evidence_ts": "00:02:00"},
            {"text": "Do B", "evidence_ts": 0},
            {"text": "Do C", "bogus": 1},
            "junk",
            {"text": "x" * 300, "evidence_ts": "00:01:00"},
            {"text": "Do D", "due": "soon", "evidence_ts": "00:01:00"},
        ])
        self.assertEqual(
            [(r["status"], r["reason"]) for r in rows],
            [("valid", None), ("valid", None),
             ("invalid", "unexpected field"), ("invalid", "not an object"),
             ("invalid", "text too long"), ("invalid", "bad due date")])
        self.assertEqual(len(rows[4]["text"]), owner_todos.CANDIDATE_TEXT_LIMIT)
        self.assertEqual(rows[3]["text"], "")
        self.assertEqual(owner_todos.classify_actions(None), [])
        self.assertIsNone(owner_todos.classify_actions("nope"))

    def test_ground_in_transcript_returns_the_matched_owner_line(self):
        body = (f"[00:20:00] Bob Stone: Not me.\n"
                f"[00:20:00] {NAME}: Mine.\n[00:21:00] Bob Stone: Hi.")
        self.assertEqual(
            owner_todos.ground_in_transcript(body, "00:20:00", [NAME]),
            (True, f"{NAME}: Mine."))
        self.assertEqual(
            owner_todos.ground_in_transcript(body, "00:21:00", [NAME]),
            (False, None))
        self.assertEqual(
            owner_todos.ground_in_transcript(body, "junk", [NAME]),
            (False, None))

    def test_cap_candidates(self):
        def cand(status):
            return {"status": status, "text": status}
        rows = [cand("invalid"), cand("valid"), cand("ungrounded"),
                cand("valid")]
        kept, dropped = owner_todos.cap_candidates(rows, 3)
        self.assertEqual([c["status"] for c in kept],
                         ["valid", "valid", "ungrounded"])
        self.assertEqual(dropped, 1)
        kept, dropped = owner_todos.cap_candidates(rows, 1)
        self.assertEqual([c["status"] for c in kept], ["valid", "valid"])
        self.assertEqual(dropped, 2)

    def test_quotes(self):
        self.assertEqual(
            owner_todos.transcript_quote(TRANSCRIPT, "00:15:10"),
            "Bob Stone: I will book the review room.")
        self.assertIsNone(owner_todos.transcript_quote(TRANSCRIPT, "09:00:00"))
        self.assertIsNone(owner_todos.transcript_quote(TRANSCRIPT, "junk"))
        body = "Notes.\n- Me Example sends the plan today.\n- Bob books."
        self.assertEqual(
            owner_todos.body_quote(body, "Send the plan", [NAME]),
            "- Me Example sends the plan today")
        self.assertIsNone(owner_todos.body_quote(body, "Book it", [NAME]))


class ReviewBackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.entries = self.root / "memory" / "entries"
        self.entries.mkdir(parents=True)
        self.write("m1", "decision", "2026-10-01", MEETING_SID,
                   "Planning sync",
                   f"- {NAME} sends the plan.\n- Bob books the room.")
        self.write("m2", "note", "2026-10-05", "whisper:W-2", "Second sync",
                   f"- {NAME} sends the notes.")
        self.write("m3", "meeting", "2026-09-01", "mila:REC-0", "Old sync",
                   f"- {NAME} sends the old plan.")
        self.write("n1", "note", "2026-10-06", "gmail:1", "A note", "body")
        self.write("t1", "todo", "2026-10-06", "mila:REC-1#action-0123456789",
                   "A todo", "body")
        self.queue = str(self.root / "state" / "q.jsonl")
        self.routine = make_routine(
            store=str(self.root), mode="review", review_queue=self.queue)
        self.store = FakeStore()

    def write(self, entry_id, etype, date, sid, title, body,
              tags="work"):
        (self.entries / f"{entry_id}.md").write_text(
            f"---\nid: {entry_id}\ntype: {etype}\ndate: '{date}'\n"
            f"title: '{title}'\ntags: [{tags}]\nsource_ids: ['{sid}']\n---\n"
            f"{body}\n")

    REPLY = json.dumps({"owner_actions": [
        {"text": "Send the plan"}, {"text": "Book the room"},
        {"text": "bad", "bogus": 1}]})

    def run_backfill(self, reply=None, **kwargs):
        with mock.patch("workspace_daemon.llm.analyze",
                        return_value=reply or self.REPLY) as analyze, \
                mock.patch.object(memory_sink, "_cli",
                                  side_effect=self.store.cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log") as log:
            result = memory_sink.backfill_action_review(
                self.routine, **kwargs)
        self.logged = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.analyze = analyze
        return result

    def rows(self):
        try:
            return [json.loads(line) for line in read_lines(self.queue)]
        except FileNotFoundError:
            return []

    def test_real_run_on_a_decision_entry(self):
        result = self.run_backfill(entry_ids=["m1"])
        self.assertEqual(result["entry_ids"], ["m1"])
        self.assertEqual((result["reviewed"], result["queued"],
                          result["candidates"]), (1, 1, 3))
        self.assertEqual(list(self.store.reviews()), ["mila:REC-1:review"])
        self.assertEqual(self.store.todos(), {})
        body = self.store.reviews()["mila:REC-1:review"]["body"]
        self.assertIn("From meeting m1", body)
        self.assertIn("[unverified: ungrounded]", body)
        self.assertIn("[invalid: unexpected field]", body)
        row, = self.rows()
        self.assertEqual(row["meeting_entry_id"], "m1")
        self.assertEqual(row["source_kind"], "mila")
        self.assertEqual(row["recording_id"], "REC-1")
        self.assertEqual(
            [c["status"] for c in row["candidates"]],
            ["valid", "ungrounded", "invalid"])
        self.assertIn("Me Example sends the plan",
                      row["candidates"][0]["quote"])

    def test_dry_run_writes_nothing_and_logs_no_text(self):
        result = self.run_backfill(since="2026-10-01", dry_run=True)
        self.assertEqual(result["entry_ids"], ["m1", "m2"])
        self.assertEqual(result["reviewed"], 2)
        self.assertEqual(self.store.calls, [])
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(self.queue))
        for secret in ("Send the plan", "Book the room", "sends the plan"):
            self.assertNotIn(secret, self.logged)

    def test_since_picks_meeting_sources_of_any_type(self):
        result = self.run_backfill(since="2026-10-01")
        self.assertEqual(result["selected"], 2)
        self.assertEqual(self.analyze.call_count, 2)
        self.assertEqual({r["meeting_entry_id"] for r in self.rows()},
                         {"m1", "m2"})
        self.assertEqual(
            {r["source_kind"] for r in self.rows()}, {"mila", "whisper"})

    def test_rerun_is_idempotent(self):
        self.run_backfill(entry_ids=["m1"])
        rows = self.rows()
        # the store now holds the review todo
        self.write("r1", "todo", "2026-10-01", "mila:REC-1:review",
                   "Review action items: Planning sync", "x")
        again = self.run_backfill(entry_ids=["m1"])
        self.assertEqual(again["already_reviewed"], 1)
        self.assertEqual(self.analyze.call_count, 0)
        self.assertEqual(self.rows(), rows)
        redo = self.run_backfill(entry_ids=["m1"], redo=True)
        self.assertEqual(redo["reviewed"], 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.store.reviews()), 1)
        self.assertEqual(self.rows()[0]["candidates"], rows[0]["candidates"])

    def test_no_actions_and_bad_model_output(self):
        none = self.run_backfill(
            reply=json.dumps({"owner_actions": []}), entry_ids=["m1"])
        self.assertEqual((none["no_actions"], none["reviewed"]), (1, 0))
        self.assertEqual(self.store.calls, [])
        self.assertFalse(os.path.exists(self.queue))
        for reply in ("not json", json.dumps({"owner_actions": "x"})):
            bad = self.run_backfill(reply=reply, entry_ids=["m1"])
            self.assertEqual(bad["errors"], 1)
        self.assertEqual(self.store.calls, [])

    def test_unsuitable_entries_are_skipped(self):
        result = self.run_backfill(entry_ids=["n1", "t1", "nope"])
        self.assertEqual(result["skipped"], 3)
        self.assertEqual(result["selected"], 0)
        self.assertEqual(self.analyze.call_count, 0)

    def test_needs_a_selector_and_a_date(self):
        with self.assertRaises(RuntimeError):
            self.run_backfill()
        with self.assertRaises(RuntimeError):
            self.run_backfill(since="yesterday")

    def test_entries_sharing_a_source_id_are_one_meeting(self):
        self.write("x1", "decision", "2026-11-01", "mila:REC-9", "Twin sync",
                   f"- {NAME} sends the plan.")
        self.write("x2", "meeting", "2026-11-01", "mila:REC-9", "Twin sync",
                   f"- {NAME} sends the plan.", tags="work, auto-captured")
        result = self.run_backfill(entry_ids=["x1", "x2"])
        self.assertEqual(result["entry_ids"], ["x2"])
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.analyze.call_count, 1)
        self.assertEqual(
            [r["meeting_entry_id"] for r in self.rows()], ["x2"])
        again = self.run_backfill(since="2026-11-01")
        self.assertEqual(again["already_reviewed"], 1)
        self.assertEqual(self.analyze.call_count, 0)

    def test_hand_logged_and_auto_capture_are_one_meeting(self):
        self.write("h1", "note", "2026-11-02", "mila:REC-8", "Hand sync",
                   f"- {NAME} sends the plan.")
        self.write("h2", "meeting", "2026-11-02", "mila:REC-8:auto",
                   "Hand sync", f"- {NAME} sends the plan.",
                   tags="work, auto-captured")
        result = self.run_backfill(since="2026-11-02")
        self.assertEqual(result["entry_ids"], ["h2"])
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(list(self.store.reviews()),
                         ["mila:REC-8:auto:review"])
        row, = self.rows()
        self.assertEqual(row["recording_id"], "REC-8")
        self.assertEqual(row["source_kind"], "mila")
        self.assertEqual(row["source_id"], "mila:REC-8:auto")

    def test_a_queue_row_counts_as_already_reviewed(self):
        # e.g. the review todo was refused as a near-duplicate: no todo
        # carries the id, but the row exists, so no repeated model call
        action_review_queue.upsert(self.queue, action_review_queue.build_row(
            meeting_entry_id="m1", review_todo_id=None, source_id=MEETING_SID,
            source_kind="mila", recording_id="REC-1", title="Planning sync",
            date="2026-10-01", at="2026-10-01T00:00:00Z", candidates=[]))
        result = self.run_backfill(entry_ids=["m1"])
        self.assertEqual(result["already_reviewed"], 1)
        self.assertEqual(self.analyze.call_count, 0)
        self.assertEqual(self.store.calls, [])

    def test_since_must_be_a_real_date(self):
        for bad in ("2026-13-45", "2026-02-30", "26-1-1"):
            with self.assertRaises(RuntimeError):
                self.run_backfill(since=bad)

    def test_file_mode_backfill_accepts_a_decision_meeting(self):
        self.routine = make_routine(store=str(self.root))
        reply = json.dumps({"owner_actions": [{"text": "Send the plan"}]})
        with mock.patch("workspace_daemon.llm.analyze", return_value=reply), \
                mock.patch.object(memory_sink, "_cli",
                                  side_effect=self.store.cli), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            result = memory_sink.backfill_owner_todos(self.routine, "m1")
        self.assertEqual(result["memory_owner_todos"], 1)
        self.assertEqual(len(self.store.todos()), 1)
        for bad in ("n1", "t1"):
            with self.assertRaises(RuntimeError):
                with mock.patch("workspace_daemon.llm.analyze"):
                    memory_sink.backfill_owner_todos(self.routine, bad)

    def test_llm_value_error_is_not_read_as_bad_output(self):
        self.routine = make_routine(store=str(self.root))
        with mock.patch("workspace_daemon.llm.analyze",
                        side_effect=ValueError("llm broke")):
            with self.assertRaisesRegex(ValueError, "llm broke"):
                memory_sink.backfill_owner_todos(self.routine, "m1")

    def test_old_backfill_refuses_review_mode(self):
        with self.assertRaises(RuntimeError):
            with mock.patch("workspace_daemon.llm.analyze"):
                memory_sink.backfill_owner_todos(self.routine, "m1")


class ReviewCliTest(unittest.TestCase):
    def run_cmd(self, result=None, error=None):
        import contextlib
        import io
        import daemon as daemon_cli
        args = mock.Mock(routine="r", entry=["m1"], since=None,
                         dry_run=False, redo=False)
        out, err = io.StringIO(), io.StringIO()
        patches = [
            mock.patch.object(daemon_cli, "set_log_file"),
            mock.patch.object(daemon_cli.config, "discover",
                              return_value=[{"id": "r"}]),
            mock.patch.object(daemon_cli.config, "validate", return_value=[]),
            mock.patch.object(daemon_cli.llm, "configure"),
            mock.patch.object(daemon_cli.identity, "configure"),
            mock.patch.object(
                daemon_cli.memory_sink, "backfill_action_review",
                side_effect=error, return_value=result),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = daemon_cli.cmd_backfill_action_review(args)
        return code, out.getvalue(), err.getvalue()

    BASE = {"selected": 1, "reviewed": 1, "queued": 1, "no_actions": 0,
            "already_reviewed": 0, "skipped": 0, "duplicates": 0,
            "errors": 0, "candidates": 2, "entry_ids": ["m1"]}

    def test_success_prints_counts_and_ids(self):
        code, out, _ = self.run_cmd(self.BASE)
        self.assertEqual(code, 0)
        self.assertIn("reviewed 1 with 2 candidate(s)", out)
        self.assertIn("  m1", out)

    def test_missing_or_non_meeting_entry_exits_1(self):
        code, _, err = self.run_cmd(dict(self.BASE, skipped=1, selected=0,
                                         reviewed=0, entry_ids=[]))
        self.assertEqual(code, 1)
        self.assertIn("not found or not a meeting", err)

    def test_error_is_printed_once(self):
        code, out, err = self.run_cmd(error=RuntimeError("boom here"))
        self.assertEqual(code, 1)
        self.assertEqual((out + err).count("boom here"), 1)


class ReviewTagOutboundTest(unittest.TestCase):
    def test_review_todos_are_not_exported(self):
        entry = {
            "id": "review-it", "type": "todo", "date": "2026-10-01",
            "title": "Review action items: Planning sync", "people": [OWNER],
            "tags": ["from-meeting", "needs-review", "auto-captured"],
            "source_ids": ["mila:REC-1:review"],
        }
        cfg = {"owner_slug": OWNER, "outbound_since": "2026-09-01",
               "from_sources": ["mila:"]}
        self.assertFalse(fibery_tasks_outbound.eligible(entry, cfg))
        entry["tags"] = ["from-meeting", "auto-captured"]
        self.assertTrue(fibery_tasks_outbound.eligible(entry, cfg))


if __name__ == "__main__":
    unittest.main()
