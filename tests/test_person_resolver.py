import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import memory_sink, person_briefs, person_resolver

SCRIPT = """#!/usr/bin/env python3
import json, sys, time
mode = {mode!r}
names = json.load(sys.stdin)["names"]
open({log!r}, "a").write(json.dumps(names) + "\\n")
if mode == "slow":
    time.sleep(5)
if mode == "fail":
    sys.exit(3)
if mode == "badjson":
    print("not json"); sys.exit(0)
if mode == "badslug":
    print(json.dumps({{"resolved": {{names[0]: "Not A Slug!"}}}})); sys.exit(0)
print(json.dumps({{
    "resolved": {{n: ("ada-lovelace" if n.lower() in ("ada", "augusta")
                     else None) for n in names}},
    "facts": {{"ada-lovelace": {{"title": "Staff Engineer", "team": "Engines"}}}},
}}))
"""


class ResolverTestBase(unittest.TestCase):
    def setUp(self):
        person_resolver.reset()
        self.addCleanup(person_resolver.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.calls = self.tmp / "calls.log"
        patch = mock.patch.object(person_resolver, "log")
        self.log = patch.start()
        self.addCleanup(patch.stop)

    def script(self, mode="ok"):
        path = self.tmp / f"resolver-{mode}.py"
        path.write_text(SCRIPT.format(mode=mode, log=str(self.calls)))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def routine(self, mode="ok", **extra):
        return {"id": "r", "memory": {"person_resolver": [self.script(mode)],
                                      **extra}}

    def call_count(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0


class CanonicalizeTest(ResolverTestBase):
    def test_success_maps_and_passes_unresolved_through(self):
        out = person_resolver.canonicalize(
            self.routine(), ["Ada", "jane-doe", "augusta"])
        self.assertEqual(out, ["ada-lovelace", "jane-doe"])

    def test_unset_is_unchanged(self):
        out = person_resolver.canonicalize({"id": "r", "memory": {}}, ["Ada", "x"])
        self.assertEqual(out, ["Ada", "x"])
        self.assertEqual(self.call_count(), 0)

    def test_timeout_falls_back_and_warns(self):
        routine = self.routine("slow", person_resolver_timeout=0.3)
        self.assertEqual(person_resolver.canonicalize(routine, ["Ada"]), ["Ada"])
        self.assertIn("timed out", self.log.call_args.args[0])

    def test_bad_json_nonzero_exit_and_bad_slug_fall_back(self):
        for mode in ("badjson", "fail", "badslug"):
            person_resolver.reset()
            self.log.reset_mock()
            out = person_resolver.canonicalize(self.routine(mode), ["Ada"])
            self.assertEqual(out, ["Ada"], mode)
            self.log.assert_called_once()

    def test_missing_executable_never_raises(self):
        routine = {"id": "r", "memory": {"person_resolver": ["/nonexistent/x"]}}
        self.assertEqual(person_resolver.canonicalize(routine, ["Ada"]), ["Ada"])

    def test_results_are_memoized(self):
        routine = self.routine()
        person_resolver.canonicalize(routine, ["Ada", "zed"])
        person_resolver.canonicalize(routine, ["Ada", "zed"])
        self.assertEqual(self.call_count(), 1)
        person_resolver.canonicalize(routine, ["Ada", "new"])
        self.assertEqual(self.call_count(), 2)
        self.assertEqual(json.loads(self.calls.read_text().splitlines()[1]),
                         ["new"])


class FactsTest(ResolverTestBase):
    def test_facts_for_slug(self):
        facts = person_resolver.facts_for(self.routine(), "ada-lovelace",
                                          ["Ada Lovelace"])
        self.assertEqual(facts, {"title": "Staff Engineer", "team": "Engines"})

    def test_facts_empty_when_unset_or_failing(self):
        self.assertEqual(person_resolver.facts_for({"memory": {}}, "ada-lovelace"), {})
        self.assertEqual(
            person_resolver.facts_for(self.routine("fail"), "ada-lovelace"), {})

    def test_brief_lines_and_prompt_use_generic_pairs(self):
        facts = person_briefs._fact_dict(
            {}, "Ada", None,
            {"first": None, "open_threads": [], "works_with": [],
             "entry_count": 1},
            {"title": "Staff Engineer", "team": "Engines"})
        lines = person_briefs._deterministic_lines("Ada", facts)
        self.assertIn("Ada: title: Staff Engineer; team: Engines.", lines)
        with mock.patch.object(person_briefs.llm, "analyze",
                               return_value="Ada: staff.") as analyze:
            person_briefs._compose({"id": "r", "memory": {}}, {}, "Ada", facts)
        self.assertIn("Staff Engineer", analyze.call_args.args[1])


class ValidateTest(ResolverTestBase):
    def problems(self, cfg):
        return person_resolver.validate(cfg, "r")

    def test_valid_and_unset(self):
        self.assertEqual(self.problems({}), [])
        self.assertEqual(self.problems({"person_resolver": [self.script()],
                                        "person_resolver_timeout": 2}), [])

    def test_rejects_bad_config(self):
        plain = self.tmp / "plain.txt"
        plain.write_text("x")
        os.chmod(plain, 0o644)
        for cfg in ({"person_resolver": "/bin/true"},
                    {"person_resolver": []},
                    {"person_resolver": ["relative/path"]},
                    {"person_resolver": [str(self.tmp / "missing")]},
                    {"person_resolver": [str(plain)]},
                    {"person_resolver": [self.script()],
                     "person_resolver_timeout": 0}):
            self.assertTrue(self.problems(cfg), cfg)


class FakeResult:
    returncode, stdout, stderr = 0, "✓ created 2026-07-27-t\n", ""


class CaptureWiringTest(ResolverTestBase):
    def capture(self, routine, known, people):
        memory_sink._slug_cache.clear()
        memory_sink._slug_cache["/store"] = set(known)
        routine["memory"].update({"store": "/store", "type": "note",
                                  "extract": True})
        item = {"id": "slack:C1:1.0", "title": "t", "date": "2026-07-27",
                "frontmatter": {}}
        extraction = {"worthy": True, "type": "decision", "title": "T",
                      "people": people, "tags": ["x"], "body": "b"}
        seen = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            seen["args"] = args
            return FakeResult()

        ident = {"email": "me@example.com", "safe": True,
                 "person": {"email": "me@example.com", "name": "Memory Owner",
                            "slug": "memory-owner",
                            "resource_name": "people/memory-owner"}}
        with mock.patch.object(memory_sink, "_cli", side_effect=fake_cli), \
                mock.patch.object(memory_sink, "_extract",
                                  return_value=extraction), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value=ident), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "log"):
            memory_sink.capture(routine, item, "summary")
        args = seen["args"]
        people = args[args.index("--people") + 1] if "--people" in args else None
        return people, args[args.index("--tags") + 1]

    def test_resolved_slug_replaces_model_string(self):
        people, tags = self.capture(
            self.routine(), {"ada-lovelace", "jane-doe"}, ["Ada", "jane-doe"])
        self.assertEqual(people, "ada-lovelace,jane-doe")
        self.assertNotIn("people-unmapped", tags)

    def test_resolved_slug_must_still_be_known(self):
        people, tags = self.capture(self.routine(), {"jane-doe"}, ["Ada"])
        self.assertIsNone(people)
        self.assertIn("people-unmapped", tags)

    def test_unset_and_failing_hook_unchanged(self):
        for routine in ({"id": "r", "memory": {}}, self.routine("fail")):
            people, tags = self.capture(
                routine, {"ada-lovelace", "jane-doe"}, ["Ada", "jane-doe"])
            self.assertEqual(people, "jane-doe")
            self.assertIn("people-unmapped", tags)


if __name__ == "__main__":
    unittest.main()
