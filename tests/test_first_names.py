"""Bare first names in meeting captures, matched against that meeting's
attendees only. All people, emails and slugs are placeholders."""
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workspace_daemon import identity, memory_sink, person_resolver

# A fake directory: first.last aliases, full names, nicknames. Bare first
# names deliberately do not resolve on their own.
DIRECTORY = {
    "ada.lovelace": "ada-lovelace", "Ada Lovelace": "ada-lovelace",
    "ada.byron": "ada-byron", "Ada Byron": "ada-byron",
    "augusta.king": "augusta-king",
    "jose.nunez": "jose-nunez",
    "me.owner": "memory-owner",
}
FIRST = {
    "ada-lovelace": ["Ada"], "ada-byron": ["Ada"],
    "augusta-king": ["Augusta", "Gus"], "jose-nunez": ["José"],
    "memory-owner": ["Me"],
}
SCRIPT = """#!/usr/bin/env python3
import json, sys
names = json.load(sys.stdin)["names"]
open({log!r}, "a").write(json.dumps(names) + "\\n")
directory, first = {directory!r}, {first!r}
resolved = {{n: directory.get(n) for n in names}}
out = {{"resolved": resolved,
        "first_names": {{s: first[s] for s in resolved.values() if s}}}}
if {stale!r}:
    out["stale"] = True
print(json.dumps(out))
"""


def attendee(email, name="", role="calendar-attendee"):
    return {"email": email, "name": name, "role": role}


class Base(unittest.TestCase):
    def setUp(self):
        person_resolver.reset()
        self.addCleanup(person_resolver.reset)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patch = mock.patch.object(person_resolver, "log")
        self.log = patch.start()
        self.addCleanup(patch.stop)

    def routine(self, stale=False):
        path = self.tmp / f"resolver-{stale}.py"
        path.write_text(SCRIPT.format(
            log=str(self.tmp / "calls.log"), directory=DIRECTORY,
            first=FIRST, stale=stale))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return {"id": "r", "memory": {"person_resolver": [str(path)]}}


class FirstNameTest(Base):
    def matches(self, names, attendees, routine=None, **kw):
        return person_resolver.first_name_matches(
            routine or self.routine(), names, attendees, **kw)

    def test_unique_attendee_first_name_and_nickname(self):
        out = self.matches(
            ["Ada", "gus", "Jose", "ada-lovelace", "Ada Lovelace"],
            [attendee("ada.lovelace@example.com"),
             attendee("augusta.king@example.com"),
             attendee("jose.nunez@example.com")])
        self.assertEqual(out, {"Ada": "ada-lovelace", "gus": "augusta-king",
                               "Jose": "jose-nunez"})

    def test_shared_first_name_stays_unresolved(self):
        out = self.matches(
            ["Ada", "Gus"],
            [attendee("ada.lovelace@example.com"),
             attendee("ada.byron@example.com"),
             attendee("augusta.king@example.com")])
        self.assertEqual(out, {"Gus": "augusta-king"})
        self.assertTrue(any("shared by several attendees" in str(c)
                            for c in self.log.call_args_list))

    def test_nickname_colliding_with_a_given_name_is_ambiguous(self):
        out = self.matches(
            ["Gus"],
            [attendee("augusta.king@example.com"),
             attendee("gus.other@example.com", "Gus Other")],
            verified=[{"email": "gus.other@example.com", "slug": "gus-other"}])
        self.assertEqual(out, {})

    def test_unknown_guest_with_the_same_first_name_makes_it_ambiguous(self):
        out = self.matches(
            ["Ada", "Gus"],
            [attendee("ada.lovelace@example.com"),
             attendee("guest@other.example", "Ada Guest"),
             attendee("augusta.king@example.com")])
        self.assertEqual(out, {"Gus": "augusta-king"})

    def test_unknown_guest_alone_is_not_matched(self):
        self.assertEqual(
            self.matches(["Ada"], [attendee("guest@other.example", "Ada Guest")]),
            {})

    def test_absent_first_name_is_never_guessed(self):
        self.assertEqual(
            self.matches(["Grace"], [attendee("ada.lovelace@example.com")]),
            {})

    def test_owner_match_is_left_out(self):
        out = self.matches(
            ["Me", "Ada"],
            [attendee("me.owner@example.com"),
             attendee("ada.lovelace@example.com")],
            exclude={"memory-owner"})
        self.assertEqual(out, {"Ada": "ada-lovelace"})

    def test_without_a_resolver_uses_verified_people_and_names(self):
        out = person_resolver.first_name_matches(
            {"id": "r", "memory": {}}, ["Grace", "Ada"],
            [attendee("g.hopper@example.com", "Grace Hopper"),
             attendee("unknown@example.com", "Ada Unknown")],
            verified=[{"email": "g.hopper@example.com", "slug": "grace-hopper"}])
        self.assertEqual(out, {"Grace": "grace-hopper"})

    def test_stale_directory_still_resolves_and_warns_once(self):
        routine = self.routine(stale=True)
        rows = [attendee("ada.lovelace@example.com")]
        self.assertEqual(self.matches(["Ada"], rows, routine),
                         {"Ada": "ada-lovelace"})
        person_resolver._memo.clear()
        self.matches(["Ada"], [attendee("augusta.king@example.com")], routine)
        warnings = [c for c in self.log.call_args_list if "stale" in str(c)]
        self.assertEqual(len(warnings), 1)

    def test_bad_first_names_shape_is_no_answer(self):
        path = self.tmp / "bad.py"
        path.write_text(
            "#!/usr/bin/env python3\nimport json,sys\n"
            "n=json.load(sys.stdin)['names']\n"
            "print(json.dumps({'resolved':{x:None for x in n},"
            "'first_names':{'ada-lovelace':'Ada'}}))\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        routine = {"id": "r", "memory": {"person_resolver": [str(path)]}}
        self.assertEqual(person_resolver.canonicalize(routine, ["Ada"]),
                         ["Ada"])


class CaptureTest(Base):
    def setUp(self):
        super().setUp()
        self.addCleanup(identity._providers.clear)

    def capture(self, people, source_people):
        routine = self.routine()
        routine["memory"].update({"store": "/store", "type": "meeting",
                                  "extract": True})
        memory_sink._slug_cache.clear()
        memory_sink._slug_cache["/store"] = {"jane-doe"}
        item = {"id": "zoom:U@1", "source_id": "zoom:U", "title": "t",
                "date": "2026-07-27", "source_kind": "zoom",
                "frontmatter": {"source_people": source_people}}
        extraction = {"worthy": True, "type": "meeting", "title": "T",
                      "people": people, "tags": ["x"], "body": "b"}
        seen = {}

        def fake_cli(store, args, stdin_text=None, timeout=120):
            seen["args"] = args
            return mock.Mock(returncode=0, stdout="✓ created e\n", stderr="")

        ident = {"email": "me.owner@example.com", "safe": True,
                 "person": {"email": "me.owner@example.com", "name": "Me Owner",
                            "slug": "memory-owner"}}
        with mock.patch.object(memory_sink, "_cli", side_effect=fake_cli), \
                mock.patch.object(memory_sink, "_extract",
                                  return_value=extraction), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value=ident), \
                mock.patch.object(memory_sink, "_verified_source_people",
                                  return_value=([], [])), \
                mock.patch.object(memory_sink, "_prefetch_identities"), \
                mock.patch.object(memory_sink, "_commit_store"), \
                mock.patch.object(memory_sink, "_emit_capture_event"), \
                mock.patch.object(memory_sink, "log"):
            memory_sink.capture(routine, item, "summary")
        args = seen["args"]
        return args[args.index("--people") + 1] if "--people" in args else None

    def test_meeting_first_names_become_attendee_slugs(self):
        people = self.capture(
            ["Ada", "Gus", "Me", "jane-doe"],
            [attendee("ada.lovelace@example.com", role="calendar-organizer"),
             attendee("augusta.king@example.com"),
             attendee("ada.byron@example.com", role="gmail-sender"),
             attendee("me.owner@example.com")])
        self.assertEqual(people, "ada-lovelace,augusta-king,jane-doe")

    def test_ambiguous_first_name_is_dropped_as_before(self):
        people = self.capture(
            ["Ada", "jane-doe"],
            [attendee("ada.lovelace@example.com"),
             attendee("ada.byron@example.com")])
        self.assertEqual(people, "jane-doe")

    def test_non_calendar_sources_are_untouched(self):
        people = self.capture(
            ["Ada"], [attendee("ada.lovelace@example.com", role="gmail-sender")])
        self.assertIsNone(people)


if __name__ == "__main__":
    unittest.main()
