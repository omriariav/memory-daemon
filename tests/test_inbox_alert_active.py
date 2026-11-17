"""Inbox 0.8.3: active alerts stay listed, alert threads, benign states
(synthetic fixtures)."""
import json
import tempfile
import time
import unittest
from pathlib import Path

from workspace_daemon import events
from workspace_daemon.inbox import items as itm, models, settings, sources
from workspace_daemon.inbox.store import InboxStore

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
        """Entries by label; `last error, <age>` is found as `last error`."""
        return {("last error" if m["who"].startswith("last error")
                 else m["who"]): m for m in item["thread"]}

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
        self.log(f"{iso(-5)} routine=a ERROR before the episode\n"
                 f"{iso(-1)} routine=b FATAL other routine\n"
                 f"{iso(-0.5)} routine=a FATAL: sample failure {'x' * 400}\n"
                 f"{iso(-0.2)} routine=a info all fine\n")
        thread = self.thread(self.load()["attention:a"])
        self.assertEqual(thread["routine"]["text"], "a")
        self.assertEqual(thread["issues"]["text"], "2 memory sink; last run")
        self.assertEqual(thread["active since"]["ts"], NOW - 4 * 3600)
        self.assertEqual(thread["active since"]["text"], time.strftime(
            "%Y-%m-%d %H:%M", time.localtime(NOW - 4 * 3600)))
        self.assertEqual(thread["last error"]["who"], "last error, 30 min ago")
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

    # NIT (b): only an error written since the alert became active
    def test_an_error_from_before_the_episode_is_left_out(self):
        self.events([alert_event("attention:a", 3, routine="a")])
        self.active({"attention:a": iso(-3)})
        self.log(f"{iso(-30)} routine=a ERROR an earlier episode\n")
        self.assertNotIn("last error", self.thread(self.load()["attention:a"]))
        # the failing run just before the alert's tick still belongs to it
        self.log(f"{iso(-3.1)} routine=a ERROR the failing run\n")
        self.assertIn("last error", self.thread(self.load()["attention:a"]))

    # NIT (a): an event of an earlier episode is not used
    def test_an_event_from_an_earlier_episode_gives_a_synthesized_row(self):
        self.events([alert_event("attention:a", 9 * DAY_H, routine="a",
                                 issues=["old issue"])])
        self.active({"attention:a": iso(-2)})
        item = self.load()["attention:a"]
        self.assertEqual(item["gist"], "a needs attention")
        self.assertAlmostEqual(item["ts"], NOW - 2 * 3600, delta=1)
        self.assertNotIn("issues", self.thread(item))

    # 1. stable id per episode
    def test_active_rows_use_one_id_per_episode(self):
        since = iso(-3)
        self.events([alert_event("attention:a", 3, routine="a")])
        self.active({"attention:a": since, "attention:ghost": since})
        got = self.load()
        want = f"events:alert-active:attention:a@{int(NOW - 3 * 3600)}"
        self.assertEqual(got["attention:a"]["id"], want)
        self.assertEqual(got["attention:ghost"]["id"],
                         f"events:alert-active:attention:ghost@{int(NOW - 3 * 3600)}")
        self.assertEqual(itm.validate_item(got["attention:a"]), [])

    def test_a_row_that_is_not_active_keeps_the_event_id(self):
        row = alert_event("attention:gone", 3, routine="gone")
        self.events([row])
        self.active({})
        self.assertEqual(self.load()["attention:gone"]["id"], f"events:{row['id']}")

    def test_done_survives_rotation_and_a_switch_to_a_synthesized_row(self):
        row = alert_event("attention:a", 3, routine="a")
        self.events([row])
        self.active({"attention:a": iso(-3)})
        before = self.load()["attention:a"]
        store = InboxStore(self.base)
        store.done(before["id"])
        # the event file rotates away: the row is now synthesized
        (self.base / "state" / "events.jsonl").unlink()
        after = self.load()["attention:a"]
        self.assertEqual(after["id"], before["id"])
        self.assertNotEqual(after["gist"], before["gist"])
        self.assertTrue(InboxStore(self.base).is_hidden(after["id"]))

    def test_a_re_fire_changes_the_id_and_shows_again(self):
        self.events([alert_event("attention:a", 8, routine="a")])
        self.active({"attention:a": iso(-8)})
        first = self.load()["attention:a"]
        store = InboxStore(self.base)
        store.done(first["id"])
        self.assertTrue(store.is_hidden(first["id"]))
        # cleared, then active again: a new event and a new time
        self.events([alert_event("attention:a", 1, routine="a")])
        self.active({"attention:a": iso(-1)})
        second = self.load()["attention:a"]
        self.assertNotEqual(second["id"], first["id"])
        self.assertFalse(store.is_hidden(second["id"]))

    # 2. thread order
    def test_alert_rows_are_fixed_and_others_are_not(self):
        self.events([alert_event("attention:a", 3, routine="a"),
                     events.build("r", "new", None, "mila:x", "A call", [],
                                  ts=iso(-1))])
        self.active({"attention:a": iso(-3)})
        res = sources.load_events(self.base, now=NOW)
        by_source = {i["source"]: i for i in res.items}
        self.assertTrue(models.thread_fixed(models.from_v1(by_source["alert"])))
        self.assertFalse(models.thread_fixed(models.from_v1(by_source["recording"])))
        self.assertFalse(models.thread_fixed(models.from_v1(dict(
            by_source["alert"], meta={"thread_fixed": "yes"}))))

    # 4. reeval reads no file
    def test_reeval_uses_the_log_and_state_it_already_read(self):
        self.events([alert_event("attention:a", 3, routine="a")])
        self.active({"attention:a": iso(-3)})
        self.log(f"{iso(-1)} routine=a ERROR first read\n")
        cfg = settings.validate({})
        units = sources.make_units(cfg, self.base)
        (unit,) = [u for u in units if u.name == "events"]
        (res,) = unit.run(NOW, False)
        self.assertIn("first read", self.thread(res.items[0])["last error"]["text"])
        for path in (self.base / "logs" / "run.log",
                     self.base / "state" / "alerts.json",
                     self.base / "state" / "events.jsonl"):
            path.unlink()
        (res,) = unit.reeval(NOW + 60)
        item = res.items[0]
        self.assertIn("first read", self.thread(item)["last error"]["text"])
        self.assertEqual(item["id"], f"events:alert-active:attention:a@{int(NOW - 3 * 3600)}")


if __name__ == "__main__":
    unittest.main()
