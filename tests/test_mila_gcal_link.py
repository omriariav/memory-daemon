"""A Mila meeting entry also carries the calendar event as `mila-gcal:<id>`, but
only when the extra id cannot change which entry the store CLI writes."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from workspace_daemon import config, memory_sink, mila_source

from test_mila_source import (
    LOCAL_MATCH, event, local_source, match_item, routine,
)

SID = "mila:X"
GCAL = f"{mila_source.CALENDAR_LINK_SCHEME}:evt1_20260801T100000Z"
STORE = "/store"


class FakeResult:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ItemExtraIdsTest(unittest.TestCase):
    def setUp(self):
        mila_source._reset_run()
        self.routine = routine("/tmp/store", local_source())

    def match(self, item, source=None):
        return mila_source.match_calendar(
            self.routine, source or local_source(), item)

    def test_accepted_match_carries_the_gcal_id(self):
        item = match_item(events=[event("evt-intro", "Alpha -- Beta - intro")])
        accepted, _ = self.match(item)
        self.assertTrue(accepted)
        self.assertEqual(item["extra_source_ids"], [f"{mila_source.CALENDAR_LINK_SCHEME}:evt-intro"])
        self.assertEqual(item["source_id"], "mila:R")

    def test_scheme_is_not_the_hand_logged_gcal(self):
        item = match_item(events=[event("evt-intro", "Alpha -- Beta - intro")])
        self.match(item)
        self.assertEqual(mila_source.CALENDAR_LINK_SCHEME, "mila-gcal")
        for value in item["extra_source_ids"]:
            self.assertFalse(value.startswith(("gcal:", "mila:")), value)

    def test_option_off_disables_it(self):
        item = match_item(events=[event("evt-intro", "Alpha -- Beta - intro")])
        accepted, _ = self.match(item, local_source(link_calendar_event=False))
        self.assertTrue(accepted)
        self.assertNotIn("extra_source_ids", item)

    def test_rejected_match_has_no_extra_ids(self):
        item = match_item(
            title="New Recording",
            events=[event("solo", "Focus", attendees=False)])
        with mock.patch.object(mila_source.local_llm, "chat_json",
                               return_value={"matched": False,
                                             "event_id": None,
                                             "confidence": "low",
                                             "reason": "unsure"}):
            accepted, _ = self.match(item)
        self.assertFalse(accepted)
        self.assertNotIn("extra_source_ids", item)

    def test_no_candidates_has_no_extra_ids(self):
        item = match_item(events=[])
        accepted, _ = self.match(item)
        self.assertFalse(accepted)
        self.assertNotIn("extra_source_ids", item)

    def _two_overlapping(self):
        long_event = event("e", "Alpha -- Beta - intro")
        first = match_item(events=[long_event])
        second = match_item(events=[long_event])
        second["source_id"] = "mila:OTHER"
        return first, second

    def test_duplicate_match_has_no_extra_ids(self):
        first, second = self._two_overlapping()
        self.assertTrue(self.match(first)[0])
        state = mila_source._run_state()
        state["captured"]["e"] = state["pending"].pop("e")
        accepted, match = self.match(second)
        self.assertFalse(accepted)
        self.assertIn("duplicate_of", match)
        self.assertNotIn("extra_source_ids", second)

    def test_held_match_has_no_extra_ids(self):
        first, second = self._two_overlapping()
        self.assertTrue(self.match(first)[0])
        accepted, match = self.match(second)
        self.assertFalse(accepted)
        self.assertTrue(match.get("held"))
        self.assertNotIn("extra_source_ids", second)


class StoreFixture(unittest.TestCase):
    def setUp(self):
        memory_sink._slug_cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.tmp.name

    def write(self, name, etype, source_ids, tags=("manual",), follows=(),
              date="2026-08-01"):
        directory = Path(self.store) / "memory" / "entries" / "2026" / "08"
        directory.mkdir(parents=True, exist_ok=True)
        front = {
            "id": name, "date": date, "type": etype, "title": f"T {name}",
            "people": [], "tags": list(tags), "source_ids": list(source_ids),
        }
        if follows:
            front["follows"] = list(follows)
        (directory / f"{name}.md").write_text(
            f"---\n{yaml.safe_dump(front)}---\nBody {name}.\n")

    def linked(self, extras=(GCAL,), etype="meeting"):
        with mock.patch.object(memory_sink, "log"):
            return memory_sink.linkable_extra_source_ids(
                self.store, SID, list(extras), etype)


class LinkableTest(StoreFixture):
    def test_kept_when_no_entry_carries_it(self):
        self.assertEqual(self.linked(), [GCAL])

    def test_kept_when_only_an_event_entry_carries_it(self):
        # No entry of ours: the CLI creates new, and with the extra it splits
        # beside the event entry, which also creates new.
        self.write("hand-event", "event", [GCAL])
        self.assertEqual(self.linked(), [GCAL])

    def test_hand_logged_gcal_id_does_not_touch_the_link(self):
        # Hand-logged entries carry `gcal:<id>`, a different id altogether.
        self.write("hand-note", "note", ["gcal:evt1_20260801T100000Z"])
        self.assertEqual(self.linked(), [GCAL])

    def test_dropped_when_another_meeting_carries_it(self):
        self.write("other-recording", "meeting", [GCAL, "mila:OTHER"])
        self.assertEqual(self.linked(), [])

    def test_dropped_when_an_open_todo_carries_it(self):
        self.write("hand-todo", "todo", [GCAL])
        self.assertEqual(self.linked(), [])

    def test_kept_when_the_todo_is_followed(self):
        self.write("hand-todo", "todo", [GCAL])
        self.write("outcome", "note", ["slack:C1:1.0"], follows=["hand-todo"])
        self.assertEqual(self.linked(), [GCAL])

    def test_open_pending_decision_is_dropped(self):
        self.write("hand-decision", "pending-decision", [GCAL])
        self.assertEqual(self.linked(), [])

    def test_dropped_when_only_a_note_carries_it(self):
        self.write("hand-note", "note", [GCAL])
        self.assertEqual(self.linked(), [])

    def test_note_capture_beside_a_note_carrier_is_dropped_too(self):
        self.write("hand-note", "note", [GCAL])
        self.assertEqual(self.linked(etype="note"), [])

    def test_recapture_keeps_it_when_our_entry_is_the_target(self):
        self.write("ours", "meeting", [SID, GCAL], tags=("auto-captured",),
                   date="2026-08-02")
        self.write("hand-note", "note", [GCAL], date="2026-07-01")
        self.assertEqual(self.linked(), [GCAL])

    def test_recapture_dropped_when_another_meeting_would_be_first(self):
        self.write("ours", "meeting", [SID], tags=("auto-captured",),
                   date="2026-08-02")
        self.write("other-recording", "meeting", [GCAL], date="2026-08-01")
        self.assertEqual(self.linked(), [])

    def test_extras_are_judged_one_by_one(self):
        other = "mila-gcal:other"
        self.write("hand-note", "note", [other])
        self.assertEqual(self.linked([other, GCAL]), [GCAL])

    def test_malformed_ids_are_never_linked(self):
        for bad in ("mila-gcal:a,b", "mila-gcal:a|b", "mila-gcal:a b",
                    "mila-gcal:a\tb", "nocolon", "mila-gcal:", ":rest",
                    "MILA-GCAL:x", "mila-gcal:\n", "", None, 7,
                    "mila-gcal:" + "x" * 300, SID):
            self.assertEqual(self.linked([bad]), [], msg=repr(bad))
        self.assertEqual(self.linked(["mila-gcal:a,b", GCAL, GCAL]), [GCAL])

    def test_store_read_error_fails_closed(self):
        with mock.patch.object(memory_sink, "_store_entries",
                               side_effect=OSError("boom")), \
                mock.patch.object(memory_sink, "log") as log:
            out = memory_sink.linkable_extra_source_ids(
                self.store, SID, [GCAL], "meeting", rid="r")
        self.assertEqual(out, [])
        self.assertIn("WARN", log.call_args[0][0])
        self.assertIn(GCAL, log.call_args[0][0])

    def test_unparseable_entry_fails_closed(self):
        directory = Path(self.store) / "memory" / "entries"
        directory.mkdir(parents=True)
        (directory / "bad.md").write_text(f"---\n[unclosed: {GCAL}\n---\nx\n")
        self.assertEqual(self.linked(), [])


class CaptureArgvTest(StoreFixture):
    def setUp(self):
        super().setUp()
        self.item = {
            "id": "mila:X@hash", "source_id": SID, "title": "Meeting",
            "date": "2026-08-01", "frontmatter": {},
            "extra_source_ids": [GCAL],
        }

    def capture(self, store=None, dry_run=False):
        cfg = {"id": "r", "memory": {"store": store or self.store,
                                     "type": "meeting", "extract": False}}
        calls = []

        def fake_cli(store, args, stdin_text=None, timeout=120):
            calls.append(list(args))
            return FakeResult("✓ created 2026-08-01-meeting\n")

        with mock.patch.object(memory_sink, "_cli", side_effect=fake_cli), \
                mock.patch.object(memory_sink, "_authenticated_identity",
                                  return_value={"email": None, "person": None,
                                                "safe": False}), \
                mock.patch.object(memory_sink.subprocess, "run",
                                  return_value=FakeResult()), \
                mock.patch.object(memory_sink, "log") as log:
            out = memory_sink.capture(cfg, self.item, "s", dry_run=dry_run)
        return out, calls, log

    def ids(self, calls):
        args = calls[0]
        return args[args.index("--source-ids") + 1]

    def test_passes_both_ids_when_nothing_carries_the_gcal_id(self):
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), f"{SID},{GCAL}")
        self.assertEqual(out["memory_extra_source_ids"], [GCAL])
        self.assertEqual(out["memory_source_id"], SID)

    def test_split_beside_an_event_entry_keeps_the_link(self):
        self.write("hand-event", "event", [GCAL])
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), f"{SID},{GCAL}")

    def test_dropped_ids_are_not_passed_nor_reported(self):
        for name, etype in (("o-meeting", "meeting"), ("o-todo", "todo"),
                            ("o-note", "note")):
            self.write(name, etype, [GCAL])
            out, calls, log = self.capture()
            self.assertEqual(self.ids(calls), SID, msg=etype)
            self.assertNotIn("memory_extra_source_ids", out)
            logged = " ".join(str(c.args[0]) for c in log.call_args_list)
            self.assertIn("dropped", logged)
            (Path(self.store) / "memory" / "entries" / "2026" / "08"
             / f"{name}.md").unlink()

    def test_closed_todo_keeps_the_link(self):
        self.write("hand-todo", "todo", [GCAL])
        self.write("outcome", "note", ["slack:C1:1.0"], follows=["hand-todo"])
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), f"{SID},{GCAL}")

    def test_recapture_of_our_own_entry_keeps_the_link(self):
        self.write("ours", "meeting", [SID, GCAL], tags=("auto-captured",),
                   date="2026-08-02")
        self.write("hand-note", "note", [GCAL], date="2026-07-01")
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), f"{SID},{GCAL}")

    def test_derived_auto_id_path_still_carries_the_extra(self):
        self.write("hand-meeting", "meeting", [SID])  # untagged: forces :auto
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), f"{SID}:auto,{GCAL}")
        self.assertEqual(out["memory_source_id"], f"{SID}:auto")
        self.assertEqual(out["memory_extra_source_ids"], [GCAL])

    def test_derived_auto_id_path_drops_an_extra_the_hand_entry_carries(self):
        self.write("hand-meeting", "meeting", [SID, GCAL])
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), f"{SID}:auto")
        self.assertNotIn("memory_extra_source_ids", out)

    def test_store_read_error_still_captures_without_extras(self):
        with mock.patch.object(memory_sink, "_store_entries",
                               side_effect=OSError("boom")):
            out, calls, log = self.capture()
        self.assertEqual(self.ids(calls), SID)
        self.assertEqual(out["memory"], "created")
        self.assertNotIn("memory_extra_source_ids", out)
        logged = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn("WARN", logged)

    def test_malformed_extras_are_never_passed(self):
        self.item["extra_source_ids"] = [
            "mila-gcal:a,b", "mila-gcal:a|b", "mila-gcal:a b", "nocolon", 7,
            None]
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), SID)
        self.assertNotIn("memory_extra_source_ids", out)

    def test_non_list_extras_are_ignored(self):
        self.item["extra_source_ids"] = GCAL
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), SID)

    def test_no_extras_means_the_old_argv(self):
        del self.item["extra_source_ids"]
        out, calls, _ = self.capture()
        self.assertEqual(self.ids(calls), SID)
        self.assertNotIn("memory_extra_source_ids", out)

    def test_dry_run_mentions_the_extras(self):
        out, calls, log = self.capture(dry_run=True)
        self.assertEqual(out, {"memory": "dry_run"})
        self.assertEqual(calls, [])
        logged = " ".join(str(c.args[0]) for c in log.call_args_list)
        self.assertIn(GCAL, logged)


class ConfigTest(unittest.TestCase):
    def problems(self, **extra):
        source = {"kind": "mila", "recordings_file": "/tmp/mila/recordings.json",
                  "match": dict(LOCAL_MATCH), **extra}
        return config.validate(routine("/tmp/store", source))

    def test_booleans_are_valid(self):
        self.assertEqual(self.problems(link_calendar_event=True), [])
        self.assertEqual(self.problems(link_calendar_event=False), [])
        self.assertEqual(self.problems(), [])

    def test_non_boolean_is_a_problem(self):
        for bad in ("yes", 1, 0, ["x"], {}):
            problems = self.problems(link_calendar_event=bad)
            self.assertTrue(
                any("link_calendar_event" in p for p in problems),
                msg=repr(bad))

    def test_other_source_kinds_reject_the_key(self):
        source = {"kind": "whisper", "transcriptions_dir": "/tmp/w",
                  "link_calendar_event": True}
        problems = config.validate(routine("/tmp/store", source))
        self.assertTrue(
            any("link_calendar_event" in p and "only supported for kind mila"
                in p for p in problems), msg=str(problems))
        source = {"kind": "mila", "recordings_file": "/tmp/mila/r.json",
                  "match": dict(LOCAL_MATCH), "link_calendar_event": True}
        self.assertEqual(config.validate(routine("/tmp/store", source)), [])

    def test_example_routines_validate(self):
        root = Path(__file__).resolve().parent.parent / "routines"
        names = sorted(root.glob("_example-*.yaml"))
        self.assertTrue(any("mila" in p.name for p in names))
        for path in names:
            loaded = yaml.safe_load(path.read_text())
            if not isinstance(loaded, dict):
                continue
            problems = [p for p in config.validate(loaded)
                        if "link_calendar_event" in p]
            self.assertEqual(problems, [], msg=path.name)
        mila = yaml.safe_load(
            (root / "_example-mila-to-memory.yaml").read_text())
        self.assertIs(mila["source"]["link_calendar_event"], True)


if __name__ == "__main__":
    unittest.main()
