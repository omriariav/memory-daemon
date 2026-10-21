import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import (
    claude_bridge, identity, memory_sink, person_briefs, state,
)


class Result:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


class FakeStore:
    """A tiny store: entry files on disk plus a CLI that reads/writes them."""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / "memory" / "entries").mkdir(parents=True)
        self.adds = []

    def put(self, eid, etype="note", people=(), tags=(), source_ids=(),
            follows=(), date="2026-01-05", title="A title", updated=None):
        meta = {"id": eid, "date": date, "type": etype, "title": title,
                "people": list(people), "tags": list(tags),
                "source_ids": list(source_ids), "follows": list(follows)}
        if updated:
            meta["updated"] = updated
        path = self.root / "memory" / "entries" / f"{eid}.md"
        path.write_text("---\n" + json.dumps(meta) + "\n---\nbody text\n")

    def entries(self):
        out = []
        for path in sorted((self.root / "memory" / "entries").glob("*.md")):
            meta = json.loads(path.read_text().split("---")[1])
            out.append((meta, path))
        return out

    def cli(self, store, args, stdin_text=None, timeout=120):
        if args[0] == "list":
            tag = args[args.index("--tag") + 1]
            rows = [(m, p) for m, p in self.entries() if tag in m["tags"]]
            return Result("\n".join(self._line(m, p) for m, p in rows))
        if args[0] == "person":
            slug = args[1]
            rows = [(m, p) for m, p in self.entries() if slug in m["people"]]
            return Result("\n".join(self._line(m, p) for m, p in rows))
        if args[0] == "add":
            opts = dict(zip(args[1::2], args[2::2]))
            self.adds.append((opts, stdin_text))
            self.put(
                "2026-02-01-" + opts["--people"] + "-brief", opts["--type"],
                opts["--people"].split(","), opts["--tags"].split(","),
                opts["--source-ids"].split(","),
                date=opts["--date"], title=opts["--title"],
            )
            return Result("✓ created 2026-02-01-x\n")
        raise AssertionError(args)

    def _line(self, meta, path):
        rel = path.relative_to(self.root)
        return f"{meta['date']}  {meta['type']:<11} {meta['title']}  ({rel})"


def make_routine(store, **extra):
    briefs = {"enabled": True, "max_per_run": 3, "max_per_day": 15}
    briefs.update(extra)
    return {
        "id": "r1", "analyze": {"provider": "claude", "model": "sonnet"},
        "memory": {
            "store": str(store), "person_briefs": briefs,
            "identity": {"provider": "slack_mcp", "owner_slug": "owner-slug",
                         "owner_email": "o@example.test",
                         "search_tool": "mcp__x__search",
                         "profile_tool": "mcp__x__profile",
                         "internal_domains": ["example.test"]},
        },
    }


class PersonBriefTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name) / "base"
        (self.base / "state").mkdir(parents=True)
        self.store = FakeStore(Path(tmp.name) / "store")
        self.routine = make_routine(self.store.root)
        identity.configure(self.base)
        self.addCleanup(identity.clear_cache)
        patches = [
            mock.patch.object(memory_sink, "_cli", side_effect=self.store.cli),
            mock.patch.object(memory_sink, "_commit_store"),
            mock.patch.object(memory_sink, "log"),
            mock.patch.object(person_briefs, "log"),
            mock.patch.object(claude_bridge, "budget_exhausted",
                              return_value=None),
            mock.patch.object(claude_bridge, "spent_today", return_value=0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        analyze = mock.patch(
            "workspace_daemon.llm.analyze",
            return_value="Jane Doe is an engineer.\nFirst seen 2026-01-01 in "
                         "2026-01-01-kickoff.",
        )
        self.analyze = analyze.start()
        self.addCleanup(analyze.stop)

    def capture(self, people, kind="mila", item=None, memory="created"):
        outcome = {"memory": memory, "memory_entry_id": "2026-01-05-kickoff",
                   "memory_people": people}
        return person_briefs.enqueue(
            self.routine, item or {"frontmatter": {}}, outcome, kind, self.base)

    def seed_person(self, slug, n=1):
        for i in range(n):
            eid = "2026-01-01-kickoff" if i == 0 else f"2026-01-0{i + 1}-k{i}"
            self.store.put(eid, people=[slug, "other-one"],
                           date=f"2026-01-0{i + 1}", title=f"Topic {i}")

    def test_new_person_gets_one_brief_and_idempotent(self):
        self.seed_person("jane-doe", 2)
        self.assertEqual(self.capture(["jane-doe"]), 1)
        out = person_briefs.process(self.routine, self.base)
        self.assertEqual(out["written"], 1)
        opts, _body = self.store.adds[0]
        self.assertEqual(opts["--people"], "jane-doe")
        self.assertIn("person:jane-doe", opts["--source-ids"])
        self.assertIn("person-brief", opts["--tags"])
        self.assertIn("auto-captured", opts["--tags"])
        self.assertEqual(opts["--type"], "note")
        self.assertEqual(self.analyze.call_args.kwargs["model"], "haiku")
        self.capture(["jane-doe"])
        again = person_briefs.process(self.routine, self.base)
        self.assertEqual(again["written"], 0)
        self.assertEqual(len(self.store.adds), 1)

    def test_existing_brief_is_left_alone(self):
        self.seed_person("jane-doe")
        self.store.put("2026-01-02-brief", people=["jane-doe"],
                       tags=["people-brief"],
                       source_ids=["slack-profile:UAAAA1"])
        self.capture(["jane-doe"])
        out = person_briefs.process(self.routine, self.base)
        self.assertEqual((out["written"], out["skipped_existing"]), (0, 1))
        self.assertEqual(self.store.adds, [])
        self.assertEqual(person_briefs.pending(self.base), 0)

    def test_user_edited_brief_untouched(self):
        self.seed_person("jane-doe")
        self.store.put("2026-01-02-mine", people=["jane-doe"],
                       tags=["person-brief"], source_ids=["person:jane-doe"])
        entry = person_briefs.brief_index(
            str(self.store.root), "person-brief")["jane-doe"]
        self.assertEqual(person_briefs.brief_state(entry), "user")
        edited = dict(entry, tags=["auto-captured"], updated="2026-03-01")
        self.assertEqual(person_briefs.brief_state(edited), "user")
        self.capture(["jane-doe"])
        person_briefs.process(self.routine, self.base)
        self.assertEqual(self.store.adds, [])

    def test_owner_excluded(self):
        self.seed_person("owner-slug")
        self.assertEqual(self.capture(["owner-slug"]), 0)
        self.assertEqual(person_briefs.pending(self.base), 0)

    def test_disabled_or_wrong_source_or_failed_capture(self):
        self.assertEqual(self.capture(["a-b"], kind="slack"), 0)
        self.assertEqual(self.capture(["a-b"], memory="skipped_not_worthy"), 0)
        off = make_routine(self.store.root, enabled=False)
        outcome = {"memory": "created", "memory_entry_id": "e",
                   "memory_people": ["a-b"]}
        self.assertEqual(
            person_briefs.enqueue(off, {}, outcome, "mila", self.base), 0)
        self.assertEqual(self.capture(["a-b"], kind="mila"), 1)

    def test_caps_and_queue_carry_over(self):
        self.routine = make_routine(self.store.root, max_per_run=2,
                                    max_per_day=3)
        names = ["ann-one", "bob-two", "cat-three", "dan-four"]
        for n in names:
            self.seed_person(n)
        self.capture(names)
        first = person_briefs.process(self.routine, self.base)
        self.assertEqual(first["written"], 2)
        self.assertEqual(person_briefs.pending(self.base), 2)
        second = person_briefs.process(self.routine, self.base)
        self.assertEqual(second["written"], 1)
        self.assertEqual(person_briefs.pending(self.base), 1)
        third = person_briefs.process(self.routine, self.base)
        self.assertEqual(third["written"], 0)
        self.assertEqual(person_briefs.pending(self.base), 1)
        qpath = self.base / "state" / person_briefs.QUEUE_FILE
        data = json.loads(qpath.read_text())
        data["days"]["r1"]["date"] = "2000-01-01"
        qpath.write_text(json.dumps(data))
        fourth = person_briefs.process(self.routine, self.base)
        self.assertEqual(fourth["written"], 1)
        self.assertEqual(person_briefs.pending(self.base), 0)

    def test_no_profile_single_cooccurrence_skips_model(self):
        self.seed_person("jane-doe", 1)
        item = {"frontmatter": {"source_people": [
            {"name": "Jane Doe", "email": "jane@corp.example"}]}}
        self.capture(["jane-doe"], item=item)
        person_briefs.process(self.routine, self.base)
        self.analyze.assert_not_called()
        _opts, body = self.store.adds[0]
        self.assertIn("First seen 2026-01-01 in 2026-01-01-kickoff", body)
        self.assertIn("corp.example", body)

    def test_slack_profile_one_pinned_call_and_untrusted_fields(self):
        self.seed_person("jane-doe", 2)
        item = {"frontmatter": {"slack_user_ids": {"Jane Doe": "UAAAA1"}}}
        self.capture(["jane-doe"], kind="slack_mcp", item=item)
        profile = ("Real Name: Jane Doe\nTitle: Staff Engineer\n"
                   "Timezone: Europe/Example\nManager: Sam Lead\n")
        calls = []

        def fake_call_tools(prompt, tools, **kw):
            calls.append((prompt, tools, kw))
            return [{"name": "mcp__x__profile", "input": {"user_id": "UAAAA1"},
                     "text": profile, "is_error": False}]

        with mock.patch.object(claude_bridge, "call_tools",
                               side_effect=fake_call_tools):
            person_briefs.process(self.routine, self.base)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1],
                         {"mcp__x__profile": {"user_id": "UAAAA1"}})
        prompt = self.analyze.call_args.args[1]
        self.assertIn("Staff Engineer", prompt)
        self.assertIn("slack-profile:UAAAA1",
                      self.store.adds[0][0]["--source-ids"])

    def test_model_citing_unknown_entry_falls_back(self):
        self.seed_person("jane-doe", 2)
        self.analyze.return_value = "Met in 2026-09-09-invented-entry."
        self.capture(["jane-doe"])
        person_briefs.process(self.routine, self.base)
        body = self.store.adds[0][1]
        self.assertNotIn("invented", body)
        self.assertIn("First seen", body)

    def test_personal_entries_not_cited(self):
        self.store.put("2026-01-01-private", people=["jane-doe"],
                       tags=["personal"], date="2026-01-01",
                       title="Private topic")
        self.store.put("2026-01-03-work", people=["jane-doe"],
                       date="2026-01-03", title="Work topic")
        self.capture(["jane-doe"])
        person_briefs.process(self.routine, self.base)
        body = self.store.adds[0][1]
        self.assertIn("2026-01-03-work", body)
        self.assertNotIn("private", body.lower())

    def test_paused_holds_queue(self):
        self.seed_person("jane-doe")
        self.capture(["jane-doe"])
        state.pause(self.base)
        out = person_briefs.process(self.routine, self.base)
        self.assertEqual((out["written"], out["held"]), (0, 1))
        self.assertEqual(self.store.adds, [])
        self.assertEqual(person_briefs.pending(self.base), 1)

    def test_guard_stop_mid_run_keeps_item(self):
        self.seed_person("jane-doe", 2)
        self.capture(["jane-doe"])
        self.analyze.side_effect = claude_bridge.BudgetExhausted("cap")
        out = person_briefs.process(self.routine, self.base)
        self.assertEqual(out["written"], 0)
        self.assertEqual(person_briefs.pending(self.base), 1)

    def test_validate(self):
        types = memory_sink.MEMORY_TYPES
        ok = {"person_briefs": {"enabled": True, "sources": ["mila"]}}
        self.assertEqual(person_briefs.validate(ok, "r", types), [])
        bad = {"person_briefs": {"type": "person", "max_per_run": 0,
                                 "sources": ["slack"], "bogus": 1}}
        self.assertEqual(len(person_briefs.validate(bad, "r", types)), 4)


if __name__ == "__main__":
    unittest.main()
