"""Relative due phrases resolved by code, against a configurable work week.

Models mis-count "early next week" or "end of next week", and the work week
differs per team (Mon-Fri, or Sun-Thu with Fri-Sat off). `resolve()` is a pure
English-only parser: when `memory.work_week` is set, the memory capture path
lets its date override the model's guess whenever one of these phrases is in
the source text.

    memory:
      work_week: {start: sunday, days: 5}     # default {start: monday, days: 5}
      # or a list of consecutive weekday names:
      # work_week: [sunday, monday, tuesday, wednesday, thursday]

Rules (WORKING day N of a week counts from the configured start):
  early next week          the 2nd working day of next week
  next week                the 1st working day of next week
  end of (the|this) week   the last working day of this week
  end of next week         the last working day of next week
  by|until|due <weekday>   the next such day (today excluded); a bare
                           "on <weekday>" is not a deadline and never matches
  <weekday> next week      that day of next week ("on Thursday next week",
                           "next week, Tuesday")
  by <weekday> EOD         the next such day (today excluded)
  end of day <weekday>, <weekday> EOD   that weekday, today included
  tomorrow                 calendar tomorrow, or the next working day when
                           that is an off day
  EOD / end of (the) day / by|until|due today   the anchor date itself
A week runs from the work-week start for seven days, so an off day belongs to
the week it follows. "End of this week" that already passed (asked on an off
day after the last working day) is unknown (None), never a past date.

The code stays out of ambiguous text and returns None, so the model's date is
kept, when: phrases in the text resolve to different dates; the text also has
an explicit date (2026-10-07, Oct 7, 7/10, "the 7th"); or a phrase follows a
negation ("not", "don't", "no", "never", "rather than", "instead of"). A
possessive ("today's") never matches.
"""
import dataclasses
import datetime
import re

DAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday")
_DAY_ALIASES = {name: i for i, name in enumerate(DAY_NAMES)}
_DAY_ALIASES.update({name[:3]: i for i, name in enumerate(DAY_NAMES)})


@dataclasses.dataclass(frozen=True)
class WorkWeek:
    """`days` consecutive working days starting on weekday `start` (Mon=0)."""
    start: int = 0
    days: int = 5

    def is_working(self, day):
        return (day.weekday() - self.start) % 7 < self.days

    def week_start(self, day):
        return day - datetime.timedelta(days=(day.weekday() - self.start) % 7)

    def working_day(self, week_start, index):
        """The `index`th (0 = first) working day of the week that starts on
        `week_start`; clamped to the last one."""
        index = min(index, self.days - 1)
        return week_start + datetime.timedelta(days=index)

    def label(self):
        names = [DAY_NAMES[(self.start + n) % 7].capitalize()
                 for n in range(self.days)]
        return names[0] if len(names) == 1 else f"{names[0]}-{names[-1]}"


DEFAULT_WEEK = WorkWeek()


def parse_week(value):
    """A WorkWeek from a config value, or ValueError naming the problem.

    `None` is the default Mon-Fri. A mapping is `{start: <weekday>, days: 1-7}`;
    a list is consecutive weekday names (any order, e.g. Sun-Thu)."""
    if value is None:
        return DEFAULT_WEEK
    if isinstance(value, dict):
        extra = set(value) - {"start", "days"}
        if extra:
            raise ValueError(
                f"unknown key(s) {', '.join(sorted(map(str, extra)))} "
                "(valid: start, days)")
        start = _day_index(value.get("start", "monday"))
        days = value.get("days", 5)
        if type(days) is not int or not 1 <= days <= 7:
            raise ValueError("days must be a whole number from 1 to 7")
        return WorkWeek(start, days)
    if isinstance(value, list) and value:
        picked = {_day_index(v) for v in value}
        if len(picked) != len(value):
            raise ValueError("a weekday is listed twice")
        if len(picked) == 7:
            return WorkWeek(0, 7)
        starts = [d for d in picked if (d - 1) % 7 not in picked]
        if len(starts) != 1:
            raise ValueError("working days must be consecutive weekdays")
        return WorkWeek(starts[0], len(picked))
    raise ValueError("must be {start: <weekday>, days: <1-7>} or a list of "
                     "consecutive weekday names")


def _day_index(value):
    name = value.strip().lower() if isinstance(value, str) else None
    if name not in _DAY_ALIASES:
        raise ValueError(f"{value!r} is not a weekday name")
    return _DAY_ALIASES[name]


def validate(cfg, rid):
    """Config problems for `memory.work_week`; an empty list means valid."""
    if not isinstance(cfg, dict) or "work_week" not in cfg:
        return []
    try:
        parse_week(cfg["work_week"])
    except ValueError as exc:
        return [f"{rid}: memory.work_week {exc}"]
    return []


def week_from_routine(routine):
    """The routine's configured work week; the default when absent or invalid
    (validation reports a bad value at load time)."""
    memory = (routine or {}).get("memory")
    try:
        return parse_week(memory.get("work_week")
                          if isinstance(memory, dict) else None)
    except ValueError:
        return DEFAULT_WEEK


def configured(routine):
    """Whether the routine opted in with an explicit `memory.work_week`. The
    one switch for the code override of owner-todo due dates: a routine
    without it behaves exactly as before."""
    memory = (routine or {}).get("memory")
    return isinstance(memory, dict) and memory.get("work_week") is not None


def prompt_block(routine):
    """Whether the date reference states the week convention: a configured
    `memory.work_week` or `memory.due_dates: true`."""
    memory = (routine or {}).get("memory")
    return configured(routine) or (
        isinstance(memory, dict) and memory.get("due_dates") is True)


def convention(week):
    """One prompt line stating the week convention of `week`."""
    off = [DAY_NAMES[d].capitalize() for d in range(7)
           if not week.is_working(datetime.date(2024, 1, 1 + d))]
    first = DAY_NAMES[week.start].capitalize()
    return (f"Work week: {week.label()}"
            + (f" (off: {', '.join(off)})" if off else "")
            + f"; a week starts on {first}.")


_DAYS_RE = "|".join(DAY_NAMES)
_CUE = r"(?:by|until|till|due(?:\s+(?:by|on))?)"
_EOD = r"(?:eod|end\s+of\s+(?:the\s+)?day)"
_PATTERNS = (
    ("early_next_week", r"early\s+next\s+week"),
    ("end_next_week", r"end\s+of\s+(?:the\s+)?next\s+week"),
    ("end_this_week", r"end\s+of\s+(?:the\s+|this\s+)?week"),
    ("day_next_week",
     r"(?:(?:" + _CUE + r"|on)\s+)?(?P<day>" + _DAYS_RE + r")\s+next\s+week"),
    ("day_next_week",
     r"next\s+week,?\s+(?:(?:on|by)\s+)?(?P<day>" + _DAYS_RE + r")"),
    ("next_week", r"(?<!late )(?<!later )(?<!mid )(?<!mid-)next\s+week"),
    ("weekday",
     _CUE + r"\s+(?:(?:this\s+)?coming\s+|this\s+)?(?P<day>" + _DAYS_RE
     + r")(?:\s+" + _EOD + ")?"),
    ("eod_day", _EOD + r"\s+(?:on\s+)?(?P<day>" + _DAYS_RE + ")"),
    ("eod_day", r"(?P<day>" + _DAYS_RE + r")\s+" + _EOD),
    ("tomorrow", r"tomorrow"),
    ("today", _CUE + r"\s+today|" + _EOD),
)
_BOUND = r"(?<![\w-])(?:{})(?![\w-])(?![\'\u2019]s\b)"
_COMPILED = tuple(
    (name, re.compile(_BOUND.format(body), re.IGNORECASE))
    for name, body in _PATTERNS
)
_MONTH = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?"
          r"|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?"
          r"|dec(?:ember)?)")
_EXPLICIT_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b"
    r"|\b" + _MONTH + r"\.?\s+\d{1,2}\b"
    r"|\b\d{1,2}\s+(?:of\s+)?" + _MONTH + r"\b",
    re.IGNORECASE,
)
_NEGATIONS = {"not", "no", "never", "cannot", "rather", "instead"}
_WORD = re.compile(r"[\w'\u2019]+")


def _negated(text, start):
    """Whether one of the three words before `start`, in the same clause,
    negates the phrase."""
    clause = re.split(r"[,.;:!?()\n]", text[:start])[-1]
    for word in _WORD.findall(clause)[-3:]:
        word = word.lower()
        if word in _NEGATIONS or word.endswith(("n't", "n\u2019t")):
            return True
    return False


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _hits(text):
    """The phrases in `text`: `(kind, weekday index or None)` each, with an
    overlapping shorter one dropped for the longer ("end of next week" is
    not also "next week") and negated ones skipped."""
    found = []
    for name, pattern in _COMPILED:
        for m in pattern.finditer(text):
            found.append((m.start(), -(m.end() - m.start()), m.end(), name, m))
    found.sort(key=lambda row: row[:2])
    kept, last_end = [], -1
    for start, _neg, end, name, m in found:
        if start < last_end:
            continue
        last_end = end
        if _negated(text, start):
            continue
        day = m.groupdict().get("day")
        kept.append((name, _DAY_ALIASES[day.lower()] if day else None))
    return kept


def _date_for(kind, weekday, day, week):
    this_week = week.week_start(day)
    next_week = this_week + datetime.timedelta(days=7)
    if kind == "early_next_week":
        return week.working_day(next_week, 1)
    if kind == "next_week":
        return week.working_day(next_week, 0)
    if kind == "end_next_week":
        return week.working_day(next_week, week.days - 1)
    if kind == "end_this_week":
        out = week.working_day(this_week, week.days - 1)
        return out if out >= day else None
    if kind == "day_next_week":
        return next_week + datetime.timedelta(
            days=(weekday - next_week.weekday()) % 7)
    if kind == "weekday":
        return day + datetime.timedelta(days=(weekday - day.weekday()) % 7 or 7)
    if kind == "eod_day":
        return day + datetime.timedelta(days=(weekday - day.weekday()) % 7)
    if kind == "tomorrow":
        out = day + datetime.timedelta(days=1)
        while not week.is_working(out):
            out += datetime.timedelta(days=1)
        return out
    return day


def analyse(text, anchor, week=None):
    """`(phrase count, date)` for `text` as seen on `anchor` (a date or ISO
    string). The count is the number of due phrases found (0: the text has
    none). The date is `YYYY-MM-DD` only when every phrase resolves to the
    same day and the text has no explicit date; otherwise None."""
    text = str(text or "")
    hits = _hits(text)
    if not hits:
        return 0, None
    day = _as_date(anchor)
    if day is None or _EXPLICIT_DATE.search(text):
        return len(hits), None
    week = week or DEFAULT_WEEK
    dates = {_date_for(kind, wd, day, week) for kind, wd in hits}
    if len(dates) != 1 or None in dates:
        return len(hits), None
    return len(hits), dates.pop().isoformat()


def resolve(text, anchor, week=None):
    """`YYYY-MM-DD` for the relative due phrase(s) in `text` as seen on
    `anchor`, or None: no phrase, an unusable anchor, phrases that disagree,
    an explicit date in the text, a negated phrase, or a past "end of this
    week"."""
    return analyse(text, anchor, week)[1]


