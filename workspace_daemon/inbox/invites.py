"""Pending calendar invites from the calendar cache, read-only.

The `calendar_cache` routine writes `<base>/state/calendar-snapshot.json`
(`{"v": 1, "fetched_at", "window_hours", "events": {event id: row}}`, rows
with `start`, `end`, `status`, `my_rsvp`, `title`, `conference`). This module
turns the rows whose `my_rsvp` is `needsAction` into inbox items. It never
touches the network, never writes, and treats a missing, unreadable or
unfamiliar file as empty or as a source error, never as a crash.

Everything text-like in the cache (titles, organizers, links) was written by
other people: it goes through `items.make_item`, which cleans it.
"""
import datetime as dt
import json
import re
from pathlib import Path

from . import daemoncfg
from . import items as itm

SNAPSHOT_FILE = "calendar-snapshot.json"
SNAPSHOT_VERSION = 1
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_TWIN_BYTES = 4 * 1024 * 1024
MAX_INVITES = 200
STALE_AFTER_H = 6.0
DEFAULT_DAYS = 7.0
ORGANIZER_MAX = 80
# A recurring instance id ends in _YYYYMMDD[THHMMSS[Z]]; the daemon announces
# one invite per series, and so does the inbox. (Same pattern as
# calendar_cache._RECURRING_ID, copied so the inbox needs no daemon imports.)
_RECURRING_ID = re.compile(r"_\d{8}(?:T\d{6}Z?)?$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def series_of(event_id):
    return _RECURRING_ID.sub("", event_id)


def _read_json(path, limit):
    """`(document, error)`; `(None, None)` when the file does not exist."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot read {path.name}: {exc.strerror or exc}"
    if len(data) > limit:
        return None, f"{path.name} is larger than {limit} bytes"
    try:
        return json.loads(data.decode("utf-8", "replace")), None
    except ValueError:
        return None, f"{path.name} is not valid JSON"


def _when(value):
    """`(epoch, all_day)` of a snapshot start/end, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        if _DATE_ONLY.match(value):
            day = dt.date.fromisoformat(value)
            return dt.datetime.combine(day, dt.time.min).timestamp(), True
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.timestamp(), False


def _clock(ts, fmt):
    try:
        return dt.datetime.fromtimestamp(ts).strftime(fmt)
    except (ValueError, OverflowError, OSError):
        return "?"


def describe_when(start, end, all_day):
    """Start (and end) in the machine's local time zone."""
    if all_day:
        return _clock(start, "%a %d %b") + " (all day)"
    text = _clock(start, "%a %d %b %H:%M")
    if end is not None and end > start:
        same_day = _clock(start, "%Y%m%d") == _clock(end, "%Y%m%d")
        text += "-" + _clock(end, "%H:%M" if same_day else "%a %d %b %H:%M")
    return text


def _organizers(path):
    """`{event id: organizer name}` from the cache's JSON twin, best effort.

    The snapshot has no organizer; the twin (which covers the shorter output
    window) does. Anything unexpected yields fewer names, never an error.
    """
    if path is None:
        return {}
    doc, _err = _read_json(path, MAX_TWIN_BYTES)
    rows = doc.get("events") if isinstance(doc, dict) else None
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("event_id"), str):
            continue
        org = row.get("organizer")
        name = org.get("name") if isinstance(org, dict) else None
        name = itm.clean_line(name) if isinstance(name, str) else ""
        if name:
            out[row["event_id"]] = itm.truncate(name, ORGANIZER_MAX)
    return out


def _gist(title, when, organizer):
    tail = f" - {when}" + (f" - from {organizer}" if organizer else "")
    room = itm.GIST_MAX - len(tail)
    if room < 8:  # a very long organizer: keep the title readable
        tail = f" - {when}"
        room = itm.GIST_MAX - len(tail)
    return itm.truncate(title, max(room, 1)) + tail


def load(base_dir, now, days=DEFAULT_DAYS):
    """`(items, stale, age_min, error)`: the unanswered invites from the cache.

    An invite counts when its `my_rsvp` is `needsAction`, its status is not
    `cancelled`, it has not ended and it starts before `now + days`. A
    recurring series is listed once, at its next instance. Declined,
    accepted, tentative and organizer rows never show.
    """
    path = Path(base_dir) / "state" / SNAPSHOT_FILE
    doc, err = _read_json(path, MAX_SNAPSHOT_BYTES)
    if err:
        return [], False, None, err
    if doc is None:
        return [], False, None, None  # the calendar cache is not in use
    if not isinstance(doc, dict) or doc.get("v") != SNAPSHOT_VERSION \
            or isinstance(doc.get("v"), bool):
        return [], False, None, (
            f"{SNAPSHOT_FILE} has an unknown format (expected v "
            f"{SNAPSHOT_VERSION})")
    rows = doc.get("events")
    fetched = _when(doc.get("fetched_at"))
    if not isinstance(rows, dict) or fetched is None or fetched[1]:
        return [], False, None, f"{SNAPSHOT_FILE} is missing events or fetched_at"
    fetched_ts = fetched[0]
    age_min = max((now - fetched_ts) / 60.0, 0.0)
    stale = age_min > STALE_AFTER_H * 60
    limit = now + float(days) * 86400

    pending = {}  # series -> (start, event id, row, end, all_day)
    for event_id, row in rows.items():
        if (not isinstance(event_id, str) or not event_id
                or not isinstance(row, dict)
                or row.get("my_rsvp") != "needsAction"
                or row.get("status") == "cancelled"):
            continue
        start, end = _when(row.get("start")), _when(row.get("end"))
        if start is None:
            continue
        end_ts = end[0] if end else start[0]
        if end_ts <= now or start[0] >= limit:
            continue  # over already, or starts beyond the window
        key = series_of(event_id)
        prev = pending.get(key)
        if prev is None or start[0] < prev[0]:
            pending[key] = (start[0], event_id, row, end_ts, start[1])

    cfg = daemoncfg.calendar_cache(base_dir)
    names = _organizers(cfg["json"]) if pending else {}
    built, skipped = [], 0
    for key, (start_ts, event_id, row, end_ts, all_day) in sorted(
            pending.items(), key=lambda kv: kv[1][0])[:MAX_INVITES]:
        title = itm.clean_line(row.get("title")) if isinstance(
            row.get("title"), str) else ""
        title = title or "(untitled)"
        organizer = names.get(event_id) or ""
        when = describe_when(start_ts, end_ts, all_day)
        link = row.get("conference")
        link = link if itm.valid_link(link) else None
        thread = [{"who": "when", "ts": start_ts, "text": when}]
        if organizer:
            thread.append({"who": "from", "ts": start_ts, "text": organizer})
        try:
            built.append(itm.make_item(
                id=f"invites:{key}", source="calendar", section="needs_reply",
                urgency="digest", sender=organizer or "calendar invite",
                where="invite", gist=_gist(title, when, organizer),
                ts=fetched_ts,  # equal ts keeps the soonest-first order
                link=link, thread=thread, draft=None,
                actions=["ack", "open"] if link else ["ack"],
                meta={"kind": "invite", "event_id": event_id,
                      "calendar_id": cfg["calendar_id"], "series": key,
                      "my_rsvp": "needsAction", "start": start_ts,
                      "source_id": f"gcal:{event_id}"}))
        except itm.ItemError:
            skipped += 1
    notes = []
    window_h = doc.get("window_hours")
    if (isinstance(window_h, (int, float)) and not isinstance(window_h, bool)
            and float(days) * 24 > window_h):
        notes.append(f"the calendar cache only covers {window_h / 24:g} days")
    if skipped:
        notes.append(f"{skipped} invite(s) skipped")
    return built, stale, age_min, "; ".join(notes) or None
