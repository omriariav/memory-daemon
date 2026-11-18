"""Task rows (0.12.0): what a command source's `meta.kind: "task"` items are.

No Textual import. A command source may list open tasks next to its other
items. A task item is an ordinary v1 item (a `source`, a `section`, a `gist`
that holds the title) whose `meta` says what it is:

    kind        "task"
    task_type   "todo" | "decision"        (anything else reads as todo)
    due         "YYYY-MM-DD" | null
    date        "YYYY-MM-DD", when the task was created
    people      [names]
    evidence    str | null
    source_ids  [str]

Task items never show in the Feed, Waiting or the Slack-style sections of the
Inbox; they have their own view (3 Tasks) and, when overdue or due today, the
DUE TODAY section of the Inbox. Everything here is a pure function of an item
and "today" (a `datetime.date`, injected so the sections are testable across
midnight). Text from `meta` is untrusted display text and is cleaned here.
"""
import datetime as dt
import re

from . import items as itm

KIND = "task"
TODO, DECISION = "todo", "decision"
GLYPHS = {TODO: "☐", DECISION: "◇"}
WEEK_DAYS = 7           # THIS WEEK: due within the next 7 days
PEOPLE_MAX = 20
NAME_MAX = 60
SOURCE_IDS_MAX = 20
SOURCE_ID_MAX = 120
EVIDENCE_MAX = 2000

# The Tasks view's sections, in order: (key, title).
OVERDUE, TODAY, WEEK, LATER, DECIDE, NODATE = (
    "overdue", "today", "week", "later", "decide", "nodate")
BUCKETS = ((OVERDUE, "OVERDUE"), (TODAY, "DUE TODAY"), (WEEK, "THIS WEEK"),
           (LATER, "LATER"), (DECIDE, "DECIDE"), (NODATE, "NO DATE"))

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def is_task(item):
    meta = getattr(item, "meta", None)
    return isinstance(meta, dict) and meta.get("kind") == KIND


def parse_day(value):
    """A `datetime.date` for a strict `YYYY-MM-DD` string, else None."""
    if not isinstance(value, str) or not _DAY.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def task_type(item):
    kind = item.meta.get("task_type") if isinstance(item.meta, dict) else None
    return DECISION if kind == DECISION else TODO


def due_of(item):
    """The task's due date (a `date` or None). A due date set here and not yet
    in a list refresh (`Item.local_due`) wins over the one the source sent."""
    local = getattr(item, "local_due", None)
    if local is not None:
        return parse_day(local[0])
    return parse_day(item.meta.get("due") if isinstance(item.meta, dict)
                     else None)


def created_of(item):
    """The creation date: `meta.date`, else the day of the item's time."""
    day = parse_day(item.meta.get("date")) if isinstance(item.meta, dict) \
        else None
    if day is not None:
        return day
    try:
        return dt.date.fromtimestamp(float(item.ts))
    except (OverflowError, OSError, ValueError):
        return None


def people_of(item):
    raw = item.meta.get("people") if isinstance(item.meta, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for name in raw[:PEOPLE_MAX]:
        if isinstance(name, str):
            name = itm.truncate(itm.clean_line(name), NAME_MAX)
            if name:
                out.append(name)
    return out


def evidence_of(item):
    raw = item.meta.get("evidence") if isinstance(item.meta, dict) else None
    if not isinstance(raw, str):
        return ""
    return itm.truncate(itm.clean_text(raw).strip(), EVIDENCE_MAX)


def source_ids_of(item):
    raw = item.meta.get("source_ids") if isinstance(item.meta, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for sid in raw[:SOURCE_IDS_MAX]:
        if isinstance(sid, str):
            sid = itm.truncate(itm.clean_line(sid), SOURCE_ID_MAX)
            if sid:
                out.append(sid)
    return out


def is_due_by(item, today):
    """True when the task is overdue or due today (a todo or a decision)."""
    due = due_of(item)
    return due is not None and due <= today


def due_count(items, today):
    """How many tasks are overdue or due today: the number on the tab."""
    return sum(1 for i in items if is_task(i) and is_due_by(i, today))


def bucket(item, today):
    """The Tasks view section of a task. A decision is always DECIDE; a todo
    is OVERDUE, DUE TODAY, THIS WEEK or LATER by its due date, NO DATE
    without one."""
    if task_type(item) == DECISION:
        return DECIDE
    due = due_of(item)
    if due is None:
        return NODATE
    if due < today:
        return OVERDUE
    if due == today:
        return TODAY
    if (due - today).days <= WEEK_DAYS:
        return WEEK
    return LATER


def sort_key(item):
    """Due ascending (no date last), then created ascending, then id."""
    due, created = due_of(item), created_of(item)
    return (due is None, due or dt.date.max, created or dt.date.max, item.key)


def due_cell(due, today):
    """`today`, `2d late`, `Thu`, `21 Nov` or `-` for the row's due cell. A
    weekday name only for the next six days (a name seven days out would read
    as today)."""
    if due is None:
        return "-"
    days = (due - today).days
    if days == 0:
        return "today"
    if days < 0:
        return f"{-days}d late"
    if days < WEEK_DAYS:
        return due.strftime("%a")
    return f"{due.day} {due.strftime('%b')}"


def due_words(due, today):
    """The detail pane's due line: the date with how far away it is."""
    if due is None:
        return "none"
    days = (due - today).days
    when = ("today" if days == 0 else f"{-days}d late" if days < 0
            else f"in {days}d")
    return f"{due.isoformat()} ({when})"


def age_label(created, today):
    """Whole days since the task was created: `0d`, `3d`; `-` when unknown."""
    if created is None:
        return "-"
    return f"{max(0, (today - created).days)}d"


def first_name(name):
    word = name.split(" ")[0] if name else ""
    return word.strip("\"'.,;:!?()[]{}<>*_~`|/\\") or name


def people_compact(names):
    """Up to two first names, then `+n` for the rest: `Ann, Bo +1`."""
    firsts = [first_name(n) for n in names if n]
    if not firsts:
        return ""
    shown = ", ".join(firsts[:2])
    return shown + (f" +{len(firsts) - 2}" if len(firsts) > 2 else "")


# --- the day pickers ---------------------------------------------------------

def next_sunday(today):
    """The first Sunday after `today` (a week out when today is Sunday)."""
    return today + dt.timedelta(days=7 - ((today.weekday() + 1) % 7))


def due_choices(today):
    """The `s` picker: `[(id, label, date or None)]`; `pick` has no date yet."""
    tomorrow = today + dt.timedelta(days=1)
    sunday = next_sunday(today)
    week = today + dt.timedelta(days=7)

    def fmt(day):
        return f"{day.strftime('%a')} {day.day} {day.strftime('%b')}"
    return [("tomorrow", f"Tomorrow  {fmt(tomorrow)}", tomorrow),
            ("sunday", f"Next Sunday  {fmt(sunday)}", sunday),
            ("week", f"+1 week  {fmt(week)}", week),
            ("pick", "Pick date…  YYYY-MM-DD", None),
            ("none", "No date", None)]
