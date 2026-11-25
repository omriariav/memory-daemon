"""The inbox views: filters and groupings of the same items (no Textual).

A view is not a copy of the data. Each function here takes the live
`models.Item` objects (marks and snoozes already applied) and returns
`Group`s of those same objects, so selection, marks and the 5 s undo work the
same in every view. Only two kinds of row are made here: a digest row (many
rows of one routine and day folded into one) and a saved-to-memory row.

1 Inbox    URGENT, I OWE (needs_reply, invites included), NUDGE DUE,
           DUE TODAY (the overdue and due-today tasks) and REVIEW (captures
           the store refused as near-duplicates; listed only when present)
2 Waiting  waiting rows by person, then bookmarks as TRACKING
3 Tasks    the task rows (tasks.py) by due date: OVERDUE, DUE TODAY, THIS
           WEEK, LATER, DECIDE, NO DATE
4 Feed     every fyi row by routine; saved-to-memory is one more routine
5 Health   not a list of items (see health.py)
6 Agents   every `meta.kind == "agent"` row, grouped NEEDS YOU / WORKING /
           DONE (`meta.bucket`, else the row's section)

A task row (`meta.kind == "task"`) never shows in Waiting, the Feed or the
Inbox's URGENT, I OWE and NUDGE DUE. A review row (a refused near-duplicate
capture) shows only in REVIEW. An agent row that is not waiting for you (a
FYI row) shows only in Agents: not in the Inbox and not in the Feed.
"""
import datetime
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List

from . import tasks
from .models import Item, Msg, fmt_age

VIEWS = (("inbox", "Inbox"), ("waiting", "Waiting"), ("tasks", "Tasks"),
         ("feed", "Feed"), ("health", "Health"), ("agents", "Agents"))
VIEW_KEYS = tuple(k for k, _ in VIEWS)
VIEW_TITLES = dict(VIEWS)
# The views that list items in the queue.
QUEUE_VIEWS = ("inbox", "waiting", "tasks", "feed", "agents")
SAVED_ROUTINE = "saved to memory"
DEFAULT_WINDOW_H = 24.0
DIGEST_PREFIX = "digest:"
SAVED_PREFIX = "saved:"
# The origin (source) names whose rows are time-based events: only these age
# out of the Feed. A command source lists state (an open item shown as fyi),
# which stays for as long as the source lists it.
EVENT_ORIGINS = ("events",)


@dataclass
class Group:
    """A titled run of rows in the queue (a section, a person, a routine)."""
    key: str
    title: str
    items: List[Item] = field(default_factory=list)
    tail: str = ""             # text after the title; default is the count
    hot: bool = False          # drawn in the urgent colour
    keep_empty: bool = False   # listed with a dash even when empty


def view_rows(groups):
    """The rows of `groups`, in order."""
    return [i for g in groups for i in g.items]


# --- predicates -------------------------------------------------------------

def is_bookmark(item):
    """A waiting row the owner bookmarked: `meta.via` starts with
    `track_reaction`, or `meta.cls` is `bookmarked`."""
    meta = item.meta if isinstance(item.meta, dict) else {}
    via = meta.get("via")
    return ((isinstance(via, str) and via.startswith("track_reaction"))
            or meta.get("cls") == "bookmarked")


def nudge_due(item):
    """A waiting row whose nudge is due: it can be nudged and a nudge draft
    is stored."""
    return (item.section == "waiting" and "nudge" in item.actions
            and bool(item.draft and item.draft.strip()))


def is_review(item):
    """A `duplicate` capture event: the store refused a capture as a
    near-duplicate (events source, `meta.kind == "duplicate"`)."""
    return item.source == "memory" and (
        isinstance(item.meta, dict) and item.meta.get("kind") == "duplicate")


def is_digest(item):
    return item.key.startswith(DIGEST_PREFIX)


def is_saved_row(item):
    return item.key.startswith(SAVED_PREFIX)


def is_synthetic(item):
    """A row the views make (a digest or a saved-to-memory row), not a source
    item: only a few keys act on it."""
    return is_digest(item) or is_saved_row(item)


AGENT_KIND = "agent"
AGENT_BUCKETS = (("needs_you", "NEEDS YOU"), ("working", "WORKING"),
                 ("done", "DONE"))


def is_agent(item):
    """A row of an agent source (`meta.kind == "agent"`): a session or task
    some tool is running for you."""
    meta = item.meta if isinstance(item.meta, dict) else {}
    return meta.get("kind") == AGENT_KIND


def is_working_agent(item):
    """An agent row that does not wait for you: a FYI row. It lives in the
    Agents view only."""
    return is_agent(item) and item.section == "fyi"


def agent_bucket(item):
    """`needs_you`, `working` or `done`: `meta.bucket` when it names one, else
    from the section (urgent and needs_reply wait for you, the rest work)."""
    bucket = item.meta.get("bucket") if isinstance(item.meta, dict) else None
    if bucket in dict(AGENT_BUCKETS):
        return bucket
    return "needs_you" if item.section in ("urgent", "needs_reply") \
        else "working"


def is_active_alert(item):
    """A daemon alert whose condition has not ended. It is always listed
    (docs/inbox.md, "Active keys are always listed"): no Feed window and no
    `hide` or `digest` rule takes it out of view."""
    return item.source == "alert" and not item.resolved


def _soonest_first(item):
    """Waiting rows and pending invites (whose ts is the event start) list the
    oldest / soonest first; everything else newest first."""
    return item.ts if (item.section == "waiting"
                       or item.meta.get("kind") == "invite") else -item.ts


# --- 1 Inbox ----------------------------------------------------------------

def inbox_groups(items, today=None):
    """URGENT, I OWE and NUDGE DUE (always listed, with a dash when empty),
    then DUE TODAY (listed only when it has rows): the overdue and due-today
    tasks, todos and decisions, by due date. A Feed row that a rule promotes
    (`feed == "inbox"`) joins URGENT. Task rows are in none of the first
    three. `today` is a `datetime.date` (default: the local date)."""
    today = today or datetime.date.today()
    rows = [i for i in items if not tasks.is_task(i) and not is_review(i)
            and not is_working_agent(i)]
    urgent = [i for i in rows
              if i.section == "urgent"
              or (i.section == "fyi" and i.feed == "inbox")]
    owe = [i for i in rows if i.section == "needs_reply"]
    nudge = [i for i in rows if nudge_due(i)]
    for group in (urgent, owe, nudge):
        group.sort(key=_soonest_first)
    due = sorted((i for i in items
                  if tasks.is_task(i) and tasks.is_due_by(i, today)),
                 key=tasks.sort_key)
    overdue = any(tasks.due_of(i) < today for i in due)
    review = sorted((i for i in items if is_review(i)), key=lambda i: -i.ts)
    groups = [Group("urgent", "URGENT", urgent, hot=True, keep_empty=True),
              Group("owe", "I OWE", owe, keep_empty=True),
              Group("nudge", "NUDGE DUE", nudge, keep_empty=True),
              Group("due_today", "DUE TODAY", due, hot=overdue)]
    if review:
        groups.append(Group("review", "REVIEW", review))
    return groups


# --- 6 Agents ---------------------------------------------------------------

def agent_groups(items):
    """The Agents view: NEEDS YOU, WORKING and DONE (empty groups left out),
    each newest row first. The rows are the same `Item` objects the other
    views list. A row a Feed rule mutes (`feed == "hide"`) stays hidden here
    too."""
    buckets = OrderedDict((key, []) for key, _title in AGENT_BUCKETS)
    for it in items:
        if is_agent(it) and it.feed != "hide":
            buckets[agent_bucket(it)].append(it)
    titles = dict(AGENT_BUCKETS)
    return [Group(key, titles[key], sorted(rows, key=lambda i: (-i.ts, i.key)),
                  hot=key == "needs_you")
            for key, rows in buckets.items() if rows]


# --- 3 Tasks ----------------------------------------------------------------

def task_groups(items, today, week_start=None):
    """The Tasks view: OVERDUE, DUE TODAY, THIS WEEK, LATER, DECIDE and NO
    DATE, in that order, empty sections left out. Inside a section the rows
    go by due date, then creation date (tasks.sort_key). `today` is a
    `datetime.date`; `week_start` is the optional weekday name that ends THIS
    WEEK (tasks.bucket)."""
    buckets = OrderedDict((key, []) for key, _title in tasks.BUCKETS)
    for it in items:
        if tasks.is_task(it):
            buckets[tasks.bucket(it, today, week_start)].append(it)
    titles = dict(tasks.BUCKETS)
    return [Group(key, titles[key], sorted(rows, key=tasks.sort_key),
                  hot=key == tasks.OVERDUE)
            for key, rows in buckets.items() if rows]


# --- 2 Waiting --------------------------------------------------------------

def person_key(item):
    return (item.who or "").strip().casefold() or "?"


def waiting_groups(items, now):
    """Waiting rows grouped by person (the sender / awaited name), the person
    with the oldest open ask first and each person's oldest ask first; the
    header reads `NAME  n open · oldest 3d`. Bookmarked rows close the list as
    TRACKING."""
    people = OrderedDict()
    tracking = []
    for it in sorted((i for i in items if i.section == "waiting"
                      and not tasks.is_task(i)), key=lambda i: i.ts):
        if is_bookmark(it):
            tracking.append(it)
        else:
            people.setdefault(person_key(it), []).append(it)
    groups = []
    for key, rows in people.items():
        oldest = fmt_age(now - rows[0].ts)
        groups.append(Group(f"person:{key}", rows[-1].who or "(unknown)", rows,
                            tail=f"{len(rows)} open · oldest {oldest}"))
    if tracking:
        groups.append(Group("tracking", "TRACKING", tracking))
    return groups


# --- 4 Feed -----------------------------------------------------------------

def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def digest_row(routine, day, members):
    """One row standing for `members` (rows of one routine and day): marking
    it done or snoozing it marks every member. It has a link when its members
    share exactly one."""
    rows = sorted(members, key=lambda i: i.ts)
    newest = rows[-1]
    n = len(rows)
    links = {m.link for m in rows if m.link}
    return Item(
        key=f"{DIGEST_PREFIX}{routine}:{day}", section="fyi", who=routine,
        where="digest", ts=newest.ts, gist=f"{n} changes · {newest.gist}"[:120],
        source=newest.source, origin=newest.origin,
        link=links.pop() if len(links) == 1 else None,
        messages=[Msg(time.strftime("%H:%M", time.localtime(m.ts)), m.ts,
                      m.gist) for m in rows],
        actions=[], meta={"routine": routine, "digest": True, "count": n},
        members=rows)


def digest_subset(row, keep):
    """A digest row rebuilt from the members for which `keep(member)` holds
    (None when none does); any other row is returned as it is. With a filter
    on, a mark made on the digest row must reach only the rows the filter
    shows."""
    if not is_digest(row):
        return row
    rows = [m for m in row.members if keep(m)]
    if not rows:
        return None
    if len(rows) == len(row.members):
        return row
    return digest_row(row.meta.get("routine") or "", row.key.rsplit(":", 1)[1],
                      rows)


def saved_item(row):
    """An `Item` for a saved-to-memory row of `sources.load_saved`."""
    return Item(
        key=f"{SAVED_PREFIX}{row['id']}", section="fyi", who=row["title"],
        where=SAVED_ROUTINE, ts=row["ts"], gist=row["title"],
        source=row["source"], link=row.get("link"), saved=True,
        actions=["open"] if row.get("link") else [],
        meta={"routine": SAVED_ROUTINE, "source_id": row.get("source_id", ""),
              "kind": row.get("kind", ""), "saved_row": True},
        origin=SAVED_ROUTINE)


@dataclass
class Feed:
    groups: list                # every routine's group, before the filter
    counts: list                # [(routine, rows)] for the routine list
    muted: list                 # [(label, rows)] rows a rule hides
    total: int = 0              # rows in the Feed (members, not digests)


def feed_view(items, now, window_h=DEFAULT_WINDOW_H, show_all=False,
              saved=(), windowed=EVENT_ORIGINS):
    """The Feed: every fyi row, by routine. Rows older than `window_h` age out
    (all of them show again with `show_all`). A row a rule hides is counted as
    muted, one a rule digests is folded into one row per routine and day, one
    a rule promotes (`inbox`) is left to the Inbox view. `saved` is a list of
    saved-to-memory `Item`s: they form the routine `saved to memory`, with no
    window of their own here (the saved view's own `saved_days` bounds them).
    The window applies to rows of the `windowed` origins only (the events
    source); an active alert is exempt from it and from `hide` and `digest`."""
    cutoff = now - float(window_h) * 3600
    by_routine = OrderedDict()
    muted = OrderedDict()
    for it in sorted((i for i in items if i.section == "fyi"
                      and not tasks.is_task(i) and not is_review(i)
                      and not is_agent(i)),
                     key=lambda i: -i.ts):
        if it.feed == "inbox":
            continue
        pinned = is_active_alert(it)
        if (not pinned and not show_all and it.origin in windowed
                and it.ts < cutoff):
            continue
        if pinned and it.feed in ("hide", "digest"):
            it.feed = "show"
        if it.feed == "hide":
            muted[it.feed_label or it.routine] = \
                muted.get(it.feed_label or it.routine, 0) + 1
            continue
        by_routine.setdefault(it.routine, []).append(it)
    for it in sorted(saved, key=lambda i: -i.ts):
        by_routine.setdefault(SAVED_ROUTINE, []).append(it)
    groups, counts = [], []
    ordered = sorted(by_routine.items(),
                     key=lambda kv: (kv[0] == SAVED_ROUTINE, -kv[1][0].ts))
    for routine, rows in ordered:
        rows = list(rows)
        folds = OrderedDict()
        for it in rows:
            if it.feed == "digest":
                folds.setdefault(_day(it.ts), []).append(it)
        shown, done = [], set()
        for it in rows:
            if it.feed != "digest":
                shown.append(it)
                continue
            day = _day(it.ts)
            if day not in done:
                done.add(day)
                shown.append(digest_row(routine, day, folds[day]))
        tail = str(len(rows))
        if folds:
            changes = sum(len(v) for v in folds.values())
            tail += f" · {changes} changes → {len(folds)} row" \
                    + ("" if len(folds) == 1 else "s")
        groups.append(Group(f"routine:{routine}", routine.upper(), shown,
                            tail=tail))
        counts.append((routine, len(rows)))
    return Feed(groups, counts, list(muted.items()),
                sum(n for _, n in counts))


def only_routine(groups, routine):
    """The groups of one routine (all of them when `routine` is None)."""
    if routine is None:
        return list(groups)
    return [g for g in groups if g.key == f"routine:{routine}"]


def group_of(groups, item):
    """The group holding `item`, or None."""
    for g in groups:
        if any(i is item or i.key == item.key for i in g.items):
            return g
    return None
