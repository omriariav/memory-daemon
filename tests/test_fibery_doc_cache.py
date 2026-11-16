"""The fibery_doc_cache maintenance kind: selection, cache files, wiring.

Everything is fictional (example.com addresses, made-up secrets). No bridge
call is real: `call_tools` is replaced by a stub that answers with a
synthetic document."""
import datetime
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:    # run by discovery (tests/ on sys.path) or by dotted module name
    from test_fibery_tasks_inbound import Doc, bid, comments_section
except ImportError:
    from tests.test_fibery_tasks_inbound import Doc, bid, comments_section
from workspace_daemon import claude_bridge, config, sleepclock
from workspace_daemon import fibery_doc_cache as fdc
from workspace_daemon import fibery_tasks_inbound as fti
from workspace_daemon import maintenance

TOOL = "mcp__claude_ai_Example_Fibery__read_document"
SECRET_A = "aaaaaaaa-1111-2222-3333-444444444444"
SECRET_B = "bbbbbbbb-1111-2222-3333-444444444444"
NOW = datetime.datetime(2026, 11, 16, 9, 0, tzinfo=datetime.timezone.utc)


def at(hours):
    return (NOW + datetime.timedelta(hours=hours)).isoformat()


def doc_text(label="agenda"):
    doc = Doc()
    doc.heading(label)
    doc.bullets([(bid(1), "null", ['"an item"'])])
    return doc.text()


def event(start=2, other="alex@example.com", count=2, status="confirmed",
          rsvp="accepted", organizer="other", **extra):
    """A snapshot row for a one-to-one with `other` (the owner is self)."""
    people = [{"name": "Me", "email": "me@example.com", "response": rsvp,
               "self": True, "optional": False}]
    if other:
        people.append({"name": "Other", "email": other, "response": "accepted",
                       "self": False, "optional": False})
    org = {"name": "x", "email": other if organizer == "other"
           else "me@example.com", "self": organizer != "other"}
    row = {"start": at(start) if isinstance(start, (int, float)) else start,
           "end": at(3), "location": "", "conference": "", "status": status,
           "my_rsvp": rsvp, "title": "1:1", "list_title": "1:1",
           "organizer": org, "attendee_count": count, "attendees": people,
           "attendees_known": True}
    row.update(extra)
    return row


def snapshot(**rows):
    return {"v": 1, "fetched_at": NOW.isoformat(), "window_hours": 48,
            "events": rows}


DOCS = [
    {"key": "doc-a", "document_secret": SECRET_A,
     "match_emails": ["alex@example.com", "ALEX.W@Example.com"]},
    {"key": "doc-b", "document_secret": SECRET_B,
     "match_emails": ["sam@example.com"]},
]


class SelectionTest(unittest.TestCase):
    def keys(self, snap, hours=24, docs=DOCS):
        clean, problems = fdc.check_docs(docs)
        self.assertEqual(problems, [])
        return fdc.qualifying_keys(clean, snap, NOW, hours)

    def test_a_two_person_event_with_a_listed_email_qualifies(self):
        self.assertEqual(self.keys(snapshot(e1=event())), {"doc-a"})

    def test_emails_compare_case_insensitively_on_both_sides(self):
        self.assertEqual(
            self.keys(snapshot(e1=event(other="Alex.W@EXAMPLE.com"))),
            {"doc-a"})
        self.assertEqual(
            self.keys(snapshot(e1=event(other="SAM@example.COM"))), {"doc-b"})

    def test_other_party_is_the_organizer_when_the_list_is_empty(self):
        row = event(other=None, organizer="other")
        row["organizer"] = {"name": "x", "email": "Sam@example.com",
                            "self": False}
        self.assertEqual(self.keys(snapshot(e1=row)), {"doc-b"})

    def test_owner_organizer_alone_gives_no_other_party(self):
        row = event(other=None, organizer="me")
        self.assertEqual(self.keys(snapshot(e1=row)), set())

    def test_the_non_self_attendee_wins_over_a_self_organizer(self):
        row = event(organizer="me")             # I organized, alex attends
        self.assertEqual(self.keys(snapshot(e1=row)), {"doc-a"})

    def test_a_count_that_is_only_a_floor_does_not_qualify(self):
        for known in (False, None, "yes"):
            row = event(attendees_known=known)
            self.assertEqual(self.keys(snapshot(e1=row)), set(), known)
        row = event()
        del row["attendees_known"]               # a row of an older snapshot
        self.assertEqual(self.keys(snapshot(e1=row)), set())

    def test_declined_guests_are_not_the_other_party(self):
        row = event(count=2)
        row["attendees"].append({"name": "Gone", "email": "gone@example.com",
                                 "response": "declined", "self": False,
                                 "optional": False})
        self.assertEqual(self.keys(snapshot(e1=row)), {"doc-a"})
        gone_only = event(other="sam@example.com")
        gone_only["attendees"][1]["response"] = "declined"
        self.assertEqual(self.keys(snapshot(e1=gone_only)), set())

    def test_a_group_address_is_never_the_other_party(self):
        row = event(count=2)
        row["attendees"][1]["group"] = True
        self.assertEqual(self.keys(snapshot(e1=row)), set())

    def test_only_exactly_two_attendees_count(self):
        for count in (1, 3, 5, None, True, "2"):
            self.assertEqual(
                self.keys(snapshot(e1=event(count=count))), set(), count)

    def test_declined_and_cancelled_events_do_not_count(self):
        self.assertEqual(self.keys(snapshot(e1=event(rsvp="declined"))), set())
        self.assertEqual(
            self.keys(snapshot(e1=event(status="cancelled"))), set())
        self.assertEqual(self.keys(snapshot(e1=event(rsvp="tentative"))),
                         {"doc-a"})

    def test_window_edges_and_outside_the_window(self):
        self.assertEqual(self.keys(snapshot(e1=event(start=23.9))), {"doc-a"})
        self.assertEqual(self.keys(snapshot(e1=event(start=24.1))), set())
        self.assertEqual(self.keys(snapshot(e1=event(start=-1))), set())
        self.assertEqual(
            self.keys(snapshot(e1=event(start=40)), hours=48), {"doc-a"})

    def test_all_day_and_malformed_starts_do_not_count(self):
        for start in ("2026-11-16", "", None, "not a date", {}):
            row = event(start=start)
            self.assertEqual(self.keys(snapshot(e1=row)), set(), start)

    def test_z_suffix_and_naive_starts_are_read_as_utc(self):
        self.assertEqual(
            self.keys(snapshot(e1=event(start="2026-11-16T10:00:00Z"))),
            {"doc-a"})
        self.assertEqual(
            self.keys(snapshot(e1=event(start="2026-11-16T10:00:00"))),
            {"doc-a"})

    def test_an_unlisted_person_matches_nothing(self):
        self.assertEqual(
            self.keys(snapshot(e1=event(other="stranger@example.com"))), set())

    def test_old_snapshot_rows_without_the_new_fields_are_ignored(self):
        old = {"start": at(2), "end": at(3), "status": "confirmed",
               "my_rsvp": "accepted", "title": "x", "list_title": "x"}
        self.assertEqual(self.keys(snapshot(e1=old, e2="junk")), set())
        self.assertEqual(fdc.qualifying_keys(
            fdc.check_docs(DOCS)[0], {"events": "junk"}, NOW, 24), set())
        self.assertEqual(fdc.qualifying_keys(
            fdc.check_docs(DOCS)[0], None, NOW, 24), set())

    def test_two_events_two_docs(self):
        snap = snapshot(e1=event(), e2=event(other="sam@example.com"))
        self.assertEqual(self.keys(snap), {"doc-a", "doc-b"})


class CheckDocsTest(unittest.TestCase):
    def test_clean_docs_are_normalised(self):
        clean, problems = fdc.check_docs(DOCS)
        self.assertEqual(problems, [])
        self.assertEqual(clean[0]["match_emails"],
                         ["alex.w@example.com", "alex@example.com"])

    def test_unknown_keys_of_an_entry_are_ignored_and_dropped(self):
        docs = [dict(DOCS[0], name="Alex", doc_url="https://example.com/d")]
        clean, problems = fdc.check_docs(docs)
        self.assertEqual(problems, [])
        self.assertEqual(set(clean[0]),
                         {"key", "document_secret", "match_emails"})

    def test_bad_entries_are_reported_without_echoing_values(self):
        bad = [
            {"key": "Bad Key", "document_secret": SECRET_A,
             "match_emails": ["x@example.com"]},
            {"key": "k1", "document_secret": "short",
             "match_emails": ["x@example.com"]},
            {"key": "k2", "document_secret": SECRET_A, "match_emails": []},
            {"key": "k3", "document_secret": SECRET_A,
             "match_emails": ["not-an-email"]},
            "text",
        ]
        clean, problems = fdc.check_docs(bad)
        self.assertEqual(clean, [])
        self.assertEqual(len(problems), 5)
        text = " ".join(problems)
        for leaked in (SECRET_A, "not-an-email", "short", "Bad Key"):
            self.assertNotIn(leaked, text)

    def test_duplicate_keys_and_empty_lists_are_refused(self):
        dup = [DOCS[0], dict(DOCS[1], key="doc-a")]
        self.assertIn("used twice", " ".join(fdc.check_docs(dup)[1]))
        self.assertTrue(fdc.check_docs([])[1])
        self.assertTrue(fdc.check_docs("x")[1])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.cfg = {"read_tool": TOOL, "docs": [dict(d) for d in DOCS],
                    "model": "haiku"}
        self.calls, self.logs = [], []
        self.text = doc_text()
        self.fail = {}
        for patcher in (
            mock.patch.object(claude_bridge, "call_tools", self._call),
            mock.patch.object(fdc, "log", self.logs.append),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, prompt, tools, **kwargs):
        self.calls.append((prompt, tools, kwargs))
        secret = tools[TOOL]["secret"]
        if secret in self.fail:
            raise self.fail[secret]
        return [{"name": TOOL, "input": {"secret": secret},
                 "text": self.text, "is_error": False}]

    def snap(self, **rows):
        path = self.dir / "state" / "calendar-snapshot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot(**rows)))

    def out(self, key="doc-a"):
        return self.dir / "state" / "fibery-docs" / f"{key}.json"

    def run_once(self, now=NOW, **cfg):
        return fdc.run({**self.cfg, **cfg}, self.dir, now=now)

    def write_old_cache(self, key, hours_ago):
        fdc.write_cache(self.out(key).parent, key, "old text",
                        NOW - datetime.timedelta(hours=hours_ago))


class RunTest(Base):
    def test_refreshes_a_qualifying_doc_with_a_pinned_exact_call(self):
        self.snap(e1=event())
        report = self.run_once()
        self.assertEqual((report["refreshed"], report["failed"]), (1, 0))
        self.assertEqual(len(self.calls), 1)
        prompt, tools, kw = self.calls[0]
        self.assertEqual(tools, {TOOL: {"secret": SECRET_A}})
        self.assertIs(kw["exact_args"], True)
        self.assertEqual(kw["model"], "haiku")
        doc = json.loads(self.out().read_text())
        self.assertEqual(set(doc), {"v", "key", "fetched_at", "text"})
        self.assertEqual((doc["v"], doc["key"], doc["text"]),
                         (1, "doc-a", self.text))
        self.assertEqual(doc["fetched_at"], NOW.isoformat())
        self.assertFalse(self.out("doc-b").exists())

    def test_files_are_0600_in_a_0700_directory_and_atomic(self):
        self.snap(e1=event())
        self.run_once()
        self.assertEqual(stat.S_IMODE(os.stat(self.out()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.out().parent).st_mode),
                         0o700)
        self.assertEqual([p.name for p in self.out().parent.iterdir()],
                         ["doc-a.json"])           # no temp file left

    def test_an_existing_loose_directory_is_tightened(self):
        self.snap(e1=event())
        self.out().parent.mkdir(parents=True, mode=0o755)
        os.chmod(self.out().parent, 0o755)
        self.run_once()
        self.assertEqual(stat.S_IMODE(os.stat(self.out().parent).st_mode),
                         0o700)

    def test_a_failed_write_keeps_the_previous_file(self):
        self.snap(e1=event())
        self.write_old_cache("doc-a", 30)
        with mock.patch.object(fdc.state, "write_atomic",
                               side_effect=OSError("disk full")):
            with self.assertRaises(RuntimeError):
                self.run_once()
        self.assertEqual(json.loads(self.out().read_text())["text"],
                         "old text")

    def test_fresh_cache_is_not_refetched_but_old_cache_is(self):
        self.snap(e1=event())
        self.write_old_cache("doc-a", 11)
        report = self.run_once()
        self.assertEqual((report["fresh"], report["refreshed"]), (1, 0))
        self.assertEqual(self.calls, [])
        self.write_old_cache("doc-a", 13)
        report = self.run_once()
        self.assertEqual((report["fresh"], report["refreshed"]), (0, 1))

    def test_max_age_is_configurable(self):
        self.snap(e1=event())
        self.write_old_cache("doc-a", 3)
        self.assertEqual(self.run_once(max_age_h=2)["refreshed"], 1)
        self.write_old_cache("doc-a", 3)
        self.assertEqual(self.run_once(max_age_h=4)["refreshed"], 0)

    def test_an_unreadable_cache_file_counts_as_missing(self):
        self.snap(e1=event())
        self.out().parent.mkdir(parents=True)
        for junk in ("not json", "{}", json.dumps(
                {"v": 1, "key": "other", "fetched_at": NOW.isoformat(),
                 "text": "x"})):
            self.out().write_text(junk)
            self.calls.clear()
            self.assertEqual(self.run_once()["refreshed"], 1)

    def test_non_qualifying_docs_are_never_fetched(self):
        self.snap(e1=event(count=3), e2=event(start=30),
                  e3=event(other="x@example.com"))
        report = self.run_once()
        self.assertEqual(report["qualifying"], 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.out().parent.exists())

    def test_a_stale_doc_that_does_not_qualify_stays_alone(self):
        self.snap()
        self.write_old_cache("doc-a", 100)
        self.run_once()
        self.assertEqual(self.calls, [])

    def test_only_upcoming_false_refreshes_every_stale_doc(self):
        self.write_old_cache("doc-b", 1)
        report = self.run_once(only_upcoming=False)
        self.assertEqual(report["refreshed"], 1)        # doc-a: missing
        self.assertEqual(report["fresh"], 1)            # doc-b: 1 h old
        self.assertEqual(self.calls[0][1], {TOOL: {"secret": SECRET_A}})

    def test_no_snapshot_means_nothing_to_do(self):
        report = self.run_once()
        self.assertEqual(report["refreshed"], 0)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("no calendar snapshot" in m for m in self.logs))

    def test_a_snapshot_with_an_unknown_version_is_not_used(self):
        path = self.dir / "state" / "calendar-snapshot.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(snapshot(e1=event()), v=99)))
        self.assertEqual(self.run_once()["refreshed"], 0)

    def test_lookahead_is_configurable(self):
        self.snap(e1=event(start=30))
        self.assertEqual(self.run_once()["qualifying"], 0)
        self.assertEqual(self.run_once(lookahead_h=48)["qualifying"], 1)

    def test_dry_run_calls_nothing_and_writes_nothing(self):
        self.snap(e1=event())
        report = fdc.run(self.cfg, self.dir, dry_run=True, now=NOW)
        self.assertEqual((self.calls, report["refreshed"]), ([], 0))
        self.assertFalse(self.out().parent.exists())
        self.assertTrue(any("dry-run would refresh key=doc-a" in m
                            for m in self.logs))

    def test_relative_and_tilde_out_dirs(self):
        self.snap(e1=event())
        self.run_once(out_dir="elsewhere")
        self.assertTrue((self.dir / "elsewhere" / "doc-a.json").exists())
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            self.run_once(out_dir="~/home-out")
        self.assertTrue((self.dir / "home-out" / "doc-a.json").exists())


class SnapshotAgeTest(Base):
    def snap_at(self, hours_ago, **rows):
        path = self.dir / "state" / "calendar-snapshot.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        snap = snapshot(**rows)
        snap["fetched_at"] = (NOW - datetime.timedelta(
            hours=hours_ago)).isoformat()
        path.write_text(json.dumps(snap))

    def test_a_snapshot_older_than_the_cap_is_not_used_and_it_is_logged(self):
        self.snap_at(7, e1=event())
        report = self.run_once()
        self.assertEqual((report["refreshed"], report["qualifying"],
                          report["stale_snapshot"]), (0, 0, True))
        self.assertEqual(self.calls, [])
        self.assertTrue(any("snapshot is 7.0h old (limit 6h)" in m
                            for m in self.logs))

    def test_a_fresh_enough_snapshot_is_used(self):
        self.snap_at(5.5, e1=event())
        report = self.run_once()
        self.assertEqual((report["refreshed"], report["stale_snapshot"]),
                         (1, False))

    def test_the_limit_shrinks_with_a_short_lookahead(self):
        self.snap_at(3, e1=event())
        self.assertTrue(self.run_once(lookahead_h=1)["stale_snapshot"])
        self.assertFalse(self.run_once(lookahead_h=2)["stale_snapshot"])

    def test_snapshot_age_helper(self):
        self.assertAlmostEqual(fdc.snapshot_age_h(
            {"fetched_at": (NOW - datetime.timedelta(hours=2)).isoformat()},
            NOW), 2.0)
        self.assertAlmostEqual(fdc.snapshot_age_h(
            {"fetched_at": "2026-11-16T07:00:00Z"}, NOW), 2.0)
        for junk in ({}, {"fetched_at": "nope"}, {"fetched_at": None}):
            self.assertIsNone(fdc.snapshot_age_h(junk, NOW))

    def test_only_upcoming_false_ignores_the_snapshot_age(self):
        self.snap_at(500, e1=event())
        self.assertEqual(self.run_once(only_upcoming=False)["refreshed"], 2)


class FailureTest(Base):
    def setUp(self):
        super().setUp()
        self.snap(e1=event(), e2=event(other="sam@example.com"))

    def test_one_failure_does_not_stop_the_others_and_fails_the_run(self):
        self.fail[SECRET_A] = claude_bridge.BridgeError("boom")
        with self.assertRaises(RuntimeError) as ctx:
            self.run_once()
        self.assertIn("doc-a", str(ctx.exception))
        self.assertTrue(self.out("doc-b").exists())
        self.assertFalse(self.out("doc-a").exists())
        self.assertTrue(any("ERROR: 1 refresh(es) failed: doc-a" in m
                            for m in self.logs))

    def test_a_document_with_comment_threads_is_cached(self):
        doc = Doc()
        doc.heading("agenda")
        doc.bullets([(bid(1), "null", ['"an item"'])])
        doc.comments = comments_section(bid(1001))
        self.text = doc.text()
        self.run_once()
        self.assertEqual(json.loads(self.out().read_text())["text"], self.text)

    def test_a_text_that_is_not_a_document_is_never_cached(self):
        self.text = "this is not a document"
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.assertFalse(self.out().exists())
        self.assertFalse(self.out("doc-b").exists())

    def test_an_outage_stops_the_run_and_keeps_the_outage_note(self):
        self.fail[SECRET_A] = claude_bridge.BridgeOutage(
            f"claude run failed ({sleepclock.OUTAGE_NOTE})")
        with self.assertRaises(claude_bridge.BridgeOutage) as ctx:
            self.run_once()
        self.assertIn(sleepclock.OUTAGE_NOTE, str(ctx.exception))
        self.assertTrue(sleepclock.is_outage(ctx.exception))
        self.assertTrue(sleepclock.text_is_outage(str(ctx.exception)))
        self.assertEqual(len(self.calls), 1)            # doc-b never tried
        self.assertFalse(self.out("doc-b").exists())

    def test_the_outage_class_survives_fetch(self):
        self.fail[SECRET_A] = claude_bridge.BridgeOutage(f"x {SECRET_A} y")
        with self.assertRaises(claude_bridge.BridgeOutage) as ctx:
            fti.fetch({"read_tool": TOOL, "document_secret": SECRET_A},
                      self.dir, exact_args=True)
        self.assertNotIn(SECRET_A, str(ctx.exception))

    def test_a_network_down_failure_stops_the_run_as_an_outage(self):
        self.fail[SECRET_A] = RuntimeError("could not resolve host")
        with self.assertRaises(claude_bridge.BridgeOutage):
            self.run_once()
        self.assertEqual(len(self.calls), 1)

    def test_an_auth_or_rate_limit_failure_stops_the_run_but_is_not_an_outage(self):
        for text in ("Unauthenticated: please log in", "rate limit reached",
                     "quota exceeded"):
            self.calls.clear()
            self.fail[SECRET_A] = claude_bridge.BridgeError(text)
            with self.assertRaises(RuntimeError) as ctx:
                self.run_once()
            self.assertNotIsInstance(ctx.exception, claude_bridge.BridgeOutage)
            self.assertIn("shared dependency", str(ctx.exception))
            self.assertFalse(sleepclock.text_is_outage(str(ctx.exception)))
            self.assertEqual(len(self.calls), 1, text)

    def test_an_ordinary_failure_still_lets_the_next_doc_run(self):
        self.fail[SECRET_A] = claude_bridge.BridgeError("tool returned an error")
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(self.out("doc-b").exists())

    def test_guard_stops_end_the_run_for_the_runner(self):
        stop = claude_bridge.GUARD_STOPS[0]("paused")
        self.fail[SECRET_A] = stop
        self.fail[SECRET_B] = stop
        with self.assertRaises(claude_bridge.GUARD_STOPS):
            self.run_once()
        self.assertEqual(len(self.calls), 1)

    def test_no_secret_and_no_email_reaches_the_logs_or_the_error(self):
        self.fail[SECRET_A] = RuntimeError(
            f"failed for {SECRET_A} and {SECRET_B} mailing "
            "Alex.W@Example.com then <sam@example.com>")
        with self.assertRaises(RuntimeError) as ctx:
            self.run_once()
        blob = "\n".join(self.logs) + str(ctx.exception)
        for secret in (SECRET_A, SECRET_B):
            self.assertNotIn(secret, blob)
        for email in ("example.com", "Alex.W", "sam@"):
            self.assertNotIn(email, blob)
        self.assertIn("<secret>", blob)
        self.assertIn("<email>", blob)

    def test_nothing_logged_on_success_carries_secrets_or_text(self):
        self.run_once()
        blob = "\n".join(self.logs)
        for needle in (SECRET_A, SECRET_B, "example.com", "an item"):
            self.assertNotIn(needle, blob)

    def test_the_bridge_error_text_is_scrubbed_by_fetch_too(self):
        self.fail[SECRET_A] = RuntimeError(f"secret {SECRET_A} rejected")
        with self.assertRaises(RuntimeError) as ctx:
            fti.fetch({"read_tool": TOOL, "document_secret": SECRET_A},
                      self.dir, exact_args=True)
        self.assertNotIn(SECRET_A, str(ctx.exception))


class DocsFileTest(Base):
    def write(self, docs=None, mode=0o600, name="docs.json"):
        path = self.dir / name
        path.write_text(json.dumps(DOCS if docs is None else docs))
        os.chmod(path, mode)
        return path

    def cfg_file(self, path):
        cfg = dict(self.cfg)
        del cfg["docs"]
        cfg["docs_file"] = str(path)
        return cfg

    def test_docs_come_from_the_file(self):
        self.snap(e1=event())
        cfg = self.cfg_file(self.write())
        report = fdc.run(cfg, self.dir, now=NOW)
        self.assertEqual(report["refreshed"], 1)
        self.assertEqual(self.calls[0][1], {TOOL: {"secret": SECRET_A}})

    def test_a_relative_file_resolves_against_the_base_dir_and_tilde_works(self):
        self.write()
        cfg = self.cfg_file("docs.json")
        self.assertEqual(len(fdc.load_docs(cfg, self.dir)), 2)
        with mock.patch.dict(os.environ, {"HOME": str(self.dir)}):
            cfg = self.cfg_file("~/docs.json")
            self.assertEqual(len(fdc.load_docs(cfg, "/nowhere")), 2)

    def test_a_symlink_is_never_followed(self):
        target = self.write(name="real.json")
        link = self.dir / "link.json"
        link.symlink_to(target)
        with self.assertRaises(fdc.DocsFileError):
            fdc.load_docs(self.cfg_file(link), self.dir)
        self.assertEqual(len(fdc.load_docs(self.cfg_file(target), self.dir)), 2)

    def test_the_open_file_is_judged_not_the_path(self):
        # the mode and the owner come from the descriptor that is read
        path = self.write()
        real = os.fstat
        seen = []

        def fake(fd):
            info = real(fd)
            seen.append(fd)
            return os.stat_result(
                (info.st_mode, info.st_ino, info.st_dev, info.st_nlink,
                 os.getuid() + 1, info.st_gid, info.st_size, info.st_atime,
                 info.st_mtime, info.st_ctime))
        with mock.patch.object(fdc.os, "fstat", side_effect=fake):
            with self.assertRaises(fdc.DocsFileError) as ctx:
                fdc.load_docs(self.cfg_file(path), self.dir)
        self.assertTrue(seen)
        self.assertIn("not owned by you", str(ctx.exception))

    def test_a_group_or_world_readable_file_is_refused(self):
        for mode in (0o640, 0o604, 0o644, 0o660):
            path = self.write(mode=mode)
            with self.assertRaises(fdc.DocsFileError) as ctx:
                fdc.load_docs(self.cfg_file(path), self.dir)
            self.assertIn("readable by others", str(ctx.exception))

    def test_a_missing_or_broken_file_has_a_message_without_content(self):
        with self.assertRaises(fdc.DocsFileError):
            fdc.load_docs(self.cfg_file(self.dir / "absent.json"), self.dir)
        path = self.dir / "bad.json"
        path.write_text(f'{{"secret": "{SECRET_A}", broken')
        os.chmod(path, 0o600)
        with self.assertRaises(fdc.DocsFileError) as ctx:
            fdc.load_docs(self.cfg_file(path), self.dir)
        self.assertNotIn(SECRET_A, str(ctx.exception))

    def test_a_file_with_bad_entries_is_refused_without_echoing_them(self):
        path = self.write([{"key": "ok", "document_secret": SECRET_A,
                            "match_emails": ["nope"]}])
        with self.assertRaises(fdc.DocsFileError) as ctx:
            fdc.load_docs(self.cfg_file(path), self.dir)
        text = str(ctx.exception)
        self.assertIn("docs_file[0].match_emails", text)
        self.assertNotIn(SECRET_A, text)

    def test_extra_keys_in_the_file_are_ignored(self):
        path = self.write([dict(DOCS[0], name="Alex",
                                doc_url="https://example.com/d")])
        self.assertEqual(len(fdc.load_docs(self.cfg_file(path), self.dir)), 1)

    def test_a_directory_is_not_a_docs_file(self):
        with self.assertRaises(fdc.DocsFileError):
            fdc.load_docs(self.cfg_file(self.dir), self.dir)


class WiringTest(unittest.TestCase):
    def routine(self, **extra):
        maint = {"kind": "fibery_doc_cache", "read_tool": TOOL,
                 "docs": [dict(d) for d in DOCS]}
        maint.update(extra)
        for key in [k for k, v in maint.items() if v is None]:
            del maint[key]
        return {"id": "fibery-docs", "enabled": True, "description": "d",
                "role": "maintenance", "schedule": {"every": "1h"},
                "maintenance": maint}

    def test_valid_with_defaults_needs_no_checkpoint_and_uses_the_bridge(self):
        routine = self.routine()
        self.assertEqual(config.validate(routine), [])
        self.assertTrue(config.uses_claude_bridge(routine))
        self.assertIn("fibery_doc_cache", config.VALID_MAINTENANCE_KINDS)

    def test_docs_file_form_is_valid_and_exactly_one_is_required(self):
        self.assertEqual(config.validate(
            self.routine(docs=None, docs_file="~/private/docs.json")), [])
        self.assertEqual(config.validate(
            self.routine(docs=None, docs_file="/abs/docs.json")), [])
        for extra in ({"docs": None},
                      {"docs_file": "/abs/docs.json"}):
            problems = config.validate(self.routine(**extra))
            self.assertTrue(any("exactly one of docs or docs_file" in p
                                for p in problems), extra)

    def test_a_relative_docs_file_is_refused(self):
        problems = config.validate(
            self.routine(docs=None, docs_file="docs.json"))
        self.assertTrue(any("docs_file" in p for p in problems))

    def test_all_options_validate_and_bad_ones_do_not(self):
        self.assertEqual(config.validate(self.routine(
            lookahead_h=6, max_age_h=0.5, only_upcoming=False,
            out_dir="state/x", model="sonnet")), [])
        for extra in (
            {"read_tool": "mcp__x__replace_block_text"},
            {"read_tool": "mcp__x__read_document__write"},
            {"lookahead_h": 0}, {"lookahead_h": 500},
            {"lookahead_h": True}, {"max_age_h": 0}, {"max_age_h": "12"},
            {"only_upcoming": "yes"}, {"out_dir": ""}, {"out_dir": 3},
            {"model": ""}, {"unknown_key": 1},
            {"docs": [{"key": "a"}]}, {"checkpoint": "state/x.json"},
        ):
            self.assertTrue(config.validate(self.routine(**extra)), extra)

    def test_validation_messages_do_not_echo_secrets_or_emails(self):
        problems = " ".join(config.validate(self.routine(
            docs=[{"key": "k", "document_secret": "bad secret!",
                   "match_emails": ["Not An Email"]}])))
        for leaked in ("bad secret", "Not An Email"):
            self.assertNotIn(leaked, problems)

    def test_maintenance_dispatch(self):
        report = {"ok": True, "dry_run": True, "docs": 2, "qualifying": 1,
                  "refreshed": 0, "fresh": 0, "failed": 0, "failed_keys": []}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            maintenance.fibery_doc_cache, "run", return_value=report
        ) as run:
            out = maintenance.run(Path(tmp), self.routine(), dry_run=True)
        self.assertIs(out, report)
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertEqual(run.call_args.kwargs["routine_id"], "fibery-docs")


if __name__ == "__main__":
    unittest.main()
