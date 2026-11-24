"""Relative due phrases: the resolver, the work-week setting, the override."""
import datetime
import json
import unittest
from unittest import mock

try:    # run by discovery (tests/ on sys.path) or by dotted module name
    import test_owner_todos as base
except ImportError:
    from tests import test_owner_todos as base
from workspace_daemon import due_phrases, llm, memory_sink
from workspace_daemon.due_phrases import WorkWeek, parse_week, resolve

MON_FRI = WorkWeek(0, 5)
SUN_THU = WorkWeek(6, 5)

# 2026-11-23 is a Monday.
MON, TUE, WED, THU, FRI, SAT, SUN = (
    f"2026-11-{n}" for n in (23, 24, 25, 26, 27, 28, 29))


class ResolveTest(unittest.TestCase):
    def check(self, text, anchor, week, expected):
        self.assertEqual(resolve(text, anchor, week), expected,
                         f"{text!r} on {anchor} ({week.label()})")

    def test_early_next_week_is_the_second_working_day(self):
        self.check("early next week", WED, MON_FRI, "2026-12-01")  # Tuesday
        self.check("early next week", WED, SUN_THU, "2026-11-30")  # Monday

    def test_next_week_is_the_first_working_day(self):
        self.check("next week", WED, MON_FRI, "2026-11-30")        # Monday
        self.check("next week", WED, SUN_THU, "2026-11-29")        # Sunday

    def test_end_of_this_week_is_the_last_working_day(self):
        for phrase in ("end of the week", "end of this week", "end of week",
                       "by the end of the week"):
            self.check(phrase, WED, MON_FRI, FRI)
            self.check(phrase, WED, SUN_THU, THU)

    def test_end_of_next_week(self):
        self.check("end of next week", WED, MON_FRI, "2026-12-04")
        self.check("end of next week", WED, SUN_THU, "2026-12-03")
        self.check("by the end of the next week", WED, MON_FRI, "2026-12-04")

    def test_a_deadline_weekday_is_the_next_such_day(self):
        for cue in ("by", "until", "due", "due by", "due on"):
            self.check(f"{cue} Thursday", WED, MON_FRI, THU)
        self.check("by Monday", WED, MON_FRI, "2026-11-30")
        self.check("by Thursday", WED, SUN_THU, THU)
        self.check("by this coming Thursday", WED, MON_FRI, THU)

    def test_asking_on_the_named_weekday_goes_a_week_out(self):
        self.check("by Thursday", THU, MON_FRI, "2026-12-03")
        self.check("due Thursday", THU, SUN_THU, "2026-12-03")

    def test_a_bare_weekday_is_not_a_deadline(self):
        for text in ("on Thursday", "Thursday", "see you on Monday",
                     "on Monday we agreed"):
            self.check(text, WED, MON_FRI, None)

    def test_weekday_phrase_keeps_an_off_day(self):
        self.check("by Friday", WED, SUN_THU, FRI)

    def test_weekday_next_week_is_that_day_of_next_week(self):
        for text in ("by Thursday next week", "on Thursday next week",
                     "Thursday next week", "next week Thursday",
                     "next week, on Thursday", "next week by Thursday"):
            self.check(text, WED, MON_FRI, "2026-12-03")
            self.check(text, WED, SUN_THU, "2026-12-03")
        self.check("next week Tuesday", WED, MON_FRI, "2026-12-01")
        self.check("on Sunday next week", WED, SUN_THU, "2026-11-29")
        self.check("on Monday next week", WED, MON_FRI, "2026-11-30")

    def test_end_of_day_with_a_weekday_is_that_weekday(self):
        for text in ("end of day Thursday", "EOD Thursday",
                     "end of the day on Thursday", "Thursday EOD"):
            self.check(text, WED, MON_FRI, THU)
        self.check("by Thursday EOD", WED, MON_FRI, THU)
        # No cue, so today counts; with "by" it never does.
        self.check("EOD Thursday", THU, MON_FRI, THU)
        self.check("by Thursday EOD", THU, MON_FRI, "2026-12-03")

    def test_tomorrow_skips_off_days(self):
        self.check("tomorrow", WED, MON_FRI, THU)
        self.check("tomorrow", FRI, MON_FRI, "2026-11-30")
        self.check("tomorrow", SAT, MON_FRI, "2026-11-30")
        self.check("tomorrow", THU, SUN_THU, "2026-11-29")
        self.check("tomorrow", WED, SUN_THU, THU)

    def test_eod_and_a_cued_today_are_the_anchor_even_on_an_off_day(self):
        for phrase in ("EOD", "end of day", "end of the day", "by today",
                       "due today", "until today", "by EOD"):
            self.check(phrase, WED, MON_FRI, WED)
            self.check(phrase, SAT, MON_FRI, SAT)

    def test_a_bare_or_possessive_today_never_matches(self):
        for text in ("today", "we spoke today", "today's plan", "by today's",
                     "tomorrow's meeting", "next week's agenda"):
            self.check(text, WED, MON_FRI, None)

    def test_asking_on_an_off_day(self):
        # Mon-Fri team, Saturday: the coming Monday starts "next week".
        self.check("next week", SAT, MON_FRI, "2026-11-30")
        self.check("early next week", SUN, MON_FRI, "2026-12-01")
        # Sun-Thu team, Friday and Saturday: Sunday starts "next week".
        self.check("next week", FRI, SUN_THU, "2026-11-29")
        self.check("early next week", SAT, SUN_THU, "2026-11-30")
        self.check("end of next week", FRI, SUN_THU, "2026-12-03")

    def test_a_passed_end_of_this_week_is_unknown(self):
        self.check("end of the week", SAT, MON_FRI, None)
        self.check("end of the week", FRI, SUN_THU, None)
        self.check("end of the week", FRI, MON_FRI, FRI)

    def test_case_insensitive_and_spaced(self):
        self.check("Early  Next WEEK", WED, MON_FRI, "2026-12-01")
        self.check("BY thursday", WED, MON_FRI, THU)

    def test_phrase_inside_a_sentence(self):
        self.check("I will send it early next week, thanks", WED, MON_FRI,
                   "2026-12-01")
        self.check("early next week, ideally by today", WED, MON_FRI, None)

    def test_phrases_that_disagree_give_none(self):
        for text in ("tomorrow or early next week",
                     "by Thursday, or at the latest next week",
                     "tomorrow and EOD", "early next week or end of next week"):
            self.check(text, WED, MON_FRI, None)

    def test_phrases_that_agree_still_resolve(self):
        self.check("by Thursday, due Thursday", WED, MON_FRI, THU)
        self.check("EOD, or by today", WED, MON_FRI, WED)

    def test_a_longer_phrase_swallows_the_shorter_one_inside_it(self):
        self.check("end of next week", WED, MON_FRI, "2026-12-04")
        self.check("early next week", WED, MON_FRI, "2026-12-01")
        self.check("end of day Thursday", WED, MON_FRI, THU)

    def test_a_negated_phrase_is_skipped(self):
        for text in ("not by Thursday", "don't send it by Thursday",
                     "not tomorrow", "no rush next week", "never by Friday",
                     "rather than tomorrow", "instead of next week",
                     "can't do it early next week", "we won\u2019t make EOD"):
            self.check(text, WED, MON_FRI, None)
        self.check("not tomorrow, early next week", WED, MON_FRI, "2026-12-01")
        self.check("I know it is not urgent. Send it by Thursday", WED,
                   MON_FRI, THU)

    def test_an_explicit_date_blocks_the_override(self):
        for text in ("by Thursday 2026-11-27", "by Thursday, Nov 26",
                     "by Thursday the 26th", "early next week (Dec 1)",
                     "by Thursday 11/26", "tomorrow, 26 November",
                     "tomorrow, 26th"):
            self.check(text, WED, MON_FRI, None)
        self.check("by Thursday, see the March notes", WED, MON_FRI, THU)

    def test_default_week_is_monday_to_friday(self):
        self.assertEqual(resolve("early next week", WED), "2026-12-01")
        self.assertEqual(resolve("early next week", WED, None), "2026-12-01")

    def test_accepts_dates_datetimes_and_iso_prefixes(self):
        day = datetime.date(2026, 11, 25)
        self.assertEqual(resolve("tomorrow", day), THU)
        self.assertEqual(
            resolve("tomorrow", datetime.datetime(2026, 11, 25, 9)), THU)
        self.assertEqual(resolve("tomorrow", "2026-11-25T09:00:00Z"), THU)

    def test_unknown_gives_none(self):
        for text in ("", None, "sometime soon", "next month", "later",
                     "late next week", "later next week", "mid next week",
                     "by next Thursday", "the weekend"):
            self.check(text, WED, MON_FRI, None)
        self.assertIsNone(resolve("tomorrow", "not a date"))
        self.assertIsNone(resolve("tomorrow", None))

    def test_no_match_inside_unrelated_words(self):
        for text in ("geod survey", "the video-eod format", "by today-ish",
                     "pretoday", "by todays", "nextweek",
                     "by Thursdays", "bye Thursday", "due Mondays",
                     "tomorrowland", "weekend", "prefix early next weeks",
                     "end of daytime"):
            self.check(text, WED, MON_FRI, None)

    def test_one_day_week_clamps_the_second_day(self):
        week = WorkWeek(0, 1)
        self.check("early next week", WED, week, "2026-11-30")
        self.check("end of next week", WED, week, "2026-11-30")

    def test_analyse_counts_the_phrases(self):
        self.assertEqual(due_phrases.analyse("nothing", WED), (0, None))
        self.assertEqual(due_phrases.analyse("by Thursday", WED), (1, THU))
        self.assertEqual(
            due_phrases.analyse("tomorrow or by Monday", WED), (2, None))
        self.assertEqual(
            due_phrases.analyse("by Thursday 2026-11-27", WED), (1, None))
        self.assertEqual(due_phrases.analyse("tomorrow", "bad"), (1, None))


class WorkWeekConfigTest(unittest.TestCase):
    def test_default_and_mapping(self):
        self.assertEqual(parse_week(None), MON_FRI)
        self.assertEqual(parse_week({"start": "sunday", "days": 5}), SUN_THU)
        self.assertEqual(parse_week({"start": "Sun"}), WorkWeek(6, 5))
        self.assertEqual(parse_week({"days": 4}), WorkWeek(0, 4))

    def test_list_of_names(self):
        self.assertEqual(parse_week(
            ["sunday", "monday", "tuesday", "wednesday", "thursday"]), SUN_THU)
        self.assertEqual(parse_week(
            ["Thursday", "Wednesday", "monday", "tuesday", "sunday"]), SUN_THU)
        self.assertEqual(parse_week(["mon", "tue", "wed", "thu", "fri"]),
                         MON_FRI)
        self.assertEqual(parse_week(
            ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]), WorkWeek(0, 7))

    def test_bad_values(self):
        for bad in ([], {}, "monday", 5, ["mon", "wed"], ["mon", "mon"],
                    ["funday"], {"start": "someday"}, {"days": 0},
                    {"days": 8}, {"days": True}, {"days": "5"},
                    {"start": "mon", "extra": 1}):
            with self.subTest(bad=bad):
                if bad == {}:
                    self.assertEqual(parse_week(bad), MON_FRI)
                    continue
                with self.assertRaises(ValueError):
                    parse_week(bad)

    def test_validate_names_the_setting(self):
        self.assertEqual(due_phrases.validate({}, "r"), [])
        self.assertEqual(due_phrases.validate(
            {"work_week": {"start": "sunday", "days": 5}}, "r"), [])
        problems = due_phrases.validate({"work_week": ["mon", "wed"]}, "r")
        self.assertEqual(len(problems), 1)
        self.assertIn("r: memory.work_week", problems[0])

    def test_memory_validate_accepts_and_rejects_the_settings(self):
        routine = base.make_routine()
        routine["memory"]["work_week"] = {"start": "sunday", "days": 5}
        routine["memory"]["due_dates"] = True
        self.assertEqual(memory_sink.validate(routine), [])
        routine["memory"]["work_week"] = "sun-thu"
        routine["memory"]["due_dates"] = "yes"
        problems = memory_sink.validate(routine)
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("work_week" in p for p in problems))
        self.assertTrue(any("due_dates" in p for p in problems))

    def test_week_from_routine_falls_back_to_default(self):
        self.assertEqual(due_phrases.week_from_routine({}), MON_FRI)
        self.assertEqual(due_phrases.week_from_routine(
            {"memory": {"work_week": "garbage"}}), MON_FRI)
        self.assertEqual(due_phrases.week_from_routine(
            {"memory": {"work_week": {"start": "sunday"}}}), SUN_THU)


def main_date_anchor(routine, item, today=None):
    """origin/main's llm.date_anchor, verbatim: the reference an unconfigured
    routine's prompt must still match byte for byte."""
    try:
        day = datetime.date.fromisoformat(str((item or {}).get("date") or "")[:10])
    except ValueError:
        day = today or datetime.datetime.now(llm.routine_zone(routine)).date()
    rows = "\n".join(
        f"{llm._WEEKDAYS[d.weekday()][:3]} {d.isoformat()}"
        for d in (day + datetime.timedelta(days=n) for n in range(1, 8))
    )
    return (
        f"Date reference: the source is dated {llm._WEEKDAYS[day.weekday()]} "
        f"{day.isoformat()}.\nThe next 7 days are:\n{rows}\n"
        "Resolve every relative day or date in the text (\"Thursday\", "
        "\"tomorrow\", \"next week\", \"by Friday\") ONLY from this table, "
        "and write dates as YYYY-MM-DD. Never count weekdays yourself; when "
        "the day is not in the table, keep the original wording.\n"
    )


class DateAnchorWeekTest(unittest.TestCase):
    ITEM = {"date": WED, "title": "t", "body": "b", "frontmatter": {}}

    def anchor(self, **memory):
        return llm.date_anchor({"memory": dict(store="/s", **memory)},
                               self.ITEM)

    def test_unconfigured_anchor_is_main_s_text_byte_for_byte(self):
        zoned = {"id": "r", "sources": [
            {"kind": "mila", "calendar_timezone": "Europe/London"}]}
        for routine in ({}, {"memory": {"store": "/s"}},
                        {"memory": {"store": "/s", "due_dates": False}},
                        {"memory": {"store": "/s", "owner_todos": {}}}, zoned):
            for item in (self.ITEM, dict(self.ITEM, date=""),
                         dict(self.ITEM, date="2026-11-24T23:30:00Z")):
                with self.subTest(routine=routine, date=item["date"]):
                    self.assertEqual(
                        llm.date_anchor(routine, item,
                                        today=datetime.date(2026, 11, 4)),
                        main_date_anchor(routine, item,
                                         today=datetime.date(2026, 11, 4)))
        self.assertNotIn("Work week", self.anchor())

    def test_unconfigured_analysis_prompt_is_unchanged(self):
        routine = {"id": "r", "analyze": {
            "provider": "claude", "model": "m", "instruction": "Summarize."},
            "memory": {"store": "/s", "type": "note", "extract": True}}
        item = dict(self.ITEM, id="x")
        with mock.patch.object(llm.secrets, "token_hex", return_value="tok"):
            now = llm.build_prompt(routine, item, [])
            with mock.patch.object(llm, "date_anchor",
                                   side_effect=main_date_anchor):
                before = llm.build_prompt(routine, item, [])
        self.assertEqual(now, before)

    def test_unconfigured_extraction_prompt_is_unchanged(self):
        routine = {"id": "r", "analyze": {
            "provider": "claude", "model": "m", "instruction": "Summarize."},
            "memory": {"store": "/s", "type": "note", "extract": True}}
        prompts = []

        def run(anchor=None):
            def fake(routine, prompt, **kwargs):
                prompts.append(prompt)
                return json.dumps(entry())
            patches = [mock.patch.object(llm, "analyze", side_effect=fake),
                       mock.patch.object(llm.secrets, "token_hex",
                                         return_value="tok"),
                       mock.patch.object(memory_sink, "known_person_slugs",
                                         return_value=set())]
            if anchor:
                patches.append(mock.patch.object(
                    llm, "date_anchor", side_effect=anchor))
            for patch in patches:
                patch.start()
            try:
                memory_sink._extract(routine, slack_item(), "s", "/store")
            finally:
                mock.patch.stopall()

        run()
        run(main_date_anchor)
        self.assertEqual(prompts[0], prompts[1])
        self.assertNotIn('"due"', prompts[0])
        self.assertNotIn("Work week", prompts[0])

    def test_work_week_or_due_dates_adds_the_convention(self):
        for memory in ({"work_week": {"start": "monday", "days": 5}},
                       {"due_dates": True}):
            with self.subTest(memory=memory):
                text = self.anchor(**memory)
                self.assertIn(
                    "Work week: Monday-Friday (off: Saturday, Sunday)", text)
                self.assertIn('"early next week" = 2026-12-01', text)
                self.assertIn('"next week" = 2026-11-30', text)
                self.assertIn('"end of this week" = 2026-11-27', text)

    def test_follows_the_configured_week(self):
        text = self.anchor(work_week={"start": "sunday", "days": 5})
        self.assertIn("Work week: Sunday-Thursday (off: Friday, Saturday)",
                      text)
        self.assertIn('"early next week" = 2026-11-30', text)
        self.assertIn('"next week" = 2026-11-29', text)
        self.assertIn('"end of next week" = 2026-12-03', text)

    def test_table_rows_are_unchanged(self):
        for memory in ({}, {"work_week": {"start": "sunday"}}):
            rows = [line for line in self.anchor(**memory).splitlines()
                    if line[:3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat",
                                    "Sun"} and line[3] == " "]
            self.assertEqual(len(rows), 7)


class AnchorDayTest(unittest.TestCase):
    ZONED = {"timezone": "Asia/Jerusalem"}

    def test_a_plain_date_is_used_as_written(self):
        self.assertEqual(llm.anchor_day(self.ZONED, {"date": "2026-11-24"},
                                        localize=True),
                         datetime.date(2026, 11, 24))

    def test_a_timestamp_converts_to_the_configured_zone(self):
        item = {"date": "2026-11-24T23:30:00Z"}
        self.assertEqual(llm.anchor_day(self.ZONED, item, localize=True),
                         datetime.date(2026, 11, 25))
        self.assertEqual(llm.anchor_day(self.ZONED, item),
                         datetime.date(2026, 11, 24))

    def test_no_zone_or_a_naive_timestamp_keeps_the_date_part(self):
        self.assertEqual(
            llm.anchor_day({}, {"date": "2026-11-24T23:30:00Z"},
                           localize=True), datetime.date(2026, 11, 24))
        self.assertEqual(
            llm.anchor_day(self.ZONED, {"date": "2026-11-24T23:30:00"},
                           localize=True), datetime.date(2026, 11, 24))

    def test_a_missing_date_falls_back_to_today(self):
        today = datetime.date(2026, 11, 4)
        self.assertEqual(llm.anchor_day({}, {}, today=today, localize=True),
                         today)


# --- the override wiring ------------------------------------------------------

MEETING_DATE = "2026-10-01"  # a Thursday
LINE = f"[00:12:03] {base.NAME}: I will send the revised plan early next week."
PLAIN_LINE = f"[00:12:03] {base.NAME}: I will send the revised plan."
TWO_LINE = (f"[00:12:03] {base.NAME}: I will send it tomorrow, or early "
            "next week at the latest.")
OPT_IN = {"start": "monday", "days": 5}


def routine_with(week=None, due_dates=False):
    routine = base.make_routine()
    if week:
        routine["memory"]["work_week"] = week
    if due_dates:
        routine["memory"]["due_dates"] = True
    return routine


class OwnerActionOverrideTest(base.CaptureHarness):
    def filed(self, line, text, model_due, **routine_kw):
        item = base.meeting_item(line)
        self.capture(
            base.meeting_extraction([base.act(text, due=model_due)]),
            item=item, routine=routine_with(**routine_kw))
        (row,) = self.store.todos().values()
        return row

    def body(self, due):
        return (f"From meeting e1 at {base.TS} "
                f"(evidence: meeting-owner-action){due}.")

    def test_without_work_week_nothing_changes_byte_for_byte(self):
        for due_dates in (False, True):
            self.store = base.FakeStore()
            row = self.filed(LINE, "Send the revised plan by Monday",
                             "2026-10-02", due_dates=due_dates)
            self.assertEqual(row["body"], self.body("; due 2026-10-02"))
            self.assertEqual(row["flags"].get("--due"),
                             "2026-10-02" if due_dates else None)

    def test_without_work_week_review_rows_are_unchanged(self):
        routine = routine_with()
        routine["memory"]["owner_todos"]["mode"] = "review"
        rows = []
        with mock.patch.object(memory_sink, "file_action_review",
                               side_effect=lambda *a, **k: rows.extend(
                                   k["candidates"]) or {}):
            self.capture(
                base.meeting_extraction(
                    [base.act("Send the plan tomorrow", due="2026-10-02")]),
                item=base.meeting_item(LINE), routine=routine)
        self.assertEqual(rows, [{
            "text": "Send the plan tomorrow", "evidence_ts": base.TS,
            "due": "2026-10-02", "status": "valid", "reason": None,
            "quote": LINE[len("[00:12:03] "):]}])

    def test_phrase_on_the_owner_line_overrides_the_model_due(self):
        row = self.filed(LINE, "Send the revised plan", "2026-10-02",
                         week=OPT_IN)
        self.assertEqual(row["body"], self.body("; due 2026-10-06"))  # Tuesday

    def test_the_configured_week_changes_the_date(self):
        row = self.filed(LINE, "Send the revised plan", "2026-10-02",
                         week={"start": "sunday", "days": 5})
        self.assertEqual(row["body"], self.body("; due 2026-10-05"))  # Monday

    def test_the_action_text_is_checked_before_the_line(self):
        row = self.filed(LINE, "Send the plan by Monday", "2026-10-02",
                         week=OPT_IN)
        self.assertEqual(row["body"], self.body("; due 2026-10-05"))
        row = self.filed(PLAIN_LINE, "Send the plan by Monday", None,
                         week=OPT_IN)
        self.assertIn("due 2026-10-05", row["body"])

    def test_an_ambiguous_action_text_keeps_the_model_due(self):
        row = self.filed(LINE, "Send it tomorrow or by Monday", "2026-10-08",
                         week=OPT_IN)
        self.assertEqual(row["body"], self.body("; due 2026-10-08"))

    def test_the_line_counts_only_with_exactly_one_phrase(self):
        row = self.filed(TWO_LINE, "Send the revised plan", "2026-10-08",
                         week=OPT_IN)
        self.assertEqual(row["body"], self.body("; due 2026-10-08"))

    def test_no_phrase_keeps_the_model_due(self):
        row = self.filed(PLAIN_LINE, "Send the revised plan", "2026-10-08",
                         week=OPT_IN)
        self.assertIn("due 2026-10-08", row["body"])

    def test_no_phrase_and_no_model_due_means_no_due(self):
        row = self.filed(PLAIN_LINE, "Send the revised plan", None,
                         week=OPT_IN)
        self.assertEqual(row["body"], self.body(""))

    def test_the_due_flag_matches_the_body_and_needs_due_dates(self):
        off = self.filed(LINE, "Send the revised plan", None, week=OPT_IN)
        self.assertNotIn("--due", off["flags"])
        self.assertIn("due 2026-10-06", off["body"])
        self.store = base.FakeStore()
        on = self.filed(LINE, "Send the revised plan", None, week=OPT_IN,
                        due_dates=True)
        self.assertEqual(on["flags"]["--due"], "2026-10-06")
        self.assertIn("due 2026-10-06", on["body"])

    def test_no_phrase_but_model_due_is_sent_when_enabled(self):
        row = self.filed(PLAIN_LINE, "Send the revised plan", "2026-10-08",
                         week=OPT_IN, due_dates=True)
        self.assertEqual(row["flags"]["--due"], "2026-10-08")
        self.assertIn("due 2026-10-08", row["body"])

    def test_review_mode_candidate_gets_the_coded_due(self):
        routine = routine_with(week=OPT_IN)
        routine["memory"]["owner_todos"]["mode"] = "review"
        rows = []
        with mock.patch.object(memory_sink, "file_action_review",
                               side_effect=lambda *a, **k: rows.extend(
                                   k["candidates"]) or {}):
            self.capture(
                base.meeting_extraction(
                    [base.act("Send the revised plan", due="2026-10-02")]),
                item=base.meeting_item(LINE), routine=routine)
        self.assertEqual([c["due"] for c in rows], ["2026-10-06"])


def slack_item(date=MEETING_DATE):
    return {"id": "slack:C1-1@x", "source_id": "slack:C1-1",
            "source_kind": "slack", "title": "Thread", "date": date,
            "body": "Please send the deck", "frontmatter": {}}


def entry(**extra):
    data = {"worthy": True, "owner_attention": True, "type": "todo",
            "title": "Send the deck", "people": [], "tags": ["deck"],
            "follows": [], "body": "Send the deck early next week."}
    data.update(extra)
    return data


class EntryDueOverrideTest(base.CaptureHarness):
    def added(self, extraction, item=None, **routine_kw):
        self.capture(extraction, item=item or slack_item(),
                     routine=routine_with(**routine_kw))
        (row,) = self.store.entries.values()
        return row["flags"]

    def test_phrase_overrides_the_model_guess(self):
        flags = self.added(entry(due="2026-10-02"), due_dates=True)
        self.assertEqual(flags["--due"], "2026-10-06")

    def test_sun_thu_week(self):
        flags = self.added(entry(due="2026-10-02"), due_dates=True,
                           week={"start": "sunday", "days": 5})
        self.assertEqual(flags["--due"], "2026-10-05")

    def test_phrase_in_the_title(self):
        flags = self.added(
            entry(title="Send the deck by Tuesday", body="Deck.", due=None),
            due_dates=True)
        self.assertEqual(flags["--due"], "2026-10-06")

    def test_phrases_that_disagree_keep_the_model_due(self):
        flags = self.added(
            entry(title="Send the deck tomorrow", due="2026-10-09"),
            due_dates=True)
        self.assertEqual(flags["--due"], "2026-10-09")

    def test_a_timestamp_anchor_uses_the_configured_zone(self):
        routine = routine_with(due_dates=True)
        routine["timezone"] = "Asia/Jerusalem"
        item = slack_item("2026-11-24T23:30:00Z")  # already the 25th there
        self.capture(entry(body="Send it tomorrow.", due=None), item=item,
                     routine=routine)
        (row,) = self.store.entries.values()
        self.assertEqual(row["flags"]["--due"], "2026-11-26")

    def test_no_phrase_keeps_the_model_due(self):
        flags = self.added(entry(body="Send the deck.", due="2026-10-09"),
                           due_dates=True)
        self.assertEqual(flags["--due"], "2026-10-09")

    def test_invalid_model_due_is_dropped(self):
        for bad in ("soon", "2026-13-45", 5, "2026-10-9"):
            with self.subTest(bad=bad):
                self.store = base.FakeStore()
                flags = self.added(entry(body="Send the deck.", due=bad),
                                   due_dates=True)
                self.assertNotIn("--due", flags)

    def test_no_phrase_and_no_due_sends_nothing(self):
        flags = self.added(entry(body="Send the deck."), due_dates=True)
        self.assertNotIn("--due", flags)

    def test_off_by_default(self):
        flags = self.added(entry(due="2026-10-02"))
        self.assertNotIn("--due", flags)

    def test_only_open_item_types_carry_a_due(self):
        for etype in ("note", "decision", "event"):
            with self.subTest(etype=etype):
                self.store = base.FakeStore()
                flags = self.added(entry(type=etype, owner_attention=False),
                                   due_dates=True)
                self.assertNotIn("--due", flags)
        self.store = base.FakeStore()
        flags = self.added(entry(type="pending-decision"), due_dates=True)
        self.assertEqual(flags["--due"], "2026-10-06")

    def test_the_verbatim_source_never_supplies_the_phrase(self):
        item = slack_item()
        item["body"] = "Bob: please do it tomorrow"
        flags = self.added(entry(body="Send the deck.", due=None), item=item,
                           due_dates=True)
        self.assertNotIn("--due", flags)

    def test_anchor_is_the_item_date(self):
        flags = self.added(entry(body="Send it by Thursday."),
                           item=slack_item("2026-11-25"), due_dates=True)
        self.assertEqual(flags["--due"], "2026-11-26")


class ExtractSchemaTest(unittest.TestCase):
    def prompt(self, routine):
        seen = {}

        def fake_analyze(routine, prompt, **kwargs):
            seen["prompt"] = prompt
            return json.dumps(entry())

        with mock.patch.object(llm, "analyze", side_effect=fake_analyze), \
                mock.patch.object(memory_sink, "known_person_slugs",
                                  return_value=set()):
            memory_sink._extract(routine, slack_item(), "s", "/store")
        return seen["prompt"]

    def test_due_key_is_offered_only_when_enabled(self):
        self.assertNotIn('"due"', self.prompt(routine_with()))
        on = self.prompt(routine_with(due_dates=True))
        self.assertIn('"due": optional', on)

    def test_prompt_carries_the_week_convention(self):
        prompt = self.prompt(routine_with(week={"start": "sunday", "days": 5}))
        self.assertIn("Work week: Sunday-Thursday", prompt)

    def test_parse_accepts_an_optional_due(self):
        out = memory_sink._parse_extraction(json.dumps(entry(due="2026-10-09")))
        self.assertEqual(out["due"], "2026-10-09")


if __name__ == "__main__":
    unittest.main()
