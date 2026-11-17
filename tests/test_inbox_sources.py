"""The events source (synthetic fixtures) and command sources."""
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_a_well_formed_memory_id_is_copied_into_meta(self):
        write_events(self.base, [ev("gmail:18c0ffee12345678", "new", "Mail",
                                    memory_id="2030-01-08-invoice-3f9a")])
        (item,) = self.load().items
        self.assertEqual(item["meta"]["memory_id"], "2030-01-08-invoice-3f9a")

    def test_a_missing_or_malformed_memory_id_is_left_out(self):
        bad = ["", "-leading-dash", "UPPER", "has space", "a/b", "x" * 202,
               "ok\n", 5, ["a"]]
        rows = [ev("gmail:18c0ffee1234567a", "new", "None")]
        for n, mid in enumerate(bad):
            row = ev(f"gmail:18c0ffee1234567{n}", "new", f"Bad {n}")
            row["memory_id"] = mid
            rows.append(row)
        write_events(self.base, rows)
        items = self.load().items
        self.assertEqual(len(items), len(bad) + 1)
        for item in items:
            self.assertNotIn("memory_id", item["meta"])
        edge = ev("gmail:18c0ffee12345670", "new", "Long", memory_id="a" * 201)
        write_events(self.base, [edge])
        got = {i["gist"]: i for i in self.load().items}
        self.assertEqual(got["Long"]["meta"]["memory_id"], "a" * 201)

    def mail_event(self, details, source_id="gmail:18c0ffee12345678", **kw):
        return ev(source_id, "new", "Memory title", ["Bo Sample"],
                  details=details, **kw)

    def test_mail_event_shows_sender_subject_snippet_and_thread_link(self):
        write_events(self.base, [self.mail_event({
            "mail_from": "Ann Example", "mail_subject": "Plan for May",
            "mail_snippet": "Can we move the review?",
            "mail_thread_id": "18c0ffee12345679"}, memory_id="2030-01-08-plan")])
        (item,) = self.load(gmail_account=2).items
        self.assertEqual((item["sender"], item["where"]),
                         ("Ann Example", "Plan for May"))
        self.assertEqual(item["gist"], "Memory title")      # unchanged
        self.assertEqual([(m["who"], m["text"]) for m in item["thread"]],
                         [("Ann Example", "Can we move the review?")])
        self.assertEqual(item["thread"][0]["ts"], item["ts"])
        self.assertEqual(item["link"],
                         "https://mail.google.com/mail/u/2/#all/18c0ffee12345679")
        self.assertEqual(item["actions"], ["open"])
        self.assertEqual(item["meta"]["memory_id"], "2030-01-08-plan")
        self.assertEqual(itm.validate_item(item), [])

    def test_mail_event_without_the_new_fields_looks_as_before(self):
        write_events(self.base, [self.mail_event(None)])
        (item,) = self.load().items
        self.assertEqual((item["sender"], item["where"], item["thread"]),
                         ("Bo Sample", "mail", []))
        self.assertTrue(item["link"].endswith("/#all/18c0ffee12345678"))

    def test_mail_fields_are_partial_capped_and_never_an_address(self):
        write_events(self.base, [self.mail_event({"mail_subject": "Only this"},
                                                 "gmail:abcdef0123456789")])
        (item,) = self.load().items
        self.assertEqual((item["sender"], item["where"], item["thread"]),
                         ("Bo Sample", "Only this", []))
        # An old or hand-written event: the reader enforces the same rules.
        bad = self.mail_event({
            "mail_from": "Ann <ann@example.com>", "mail_subject": "s" * 300,
            "mail_snippet": "t" * 500}, "gmail:abcdef0123456780")
        bad["details"]["mail_from"] = "Ann <ann@example.com>"
        addr = self.mail_event({"mail_from": "x"}, "gmail:abcdef0123456781")
        addr["details"]["mail_from"] = "ann\uff20example.com"
        write_events(self.base, [bad, addr])
        got = {i["id"]: i for i in self.load().items}
        shown = [i for i in got.values() if i["where"] != "Only this"]
        self.assertEqual(len(shown), 2)
        for i in shown:
            self.assertNotIn("@", i["sender"])
        capped = next(i for i in shown if i["thread"])
        self.assertEqual(capped["sender"], "Ann")
        self.assertEqual(len(capped["where"]), 200)
        self.assertEqual(len(capped["thread"][0]["text"]), 300)
        fallback = next(i for i in shown if not i["thread"])
        self.assertEqual(fallback["sender"], "Bo Sample")

    def test_a_mail_thread_id_gives_a_link_when_the_source_id_has_none(self):
        write_events(self.base, [self.mail_event(
            {"mail_thread_id": "18c0ffee12345678"}, "gmail:not-a-thread")])
        (item,) = self.load().items
        self.assertTrue(item["link"].endswith("/#all/18c0ffee12345678"))
        self.assertEqual(item["actions"], ["open"])

    def test_gmail_followup_id_uses_the_thread_segment(self):
        write_events(self.base, [ev("gmail:abcdef0123456789:followup-open",
                                    "new", "Follow up")])
        (item,) = self.load().items
        self.assertTrue(item["link"].endswith("/#all/abcdef0123456789"))
        self.assertEqual(item["actions"], ["open"])

    def test_gmail_message_version_suffix_is_stripped(self):
        write_events(self.base, [ev("gmail:abcdef0123456789@18c0ffee", "new", "V")])
        (item,) = self.load().items
        self.assertTrue(item["link"].endswith("/#all/abcdef0123456789"))

    def test_gmail_id_that_is_not_a_thread_has_no_link_and_no_actions(self):
        write_events(self.base, [ev("gmail:not-a-thread", "new", "Odd"),
                                 ev("gmail:", "new", "Empty")])
        for item in self.load().items:
            self.assertIsNone(item["link"])
            self.assertEqual(item["actions"], [])  # nothing to open

    def test_gmail_account_index_is_configurable(self):
        write_events(self.base, [ev("gmail:18c0ffee12345678", "new", "Hi")])
        (item,) = sources.load_events(self.base, now=NOW, gmail_account=2).items
        self.assertIn("/mail/u/2/#all/18c0ffee12345678", item["link"])
        (item,) = self.load().items
        self.assertIn("/mail/u/0/", item["link"])

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
                    for i in self.load(show_rsvp_replies=True).items}
        self.assertEqual(set(by_title), {"Weekly sync", "Offsite", "Review", "Lunch"})
        sync = by_title["Weekly sync"]
        self.assertEqual(sync["source"], "calendar")
        self.assertEqual(sync["actions"], ["ack"])  # no link, so no open
        self.assertTrue(sync["gist"].startswith("Updated: "))
        texts = [m["text"] for m in sync["thread"]]
        self.assertIn("09:00 -> 10:00", texts)
        self.assertTrue(by_title["Offsite"]["gist"].startswith("Cancelled: "))
        self.assertTrue(by_title["Review"]["gist"].startswith("RSVP: "))
        for item in by_title.values():
            self.assertEqual(itm.validate_item(item), [])

    def test_rsvp_replies_are_hidden_unless_asked_for(self):
        write_events(self.base, [
            ev("gcal:e1", "rsvp", "Review", ["Di Test"], ago_h=1),
            ev("gcal:e2", "new", "Lunch", ago_h=1),
            ev("gcal:e3", "update", "Sync", ago_h=1),
            ev("gcal:e4", "cancel", "Offsite", ago_h=1)])
        gists = lambda **kw: sorted(i["gist"].split(":")[0]
                                    for i in self.load(**kw).items)
        self.assertEqual(gists(), ["Cancelled", "New invite", "Updated"])
        self.assertEqual(gists(show_rsvp_replies=False),
                         ["Cancelled", "New invite", "Updated"])
        self.assertEqual(gists(show_rsvp_replies=True),
                         ["Cancelled", "New invite", "RSVP", "Updated"])

    def test_show_rsvp_replies_setting(self):
        from workspace_daemon.inbox import settings
        self.assertIs(settings.default_settings()["show_rsvp_replies"], False)
        self.assertIs(settings.validate(
            {"show_rsvp_replies": True})["show_rsvp_replies"], True)
        for bad in ("yes", 1, None, []):
            with self.assertRaises(settings.SettingsError):
                settings.validate({"show_rsvp_replies": bad})

    def test_unit_passes_the_setting_through(self):
        from workspace_daemon.inbox import settings
        write_events(self.base, [ev("gcal:e1", "rsvp", "Review", ago_h=1)])
        for flag, count in ((False, 0), (True, 1)):
            cfg = settings.validate({"show_rsvp_replies": flag})
            (unit,) = [u for u in sources.make_units(cfg, self.base)
                       if u.name == "events"]
            (res,) = unit.run(NOW, False)
            self.assertEqual(len(res.items), count)

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
            self.assertEqual(i["actions"], ["ack"])
            self.assertIsNone(i["link"])

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
        self.assertEqual(low["actions"], ["ack"])

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

    def test_event_in_both_files_is_listed_once(self):
        row = ev("mila:dup", "new", "Same event", ago_h=1)
        write_events(self.base, [row], name="events.jsonl.1")
        write_events(self.base, [row])
        self.assertEqual(len(self.load().items), 1)
        self.assertEqual(len(sources.read_events(self.base)), 1)

    def test_live_file_is_read_before_the_rotation(self):
        order = []
        real = sources._read_file
        def spy(path):
            order.append(os.path.basename(path))
            return real(path)
        with mock.patch.object(sources, "_read_file", spy):
            sources.read_events(self.base)
        self.assertEqual(order, ["events.jsonl", "events.jsonl.1"])

    def test_rotation_between_the_two_reads_loses_nothing(self):
        """The writer rotates live -> .1 after we read live but before we read
        .1: the old events must still be seen (once)."""
        write_events(self.base, [ev("mila:old", "new", "Old", ago_h=2)])
        state = Path(self.base) / "state"
        real = sources._read_file
        def rotate_after_live(path):
            got = real(path)
            if path.endswith("events.jsonl"):
                os.replace(state / "events.jsonl", state / "events.jsonl.1")
                write_events(self.base, [ev("mila:new", "new", "New", ago_h=1)])
            return got
        with mock.patch.object(sources, "_read_file", rotate_after_live):
            titles = [e["title"] for e in sources.read_events(self.base)]
        self.assertEqual(titles, ["Old"])  # next poll sees New; Old is not lost
        self.assertEqual({e["title"] for e in sources.read_events(self.base)},
                         {"Old", "New"})

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

    def test_output_is_capped_while_reading_and_the_group_is_killed(self):
        marker = Path(self.tmp.name) / "child-survived"
        started = time.time()
        res = self.run_src(f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c",
                "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"])
            chunk = b"x" * 65536
            out = sys.stdout.buffer
            while True:
                out.write(chunk); out.flush()""", timeout=20)
        self.assertLess(time.time() - started, 10)
        self.assertIn("larger than", res.error)
        self.assertEqual(res.items, [])
        time.sleep(3.3)
        self.assertFalse(marker.exists(), "child outlived the killed group")

    def test_stderr_flood_is_bounded_and_does_not_block_the_child(self):
        res = self.run_src("""
            import sys, json
            sys.stderr.write("e" * (20 * 1024 * 1024))
            sys.stderr.write("\\nTAIL-MARK")
            print(json.dumps({"v": 1, "items": [], "error": None}))
            sys.exit(2)""")
        self.assertIn("exit 2", res.error)
        self.assertIn("TAIL-MARK", res.error)
        self.assertLess(len(res.error), 400)

    def test_stderr_flood_on_success_is_harmless(self):
        res = self.run_src("""
            import sys, json
            sys.stderr.write("e" * (5 * 1024 * 1024))
            print(json.dumps({"v": 1, "items": [], "error": None}))""")
        self.assertEqual((res.items, res.error), ([], None))

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

    def test_act_timeout_kills_the_process_group(self):
        marker = Path(self.tmp.name) / "act-child"
        argv = self.script(f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c",
                "import time; time.sleep(2); open({str(marker)!r}, 'w').write('x')"])
            time.sleep(30)""")
        started = time.time()
        out = sources.run_command_act(
            argv, {"action": "done", "id": "x", "meta": {}}, timeout_s=0.5)
        self.assertLess(time.time() - started, 5)
        self.assertFalse(out["ok"])
        self.assertIn("timed out", out["message"])
        time.sleep(2.5)
        self.assertFalse(marker.exists(), "child outlived the killed group")

    def test_act_oversized_response_is_refused(self):
        argv = self.script("""
            import sys
            chunk = b"x" * 65536
            while True:
                sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()""")
        out = sources.run_command_act(
            argv, {"action": "done", "id": "x", "meta": {}}, timeout_s=20)
        self.assertFalse(out["ok"])
        self.assertIn("too large", out["message"])

    def test_act_failures_never_raise(self):
        out = sources.run_command_act(
            self.script("import sys; sys.exit(1)"),
            {"action": "done", "id": "x", "meta": {}})
        self.assertFalse(out["ok"])
        out = sources.run_command_act(
            self.script("print('nope')"),
            {"action": "done", "id": "x", "meta": {}})
        self.assertIn("bad response", out["message"])


class AlertRulesTest(unittest.TestCase):
    """Stale and recovered daemon alerts (synthetic fixtures)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def alert(self, key, ago_h, **details):
        return ev(f"daemon:{key}-{ago_h}", "alert", f"alert {key}", ago_h=ago_h,
                  details=dict({"key": key}, **details))

    def attention(self, routine, ago_h, issues=("last run",)):
        return self.alert(f"attention:{routine}", ago_h, routine=routine,
                          issues=list(issues))

    def active(self, doc):
        """Write the daemon's alerts.json (key -> stamp, or pending)."""
        state = self.base / "state"
        state.mkdir(exist_ok=True)
        text = doc if isinstance(doc, str) else json.dumps(doc)
        (state / "alerts.json").write_text(text)

    def load(self, **kw):
        res = sources.load_events(self.base, now=NOW, **kw)
        return {i["meta"].get("key", i["gist"]): i for i in res.items}

    def test_old_alert_drops_to_fyi_and_the_age_is_configurable(self):
        write_events(self.base, [self.attention("a", 7), self.attention("b", 5)])
        got = self.load()
        self.assertEqual((got["attention:a"]["section"], got["attention:a"]["urgency"]),
                         ("fyi", "digest"))
        self.assertEqual(got["attention:b"]["section"], "urgent")
        self.assertNotIn("resolved", got["attention:a"]["meta"])
        self.assertEqual(self.load(alert_max_age_h=8)["attention:a"]["section"], "urgent")
        self.assertEqual(self.load(alert_max_age_h=1)["attention:b"]["section"], "fyi")

    def test_collect_passes_the_setting_through(self):
        write_events(self.base, [self.attention("a", 3)])
        cfg = settings.validate({"alert_urgent_max_age_h": 2})
        (res,) = sources.collect(cfg, self.base, now=NOW)
        self.assertEqual(res.items[0]["section"], "fyi")
        (res,) = sources.collect(settings.default_settings(), self.base, now=NOW)
        self.assertEqual(res.items[0]["section"], "urgent")

    def test_alert_is_resolved_when_its_key_left_alerts_json(self):
        write_events(self.base, [
            self.attention("a", 3), self.attention("b", 3), self.attention("c", 3),
            self.attention("calendar-cache", 3), self.alert("connector:srv", 3),
            self.alert("budget-hit:2023-11-15", 3)])
        self.active({"attention:b": iso(-3),
                     "attention:c": {"pending": iso(-3)},
                     "budget-hit:2023-11-15": iso(-3)})
        got = self.load()
        for key in ("attention:a", "attention:calendar-cache", "connector:srv"):
            self.assertTrue(got[key]["meta"]["resolved"], key)
            self.assertEqual((got[key]["section"], got[key]["urgency"]),
                             ("fyi", "digest"), key)
        for key in ("attention:b", "attention:c", "budget-hit:2023-11-15"):
            self.assertNotIn("resolved", got[key]["meta"], key)
            self.assertEqual(got[key]["section"], "urgent", key)

    def test_a_key_that_is_still_active_is_never_resolved_by_other_logs(self):
        # The issue changed (last-run error -> memory sink) but the daemon
        # still lists the key, and the run log shows a clean tick and a held
        # tick: none of that resolves it.
        write_events(self.base, [self.attention("a", 3, ("2 memory sink",))])
        self.active({"attention:a": iso(-3)})
        logs = self.base / "logs"
        logs.mkdir()
        (logs / "run.log").write_text(
            f"{iso(-1)} tick: due=a\n"
            f"{iso(-1)} tick done: 0 processed, 0 already-seen, 0 error(s)\n"
            f"{iso(-0.5)} tick: due=a held=a\n"
            f"{iso(-0.5)} tick done: 0 processed, 0 already-seen, 0 error(s)\n")
        item = self.load()["attention:a"]
        self.assertNotIn("resolved", item["meta"])
        self.assertEqual(item["section"], "urgent")

    def test_interrupted_key_is_resolved_like_attention(self):
        write_events(self.base, [self.alert("interrupted:a", 2, routine="a"),
                                 self.alert("interrupted:b", 2, routine="b")])
        self.active({"interrupted:b": iso(-2)})
        got = self.load()
        self.assertTrue(got["interrupted:a"]["meta"]["resolved"])
        self.assertNotIn("resolved", got["interrupted:b"]["meta"])

    def test_newest_alert_per_key_wins(self):
        write_events(self.base, [
            self.alert("attention:a", 5, routine="a", issues=["last run"]),
            ev("daemon:other-id", "alert", "newer alert text", ago_h=1,
               details={"key": "attention:a", "routine": "a", "issues": ["last run"]}),
            self.alert("budget-hit:2023-11-15", 4)])
        res = sources.load_events(self.base, now=NOW)
        self.assertEqual(sorted(i["gist"] for i in res.items),
                         ["alert budget-hit:2023-11-15", "newer alert text"])

    def test_unknown_alerts_json_falls_back_to_age_only(self):
        write_events(self.base, [self.attention("a", 3), self.attention("b", 7)])
        bad_docs = [None,                                  # no file at all
                    "not json", "[]", '"x"', "null", "{",
                    '{"attention:a": 5}', '{"attention:a": ""}',
                    '{"attention:a": {"pending": 1}}', '{"attention:a": null}',
                    '{"x": "' + "y" * (1024 * 1024) + '"}']
        for doc in bad_docs:
            if doc is not None:
                self.active(doc)
            got = self.load()
            self.assertEqual(got["attention:a"]["section"], "urgent", str(doc)[:30])
            self.assertEqual(got["attention:b"]["section"], "fyi", str(doc)[:30])
            for key in got:
                self.assertNotIn("resolved", got[key]["meta"], str(doc)[:30])

    def test_unreadable_alerts_json_is_unknown(self):
        write_events(self.base, [self.attention("a", 3)])
        (self.base / "state" / "alerts.json").mkdir()  # open() fails
        self.assertNotIn("resolved", self.load()["attention:a"]["meta"])

    def test_alerts_json_older_than_the_alert_says_nothing(self):
        write_events(self.base, [self.attention("a", 3)])
        self.active({})
        stale = NOW - 10 * 3600
        os.utime(self.base / "state" / "alerts.json", (stale, stale))
        self.assertNotIn("resolved", self.load()["attention:a"]["meta"])
        fresh = NOW
        os.utime(self.base / "state" / "alerts.json", (fresh, fresh))
        self.assertTrue(self.load()["attention:a"]["meta"]["resolved"])

    def test_alert_without_a_key_still_ages_out(self):
        write_events(self.base, [
            ev("daemon:nokey", "alert", "no key", ago_h=9, details={}),
            ev("daemon:nokey2", "alert", "no key either", ago_h=1, details={})])
        self.active({})
        res = sources.load_events(self.base, now=NOW, rules=[{"source": "daemon"}])
        self.assertEqual({i["gist"]: i["section"] for i in res.items},
                         {"no key": "fyi", "no key either": "urgent"})
        self.assertFalse(any(i["meta"].get("resolved") for i in res.items))

    def test_reading_state_does_not_modify_it(self):
        write_events(self.base, [self.attention("a", 3)])
        self.active({"attention:b": iso(-1)})
        snap = lambda: {p: (p.stat().st_mtime_ns, p.read_bytes())
                        for p in self.base.rglob("*") if p.is_file()}
        before = snap()
        self.load()
        self.assertEqual(before, snap())


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
