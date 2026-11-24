"""Relative due phrases resolved by code, against a configurable work week.

Models mis-count "early next week" or "end of next week", and the work week
differs per team (Mon-Fri, or Sun-Thu with Fri-Sat off). `resolve()` is a pure
English-only parser: the memory capture path lets its date override the
model's guess whenever one of these phrases is in the source text.

    memory:
      work_week: {start: sunday, days: 5}     # default {start: monday, days: 5}
      # or a list of consecutive weekday names:
      # work_week: [sunday, monday, tuesday, wednesday, thursday]

Rules (WORKING day N of a week counts from the configured start):
  early next week      the 2nd working day of next week
  next week            the 1st working day of next week
  end of (the|this) week   the last working day of this week
  end of next week     the last working day of next week
  by|on <weekday>      the next such day (today is excluded)
  tomorrow             calendar tomorrow, or the next working day when that
                       is an off day
  today / EOD / end of (the) day   the anchor date itself
A week runs from the work-week start for seven days, so an off day belongs to
the week it follows. "End of this week" that already passed (asked on an off
day after the last working day) is unknown (None), never a past date. When
several phrases are present the earliest one in the text wins.
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


def convention(week):
    """One prompt line stating the week convention of `week`."""
    off = [DAY_NAMES[d].capitalize() for d in range(7)
           if not week.is_working(datetime.date(2024, 1, 1 + d))]
    first = DAY_NAMES[week.start].capitalize()
    return (f"Work week: {week.label()}"
            + (f" (off: {', '.join(off)})" if off else "")
            + f"; a week starts on {first}.")


_DAYS_RE = "|".join(DAY_NAMES)
_NOT_DAY_AFTER = (r"(?!\s*,?\s*(?:on\s+|by\s+)?(?:" + _DAYS_RE + r")\b)")
_PATTERNS = (
    ("early_next_week", r"early\s+next\s+week"),
    ("end_next_week", r"end\s+of\s+(?:the\s+)?next\s+week"),
    ("end_this_week", r"end\s+of\s+(?:the\s+|this\s+)?week"),
    ("next_week",
     r"(?<!late )(?<!later )(?<!mid )(?<!mid-)next\s+week" + _NOT_DAY_AFTER),
    ("weekday",
     r"(?:by|on)\s+(?:(?:this\s+)?coming\s+|this\s+)?(?P<day>"
     + _DAYS_RE + r")"),
    ("tomorrow", r"tomorrow"),
    ("today", r"today|eod|end\s+of\s+(?:the\s+)?day"),
)
_BOUND = r"(?<![\w-])(?:{})(?![\w-])"
_COMPILED = tuple(
    (name, re.compile(_BOUND.format(body), re.IGNORECASE))
    for name, body in _PATTERNS
)


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def find(text):
    """(phrase kind, matched text, weekday index or None) of the earliest
    due phrase in `text`, the longest on a tie; None when there is none."""
    best = None
    for name, pattern in _COMPILED:
        m = pattern.search(str(text or ""))
        if m and (best is None or (m.start(), -len(m.group(0)))
                  < (best[0], -len(best[2].group(0)))):
            best = (m.start(), name, m)
    if best is None:
        return None
    _start, name, m = best
    day = m.groupdict().get("day")
    return name, m.group(0), _DAY_ALIASES[day.lower()] if day else None


def resolve(text, anchor, week=None):
    """`YYYY-MM-DD` for the earliest relative due phrase in `text` as seen on
    `anchor` (a date or ISO string), or None (no phrase, unusable anchor,
    or a past "end of this week")."""
    day = _as_date(anchor)
    found = find(text)
    if day is None or found is None:
        return None
    week = week or DEFAULT_WEEK
    kind, _phrase, weekday = found
    this_week = week.week_start(day)
    next_week = this_week + datetime.timedelta(days=7)
    if kind == "early_next_week":
        out = week.working_day(next_week, 1)
    elif kind == "next_week":
        out = week.working_day(next_week, 0)
    elif kind == "end_next_week":
        out = week.working_day(next_week, week.days - 1)
    elif kind == "end_this_week":
        out = week.working_day(this_week, week.days - 1)
        if out < day:
            return None
    elif kind == "weekday":
        out = day + datetime.timedelta(
            days=(weekday - day.weekday()) % 7 or 7)
    elif kind == "tomorrow":
        out = day + datetime.timedelta(days=1)
        while not week.is_working(out):
            out += datetime.timedelta(days=1)
    else:
        out = day
    return out.isoformat()


def resolve_first(texts, anchor, week=None):
    """The first date `resolve` finds across `texts` in order (the owner's
    own line before the item text), else None."""
    for text in texts:
        if text:
            out = resolve(text, anchor, week)
            if out:
                return out
    return None
