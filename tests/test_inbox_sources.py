"""The events source (synthetic fixtures) and command sources."""
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import items as itm, settings, sources

NOW = 1_700_000_000.0


def iso(offset_h=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(NOW + offset_h * 3600))


def ev(source_id, kind, title, people=(), ago_h=1.0, routine="r", details=None,
       memory_id=None):
    return events.build(routine, kind, memory_id, source_id, title,
                        list(people), ts=iso(-ago_h), details=details)


def write_events(base, rows, name="events.jsonl"):
    state = Path(base) / "state"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / name, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


class EventsMappingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name

    def load(self, **kw):
        return sources.load_events(self.base, now=NOW, **kw)

    def test_missing_file_is_empty_not_an_error(self):
        res = self.load()
        self.assertEqual((res.items, res.error), ([], None))

    def test_gmail_maps_to_email_with_open_only(self):
        write_events(self.base, [ev("gmail:18c0ffee12345678", "new",
                                    "Invoice for May", ["Bo Sample"])])
        (item,) = self.load().items
        self.assertEqual(item["source"], "email")
        self.assertEqual(item["actions"], ["open"])
        self.assertEqual(item["section"], "fyi")
        self.assertEqual(item["sender"], "Bo Sample")
        self.assertTrue(item["id"].startswith("events:"))
        self.assertEqual(itm.validate_item(item), [])
        self.assertTrue(item["link"].startswith("https://mail.google.com/"))

    def test_gmail_suffixed_id_gets_no_link(self):
        write_events(self.base, [ev("gmail:abcdef0123456789:followup-open",
                                    "new", "Follow up")])
        (item,) = self.load().items
        self.assertIsNone(item["link"])

    def test_calendar_kinds_and_details(self):
        write_events(self.base, [
            ev("gcal:e1", "update", "Weekly sync", ["Cy Demo"], ago_h=3,
               details={"start": "2023-11-15T10:00:00+00:00",
                        "end": "2023-11-15T10:30:00+00:00",
                        "changed": {"start": ["09:00", "10:00"]}}),
            ev("gcal:e2", "cancel", "Offsite", ago_h=2),
            ev("gcal:e3", "rsvp", "Review", ["Di Test"], ago_h=1,
               details={"changed": {"Di Test": ["needsAction", "accepted"]}}),
            ev("gcal:e4", "new", "Lunch", ago_h=1),
        ])
        by_title = {i["gist"].split(": ")[1].split(" (")[0]: i
                    for i in self.load().items}
        self.assertEqual(set(by_title), {"Weekly sync", "Offsite", "Review", "Lunch"})
        sync = by_title["Weekly sync"]
        self.assertEqual(sync["source"], "calendar")
        self.assertEqual(sync["actions"], ["ack", "open"])
        self.assertTrue(sync["gist"].startswith("Updated: "))
        texts = [m["text"] for m in sync["thread"]]
        self.assertIn("09:00 -> 10:00", texts)
        self.assertTrue(by_title["Offsite"]["gist"].startswith("Cancelled: "))
        self.assertTrue(by_title["Review"]["gist"].startswith("RSVP: "))
        for item in by_title.values():
            self.assertEqual(itm.validate_item(item), [])

    def test_unknown_calendar_kind_is_skipped(self):
        write_events(self.base, [ev("gcal:e9", "teleport", "X")])
        self.assertEqual(self.load().items, [])

    def test_recordings(self):
        write_events(self.base, [ev("mila:r1", "new", "Standup notes"),
                                 ev("zoom:u1", "new", "Planning call")])
        rows = self.load().items
        self.assertEqual({i["source"] for i in rows}, {"recording"})
        self.assertEqual({i["where"] for i in rows}, {"mila", "zoom"})
        for i in rows:
            self.assertEqual(i["actions"], ["ack", "open"])

    def test_alert_urgency_by_key(self):
        def alert(key, ago):
            return ev(f"daemon:{key}", "alert", f"alert {key}", ago_h=ago,
                      details={"key": key})
        write_events(self.base, [
            alert("budget-hit:2023-11-15", 5), alert("connector:srv", 4),
            alert("attention:routine-a", 3), alert("budget-80:2023-11-15", 2)])
        by_key = {i["gist"].split()[1]: i for i in self.load().items}
        for key in ("budget-hit:2023-11-15", "connector:srv", "attention:routine-a"):
            self.assertEqual(by_key[key]["section"], "urgent", key)
            self.assertEqual(by_key[key]["urgency"], "urgent")
        low = by_key["budget-80:2023-11-15"]
        self.assertEqual((low["section"], low["urgency"]), ("fyi", "digest"))
        self.assertEqual(low["source"], "alert")
        self.assertEqual(low["actions"], ["ack", "open"])

    def test_non_alert_daemon_events_are_skipped(self):
        write_events(self.base, [ev("daemon:x", "note", "nope")])
        self.assertEqual(self.load().items, [])

    def test_custom_rules_replace_defaults(self):
        write_events(self.base, [
            ev("gcal:e1", "cancel", "Soon", ago_h=1,
               details={"start": iso(5)}),
            ev("gcal:e2", "cancel", "Later", ago_h=1,
               details={"start": iso(100)}),
            ev("daemon:connector:s", "alert", "c", details={"key": "connector:s"})])
        rules = [{"source": "gcal", "kind": "cancel", "starts_within_h": 24}]
        by = {i["gist"]: i for i in self.load(rules=rules).items}
        urgent = {g.split(": ")[1].split(" (")[0] for g, i in by.items()
                  if i["section"] == "urgent"}
        self.assertEqual(urgent, {"Soon"})
        self.assertEqual(by["c"]["section"], "fyi")

    def test_title_regex_rule(self):
        write_events(self.base, [ev("gmail:aaaaaaaaaa", "new", "URGENT: pay"),
                                 ev("gmail:bbbbbbbbbb", "new", "hello")])
        res = self.load(rules=[{"source": "gmail", "title_regex": "^urgent"}])
        sec = {i["gist"]: i["section"] for i in res.items}
        self.assertEqual(sec, {"URGENT: pay": "urgent", "hello": "fyi"})

    def test_window_default_48h_and_configurable(self):
        write_events(self.base, [ev("mila:old", "new", "Old", ago_h=50),
                                 ev("mila:new", "new", "New", ago_h=47)])
        self.assertEqual([i["gist"] for i in self.load().items], ["New"])
        self.assertEqual(len(self.load(window_h=72).items), 2)
        self.assertEqual(len(self.load(window_h=1).items), 0)

    def test_rotation_file_is_read_too(self):
        write_events(self.base, [ev("mila:a", "new", "From rotation")],
                     name="events.jsonl.1")
        write_events(self.base, [ev("mila:b", "new", "Live")])
        self.assertEqual({i["gist"] for i in self.load().items},
                         {"From rotation", "Live"})

    def test_latest_event_per_source_id_wins(self):
        write_events(self.base, [
            ev("gcal:e1", "new", "Standup", ago_h=5),
            ev("gcal:e1", "update", "Standup", ago_h=1)])
        (item,) = self.load().items
        self.assertTrue(item["gist"].startswith("Updated"))

    def test_torn_and_garbage_lines_are_skipped(self):
        write_events(self.base, [
            "not json", '{"v": 1', "[]", "",
            ev("mila:ok", "new", "Fine"),
            json.dumps({"v": 1, "id": "x", "ts": "garbage", "source": "mila",
                        "kind": "new", "title": "bad ts"}),
            json.dumps({"id": "y", "ts": iso(-1), "source": "mila", "kind": "new",
                        "title": "t", "people": "notalist"})])
        titles = {i["gist"] for i in self.load().items}
        self.assertIn("Fine", titles)
        self.assertNotIn("bad ts", titles)

    def test_untrusted_text_is_cleaned(self):
        write_events(self.base, [ev(
            "gmail:aaaaaaaaaaaa", "new",
            "\x1b[2Jhello\nworld‮[b]x[/b]",
            ["\x1b]0;pwn\x07Eve\r\nMallory"])])
        (item,) = self.load().items
        for field in ("gist", "sender"):
            self.assertNotIn("\x1b", item[field])
            self.assertNotIn("\n", item[field])
            self.assertNotIn("‮", item[field])
        self.assertIn("[b]x[/b]", item["gist"])  # kept as inert text
        self.assertEqual(itm.validate_item(item), [])

    def test_reading_does_not_modify_or_lock(self):
        write_events(self.base, [ev("mila:a", "new", "A")])
        path = Path(self.base) / "state" / "events.jsonl"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.load()
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertFalse((Path(self.base) / "state" / "events.lock").exists())

    def test_age_reports_newest_event(self):
        write_events(self.base, [ev("mila:a", "new", "A", ago_h=2)])
        self.assertAlmostEqual(self.load().age_min, 120.0, places=0)


class CommandSourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def script(self, body):
        path = Path(self.tmp.name) / "src.py"
        path.write_text(textwrap.dedent(body))
        return [sys.executable, str(path)]

    def run_src(self, body, timeout=10):
        return sources.run_command_source("cmd", self.script(body), timeout)

    ITEM = {"v": 1, "id": "cmd:1", "source": "slack", "section": "needs_reply",
            "urgency": "digest", "sender": "Ann", "where": "#room",
            "gist": "hi", "ts": 1.0, "link": None, "thread": [], "draft": None,
            "actions": ["reply"], "meta": {"ch": "C1"}}

    def test_valid_output(self):
        doc = {"v": 1, "items": [self.ITEM], "stale": True, "age_min": 12,
               "error": None}
        res = self.run_src(f"""
            import json, sys
            assert sys.argv[1] == "list", sys.argv
            print(json.dumps({doc!r}))""")
        self.assertEqual(res.error, None)
        self.assertEqual(res.items[0]["id"], "cmd:1")
        self.assertEqual(res.items[0]["meta"], {"ch": "C1"})
        self.assertTrue(res.stale)
        self.assertEqual(res.age_min, 12)

    def test_malformed_output_is_an_error_not_a_crash(self):
        for body in ("print('hello')", "print('{\"v\": 1, \"items\": 5}')",
                     "print('')", "import sys; sys.stdout.write('\\xff\\xfe')"):
            res = self.run_src(body)
            self.assertEqual(res.items, [], body)
            self.assertIn("bad output", res.error, body)

    def test_nonzero_exit(self):
        res = self.run_src("""
            import sys
            sys.stderr.write("boom \\x1b[31mred\\n")
            sys.exit(3)""")
        self.assertEqual(res.items, [])
        self.assertIn("exit 3", res.error)
        self.assertIn("boom", res.error)
        self.assertNotIn("\x1b", res.error)

    def test_timeout_kills_the_process(self):
        marker = Path(self.tmp.name) / "survived"
        started = time.time()
        res = self.run_src(f"""
            import time
            time.sleep(5)
            open({str(marker)!r}, "w").write("x")""", timeout=0.5)
        self.assertLess(time.time() - started, 4)
        self.assertIn("timed out", res.error)
        self.assertEqual(res.items, [])
        time.sleep(0.2)
        self.assertFalse(marker.exists())

    def test_missing_binary(self):
        res = sources.run_command_source("cmd", ["/no/such/binary-xyz"], 2)
        self.assertIn("cannot start", res.error)

    def test_one_bad_row_does_not_hide_the_rest(self):
        bad = dict(self.ITEM, id="cmd:2", section="nope")
        doc = {"v": 1, "items": [self.ITEM, bad]}
        res = self.run_src(f"import json; print(json.dumps({doc!r}))")
        self.assertEqual([i["id"] for i in res.items], ["cmd:1"])
        self.assertIn("dropped", res.error)

    def test_stdin_is_closed(self):
        res = self.run_src("""
            import sys, json
            data = sys.stdin.read()
            print(json.dumps({"v": 1, "items": [], "error": "stdin=%r" % data}))""")
        self.assertEqual(res.error, "stdin=''")

    def test_act_round_trip(self):
        argv = self.script("""
            import sys, json
            assert sys.argv[1] == "act"
            req = json.load(sys.stdin)
            print(json.dumps({"ok": True, "message": "did " + req["action"]}))""")
        out = sources.run_command_act(
            argv, {"action": "done", "id": "x", "meta": {}})
        self.assertEqual(out, {"ok": True, "message": "did done"})

    def test_act_failures_never_raise(self):
        out = sources.run_command_act(
            self.script("import sys; sys.exit(1)"),
            {"action": "done", "id": "x", "meta": {}})
        self.assertFalse(out["ok"])
        out = sources.run_command_act(
            self.script("print('nope')"),
            {"action": "done", "id": "x", "meta": {}})
        self.assertIn("bad response", out["message"])


class CollectTest(unittest.TestCase):
    def test_collect_mixes_sources_and_flags_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_events(tmp, [ev("mila:a", "new", "A", ago_h=1)])
            item = dict(CommandSourceTest.ITEM)
            dup_id = sources.load_events(tmp, now=NOW).items[0]["id"]
            doc = {"v": 1, "items": [item, dict(item, id=dup_id)]}
            script = Path(tmp) / "s.py"
            script.write_text(f"import json; print(json.dumps({doc!r}))")
            cfg = settings.validate({"sources": [
                {"name": "events", "type": "events"},
                {"name": "cmd", "type": "command",
                 "argv": [sys.executable, str(script)]}]})
            results = sources.collect(cfg, tmp, now=NOW)
            self.assertEqual([r.name for r in results], ["events", "cmd"])
            self.assertEqual([i["id"] for i in results[1].items], ["cmd:1"])
            self.assertIn("duplicate", results[1].error)


if __name__ == "__main__":
    unittest.main()
