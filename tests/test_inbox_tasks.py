"""Tasks (inbox 0.12.0), without Textual: validation of a task item, the Tasks
view sections (across a date boundary: `today` is injected), the Inbox DUE
TODAY section, the due cell and the people text, the acts a task takes (the
request meta, the inverse of `due`) and a read-only source. Synthetic data."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inbox_acts_support as support  # noqa: E402

from workspace_daemon.inbox import acts, items as itm, settings, tasks, views  # noqa: E402
from workspace_daemon.inbox.actions import LocalActions  # noqa: E402
from workspace_daemon.inbox.models import from_v1  # noqa: E402
from workspace_daemon.inbox.store import InboxStore  # noqa: E402

D = dt.date
TODAY = D(2030, 1, 10)        # a Thursday


def task(n, task_type="todo", due=None, date="2030-01-01", people=(),
         origin="tasks", **kw):
    raw = itm.parse_item(support.task_item(n, task_type, due, date, people,
                                           **kw))
    it = from_v1(raw)
    it.origin = origin
    return it


def keys(rows):
    return [i.key for i in rows]


def day(offset, base=TODAY):
    return (base + dt.timedelta(days=offset)).isoformat()


class TaskItemTest(unittest.TestCase):
    def test_the_private_source_shape_survives_validation(self):
        raw = support.task_item(1, "decision", due="2030-01-12",
                                people=["Ann Example"], source="memory",
                                title="Pick one", memory_id="entry-1",
                                evidence=None)
        raw["kind"] = "task"                 # a top-level kind is ignored
        item = itm.parse_item(raw)
        self.assertEqual(item["source"], "memory")
        self.assertEqual(item["meta"]["kind"], "task")
        self.assertEqual(item["actions"], ["done"])
        row = from_v1(item)
        self.assertTrue(tasks.is_task(row))
        self.assertEqual(tasks.task_type(row), "decision")

    def test_a_task_with_a_link_and_open_action_is_valid(self):
        item = itm.parse_item(support.task_item(
            2, link="https://example.com/t2", source="memory"))
        self.assertEqual(item["actions"], ["done", "open"])

    def test_drop_and_due_are_act_names_and_settings(self):
        for name in ("drop", "due", "reopen"):
            self.assertIn(name, itm.ACT_NAMES)
        self.assertEqual(acts.INVERSE["drop"], "reopen")
        self.assertEqual(acts.INVERSE["due"], "due")
        cfg = settings.validate({"sources": [{
            "name": "tasks", "type": "command", "argv": ["x"],
            "acts": ["close", "drop", "due"]}]})
        self.assertEqual(cfg["sources"][0]["acts"], ["close", "drop", "due"])
        with self.assertRaises(settings.SettingsError):
            settings.validate({"sources": [{
                "name": "tasks", "type": "command", "argv": ["x"],
                "acts": ["close", "delete"]}]})

    def test_untrusted_meta_text_is_cleaned_and_bad_values_are_ignored(self):
        it = task(1, people=["Ann\x1b[31m Example", 7, ""],
                  evidence="line one\x00\nline two", due="not a date",
                  date="2030-02-31")
        self.assertEqual(tasks.people_of(it), ["Ann Example"])
        self.assertEqual(tasks.evidence_of(it), "line one\nline two")
        self.assertIsNone(tasks.due_of(it))
        self.assertIsNotNone(tasks.created_of(it))       # from ts
        self.assertEqual(tasks.task_type(task(2, task_type="weird")), "todo")


class SectionTest(unittest.TestCase):
    def rows(self):
        return [
            task(1, due=day(-2), date="2030-01-02"),                  # overdue
            task(2, due=day(-2), date="2030-01-01"),                  # older
            task(3, due=day(-5)),                                     # overdue
            task(4, due=day(0)),                                      # today
            task(5, due=day(1)),                                      # week
            task(6, due=day(7)),                                      # week
            task(7, due=day(8)),                                      # later
            task(8),                                                  # no date
            task(9, task_type="decision", due=day(-1)),               # decide
            task(10, task_type="decision"),                           # decide
            task(11, task_type="decision", due=day(3)),               # decide
        ]

    def groups(self, today=TODAY):
        return {g.key: keys(g.items)
                for g in views.task_groups(self.rows(), today)}

    def test_sections_in_order_with_their_rows(self):
        titles = [g.title for g in views.task_groups(self.rows(), TODAY)]
        self.assertEqual(titles, ["OVERDUE", "DUE TODAY", "THIS WEEK", "LATER",
                                  "DECIDE", "NO DATE"])
        got = self.groups()
        # due ascending, then created ascending
        self.assertEqual(got["overdue"], ["task:3", "task:2", "task:1"])
        self.assertEqual(got["today"], ["task:4"])
        self.assertEqual(got["week"], ["task:5", "task:6"])
        self.assertEqual(got["later"], ["task:7"])
        self.assertEqual(got["decide"], ["task:9", "task:11", "task:10"])
        self.assertEqual(got["nodate"], ["task:8"])

    def test_empty_sections_are_hidden(self):
        groups = views.task_groups([task(1, due=day(30))], TODAY)
        self.assertEqual([g.key for g in groups], ["later"])
        self.assertEqual(views.task_groups([], TODAY), [])

    def test_the_overdue_header_is_hot(self):
        hot = {g.key: g.hot for g in views.task_groups(self.rows(), TODAY)}
        self.assertTrue(hot["overdue"])
        self.assertFalse(hot["today"])

    def test_sections_move_across_midnight(self):
        row = task(1, due="2030-01-10")
        self.assertEqual(views.task_groups([row], D(2030, 1, 9))[0].key, "week")
        self.assertEqual(views.task_groups([row], D(2030, 1, 10))[0].key,
                         "today")
        self.assertEqual(views.task_groups([row], D(2030, 1, 11))[0].key,
                         "overdue")
        self.assertEqual(views.task_groups(
            [task(2, due="2030-01-17")], D(2030, 1, 10))[0].key, "week")
        self.assertEqual(views.task_groups(
            [task(2, due="2030-01-17")], D(2030, 1, 9))[0].key, "later")

    def test_the_tab_count_is_overdue_plus_due_today(self):
        # todos 1-4 and the decision due yesterday (9)
        self.assertEqual(tasks.due_count(self.rows(), TODAY), 5)
        self.assertEqual(tasks.due_count(self.rows(), D(2030, 1, 9)), 4)
        self.assertEqual(tasks.due_count([], TODAY), 0)

    def test_a_local_due_wins_over_the_listed_one(self):
        row = task(1, due=day(-3))
        self.assertEqual(views.task_groups([row], TODAY)[0].key, "overdue")
        row.local_due = (day(2),)
        self.assertEqual(views.task_groups([row], TODAY)[0].key, "week")
        row.local_due = (None,)
        self.assertEqual(views.task_groups([row], TODAY)[0].key, "nodate")


class InboxSectionTest(unittest.TestCase):
    def test_due_today_follows_nudge_due_with_overdue_and_today_rows(self):
        rows = [task(1, due=day(1)), task(2, due=day(0)),
                task(3, due=day(-4)), task(4, task_type="decision",
                                           due=day(-1)),
                task(5, task_type="decision", due=day(2)), task(6)]
        groups = views.inbox_groups(rows, TODAY)
        self.assertEqual([g.title for g in groups],
                         ["URGENT", "I OWE", "NUDGE DUE", "DUE TODAY"])
        due = groups[3]
        self.assertEqual(keys(due.items), ["task:3", "task:4", "task:2"])
        self.assertTrue(due.hot)
        self.assertFalse(due.keep_empty)
        # a day earlier only what was already due then
        self.assertEqual(keys(views.inbox_groups(rows, D(2030, 1, 9))[3].items),
                         ["task:3", "task:4"])
        self.assertEqual(keys(views.inbox_groups(rows, D(2030, 1, 5))[3].items),
                         [])

    def test_task_rows_are_in_no_other_group_or_view(self):
        urgent = task(1, due=day(0))
        urgent.section = "urgent"
        reply = task(2)
        reply.section = "needs_reply"
        wait = task(3)
        wait.section = "waiting"
        wait.actions = ["nudge"]
        wait.draft = "nudge text"
        fyi = task(4)
        fyi.feed = "inbox"
        rows = [urgent, reply, wait, fyi]
        groups = {g.key: g for g in views.inbox_groups(rows, TODAY)}
        for key in ("urgent", "owe", "nudge"):
            self.assertEqual(groups[key].items, [])
        self.assertEqual(keys(groups["due_today"].items), ["task:1"])
        self.assertEqual(views.waiting_groups(rows, 0.0), [])
        self.assertEqual(views.feed_view(rows, 0.0).total, 0)


class WeekStartTest(unittest.TestCase):
    """`work_week.start` ends THIS WEEK at the next start of the week."""
    WED = D(2026, 11, 25)
    SUN = D(2026, 11, 29)

    def bucket(self, due, today, week_start=None):
        return tasks.bucket(task(1, due=due.isoformat()), today, week_start)

    def test_on_a_wednesday_with_sunday(self):
        wed = self.WED
        for off, want in ((1, tasks.WEEK), (3, tasks.WEEK),   # Thu, Sat
                          (4, tasks.LATER), (5, tasks.LATER),  # Sun, Mon
                          (0, tasks.TODAY), (-1, tasks.OVERDUE)):
            self.assertEqual(
                self.bucket(wed + dt.timedelta(days=off), wed, "sunday"),
                want, off)

    def test_on_the_week_start_day_itself(self):
        sun = self.SUN
        for off, want in ((1, tasks.WEEK), (6, tasks.WEEK),
                          (7, tasks.LATER)):
            self.assertEqual(
                self.bucket(sun + dt.timedelta(days=off), sun, "sunday"),
                want, off)

    def test_unset_keeps_the_rolling_seven_days(self):
        wed = self.WED
        for off, want in ((4, tasks.WEEK), (7, tasks.WEEK), (8, tasks.LATER)):
            self.assertEqual(
                self.bucket(wed + dt.timedelta(days=off), wed), want, off)

    def test_next_week_start(self):
        self.assertEqual(tasks.next_week_start(self.WED, "sunday"), self.SUN)
        self.assertEqual(tasks.next_week_start(self.SUN, "sunday"),
                         D(2026, 12, 6))
        self.assertEqual(tasks.next_week_start(self.WED, "monday"),
                         D(2026, 11, 30))
        self.assertEqual(tasks.next_week_start(self.WED, "wednesday"),
                         D(2026, 12, 2))

    def test_task_groups_pass_it_on(self):
        rows = [task(1, due=self.SUN.isoformat()),
                task(2, due="2026-11-28")]
        groups = {g.key: keys(g.items)
                  for g in views.task_groups(rows, self.WED, "sunday")}
        self.assertEqual(groups, {tasks.WEEK: ["task:2"],
                                  tasks.LATER: ["task:1"]})
        groups = {g.key: keys(g.items)
                  for g in views.task_groups(rows, self.WED)}
        self.assertEqual(list(groups), [tasks.WEEK])

    def test_the_due_picker(self):
        plain = {k: (l, d) for k, l, d in tasks.due_choices(self.WED)}
        self.assertEqual(plain["sunday"], ("Next Sunday  Sun 29 Nov", self.SUN))
        wk = {k: (l, d) for k, l, d in tasks.due_choices(self.WED, "monday")}
        self.assertEqual(wk["sunday"],
                         ("Next week  Mon 30 Nov", D(2026, 11, 30)))
        sun = {k: (l, d) for k, l, d in tasks.due_choices(self.WED, "sunday")}
        self.assertEqual(sun["sunday"], ("Next week  Sun 29 Nov", self.SUN))

    def test_on_a_saturday_with_sunday(self):
        sat = D(2026, 11, 28)
        sun = sat + dt.timedelta(days=1)
        self.assertEqual(self.bucket(sun, sat, "sunday"), tasks.LATER)
        self.assertEqual(self.bucket(sat, sat, "sunday"), tasks.TODAY)
        groups = views.task_groups([task(1, due=sun.isoformat())], sat,
                                   "sunday")
        self.assertEqual([g.key for g in groups], [tasks.LATER])   # no THIS WEEK
        picks = {k: (l, d) for k, l, d in tasks.due_choices(sat, "sunday")}
        self.assertEqual(picks["sunday"][1], sun)
        self.assertEqual(picks["tomorrow"][1], sun)
        self.assertEqual(picks["sunday"][0], "Next week  Sun 29 Nov")

    def test_the_due_cell_names_a_weekday_only_inside_this_week(self):
        wed = self.WED
        cell = lambda off, ws=None: tasks.due_cell(   # noqa: E731
            wed + dt.timedelta(days=off), wed, ws)
        self.assertEqual(cell(3, "sunday"), "Sat")
        self.assertEqual(cell(4, "sunday"), "29 Nov")     # Sunday: LATER
        self.assertEqual(cell(5, "sunday"), "30 Nov")
        self.assertEqual(cell(4), "Sun")                  # unset: unchanged
        self.assertEqual(cell(7), "2 Dec")
        self.assertEqual(cell(0, "sunday"), "today")
        self.assertEqual(cell(-2, "sunday"), "2d late")

    def test_the_picker_shows_the_year_when_it_differs(self):
        late = D(2026, 12, 29)
        labels = {k: l for k, l, _d in tasks.due_choices(late)}
        self.assertEqual(labels["tomorrow"], "Tomorrow  Wed 30 Dec")
        self.assertEqual(labels["sunday"], "Next Sunday  Sun 3 Jan 2027")
        self.assertEqual(labels["week"], "+1 week  Tue 5 Jan 2027")

    def test_work_week_settings(self):
        self.assertIsNone(settings.validate({})["work_week"])
        ok = lambda v: settings.validate({"work_week": v})["work_week"]  # noqa: E731
        self.assertEqual(ok({"start": "sunday"}),
                         {"start": "sunday", "days": 5})
        self.assertEqual(ok({"start": "Monday", "days": 6}),
                         {"start": "monday", "days": 6})
        self.assertEqual(ok({"start": " SATURDAY ", "days": 1}),
                         {"start": "saturday", "days": 1})
        self.assertEqual(ok({"start": "sunday", "days": 7})["days"], 7)
        for bad in ("sunday", ["sunday"], 5, {}, {"days": 5},
                    {"start": "funday"}, {"start": 1}, {"start": ""},
                    {"start": "sunday", "days": 0},
                    {"start": "sunday", "days": 8},
                    {"start": "sunday", "days": 2.5},
                    {"start": "sunday", "days": "5"},
                    {"start": "sunday", "days": True},
                    {"start": "sunday", "week": 1}):
            with self.assertRaises(settings.SettingsError, msg=repr(bad)):
                settings.validate({"work_week": bad})
        with self.assertRaises(settings.SettingsError):
            settings.validate({"week_start": "sunday"})   # never released


class NextWorkDayTest(unittest.TestCase):
    WEEK = D(2026, 11, 23)        # a Monday

    def days(self, start, count):
        return [tasks.next_work_day(self.WEEK + dt.timedelta(days=n), start,
                                    count) for n in range(7)]

    def test_sunday_five_every_weekday(self):
        # Mon 23 .. Sun 29: work days are Sun-Thu
        want = [D(2026, 11, d) for d in (24, 25, 26, 29, 29, 29, 30)]
        self.assertEqual(self.days("sunday", 5), want)

    def test_monday_five_every_weekday(self):
        want = [D(2026, 11, d) for d in (24, 25, 26, 27, 30, 30, 30)]
        self.assertEqual(self.days("monday", 5), want)

    def test_seven_days_is_tomorrow(self):
        for start in tasks.WEEKDAYS:
            want = [self.WEEK + dt.timedelta(days=n + 1) for n in range(7)]
            self.assertEqual(self.days(start, 7), want, start)

    def test_one_day_is_the_next_week_start(self):
        for start in ("monday", "thursday", "sunday"):
            for n in range(7):
                today = self.WEEK + dt.timedelta(days=n)
                self.assertEqual(tasks.next_work_day(today, start, 1),
                                 tasks.next_week_start(today, start), (start, n))

    def test_the_issue_examples(self):
        self.assertEqual(tasks.next_work_day(D(2026, 11, 26), "sunday", 5),
                         D(2026, 11, 29))             # Thu -> Sun
        self.assertEqual(tasks.next_work_day(D(2026, 11, 25), "sunday", 5),
                         D(2026, 11, 26))             # Wed -> Thu

    def test_a_week_that_wraps_the_weekend(self):
        # saturday/3: Sat, Sun, Mon work; on a Monday the next is Saturday
        self.assertEqual(tasks.next_work_day(D(2026, 11, 23), "saturday", 3),
                         D(2026, 11, 28))
        self.assertEqual(tasks.next_work_day(D(2026, 11, 28), "saturday", 3),
                         D(2026, 11, 29))
        self.assertEqual(tasks.next_work_day(D(2026, 11, 29), "saturday", 3),
                         D(2026, 11, 30))

    def test_days_out_of_range_are_clamped(self):
        self.assertEqual(tasks.next_work_day(D(2026, 11, 23), "friday", 0),
                         D(2026, 11, 27))
        self.assertEqual(tasks.next_work_day(D(2026, 11, 23), "friday", 99),
                         D(2026, 11, 24))


class DueChoicesTest(unittest.TestCase):
    def ids(self, today, *args):
        return [k for k, _l, _d in tasks.due_choices(today, *args)]

    def test_without_work_week(self):
        got = tasks.due_choices(TODAY)
        self.assertEqual([k for k, _l, _d in got],
                         ["today", "tomorrow", "sunday", "week", "pick",
                          "none"])
        self.assertEqual([l for _k, l, _d in got][:4],
                         ["Today  Thu 10 Jan", "Tomorrow  Fri 11 Jan",
                          "Next Sunday  Sun 13 Jan", "+1 week  Thu 17 Jan"])
        self.assertEqual([d for _k, _l, d in got],
                         [TODAY, D(2030, 1, 11), D(2030, 1, 13),
                          D(2030, 1, 17), None, None])
        self.assertTrue(got[4][1].startswith("Pick date"))

    def test_with_work_week(self):
        thu = D(2026, 11, 26)
        got = {k: (l, d) for k, l, d in tasks.due_choices(thu, "sunday", 5)}
        self.assertEqual(got["workday"],
                         ("Next work day  Sun 29 Nov", D(2026, 11, 29)))
        self.assertEqual(got["today"][0], "Today  Thu 26 Nov")
        self.assertEqual(got["tomorrow"][1], D(2026, 11, 27))
        self.assertEqual(got["sunday"][0], "Next week  Sun 29 Nov")
        self.assertEqual(self.ids(D(2026, 11, 27), "monday", 5),
                         ["today", "tomorrow", "workday", "sunday", "week",
                          "pick", "none"])

    def test_next_work_day_is_left_out_when_it_is_tomorrow(self):
        plain = ["today", "tomorrow", "sunday", "week", "pick", "none"]
        self.assertEqual(self.ids(D(2026, 11, 25), "sunday", 5), plain)  # Wed
        self.assertEqual(self.ids(D(2026, 11, 28), "sunday", 5), plain)  # Sat
        self.assertEqual(self.ids(D(2026, 11, 25), "sunday", 7), plain)  # 7 days

    def test_next_work_day_stays_when_it_is_also_next_week(self):
        for today in (D(2026, 11, 26), D(2026, 11, 27)):     # Thu, Fri
            got = {k: d for k, _l, d in tasks.due_choices(today, "sunday", 5)}
            self.assertEqual(got["workday"], D(2026, 11, 29))
            self.assertEqual(got["sunday"], D(2026, 11, 29))

    def test_one_work_day_a_week(self):
        got = {k: d for k, _l, d in tasks.due_choices(D(2026, 11, 25),
                                                      "sunday", 1)}
        self.assertEqual(got["workday"], D(2026, 11, 29))
        self.assertEqual(got["sunday"], D(2026, 11, 29))

    def test_the_work_days_default_to_five(self):
        self.assertEqual(tasks.due_choices(D(2026, 11, 26), "sunday"),
                         tasks.due_choices(D(2026, 11, 26), "sunday", 5))

    def test_labels_carry_the_year_when_it_is_not_this_year(self):
        late = D(2026, 12, 31)                    # a Thursday
        got = {k: l for k, l, _d in tasks.due_choices(late, "sunday", 5)}
        self.assertEqual(got["today"], "Today  Thu 31 Dec")
        self.assertEqual(got["tomorrow"], "Tomorrow  Fri 1 Jan 2027")
        self.assertEqual(got["workday"], "Next work day  Sun 3 Jan 2027")
        self.assertEqual(got["sunday"], "Next week  Sun 3 Jan 2027")
        self.assertEqual(got["week"], "+1 week  Thu 7 Jan 2027")


class ParseDueInputTest(unittest.TestCase):
    TODAY = D(2026, 11, 25)       # a Wednesday

    def ok(self, text, want, today=None):
        got, why = tasks.parse_due_input(text, today or self.TODAY)
        self.assertEqual((got, why), (want, None), text)

    def bad(self, text, *words, today=None):
        got, why = tasks.parse_due_input(text, today or self.TODAY)
        self.assertIsNone(got, text)
        for word in words:
            self.assertIn(word, why, text)

    def test_strict_date(self):
        self.ok("2026-11-25", self.TODAY)
        self.ok("2030-01-31", D(2030, 1, 31))
        self.ok("  2026-12-01 ", D(2026, 12, 1))
        self.bad("2026-11-24", "has passed", "2026-11-25")
        self.bad("2025-01-09", "has passed")
        self.bad("2030-02-30", "Not a date")
        self.bad("2030-1-31", "Not a date")

    def test_today_and_tomorrow(self):
        self.ok("today", self.TODAY)
        self.ok("TODAY", self.TODAY)
        self.ok("Tomorrow", D(2026, 11, 26))
        self.ok(" tomorrow ", D(2026, 11, 26))
        self.ok("tomorrow", D(2027, 1, 1), D(2026, 12, 31))

    def test_weekday_names(self):
        # Wednesday 25 Nov 2026
        self.ok("thu", D(2026, 11, 26))
        self.ok("Thursday", D(2026, 11, 26))
        self.ok("sun", D(2026, 11, 29))
        self.ok("SUNDAY", D(2026, 11, 29))
        self.ok("mon", D(2026, 11, 30))
        self.ok("tue", D(2026, 12, 1))
        self.ok("wed", D(2026, 12, 2))            # today's name: a week out
        self.ok("wednesday", D(2026, 12, 2))
        self.ok("fri", D(2026, 11, 27))
        self.ok("sat", D(2026, 11, 28))

    def test_every_weekday_is_one_to_seven_days_ahead(self):
        for n in range(7):
            today = self.TODAY + dt.timedelta(days=n)
            for name in tasks.WEEKDAYS:
                for form in (name, name[:3]):
                    got, _ = tasks.parse_due_input(form, today)
                    self.assertEqual(got.strftime("%A").lower(), name)
                    self.assertTrue(1 <= (got - today).days <= 7, (today, form))

    def test_a_weekday_abbreviation_must_be_exact(self):
        for word in ("th", "thur", "thurs", "tues", "sund", "su", "wedn",
                     "sunday!", "sun day"):
            self.bad(word, "Not a date")

    def test_day_and_month(self):
        self.ok("25/11", self.TODAY)               # today is not passed
        self.ok("26/11", D(2026, 11, 26))
        self.ok("3.1", D(2027, 1, 3))              # passed: next year
        self.ok("03.01", D(2027, 1, 3))
        self.ok("24/11", D(2027, 11, 24))
        self.ok("1/12", D(2026, 12, 1))
        self.ok("31/12", D(2026, 12, 31))
        self.ok("1/1", D(2027, 1, 1))
        self.ok("25.11", self.TODAY)
        self.ok(" 3/1 ", D(2027, 1, 3))

    def test_day_and_month_in_a_year_change(self):
        dec = D(2026, 12, 31)
        self.ok("31/12", dec, dec)
        self.ok("1/1", D(2027, 1, 1), dec)
        self.ok("30/12", D(2027, 12, 30), dec)
        jan = D(2027, 1, 1)
        self.ok("1/1", jan, jan)
        self.ok("31/12", D(2027, 12, 31), jan)

    def test_leap_day(self):
        self.ok("29/2", D(2028, 2, 29), D(2027, 3, 1))     # next leap year
        self.ok("29/2", D(2028, 2, 29), D(2028, 1, 5))
        self.ok("29/2", D(2028, 2, 29), D(2028, 2, 29))
        self.bad("29/2", "calendar", today=D(2025, 3, 1))  # none in range

    def test_day_and_month_that_do_not_exist(self):
        for text in ("31/2", "30/2", "31/4", "0/5", "5/0", "32/1", "1/13",
                     "99/99"):
            self.bad(text, "not a day of the calendar", "Not a date")

    def test_day_and_month_wrong_shape(self):
        for text in ("25/11/2026", "25-11", "2026/11", "1/2/3", "/11", "25/",
                     "25//11", "123/4", "1/234"):
            self.bad(text, "Not a date")

    def test_plus_days(self):
        self.ok("+1", D(2026, 11, 26))
        self.ok("+1d", D(2026, 11, 26))
        self.ok("+3d", D(2026, 11, 28))
        self.ok("+7", D(2026, 12, 2))
        self.ok("+10D", D(2026, 12, 5))
        self.ok("+365", D(2027, 11, 25))
        self.ok("+365d", D(2027, 11, 25))
        self.ok("+007", D(2026, 12, 2))

    def test_plus_weeks(self):
        self.ok("+1w", D(2026, 12, 2))
        self.ok("+2w", D(2026, 12, 9))
        self.ok("+2W", D(2026, 12, 9))
        self.ok("+52w", D(2027, 11, 24))

    def test_plus_out_of_range(self):
        for text in ("+0", "+0d", "+366", "+366d", "+1000", "+9999d"):
            self.bad(text, "1 to 365 days")
        for text in ("+0w", "+53w", "+500w"):
            self.bad(text, "1 to 52 weeks")

    def test_plus_wrong_shape(self):
        for text in ("+", "+d", "+w", "+-3", "+3x", "+3 d", "+3dd", "3d",
                     "+1.5", "++3", "+12345"):
            self.bad(text, "Not a date")

    def test_anything_else_lists_the_forms(self):
        for text in ("", "   ", "soon", "next week", "yesterday", "tomorow",
                     "nov 25", "25 nov", "-1", "0", "2026", "2026-11",
                     "20261125", "now", None, 5, ["today"]):
            self.bad(text, "Not a date", "YYYY-MM-DD", "tomorrow", "thu",
                     "25/11", "+3d", "+2w")

    def test_the_result_is_never_before_today(self):
        for n in range(0, 800, 17):
            today = self.TODAY + dt.timedelta(days=n)
            for text in ("today", "tomorrow", "sun", "wed", "1/1", "29/2",
                         "31/12", "+1", "+30d", "+4w", "15.6"):
                got, why = tasks.parse_due_input(text, today)
                if got is not None:
                    self.assertGreaterEqual(got, today, (today, text))
                else:
                    self.assertTrue(why)


class CellTest(unittest.TestCase):
    def test_due_cell(self):
        cell = lambda offset: tasks.due_cell(
            None if offset is None else TODAY + dt.timedelta(days=offset),
            TODAY)
        self.assertEqual(cell(None), "-")
        self.assertEqual(cell(0), "today")
        self.assertEqual(cell(-2), "2d late")
        self.assertEqual(cell(1), "Fri")
        self.assertEqual(cell(6), "Wed")
        self.assertEqual(cell(7), "17 Jan")
        self.assertEqual(cell(40), "19 Feb")

    def test_people_and_age(self):
        self.assertEqual(tasks.people_compact([]), "")
        self.assertEqual(tasks.people_compact(["Ann Example"]), "Ann")
        self.assertEqual(tasks.people_compact(["Ann E", "Bo F"]), "Ann, Bo")
        self.assertEqual(tasks.people_compact(["Ann E", "Bo F", "Cy G", "Di H"]),
                         "Ann, Bo +2")
        self.assertEqual(tasks.age_label(D(2030, 1, 7), TODAY), "3d")
        self.assertEqual(tasks.age_label(TODAY, TODAY), "0d")
        self.assertEqual(tasks.age_label(None, TODAY), "-")

    def test_the_due_pickers(self):
        choices = {k: d for k, _l, d in tasks.due_choices(TODAY)}
        self.assertEqual(choices["today"], TODAY)
        self.assertEqual(choices["tomorrow"], D(2030, 1, 11))
        self.assertEqual(choices["sunday"], D(2030, 1, 13))
        self.assertEqual(choices["week"], D(2030, 1, 17))
        self.assertIsNone(choices["pick"])
        self.assertIsNone(choices["none"])
        # on a Sunday "next Sunday" is a week away
        self.assertEqual(tasks.next_sunday(D(2030, 1, 13)), D(2030, 1, 20))
        self.assertEqual(tasks.next_sunday(D(2030, 1, 12)), D(2030, 1, 13))
        self.assertEqual(tasks.parse_day("2030-01-31"), D(2030, 1, 31))
        for bad in ("2030-1-31", "2030-02-30", "tomorrow", "", None, 5):
            self.assertIsNone(tasks.parse_day(bad))


class ActsTest(unittest.TestCase):
    def setUp(self):
        self.src = support.FakeSource()
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = InboxStore(self.base)
        self.addCleanup(self.src.cleanup)
        self.addCleanup(self.tmp.cleanup)

    def actions(self, acts_list=None, **kw):
        return LocalActions(self.store, base_dir=self.base, **kw,
                            acts=support.acts_cfg(
                                self.src, name="tasks",
                                acts=support.TASK_ACTS if acts_list is None
                                else acts_list))

    def test_close_and_drop_send_the_note(self):
        a = self.actions()
        it = task(1)
        self.assertEqual(a.send_act(it, "close", {"note": "done it"}).status,
                         acts.OK)
        self.assertEqual(a.send_act(it, "drop", {"note": ""}).status, acts.OK)
        close, drop = self.src.requests()
        self.assertEqual((close["action"], close["args"]),
                         ("close", {"note": "done it"}))
        self.assertEqual((drop["action"], drop["args"]),
                         ("drop", {"note": ""}))
        self.assertEqual(close["meta"]["kind"], "task")

    def test_due_sends_the_date_and_the_inverse_restores_the_previous(self):
        a = self.actions()
        it = task(1, due="2030-01-12")
        out = a.send_act(it, "due", {"date": "2030-01-20"})
        self.assertEqual(out.status, acts.OK)
        (req,) = self.src.requests()
        self.assertEqual((req["action"], req["args"], req["meta"]["due"]),
                         ("due", {"date": "2030-01-20"}, "2030-01-12"))
        (logged,) = a.ops_rows()
        self.assertEqual(logged["prev_due"], "2030-01-12")
        self.assertEqual(a.invertible(logged), "due")
        inv = a.send_inverse(logged)
        self.assertEqual(inv.status, acts.OK)
        back = self.src.requests()[1]
        self.assertEqual((back["action"], back["args"]),
                         ("due", {"date": "2030-01-12"}))

    def test_the_inverse_of_a_move_from_no_date_clears_the_date(self):
        a = self.actions()
        a.send_act(task(1), "due", {"date": "2030-01-20"})
        (logged,) = a.ops_rows()
        self.assertIsNone(logged["prev_due"])
        a.send_inverse(logged)
        self.assertEqual(self.src.requests()[1]["args"], {"date": None})

    def test_a_second_move_remembers_the_date_shown_not_the_listed_one(self):
        a = self.actions()
        it = task(1, due="2030-01-12")
        it.local_due = ("2030-01-15",)       # a move not yet in the list
        a.send_act(it, "due", {"date": "2030-01-20"})
        (logged,) = a.ops_rows()
        self.assertEqual(logged["prev_due"], "2030-01-15")
        self.assertEqual(self.src.requests()[0]["meta"]["due"], "2030-01-15")

    def test_a_due_row_without_a_known_previous_date_has_no_inverse(self):
        a = self.actions()
        a.send_act(task(1), "due", {"date": "2030-01-20"})
        (logged,) = a.ops_rows()
        gone = dict(logged)
        del gone["prev_due"]
        self.assertIsNone(a.invertible(gone))
        self.assertIsNone(a.invertible(dict(logged, prev_due="soon")))

    def test_close_and_drop_have_the_reopen_inverse(self):
        a = self.actions()
        a.send_act(task(1), "close", {"note": ""})
        a.send_act(task(2), "drop", {"note": "no"})
        for logged in a.ops_rows():
            self.assertEqual(a.invertible(logged), "reopen")
            self.assertEqual(acts.inverse_args(logged), {})

    def test_the_fake_source_round_trip_changes_the_list(self):
        a = self.actions()
        self.src.set_items([support.task_item(1, due="2030-01-12"),
                            support.task_item(2)])
        a.send_act(task(1, due="2030-01-12"), "due", {"date": "2030-01-20"})
        a.send_act(task(2), "close", {"note": ""})
        listed = {i["id"]: i for i in self.src.items()}
        self.assertEqual(list(listed), ["task:1"])
        self.assertEqual(listed["task:1"]["meta"]["due"], "2030-01-20")
        for logged in a.ops_rows():
            a.send_inverse(logged)
        listed = {i["id"]: i for i in self.src.items()}
        self.assertEqual(listed["task:1"]["meta"]["due"], "2030-01-12")
        self.assertIn("task:2", listed)

    def test_a_task_with_a_big_meta_is_still_flagged_and_undoable(self):
        a = self.actions()
        it = task(1, due="2030-01-12", evidence="x" * 1900,
                  extra_blob="y" * 9000)         # over both meta limits
        out = a.send_act(it, "due", {"date": "2030-01-20"})
        self.assertEqual(out.status, acts.OK)
        (logged,) = a.ops_rows()
        self.assertEqual(logged["meta"], {})          # trimmed in the log
        self.assertIs(logged["task"], True)
        self.assertEqual(logged["prev_due"], "2030-01-12")
        self.assertEqual(a.invertible(logged), "due")
        a.send_inverse(logged)
        self.assertEqual(self.src.requests()[1]["args"],
                         {"date": "2030-01-12"})

    def test_a_pending_task_op_keeps_its_log_extra_for_the_retry(self):
        a = self.actions()
        self.src.configure("fail")
        a.send_act(task(1, due="2030-01-12"), "due", {"date": "2030-01-20"})
        (entry,) = a._pending_ops.all()
        self.assertEqual(entry["log_extra"],
                         {"task": True, "prev_due": "2030-01-12"})
        self.src.configure("ok")
        report = a.retry_pending(now=entry["queued_at"] + 10_000)
        self.assertEqual(report.results[0][1].status, acts.OK)
        retried = [r for r in a.ops_rows() if r["result"] == "ok"][0]
        self.assertTrue(retried["retry"] and retried["task"])
        self.assertEqual(retried["prev_due"], "2030-01-12")

    def test_an_older_pending_due_is_never_retried_after_a_newer_one(self):
        a = self.actions()
        self.src.set_items([support.task_item(1, due="2030-01-12")])
        self.src.configure("fail")
        a.send_act(task(1, due="2030-01-12"), "due", {"date": "2030-01-15"})
        (older,) = a._pending_ops.all()
        self.src.configure("ok")
        a.send_act(task(1, due="2030-01-12"), "due", {"date": "2030-01-20"})
        self.assertEqual(a._pending_ops.all(), [])
        report = a.retry_pending(now=older["queued_at"] + 10_000)
        self.assertEqual(report.results, [])
        self.assertEqual(self.src.items()[0]["meta"]["due"], "2030-01-20")
        self.assertEqual([r["args"]["date"] for r in self.src.requests()
                          if r["action"] == "due"],
                         ["2030-01-15", "2030-01-20"])

    def test_a_retry_that_read_the_list_before_the_newer_due_skips_it(self):
        a = self.actions()
        self.src.set_items([support.task_item(1, due="2030-01-12")])
        self.src.configure("fail")
        a.send_act(task(1, due="2030-01-12"), "due", {"date": "2030-01-15"})
        (older,) = a._pending_ops.all()
        stale_list = [older]                 # what a retry had already read
        self.src.configure("ok")
        a.send_act(task(1, due="2030-01-12"), "due", {"date": "2030-01-20"})
        real_due = a._pending_ops.due
        a._pending_ops.due = lambda now=None: stale_list
        try:
            report = a.retry_pending(now=older["queued_at"] + 10_000)
        finally:
            a._pending_ops.due = real_due
        self.assertEqual(report.results, [])
        self.assertEqual(self.src.items()[0]["meta"]["due"], "2030-01-20")
        self.assertEqual(len([r for r in self.src.requests()
                              if r["action"] == "due"]), 2)

    def test_a_source_without_acts_is_read_only(self):
        # a source with no `acts:` is left out of the config entirely
        a = LocalActions(self.store, base_dir=self.base, acts={})
        self.assertFalse(a.acts_configured)
        it = task(1)
        for name in ("close", "drop", "due"):
            self.assertFalse(a.act_enabled(it, name))
        self.assertEqual(a.send_act(it, "close", {"note": ""}).status,
                         "unavailable")
        self.assertEqual(self.src.requests(), [])

    def test_a_source_listing_only_close_does_not_take_due(self):
        a = self.actions(acts_list=["close"])
        self.assertTrue(a.act_enabled(task(1), "close"))
        self.assertFalse(a.act_enabled(task(1), "due"))
        self.assertFalse(a.act_enabled(task(1), "drop"))


if __name__ == "__main__":
    unittest.main()
