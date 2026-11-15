"""Inbox 0.2.0 read-side features (synthetic fixtures, no Textual):
the show-all toggle's `--all`, pending calendar invites, the mail label
filter, the Saved view's rows and the new settings."""
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import daemoncfg, invites, models, settings, sources
from workspace_daemon.inbox import items as itm

NOW = 1_700_000_000.0
H = 3600.0


def iso(offset_h=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW + offset_h * H))


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def write_events(base, rows):
    path = Path(base) / "state" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for row in rows:
            fh.write((row if isinstance(row, str) else json.dumps(row)) + "\n")


def ev(source_id, kind, title, ago_h=1.0, routine="r", memory_id=None,
       details=None, people=()):
    return events.build(routine, kind, memory_id, source_id, title,
                        list(people), ts=iso(-ago_h), details=details)


def row(start_h, end_h=None, rsvp="needsAction", title="Planning chat",
        status="confirmed", conference="", **extra):
    end_h = start_h + 1 if end_h is None else end_h
    return dict({"start": iso(start_h), "end": iso(end_h), "status": status,
                 "my_rsvp": rsvp, "title": title, "list_title": title,
                 "location": "", "conference": conference}, **extra)


def write_snapshot(base, rows, fetched_ago_h=0.1, window_hours=168.0, v=1):
    doc = {"v": v, "fetched_at": iso(-fetched_ago_h),
           "window_hours": window_hours, "events": rows}
    return write(Path(base) / "state" / invites.SNAPSHOT_FILE, json.dumps(doc))


class TmpBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)


# --- A: show all ----------------------------------------------------------

class ShowAllTest(TmpBase):
    def sources_cfg(self):
        log = self.base / "calls.jsonl"
        script = write(self.base / "src.py", textwrap.dedent(f"""
            import json, sys
            with open({str(log)!r}, "a") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            print(json.dumps({{"v": 1, "items": [], "stale": False,
                              "age_min": None, "error": None}}))"""))
        argv = [sys.executable, str(script)]
        return log, settings.validate({"sources": [
            {"name": "plain", "type": "command", "argv": argv + ["plain"]},
            {"name": "wide", "type": "command", "argv": argv + ["wide"],
             "supports_all": True}]})

    def calls(self, log):
        return [json.loads(line) for line in log.read_text().splitlines()]

    def test_all_goes_only_to_sources_that_declare_it(self):
        log, cfg = self.sources_cfg()
        sources.collect(cfg, self.base, now=NOW, show_all=True)
        self.assertCountEqual(self.calls(log), [
            ["plain", "list"], ["wide", "list", "--all"]])

    def test_off_by_default_nobody_gets_all(self):
        log, cfg = self.sources_cfg()
        sources.collect(cfg, self.base, now=NOW)
        sources.collect(cfg, self.base, now=NOW, show_all=False)
        self.assertTrue(all("--all" not in c for c in self.calls(log)))
        self.assertEqual(len(self.calls(log)), 4)

    def test_run_command_source_adds_the_flag_last(self):
        log, _ = self.sources_cfg()
        script = self.base / "src.py"
        sources.run_command_source("x", [sys.executable, str(script), "q"],
                                   show_all=True)
        self.assertEqual(self.calls(log), [["q", "list", "--all"]])

    def test_supports_all_setting(self):
        cfg = settings.validate({"sources": [
            {"type": "command", "name": "a", "argv": ["x"]},
            {"type": "command", "name": "b", "argv": ["x"],
             "supports_all": True}]})
        self.assertEqual([s["supports_all"] for s in cfg["sources"]],
                         [False, True])
        with self.assertRaises(settings.SettingsError) as ctx:
            settings.validate({"sources": [
                {"type": "command", "argv": ["x"], "supports_all": "yes"}]})
        self.assertIn("supports_all", str(ctx.exception))
        with self.assertRaises(settings.SettingsError):
            settings.validate({"sources": [
                {"type": "events", "supports_all": True}]})


# --- pending calendar invites ---------------------------------------------

class InvitesTest(TmpBase):
    def load(self, days=7, now=NOW):
        return invites.load(self.base, now, days)

    def titles(self, **kw):
        items, _stale, _age, err = self.load(**kw)
        return sorted(i["gist"].split(" - ")[0] for i in items), err

    def test_only_needs_action_is_shown(self):
        write_snapshot(self.base, {
            "e-open": row(5, title="Open invite"),
            "e-acc": row(5, rsvp="accepted", title="Accepted"),
            "e-dec": row(5, rsvp="declined", title="Declined"),
            "e-tent": row(5, rsvp="tentative", title="Tentative"),
            "e-org": row(5, rsvp="organizer", title="Mine"),
            "e-none": row(5, rsvp=None, title="Not invited"),
            "e-cxl": row(5, status="cancelled", title="Cancelled"),
        })
        titles, err = self.titles()
        self.assertEqual((titles, err), (["Open invite"], None))

    def test_window_is_now_to_now_plus_days(self):
        write_snapshot(self.base, {
            "past": row(-5, -4, title="Over"),
            "running": row(-0.5, 0.5, title="Running"),
            "soon": row(2, title="Soon"),
            "day6": row(6 * 24, title="Day six"),
            "day8": row(8 * 24, title="Day eight"),
        })
        self.assertEqual(self.titles()[0], ["Day six", "Running", "Soon"])
        self.assertEqual(self.titles(days=1)[0], ["Running", "Soon"])
        self.assertEqual(self.titles(days=9)[0],
                         ["Day eight", "Day six", "Running", "Soon"])

    def test_the_item_follows_the_contract(self):
        write_snapshot(self.base, {
            "e1": row(3, 4, title="Design review",
                      conference="https://meet.example.com/abc-defg")})
        (item,), *_ = self.load()
        self.assertEqual(itm.validate_item(item), [])
        self.assertEqual((item["source"], item["section"], item["urgency"]),
                         ("calendar", "needs_reply", "digest"))
        self.assertEqual(item["id"], "invites:e1")
        self.assertEqual(item["actions"], ["ack", "open"])
        self.assertEqual(item["link"], "https://meet.example.com/abc-defg")
        self.assertEqual(item["meta"]["event_id"], "e1")
        self.assertEqual(item["meta"]["calendar_id"], "primary")
        self.assertIn("Design review", item["gist"])
        self.assertEqual(models.from_v1(item).kind, "ack")  # `a` hides it

    def test_time_is_shown_in_the_local_zone(self):
        write_snapshot(self.base, {"e1": row(3, 4)})
        (item,), *_ = self.load()
        local = time.strftime("%a %d %b %H:%M", time.localtime(NOW + 3 * H))
        self.assertIn(local, item["gist"])
        self.assertIn(local, item["thread"][0]["text"])

    def test_no_link_or_unsafe_link_means_no_open(self):
        write_snapshot(self.base, {
            "a": row(2, title="No link"),
            "b": row(3, title="Bad link", conference="javascript:alert(1)"),
            "c": row(4, title="Spacey", conference="https://x.example.com/a b")})
        items, *_ = self.load()
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertIsNone(item["link"])
            self.assertEqual(item["actions"], ["ack"])

    def test_recurring_series_is_one_row_at_its_next_instance(self):
        write_snapshot(self.base, {
            "weekly_20231116T100000Z": row(30, title="Weekly sync"),
            "weekly_20231115T100000Z": row(12, title="Weekly sync"),
            "weekly_20231122T100000Z": row(150, title="Weekly sync")})
        (item,), *_ = self.load()
        self.assertEqual(item["id"], "invites:weekly")
        self.assertEqual(item["meta"]["event_id"], "weekly_20231115T100000Z")

    def test_all_day_event(self):
        day = time.strftime("%Y-%m-%d", time.localtime(NOW + 2 * 86400))
        nxt = time.strftime("%Y-%m-%d", time.localtime(NOW + 3 * 86400))
        write_snapshot(self.base, {"e1": {
            "start": day, "end": nxt, "status": "confirmed",
            "my_rsvp": "needsAction", "title": "Offsite", "conference": ""}})
        (item,), *_ = self.load()
        self.assertIn("all day", item["gist"])

    def test_untrusted_text_is_cleaned_and_the_gist_is_bounded(self):
        write_snapshot(self.base, {"e1": row(
            2, title="\x1b[31mRed [b]alert[/b]‮" + "x" * 400)})
        (item,), *_ = self.load()
        self.assertNotIn("\x1b", item["gist"])
        self.assertNotIn("‮", item["gist"])
        self.assertIn("[b]alert[/b]", item["gist"])  # literal, not markup
        self.assertLessEqual(len(item["gist"]), itm.GIST_MAX)
        self.assertEqual(itm.validate_item(item), [])

    def test_organizer_and_calendar_id_come_from_the_daemon_config(self):
        out = self.base / "cache" / "next48h.txt"
        write(out.with_suffix(".json"), json.dumps({"events": [
            {"event_id": "e1",
             "organizer": {"email": "kim@example.com",
                           "name": "Kim \x1b[1mExample\x1b[0m"}},
            {"event_id": "e2", "organizer": None}, "junk"]}))
        write(self.base / "routines" / "cal.yaml", textwrap.dedent(f"""
            id: calendar-cache
            maintenance:
              kind: calendar_cache
              calendar_id: team@example.com
              output: {out}"""))
        write_snapshot(self.base, {"e1": row(2, title="Budget talk"),
                                   "e2": row(3, title="No organizer")})
        items, *_ = self.load()
        by = {i["meta"]["event_id"]: i for i in items}
        self.assertEqual(by["e1"]["sender"], "Kim Example")
        self.assertTrue(by["e1"]["gist"].endswith("from Kim Example"))
        self.assertEqual(by["e1"]["meta"]["calendar_id"], "team@example.com")
        self.assertEqual(by["e2"]["sender"], "calendar invite")
        self.assertNotIn("from", by["e2"]["gist"])

    def test_ts_is_the_event_start_not_the_cache_fetch_time(self):
        write_snapshot(self.base, {"e1": row(30, title="Later")},
                       fetched_ago_h=1)
        (item,), *_ = self.load()
        self.assertEqual(item["ts"], NOW + 30 * H)

    def test_build_re_evaluates_the_clock_without_reading_a_file(self):
        write_snapshot(self.base, {"soon": row(2, title="Soon"),
                                   "later": row(5 * 24, title="Later")})
        data = invites.read(self.base)
        (self.base / "state" / invites.SNAPSHOT_FILE).unlink()   # gone
        titles = lambda n, d=7: sorted(i["gist"].split(" - ")[0]
                                       for i in invites.build(data, n, d)[0])
        self.assertEqual(titles(NOW), ["Later", "Soon"])
        self.assertEqual(titles(NOW + 3 * H), ["Later"])            # Soon is over
        self.assertEqual(titles(NOW, d=1), ["Soon"])
        self.assertTrue(invites.build(data, NOW + 9 * H, 7)[1])     # now stale

    def test_missing_cache_is_empty_not_an_error(self):
        self.assertEqual(self.load(), ([], False, None, None))

    def test_garbage_cache_is_an_error_or_empty_never_a_crash(self):
        path = self.base / "state" / invites.SNAPSHOT_FILE
        cases = {
            "not json": "{{{",
            "binary": "\x00\x01\x02",
            "a list": "[]",
            "unknown version": json.dumps({"v": 2, "fetched_at": iso(),
                                           "events": {}}),
            "bool version": json.dumps({"v": True, "fetched_at": iso(),
                                        "events": {}}),
            "events not a dict": json.dumps({"v": 1, "fetched_at": iso(),
                                             "events": []}),
            "no fetched_at": json.dumps({"v": 1, "events": {}}),
            "bad fetched_at": json.dumps({"v": 1, "fetched_at": "x",
                                          "events": {}}),
        }
        for label, text in cases.items():
            write(path, text)
            items, stale, age, err = self.load()
            self.assertEqual(items, [], label)
            self.assertTrue(err, label)
        # An unreadable file (a directory in its place) is an error too.
        path.unlink()
        path.mkdir()
        items, _s, _a, err = self.load()
        self.assertEqual(items, [])
        self.assertIn("cannot read", err)

    def test_garbage_rows_are_skipped(self):
        write_snapshot(self.base, {
            "ok": row(2, title="Fine"),
            "str": "nope", "nested": {"my_rsvp": "needsAction"},
            "bad-start": dict(row(2), start="tomorrow-ish"),
            "num-start": dict(row(2), start=12345),
            "bad-title": dict(row(2), title=["x"]),
            "": row(2, title="Empty id")})
        titles, err = self.titles()
        self.assertEqual(titles, ["(untitled)", "Fine"])
        self.assertIsNone(err)

    def test_oversized_cache_is_an_error(self):
        write(self.base / "state" / invites.SNAPSHOT_FILE,
              " " * (invites.MAX_SNAPSHOT_BYTES + 10))
        _items, _s, _a, err = self.load()
        self.assertIn("larger than", err)

    def test_stale_cache_is_flagged(self):
        write_snapshot(self.base, {"e1": row(2)}, fetched_ago_h=0.5)
        _i, stale, age, _e = self.load()
        self.assertFalse(stale)
        self.assertAlmostEqual(age, 30, delta=0.1)
        write_snapshot(self.base, {"e1": row(2)}, fetched_ago_h=8)
        _i, stale, age, _e = self.load()
        self.assertTrue(stale)

    def test_asking_past_the_cache_window_adds_a_note(self):
        write_snapshot(self.base, {"e1": row(2)}, window_hours=168)
        self.assertIsNone(self.load(days=7)[3])
        self.assertIn("covers 7 days", self.load(days=14)[3])

    def test_collect_adds_the_invites_source_and_dedups_ids(self):
        write_snapshot(self.base, {"e1": row(2, title="Retro")})
        cfg = settings.default_settings()
        names = [r.name for r in sources.collect(cfg, self.base, now=NOW)]
        self.assertEqual(names, ["events", "events.invites"])
        res = sources.collect(cfg, self.base, now=NOW)[1]
        self.assertEqual([i["id"] for i in res.items], ["invites:e1"])
        self.assertIsNone(res.error)

    def test_no_cache_adds_no_source_and_zero_days_turns_it_off(self):
        cfg = settings.default_settings()
        self.assertEqual([r.name for r in sources.collect(cfg, self.base,
                                                          now=NOW)], ["events"])
        write_snapshot(self.base, {"e1": row(2)})
        off = settings.validate({"sources": [
            {"type": "events", "invites_days": 0}]})
        self.assertEqual([r.name for r in sources.collect(off, self.base,
                                                          now=NOW)], ["events"])

    def test_a_broken_cache_is_a_source_error_in_collect(self):
        write(self.base / "state" / invites.SNAPSHOT_FILE, "{{{")
        res = sources.collect(settings.default_settings(), self.base, now=NOW)
        self.assertEqual(res[0].error, None)
        self.assertEqual(res[1].name, "events.invites")
        self.assertEqual(res[1].items, [])
        self.assertIn("not valid JSON", res[1].error)

    def test_a_new_invite_row_is_dropped_when_its_series_has_a_pending_row(self):
        write_snapshot(self.base, {
            "weekly_20231116T100000Z": row(30, title="Weekly sync"),
            "solo": row(40, title="Solo invite")})
        write_events(self.base, [
            ev("gcal:weekly_20231123T100000Z", "new", "Weekly sync"),
            ev("gcal:solo", "update", "Solo invite"),       # not a `new`
            ev("gcal:other", "new", "Unrelated new invite")])
        res = sources.collect(settings.default_settings(), self.base, now=NOW)
        by = {r.name: [i["gist"] for i in r.items] for r in res}
        self.assertEqual(sorted(g.split(" (")[0] for g in by["events"]),
                         ["New invite: Unrelated new invite",
                          "Updated: Solo invite"])
        self.assertEqual(len(by["events.invites"]), 2)
        self.assertIsNone(res[0].error)       # not reported as a duplicate id

    def test_without_a_pending_row_the_new_invite_stays(self):
        write_events(self.base, [ev("gcal:e5", "new", "Standalone")])
        (res,) = sources.collect(settings.default_settings(), self.base,
                                 now=NOW)
        self.assertEqual(len(res.items), 1)

    def test_invites_days_setting(self):
        self.assertEqual(settings.default_settings()["sources"][0]
                         ["invites_days"], 7.0)
        for bad in (-1, 61, "7", True, None):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"sources": [
                    {"type": "events", "invites_days": bad}]})


# --- mail label filter ----------------------------------------------------

class LabelFilterTest(TmpBase):
    def routine(self, name, query, saved=True, extra=""):
        write(self.base / "routines" / f"{name}.yaml", textwrap.dedent(f"""
            id: {name}
            source:
              kind: gmail_mcp
              query: '{query}'
              saved_request: {str(saved).lower()}
            {extra}"""))

    def mail(self):
        write_events(self.base, [
            ev("gmail:aaaaaaaa11111111", "new", "Labelled thread",
               routine="mail-saved", memory_id="m1"),
            ev("gmail:bbbbbbbb22222222", "new", "General thread",
               routine="mail-all", memory_id="m2"),
            ev("gmail:cccccccc33333333", "new", "Unknown routine",
               routine="gone", memory_id="m3"),
            ev("gcal:c1", "new", "A meeting", routine="calendar-cache"),
        ])

    def titles(self, label):
        src = {"type": "events", "email_require_label": label}
        cfg = settings.validate({"sources": [src]})
        res = sources.collect(cfg, self.base, now=NOW)[0]
        return sorted(i["gist"] for i in res.items), res.error

    def test_off_by_default_every_mail_event_shows(self):
        self.mail()
        self.routine("mail-saved", "label:MY_LABEL")
        titles, err = self.titles(None)
        self.assertEqual(len(titles), 4)
        self.assertIsNone(err)
        res = sources.collect(settings.default_settings(), self.base,
                              now=NOW)[0]
        self.assertEqual(len(res.items), 4)

    def test_only_mail_from_a_routine_that_requires_the_label(self):
        self.mail()
        self.routine("mail-saved", "in:inbox label:MY_LABEL")
        self.routine("mail-all", "in:inbox -category:promotions")
        titles, err = self.titles("MY_LABEL")
        # The calendar event is not mail: untouched.
        self.assertEqual(titles, ["Labelled thread", "New invite: A meeting"])
        self.assertIsNone(err)

    def test_label_match_is_case_insensitive(self):
        self.mail()
        self.routine("mail-saved", "label:my_label")
        self.assertIn("Labelled thread", self.titles("MY_LABEL")[0])

    def test_no_matching_routine_hides_mail_and_says_why(self):
        self.mail()
        self.routine("mail-saved", "label:OTHER_LABEL")
        titles, err = self.titles("MY_LABEL")
        self.assertEqual(titles, ["New invite: A meeting"])
        self.assertIn("email_require_label 'MY_LABEL'", err)

    def test_missing_routines_dir_hides_mail_and_says_why(self):
        self.mail()
        titles, err = self.titles("MY_LABEL")
        self.assertEqual(titles, ["New invite: A meeting"])
        self.assertTrue(err)

    def test_query_rules(self):
        ok = daemoncfg.query_requires_label
        self.assertTrue(ok("label:MY_LABEL", "MY_LABEL"))
        self.assertTrue(ok("in:inbox  label:My_Label  newer_than:2d", "my_label"))
        self.assertTrue(ok('label:"My Label"', "My Label"))
        self.assertTrue(ok("label:my-label", "My Label"))
        self.assertFalse(ok("-label:MY_LABEL", "MY_LABEL"))
        self.assertFalse(ok("label:MY_LABEL OR from:boss", "MY_LABEL"))
        self.assertFalse(ok("(label:MY_LABEL)", "MY_LABEL"))
        self.assertFalse(ok("{label:MY_LABEL label:X}", "MY_LABEL"))
        self.assertFalse(ok("xlabel:MY_LABEL", "MY_LABEL"))
        self.assertFalse(ok("label:MY_LABEL_2", "MY_LABEL"))
        self.assertFalse(ok("in:inbox", "MY_LABEL"))
        self.assertFalse(ok(None, "MY_LABEL"))
        self.assertFalse(ok(["label:MY_LABEL"], "MY_LABEL"))
        self.assertFalse(ok("label:MY_LABEL", ""))

    def test_only_gmail_mcp_sources_in_routines_with_either_layout(self):
        write(self.base / "routines" / "multi.yaml", textwrap.dedent("""
            id: multi
            sources:
              - kind: slack_mcp
                query: 'label:MY_LABEL'
              - kind: gmail_mcp
                query: 'label:MY_LABEL'
            """))
        write(self.base / "routines" / "other.yaml", textwrap.dedent("""
            id: other
            source:
              kind: slack_mcp
              query: 'label:MY_LABEL'
            """))
        write(self.base / "routines" / "_template.yaml",
              "id: tpl\nsource: {kind: gmail_mcp, query: 'label:MY_LABEL'}\n")
        write(self.base / "routines" / "broken.yaml", "a: [unclosed")
        write(self.base / "routines" / "list.yaml", "- 1\n- 2\n")
        self.assertEqual(daemoncfg.label_routines(self.base, "MY_LABEL"),
                         {"multi"})

    def test_query_parser_ignores_quoted_phrases_and_rejects_operators(self):
        ok = daemoncfg.query_requires_label
        self.assertFalse(ok('subject:"label:MY_LABEL"', "MY_LABEL"))
        self.assertFalse(ok('subject:"a label:MY_LABEL b"', "MY_LABEL"))
        self.assertFalse(ok('"label:MY_LABEL"', "MY_LABEL"))
        self.assertFalse(ok('-label:"MY_LABEL"', "MY_LABEL"))
        self.assertTrue(ok('label:"MY_LABEL"', "MY_LABEL"))
        self.assertTrue(ok('subject:"two words" label:MY_LABEL', "MY_LABEL"))
        self.assertTrue(ok('label:MY_LABEL subject:"this OR that"', "MY_LABEL"))
        for bad in ("label:MY_LABEL NOT from:boss", "label:MY_LABEL not x",
                    "label:MY_LABEL | label:X", "label:MY_LABEL or x",
                    "label:MY_LABEL OR x", "label:MY_LABEL)"):
            self.assertFalse(ok(bad, "MY_LABEL"), bad)

    def test_every_gmail_block_of_a_routine_must_require_the_label(self):
        def routine(name, blocks):
            body = "\n".join(
                f"  - kind: {kind}\n    query: '{query}'" for kind, query in blocks)
            write(self.base / "routines" / f"{name}.yaml",
                  f"id: {name}\nsources:\n{body}\n")
        routine("all-labelled", [("gmail_mcp", "label:MY_LABEL"),
                                 ("gmail_mcp", "label:MY_LABEL newer_than:2d")])
        routine("mixed", [("gmail_mcp", "label:MY_LABEL"),
                          ("gmail_mcp", "in:inbox")])
        routine("mixed-with-gmail", [("gmail_mcp", "label:MY_LABEL"),
                                     ("gmail", "in:inbox")])
        routine("gmail-only", [("gmail", "label:MY_LABEL")])
        routine("labelled-plus-slack", [("gmail_mcp", "label:MY_LABEL"),
                                        ("slack_mcp", "in:inbox")])
        self.assertEqual(daemoncfg.label_routines(self.base, "MY_LABEL"),
                         {"all-labelled", "labelled-plus-slack"})

    def test_an_oversized_routine_file_is_skipped_without_being_parsed(self):
        write(self.base / "routines" / "big.yaml",
              "id: big\nsource: {kind: gmail_mcp, query: 'label:MY_LABEL'}\n"
              + "# pad\n" * (daemoncfg.MAX_FILE_BYTES // 6 + 10))
        self.assertEqual(daemoncfg.label_routines(self.base, "MY_LABEL"), set())

    def test_setting_validation(self):
        cfg = settings.validate({"sources": [
            {"type": "events", "email_require_label": " MY_LABEL "}]})
        self.assertEqual(cfg["sources"][0]["email_require_label"], "MY_LABEL")
        self.assertIsNone(settings.default_settings()["sources"][0]
                          ["email_require_label"])
        for bad in ("", "  ", 5, ["x"], "a\nb", "x" * 101):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"sources": [
                    {"type": "events", "email_require_label": bad}]})


# --- time-dependent rules without reading files ----------------------------

class ReevalTest(TmpBase):
    def units(self, **src):
        cfg = settings.validate({"sources": [dict({"type": "events"}, **src)]})
        return {u.name: u for u in sources.make_units(cfg, self.base)}

    def test_the_events_window_and_alert_age_follow_the_clock(self):
        write_events(self.base, [
            ev("slack:C1:1", "new", "x"),   # not a mapped source: ignored
            ev("gmail:aaaaaaaa11111111", "new", "Old mail", ago_h=40,
               memory_id="m1"),
            ev("daemon:alert:b", "alert", "Budget hit", ago_h=1,
               details={"key": "budget-hit:x"})])
        unit = self.units(window_h=48)["events"]
        (res,) = unit.run(NOW, False)
        by = {i["gist"]: i for i in res.items}
        self.assertEqual(by["Budget hit"]["section"], "urgent")
        self.assertIn("Old mail", by)
        for path in (self.base / "state").glob("events.jsonl*"):
            path.unlink()                    # no file is read from here on
        (later,) = unit.reeval(NOW + 7 * H)  # 7 h on: past the 6 h alert age
        by = {i["gist"]: i for i in later.items}
        self.assertEqual(by["Budget hit"]["section"], "fyi")
        self.assertIn("Old mail", by)
        (much,) = unit.reeval(NOW + 20 * H)  # the 40 h-old mail leaves the window
        self.assertEqual([i["gist"] for i in much.items], ["Budget hit"])

    def test_starts_within_rule_follows_the_clock(self):
        write_events(self.base, [ev(
            "gcal:e1", "cancel", "Offsite", ago_h=1,
            details={"start": iso(30), "end": iso(31)})])
        unit = self.units(urgent=[{"source": "gcal", "kind": "cancel",
                                   "starts_within_h": 24}])["events"]
        (res,) = unit.run(NOW, False)
        self.assertEqual(res.items[0]["section"], "fyi")
        (later,) = unit.reeval(NOW + 10 * H)     # now within 24 h of the start
        self.assertEqual(later.items[0]["section"], "urgent")

    def test_resolved_alert_state_is_not_re_read(self):
        write_events(self.base, [ev("daemon:alert:b", "alert", "Budget hit",
                                    ago_h=1, details={"key": "budget-hit:x"})])
        write(self.base / "state" / "alerts.json", "{}")
        unit = self.units()["events"]
        (res,) = unit.run(NOW + 5, False)
        self.assertTrue(res.items[0]["meta"].get("resolved"))
        write(self.base / "state" / "alerts.json",
              json.dumps({"budget-hit:x": iso(-2)}))   # active again, unseen
        (again,) = unit.reeval(NOW + 6)
        self.assertTrue(again.items[0]["meta"].get("resolved"))

    def test_reeval_before_any_run_has_nothing_to_do(self):
        units = self.units()
        self.assertIsNone(units["events"].reeval(NOW))
        self.assertIsNone(units["events.invites"].reeval(NOW))

    def test_invites_unit_reeval_hides_an_invite_that_ended(self):
        write_snapshot(self.base, {"e1": row(2, title="Soon")})
        unit = self.units()["events.invites"]
        (res,) = unit.run(NOW, False)
        self.assertEqual(len(res.items), 1)
        (later,) = unit.reeval(NOW + 4 * H)
        self.assertEqual(later.items, [])


# --- Saved view rows ------------------------------------------------------

class SavedRowsTest(TmpBase):
    def test_missing_file_is_empty(self):
        self.assertEqual(sources.load_saved(self.base, now=NOW), ([], None))

    def test_rows_are_captures_in_the_window_newest_first(self):
        write_events(self.base, [
            ev("slack:C1:1.0", "new", "Launch thread", ago_h=50,
               memory_id="m1"),
            ev("gmail:aaaaaaaa11111111", "update", "Invoice mail", ago_h=5,
               memory_id="m2"),
            ev("zoom:u1", "new", "Planning call", ago_h=2, memory_id="m3"),
            ev("mila:r1", "new", "Standup notes", ago_h=1, memory_id="m4"),
            ev("slack:C9:9.0", "new", "Too old", ago_h=100, memory_id="m5"),
            # Not captures:
            ev("gcal:e1", "new", "A meeting", ago_h=1),
            ev("gcal:e2", "update", "Moved meeting", ago_h=1),
            ev("daemon:alert:x", "alert", "Alert", ago_h=1),
            ev("slack:C2:2.0", "new", "No memory id", ago_h=1),
            ev("zoom:u2", "diff", "Disagreement", ago_h=1, memory_id="m6"),
            ev("sheet:s1", "added", "Sheet row", ago_h=1, memory_id="m7"),
        ])
        rows, err = sources.load_saved(self.base, now=NOW, days=2)
        self.assertIsNone(err)
        self.assertEqual([r["title"] for r in rows],
                         ["Standup notes", "Planning call", "Invoice mail"])
        self.assertEqual([r["source"] for r in rows],
                         ["recording", "recording", "email"])
        self.assertEqual([r["kind"] for r in rows], ["new", "new", "update"])
        self.assertEqual(sorted(rows[0]), sorted(
            ["id", "source", "kind", "ts", "title", "source_id", "link"]))
        wide, _ = sources.load_saved(self.base, now=NOW, days=3)
        self.assertEqual(len(wide), 4)  # the 50 h-old Slack capture joins

    def test_gmail_row_has_a_thread_link_and_others_none(self):
        write_events(self.base, [
            ev("gmail:aaaaaaaa11111111", "new", "Mail", memory_id="m1"),
            ev("slack:C1:1.0", "new", "Chat", memory_id="m2")])
        rows, _ = sources.load_saved(self.base, now=NOW, gmail_account=1)
        by = {r["title"]: r for r in rows}
        self.assertEqual(by["Mail"]["link"],
                         "https://mail.google.com/mail/u/1/#all/aaaaaaaa11111111")
        self.assertIsNone(by["Chat"]["link"])

    def test_one_row_per_source_id_newest_wins(self):
        write_events(self.base, [
            ev("slack:C1:1.0", "new", "First capture", ago_h=10,
               memory_id="m1"),
            ev("slack:C1:1.0", "update", "Refreshed", ago_h=3, memory_id="m1")])
        rows, _ = sources.load_saved(self.base, now=NOW)
        self.assertEqual([(r["title"], r["kind"]) for r in rows],
                         [("Refreshed", "update")])

    def test_titles_are_cleaned_and_garbage_lines_skipped(self):
        write_events(self.base, [
            "not json", "[1, 2]", "",
            ev("slack:C1:1.0", "new", "\x1b[31mRed\x1b[0m [b]x[/b]‮",
               memory_id="m1")])
        rows, err = sources.load_saved(self.base, now=NOW)
        self.assertIsNone(err)
        self.assertEqual([r["title"] for r in rows], ["Red [b]x[/b]"])

    def test_saved_rows_are_not_inbox_items(self):
        write_events(self.base, [ev("slack:C1:1.0", "new", "Chat",
                                    memory_id="m1")])
        (saved,), _ = sources.load_saved(self.base, now=NOW)
        self.assertNotIn("actions", saved)
        self.assertNotIn("section", saved)

    def test_saved_days_setting(self):
        self.assertEqual(settings.default_settings()["sources"][0]
                         ["saved_days"], 2.0)
        for bad in (0, 31, "2", True):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"sources": [
                    {"type": "events", "saved_days": bad}]})


if __name__ == "__main__":
    unittest.main()
