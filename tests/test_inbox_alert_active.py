"""Inbox 0.8.3: active alerts stay listed, alert threads, benign states
(synthetic fixtures)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import items as itm, sources

NOW = 1_700_000_000.0
DAY_H = 24.0


def iso(offset_h=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(NOW + offset_h * 3600))


def alert_event(key, ago_h, **details):
    return events.build("r", "alert", None, f"daemon:{key}", f"alert {key}",
                        [], ts=iso(-ago_h), details=dict({"key": key}, **details))


class ActiveAlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "state").mkdir()

    def events(self, rows):
        with open(self.base / "state" / "events.jsonl", "a") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def active(self, doc):
        (self.base / "state" / "alerts.json").write_text(json.dumps(doc))

    def log(self, text):
        (self.base / "logs").mkdir(exist_ok=True)
        (self.base / "logs" / "run.log").write_text(text)

    def load(self, **kw):
        res = sources.load_events(self.base, now=NOW, **kw)
        return {i["meta"].get("key", i["gist"]): i for i in res.items}

    @staticmethod
    def thread(item):
        return {m["who"]: m for m in item["thread"]}

    # 1. active but older than the window
    def test_active_alert_older_than_the_window_is_listed(self):
        self.events([alert_event("attention:a", 6 * DAY_H, routine="a",
                                 issues=["3 failed runs in a row"]),
                     alert_event("attention:b", 6 * DAY_H, routine="b")])
        self.active({"attention:a": iso(-6 * DAY_H)})
        got = self.load()
        self.assertEqual(list(got), ["attention:a"])
        item = got["attention:a"]
        # The newest event is used: its id and text, not a synthesized row.
        self.assertEqual(item["gist"], "alert attention:a")
        self.assertEqual((item["section"], item["urgency"]), ("fyi", "digest"))
        self.assertNotIn("resolved", item["meta"])
        self.assertEqual(itm.validate_item(item), [])

    def test_the_newest_event_of_an_old_active_key_wins(self):
        self.events([alert_event("attention:a", 8 * DAY_H, routine="a"),
                     events.build("r", "alert", None, "daemon:other", "newer",
                                  [], ts=iso(-5 * DAY_H),
                                  details={"key": "attention:a"})])
        self.active({"attention:a": iso(-8 * DAY_H)})
        self.assertEqual(self.load()["attention:a"]["gist"], "newer")

    def test_young_active_alert_is_urgent_old_is_fyi(self):
        self.events([alert_event("attention:a", 2, routine="a")])
        self.active({"attention:a": iso(-2)})
        self.assertEqual(self.load()["attention:a"]["section"], "urgent")
        self.assertEqual(self.load(alert_max_age_h=1)["attention:a"]["section"],
                         "fyi")

    # 1b. no event at all
    def test_active_key_without_an_event_gets_a_synthesized_row(self):
        self.active({"attention:ghost": iso(-3),
                     "connector:srv": {"pending": iso(-20)},
                     "budget-hit:2023-11-10": iso(-6 * DAY_H)})
        got = self.load()
        self.assertEqual(sorted(got), ["attention:ghost", "budget-hit:2023-11-10",
                                       "connector:srv"])
        ghost = got["attention:ghost"]
        self.assertEqual(ghost["gist"], "ghost needs attention")
        self.assertEqual(ghost["source"], "alert")
        self.assertEqual(ghost["section"], "urgent")  # 3 h old, attention:
        self.assertAlmostEqual(ghost["ts"], NOW - 3 * 3600, delta=1)
        self.assertEqual(self.thread(ghost)["routine"]["text"], "ghost")
        self.assertEqual(got["connector:srv"]["gist"], "Alert active: connector:srv")
        self.assertEqual(got["connector:srv"]["section"], "fyi")  # 20 h old
        self.assertEqual(got["budget-hit:2023-11-10"]["section"], "fyi")
        for item in got.values():
            self.assertEqual(itm.validate_item(item), [])

    def test_unknown_alerts_json_adds_nothing(self):
        (self.base / "state" / "alerts.json").write_text("not json")
        self.assertEqual(self.load(), {})

    # 4. resolved is unchanged
    def test_resolved_alert_behaves_as_before(self):
        self.events([alert_event("attention:gone", 3, routine="gone"),
                     alert_event("attention:old", 6 * DAY_H, routine="old")])
        self.active({"attention:other": iso(-1)})
        got = self.load()
        self.assertTrue(got["attention:gone"]["meta"]["resolved"])
        self.assertEqual(got["attention:gone"]["section"], "fyi")
        self.assertNotIn("attention:old", got)   # out of the window, not active
        self.assertNotIn("resolved", got["attention:other"]["meta"])

    # 2. thread
    def test_thread_has_routine_issues_since_error_and_hint(self):
        self.events([alert_event("attention:a", 3, routine="a",
                                 issues=["2 memory sink", "last run"])])
        self.active({"attention:a": iso(-4)})
        self.log(f"{iso(-5)} routine=a ERROR old failure\n"
                 f"{iso(-1)} routine=b FATAL other routine\n"
                 f"{iso(-0.5)} routine=a FATAL: sample failure {'x' * 400}\n"
                 f"{iso(-0.2)} routine=a info all fine\n")
        thread = self.thread(self.load()["attention:a"])
        self.assertEqual(thread["routine"]["text"], "a")
        self.assertEqual(thread["issues"]["text"], "2 memory sink; last run")
        self.assertEqual(thread["active since"]["ts"], NOW - 4 * 3600)
        self.assertIn("2023-11-14", thread["active since"]["text"])
        err = thread["last error"]
        self.assertTrue(err["text"].startswith(f"{iso(-0.5)} routine=a FATAL: sample"))
        self.assertLessEqual(len(err["text"]), 200)
        self.assertTrue(err["text"].endswith("…"))
        self.assertEqual(err["ts"], NOW - 0.5 * 3600)
        self.assertEqual(thread["hint"]["text"],
                         "run `daemon.py status` for details")

    def test_missing_log_leaves_out_the_error_only(self):
        self.events([alert_event("attention:a", 3, routine="a")])
        thread = self.thread(self.load()["attention:a"])
        self.assertNotIn("last error", thread)
        self.assertIn("hint", thread)
        self.assertEqual(thread["active since"]["ts"], NOW - 3 * 3600)

    def test_log_is_read_from_the_tail_only(self):
        self.events([alert_event("attention:a", 3, routine="a"),
                     alert_event("attention:b", 3, routine="b")])
        head = f"{iso(-9)} routine=b ERROR only in the head\n"
        filler = "".join(f"{iso(-8)} tick: filler line {i:06d}\n"
                         for i in range(sources.LOG_TAIL_BYTES // 30))
        self.log(head + filler + f"{iso(-1)} routine=a ERROR in the tail\n")
        got = self.load()
        self.assertIn("in the tail", self.thread(got["attention:a"])["last error"]["text"])
        self.assertNotIn("last error", self.thread(got["attention:b"]))

    def test_log_is_read_once_per_load(self):
        self.events([alert_event(f"attention:r{i}", 3, routine=f"r{i}")
                     for i in range(5)])
        self.log(f"{iso(-1)} routine=r1 ERROR boom\n")
        opened = []

        class Spy(sources._RunLog):
            def _scan(self):
                opened.append(1)
                return super()._scan()

        orig, sources._RunLog = sources._RunLog, Spy
        self.addCleanup(setattr, sources, "_RunLog", orig)
        self.assertEqual(len(self.load()), 5)
        self.assertEqual(len(opened), 1)

    def test_alert_without_a_routine_has_no_error_lookup(self):
        self.events([alert_event("budget-hit:2023-11-14", 2, spent_usd=5)])
        self.log(f"{iso(-1)} routine=a ERROR boom\n")
        thread = self.thread(self.load()["budget-hit:2023-11-14"])
        self.assertEqual(sorted(thread), ["active since", "hint"])

    # 3. benign
    def test_benign_issues_are_never_urgent(self):
        for issues in (["overlap"], ["interrupted"], ["incomplete"],
                       ["overlap", "Incomplete"]):
            with self.subTest(issues=issues):
                base = Path(tempfile.mkdtemp(dir=self.tmp.name))
                (base / "state").mkdir()
                row = alert_event("attention:a", 1, routine="a", issues=issues)
                (base / "state" / "events.jsonl").write_text(json.dumps(row) + "\n")
                (item,) = sources.load_events(base, now=NOW).items
                self.assertEqual((item["section"], item["urgency"]),
                                 ("fyi", "digest"))
                # also under a rule that matches every daemon event
                (item,) = sources.load_events(
                    base, now=NOW, rules=[{"source": "daemon"}]).items
                self.assertEqual(item["section"], "fyi")

    def test_a_real_issue_next_to_a_benign_one_stays_urgent(self):
        self.events([alert_event("attention:a", 1, routine="a",
                                 issues=["overlap", "2 memory sink"]),
                     alert_event("attention:b", 1, routine="b", issues=[])])
        got = self.load()
        self.assertEqual(got["attention:a"]["section"], "urgent")
        self.assertEqual(got["attention:b"]["section"], "urgent")  # no issues named


if __name__ == "__main__":
    unittest.main()
