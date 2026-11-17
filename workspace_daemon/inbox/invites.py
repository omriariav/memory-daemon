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
NAME_MAX = 60             # one attendee's name in the attendees line
ATTENDEES_SHOWN = 8       # names in the attendees thread line
ATTENDEES_META_MAX = 25   # names in meta.attendees
LOCATION_MAX = 120
SUBJECT_MAX = 500
CONFLICTS_SHOWN = 5
CONFLICT_TITLE_MAX = 60
# `@` as typed, full-width and small (sources._AT_SIGNS, copied: that module
# imports this one). A name holding one is an address, not a name.
_AT_SIGNS = ("@", "\uff20", "\ufe6b")
# What a conflict line calls a row, by my_rsvp.
_CONFLICT_LABEL = {"accepted": "accepted", "tentative": "tentative",
                   "needsAction": "pending"}
# A recurring instance id ends in _YYYYMMDD[THHMMSS[Z]]; the daemon announces
# one invite per series, and so does the inbox. (Same pattern as
# calendar_cache._RECURRING_ID, copied so the inbox needs no daemon imports.)
_RECURRING_ID = re.compile(r"_\d{8}(?:T\d{6}Z?)?$")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def series_of(event_id):
    return _RECURRING_ID.sub("", event_id)


# What an id may contain before it is allowed into an RSVP call: Google event
# ids are base32hex plus a `_<timestamp>` suffix, calendar ids are `primary`
# or an address. Anything else is refused rather than passed on.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_@.+=-]{1,200}$")


def rsvp_target(item):
    """`(event_id, calendar_id, recurring)` for a pending invite the inbox
    built itself, else None. A command source's item never qualifies, whatever
    its `meta` says: only the `invites:` ids of this module do."""
    meta = getattr(item, "meta", None)
    if (not str(getattr(item, "key", "")).startswith("invites:")
            or not isinstance(meta, dict) or meta.get("kind") != "invite"):
        return None
    event_id, calendar_id = meta.get("event_id"), meta.get("calendar_id")
    if not (isinstance(event_id, str) and isinstance(calendar_id, str)
            and _SAFE_ID.match(event_id) and _SAFE_ID.match(calendar_id)):
        return None
    return event_id, calendar_id, series_of(event_id) != event_id


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


def _name(value, limit=ORGANIZER_MAX):
    """A person's display name, or "": cleaned, cut, and dropped when it holds
    an at-sign (a calendar may show the address as the name). Never an
    address, never a part of one."""
    if not isinstance(value, str):
        return ""
    text = itm.clean_line(value)
    if any(a in text for a in _AT_SIGNS):
        return ""
    return itm.truncate(text, limit)


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
        name = _name(org.get("name") if isinstance(org, dict) else None)
        if name:
            out[row["event_id"]] = name
    return out


def _attendees(row):
    """`(names, total, known)` from a snapshot row: the named people (rooms
    and groups skipped, declined guests and the owner left out, a nameless or
    address-like entry not named), how many people there are, and whether that
    number is firm. `attendee_count` (the cache already leaves declined guests
    out of it) is trusted when it is a valid integer; the listed people are
    counted only when it is missing or invalid. Anything of the wrong type is
    ignored."""
    people = row.get("attendees")
    names, listed = [], 0
    for person in people if isinstance(people, list) else []:
        if (not isinstance(person, dict) or person.get("group") is True
                or person.get("response") == "declined"):
            continue
        listed += 1
        if person.get("self") is True:
            continue  # the owner is counted, never named
        name = _name(person.get("name"), NAME_MAX)
        if name:
            names.append(name)
    count = row.get("attendee_count")
    total = count if (isinstance(count, int) and not isinstance(count, bool)
                      and count >= 0) else listed
    return names, total, row.get("attendees_known") is not False


def _attendees_line(names, total, known):
    """The text of the `attendees` thread line, or "" when there is nothing
    to say."""
    if not known:
        return "attendees unknown"
    if total <= 0:
        return ""
    text = f"{total} person" if total == 1 else f"{total} people"
    shown = names[:ATTENDEES_SHOWN]
    if shown:
        text += ": " + ", ".join(shown)
        if total > len(shown):
            text += f", +{total - len(shown)} more"
    return text


def _organizer_name(row, fallback):
    """The organizer's name from the row itself, else the twin's."""
    org = row.get("organizer")
    return (_name(org.get("name")) if isinstance(org, dict) else "") \
        or fallback or ""


def _where(row):
    loc = row.get("location")
    if not isinstance(loc, str):
        return ""
    text = itm.clean_line(loc)
    if any(a in text for a in _AT_SIGNS):
        return ""
    return itm.truncate(text, LOCATION_MAX)


def _busy(rows):
    """The rows the owner is going to, or might go to, as
    `[(start, end, series, title, label)]` sorted by start: accepted,
    tentative, pending or organized by them; not cancelled, not all-day, with
    a start and an end that parse and follow each other."""
    out = []
    for event_id, row in rows.items():
        if (not isinstance(event_id, str) or not isinstance(row, dict)
                or row.get("status") == "cancelled"):
            continue
        org = row.get("organizer")
        mine = (isinstance(org, dict) and org.get("self") is True) \
            or row.get("my_rsvp") == "organizer"
        rsvp = row.get("my_rsvp")
        label = _CONFLICT_LABEL.get(rsvp) if isinstance(rsvp, str) else None
        if label is None and mine:
            label = "organizer"
        if label is None:
            continue
        start, end = _when(row.get("start")), _when(row.get("end"))
        if start is None or end is None or start[1] or end[0] <= start[0]:
            continue
        title = itm.clean_line(row.get("title")) \
            if isinstance(row.get("title"), str) else ""
        out.append((start[0], end[0], series_of(event_id),
                    itm.truncate(title or "(untitled)", CONFLICT_TITLE_MAX),
                    label))
    return sorted(out, key=lambda b: b[:2])


def _conflicts(busy, key, start_ts, end_ts, all_day):
    """`(clashes, also_pending)`: the busy rows that overlap
    `[start_ts, end_ts)` (touching edges do not), leaving out the invite's own
    series. A clash is with something you are going to or organize; an overlap
    with another unanswered invite is only "also pending" (information, not a
    warning, since you can still decline one)."""
    if all_day or end_ts <= start_ts:
        return [], []
    hits = [b for b in busy
            if b[0] < end_ts and b[1] > start_ts and b[2] != key]
    return ([b for b in hits if b[4] != "pending"],
            [b for b in hits if b[4] == "pending"])


def _span(b, start_ts):
    """`10:30-11:00`; with the date when the other event is not on the
    invite's day."""
    if (_clock(b[0], "%Y%m%d") == _clock(start_ts, "%Y%m%d")
            and _clock(b[1], "%Y%m%d") == _clock(b[0], "%Y%m%d")):
        return _clock(b[0], "%H:%M") + "-" + _clock(b[1], "%H:%M")
    return describe_when(b[0], b[1], False)


def _conflict_line(b, start_ts):
    """`⚠ Title 10:30-11:00 (label)`."""
    return f"\u26a0 {b[3]} {_span(b, start_ts)} ({b[4]})"


def _pending_line(b, start_ts):
    """`Title 10:30-11:00`: another unanswered invite at the same time."""
    return f"{b[3]} {_span(b, start_ts)}"


def _gist(title, when, organizer, warn=False):
    head = "\u26a0 " if warn else ""
    tail = f" - {when}" + (f" - from {organizer}" if organizer else "")
    room = itm.GIST_MAX - len(head) - len(tail)
    if room < 8:  # a very long organizer: keep the title readable
        tail = f" - {when}"
        room = itm.GIST_MAX - len(head) - len(tail)
    return head + itm.truncate(title, max(room, 1)) + tail


def read(base_dir):
    """Everything `build` needs, read once: the snapshot, the organizer names
    from the JSON twin and the calendar id. `{"error": str}` or `{}` (no
    cache in use) when there is nothing to build from."""
    path = Path(base_dir) / "state" / SNAPSHOT_FILE
    doc, err = _read_json(path, MAX_SNAPSHOT_BYTES)
    if err:
        return {"error": err}
    if doc is None:
        return {}  # the calendar cache is not in use
    if not isinstance(doc, dict) or doc.get("v") != SNAPSHOT_VERSION \
            or isinstance(doc.get("v"), bool):
        return {"error": f"{SNAPSHOT_FILE} has an unknown format (expected v "
                         f"{SNAPSHOT_VERSION})"}
    rows = doc.get("events")
    fetched = _when(doc.get("fetched_at"))
    if not isinstance(rows, dict) or fetched is None or fetched[1]:
        return {"error": f"{SNAPSHOT_FILE} is missing events or fetched_at"}
    cfg = daemoncfg.calendar_cache(base_dir)
    asked = any(isinstance(r, dict) and r.get("my_rsvp") == "needsAction"
                for r in rows.values())
    return {"doc": doc, "fetched": fetched[0], "cfg": cfg,
            "names": _organizers(cfg["json"]) if asked else {}}


def load(base_dir, now, days=DEFAULT_DAYS):
    """`(items, stale, age_min, error)`: the unanswered invites from the cache
    (`read`, then `build`)."""
    return build(read(base_dir), now, days)


def build(data, now, days=DEFAULT_DAYS):
    """`(items, stale, age_min, error)` from what `read` returned; pure, so
    the time-dependent parts (not yet over, the window, staleness) can be
    re-evaluated later without touching a file.

    An invite counts when its `my_rsvp` is `needsAction`, its status is not
    `cancelled`, it has not ended and it starts before `now + days`. A
    recurring series is listed once, at its next instance. Declined,
    accepted, tentative and organizer rows never show.

    The item `ts` is the event's start: the cache records no arrival time
    for an invite. (The app lists invites soonest first.)
    """
    if data.get("error"):
        return [], False, None, data["error"]
    if not data:
        return [], False, None, None
    doc, fetched_ts, cfg = data["doc"], data["fetched"], data["cfg"]
    rows = doc["events"]
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

    names = data["names"]
    busy = _busy(rows)
    built, skipped = [], 0
    for key, (start_ts, event_id, row, end_ts, all_day) in sorted(
            pending.items(), key=lambda kv: kv[1][0])[:MAX_INVITES]:
        title = itm.clean_line(row.get("title")) if isinstance(
            row.get("title"), str) else ""
        title = title or "(untitled)"
        organizer = _organizer_name(row, names.get(event_id))
        when = describe_when(start_ts, end_ts, all_day)
        link = row.get("conference")
        link = link if itm.valid_link(link) else None
        att_names, att_total, att_known = _attendees(row)
        clashes, pending_too = _conflicts(busy, key, start_ts, end_ts, all_day)
        thread = [{"who": "subject", "ts": start_ts,
                   "text": itm.truncate(title, SUBJECT_MAX)},
                  {"who": "when", "ts": start_ts, "text": when}]
        if organizer:
            thread.append({"who": "from", "ts": start_ts, "text": organizer})
        att_line = _attendees_line(att_names, att_total, att_known)
        if att_line:
            thread.append({"who": "attendees", "ts": start_ts,
                           "text": att_line})
        place = _where(row)
        if place:
            thread.append({"who": "where", "ts": start_ts, "text": place})
        for clash in clashes[:CONFLICTS_SHOWN]:
            thread.append({"who": "conflict", "ts": start_ts,
                           "text": _conflict_line(clash, start_ts)})
        if len(clashes) > CONFLICTS_SHOWN:
            thread.append({"who": "conflict", "ts": start_ts,
                           "text": f"+{len(clashes) - CONFLICTS_SHOWN} more"})
        for other in pending_too[:CONFLICTS_SHOWN]:
            thread.append({"who": "also pending", "ts": start_ts,
                           "text": _pending_line(other, start_ts)})
        if len(pending_too) > CONFLICTS_SHOWN:
            thread.append({"who": "also pending", "ts": start_ts,
                           "text": f"+{len(pending_too) - CONFLICTS_SHOWN} more"})
        try:
            built.append(itm.make_item(
                id=f"invites:{key}", source="calendar", section="needs_reply",
                urgency="digest", sender=organizer or "calendar invite",
                where="invite",
                gist=_gist(title, when, organizer, bool(clashes)),
                ts=start_ts, link=link, thread=thread, draft=None,
                actions=["ack", "open"] if link else ["ack"],
                meta={"kind": "invite", "event_id": event_id,
                      "calendar_id": cfg["calendar_id"], "series": key,
                      "my_rsvp": "needsAction", "start": start_ts,
                      "title": itm.truncate(title, itm.FIELD_MAX),
                      "when": when, "cache_fetched": fetched_ts,
                      "attendees": [itm.truncate(n, ORGANIZER_MAX)
                                    for n in att_names[:ATTENDEES_META_MAX]],
                      "attendee_count": att_total if att_known else None,
                      "attendees_known": att_known,
                      "conflicts": len(clashes),
                      "also_pending": len(pending_too),
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
