"""Inbox sources: the built-in events reader and external command sources.

Phase 1 only calls `list`. A source never raises into the UI: every failure
becomes a `SourceResult.error` string.
"""
import glob
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone

from . import daemoncfg, invites
from . import items as itm
from .alertstate import MTIME_SLACK_S, Recovery
from .store import rsvp_state

SourceResult = namedtuple(
    "SourceResult", "name items stale age_min error", defaults=(None,)
)

EVENTS_FILE = "events.jsonl"
MAX_EVENT_LINE = 64 * 1024
MAX_EVENTS_BYTES = 16 * 1024 * 1024
DEFAULT_WINDOW_H = 48.0
DEFAULT_ALERT_MAX_AGE_H = 6.0
DEFAULT_SAVED_DAYS = 2.0
SAVED_MAX_ROWS = 500
LOG_TAIL_BYTES = 256 * 1024  # tail of logs/run.log read for an alert's error
LOG_LINE_MAX = 200           # characters of a log line shown in a thread
# A routine's failing run is logged before the tick that raises the alert, so
# an error up to this much older than the alert's time still belongs to it.
LOG_ERROR_LEAD_S = 900
STDERR_TAIL = 200       # characters of stderr shown in an error
STDERR_KEEP = 64 * 1024  # bytes of stderr kept while the child runs

# Alert keys that are urgent unless the config says otherwise.
DEFAULT_URGENT_RULES = (
    {"source": "daemon", "key_prefix": "budget-hit:"},
    {"source": "daemon", "key_prefix": "connector:"},
    {"source": "daemon", "key_prefix": "attention:"},
)

# Run states a routine passes through without being at fault: an alert that
# lists only these is never urgent (see docs/inbox.md, "Stale and recovered
# alerts").
BENIGN_ALERT_ISSUES = frozenset(("overlap", "interrupted", "incomplete"))
ALERT_HINT = "run `daemon.py status` for details"
_LOG_ERROR = re.compile(
    r"^(?P<ts>\S+)\s+routine=(?P<id>\S+)\b.*\b(?:ERROR|FATAL)\b")

_CAL_KIND = {"new": "New invite", "update": "Updated", "cancel": "Cancelled",
             "rsvp": "RSVP"}
_THREAD_ID = re.compile(r"^[0-9a-f]{8,24}$")


# --- time -------------------------------------------------------------------

def parse_iso(value):
    """ISO 8601 text (a trailing Z is fine) to epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


# --- events source ----------------------------------------------------------

def _read_file(path):
    """Parseable event objects of one file, in file order."""
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > MAX_EVENTS_BYTES:
                handle.seek(size - MAX_EVENTS_BYTES)
                handle.readline()  # drop the partial first line
            data = handle.read(MAX_EVENTS_BYTES)
    except FileNotFoundError:
        return []
    out = []
    for line in data.split(b"\n"):
        if not line.strip() or len(line) > MAX_EVENT_LINE:
            continue
        try:
            event = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def read_events(base_dir):
    """Every parseable event, oldest file first, de-duplicated by `id`.

    Read-only; takes no lock (writers append whole lines, so a torn last line
    is simply skipped). The live file is read BEFORE the `.1` rotation: if a
    rotation lands between the two reads, the old live content is then seen
    twice (and de-duplicated) instead of not at all.
    """
    state_dir = os.path.join(str(base_dir), "state")
    live = _read_file(os.path.join(state_dir, EVENTS_FILE))
    rotated = _read_file(os.path.join(state_dir, EVENTS_FILE + ".1"))
    seen, out = set(), []
    for event in rotated + live:
        eid = event.get("id")
        if isinstance(eid, str) and eid:
            if eid in seen:
                continue
            seen.add(eid)
        out.append(event)
    return out


def _text(value):
    return itm.clean_line(value) if isinstance(value, (str, int, float)) else ""


def _people(event):
    people = event.get("people")
    if not isinstance(people, list):
        return []
    return [p for p in (_text(x) for x in people) if p]


def _rule_matches(rule, event, kind, key, start_ts, now):
    for field, got in (("source", event.get("source")), ("kind", kind)):
        if field in rule and rule[field] != got:
            return False
    if "key_prefix" in rule and not (key or "").startswith(rule["key_prefix"]):
        return False
    if "title_regex" in rule:
        try:
            if not re.search(rule["title_regex"], _text(event.get("title")),
                             re.IGNORECASE):
                return False
        except re.error:
            return False
    if "starts_within_h" in rule:
        if start_ts is None:
            return False
        ahead = start_ts - now
        if not 0 <= ahead <= float(rule["starts_within_h"]) * 3600:
            return False
    return True


def _map_source(event):
    src = event.get("source")
    kind = event.get("kind")
    if src == "gmail":
        return "email"
    if src == "gcal" and kind in _CAL_KIND:
        return "calendar"
    if src in ("mila", "zoom"):
        return "recording"
    if src == "daemon" and kind == "alert":
        return "alert"
    return None


def _event_link(event, source, gmail_account=0):
    if source == "email":
        details = event.get("details") if isinstance(event.get("details"), dict) \
            else {}
        thread = _text(details.get("mail_thread_id"))
        if not _THREAD_ID.match(thread):
            # gmail:<thread>, gmail:<thread>:followup-open, gmail:<thread>@<msg>
            parts = _text(event.get("source_id")).split(":")
            thread = parts[1].split("@")[0] if len(parts) > 1 else ""
        if _THREAD_ID.match(thread):
            return f"https://mail.google.com/mail/u/{int(gmail_account)}/#all/{thread}"
    return None


def _calendar_parts(event, kind, ts):
    details = event.get("details") if isinstance(event.get("details"), dict) \
        else {}
    start = _text(details.get("start"))
    gist = f"{_CAL_KIND[kind]}: {_text(event.get('title')) or '(untitled)'}"
    if start:
        gist += f" ({start})"
    thread = []
    changed = details.get("changed")
    if isinstance(changed, dict):
        for field, pair in list(changed.items())[:20]:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                old, new = _text(pair[0]) or "-", _text(pair[1]) or "-"
                thread.append({"who": _text(field) or "change", "ts": ts,
                               "text": f"{old} -> {new}"})
    if start or details.get("end"):
        thread.append({
            "who": "when", "ts": ts,
            "text": f"{start or '?'} to {_text(details.get('end')) or '?'}"})
    return gist, thread, start


MAIL_FROM_MAX, MAIL_SUBJECT_MAX, MAIL_SNIPPET_MAX = 80, 200, 300
_ANGLE_ADDRESS = re.compile(r"<[^<>]*[@\uff20\ufe6b][^<>]*>")
_AT_SIGNS = ("@", "\uff20", "\ufe6b")


def _mail_from(value):
    """A display name only: any <...@...> is removed and a value that still
    holds an address is dropped (an old or hand-written event may carry one)."""
    text = _text(_ANGLE_ADDRESS.sub("", value if isinstance(value, str) else ""))
    text = text.strip(" \"'")
    return "" if any(a in text for a in _AT_SIGNS) \
        else itm.truncate(text, MAIL_FROM_MAX)


# A memory entry id as a context command may link it (`meta.memory_id`).
MEMORY_ID = itm.MEMORY_ID

# The Slack ids of `meta.reply_to` (same patterns as writes.CHANNEL_ID and
# writes.SLACK_TS, copied: writes imports this module).
_SLACK_CHANNEL = re.compile(r"\A[CDG][A-Z0-9]{8,12}\Z")
_SLACK_TS = re.compile(r"\A\d{9,11}\.\d{6}\Z")
SOURCE_ID_MAX = 300


def memory_index(events):
    """`{source_id: memory_id}` from the daemon's event rows: the memory entry
    the daemon saved each source item as. The newest row wins; a row counts
    only with a clean one-line source id and a well-formed memory id."""
    best = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        sid, mid = event.get("source_id"), event.get("memory_id")
        if (not isinstance(sid, str) or not sid or len(sid) > SOURCE_ID_MAX
                or itm.clean_line(sid) != sid
                or not isinstance(mid, str) or not MEMORY_ID.match(mid)):
            continue
        ts = parse_iso(event.get("ts")) or 0.0
        prev = best.get(sid)
        if prev is None or ts >= prev[0]:
            best[sid] = (ts, mid)
    return {sid: mid for sid, (_ts, mid) in best.items()}


def memory_match(meta, index):
    """`(exact, thread)`: the memory entry an item is saved as, and the entry
    its Slack thread was saved as (each "" when unknown; never a claim that
    the item is not saved). `exact`, in order: `meta.memory_id`;
    `meta.source_id` in the index; for a Slack item with a valid
    `meta.reply_to`, `slack:<channel>:<thread_ts>:<message_ts>` (a reply the
    daemon saved) then `slack:<channel>:<message_ts>`. Without a valid
    `reply_to` channel (it is a send-safety contract, so many items carry
    none) the same ids come from `meta.channel_id`, `meta.thread_ts` and
    `meta.message_ts`, validated alike: for matching only, never a send
    destination. `thread` is
    `slack:<channel>:<thread_ts>` (the thread root's entry), reported only
    when there is no exact match: the thread is in memory, this message may
    not be."""
    if not isinstance(meta, dict):
        return "", ""
    mid = meta.get("memory_id")
    if isinstance(mid, str) and MEMORY_ID.match(mid):
        return mid, ""
    sid = meta.get("source_id")
    if isinstance(sid, str) and sid in index:
        return index[sid], ""
    raw = meta.get("reply_to")
    channel = raw.get("channel") if isinstance(raw, dict) else None
    if not isinstance(channel, str) or not _SLACK_CHANNEL.match(channel):
        # No usable reply_to: match on the item's own Slack keys.
        raw = {"channel": meta.get("channel_id"),
               "thread_ts": meta.get("thread_ts"),
               "message_ts": meta.get("message_ts")}
        channel = raw["channel"]
        if not isinstance(channel, str) or not _SLACK_CHANNEL.match(channel):
            return "", ""
    stamps = {}
    for key in ("thread_ts", "message_ts"):
        ts = raw.get(key)
        stamps[key] = ts if isinstance(ts, str) and _SLACK_TS.match(ts) \
            else None
    if (isinstance(meta.get("reply_to"), dict) and raw is meta["reply_to"]
            and meta.get("channel_id") == channel
            and not stamps["thread_ts"] and not stamps["message_ts"]):
        # A reply_to with only a channel: borrow the item's own stamps
        # (matching only), for the same channel.
        for key in ("thread_ts", "message_ts"):
            ts = meta.get(key)
            if isinstance(ts, str) and _SLACK_TS.match(ts):
                stamps[key] = ts
    thread, message = stamps["thread_ts"], stamps["message_ts"]
    tries = []
    if thread and message:
        tries.append(f"slack:{channel}:{thread}:{message}")
    if message:
        tries.append(f"slack:{channel}:{message}")
    for sid in tries:
        if sid in index:
            return index[sid], ""
    if thread and f"slack:{channel}:{thread}" in index:
        return "", index[f"slack:{channel}:{thread}"]
    return "", ""


def memory_id_for(meta, index):
    """The exact memory entry of an item, or "" (see `memory_match`)."""
    return memory_match(meta, index)[0]


def annotate_memory(results, index):
    """`results` with `meta.memory_id` (an exact match) or
    `meta.thread_memory_id` (only the thread is in memory) filled in on every
    item the index knows (a copy; the cached rows are never changed)."""
    if not index:
        return results
    out = []
    for res in results:
        rows = []
        for item in res.items:
            meta = item.get("meta")
            exact, thread = memory_match(meta, index)
            if isinstance(meta, dict):
                add = {}
                if exact and meta.get("memory_id") != exact:
                    add["memory_id"] = exact
                if thread and meta.get("thread_memory_id") != thread:
                    add["thread_memory_id"] = thread
                if add:
                    item = dict(item, meta=dict(meta, **add))
            rows.append(item)
        out.append(res._replace(items=rows))
    return out


def units_memory_index(units):
    """The merged memory index of the units that keep one."""
    index = {}
    for unit in units:
        getter = getattr(unit, "memory", None)
        if getter is not None:
            index.update(getter())
    return index


def event_to_item(event, now, rules=DEFAULT_URGENT_RULES, gmail_account=0):
    """One event line to an item, or None if the event is not shown."""
    if not isinstance(event, dict):
        return None
    source = _map_source(event)
    ts = parse_iso(event.get("ts"))
    eid = _text(event.get("id"))
    if source is None or ts is None or not eid:
        return None
    kind = event.get("kind")
    title = _text(event.get("title")) or "(untitled)"
    people = _people(event)
    details = event.get("details") if isinstance(event.get("details"), dict) \
        else {}
    key = _text(details.get("key")) if source == "alert" else ""
    start_ts = parse_iso(details.get("start")) if source == "calendar" \
        else None
    thread = []
    if source == "calendar":
        gist, thread, _ = _calendar_parts(event, kind, ts)
        where = "calendar"
    elif source == "email":
        gist = title
        where = itm.truncate(_text(details.get("mail_subject")),
                             MAIL_SUBJECT_MAX) or "mail"
        snippet = itm.truncate(_text(details.get("mail_snippet")),
                               MAIL_SNIPPET_MAX)
    elif source == "recording":
        gist = title
        where = _text(event.get("source")) or "recording"
    else:
        gist = title
        where = "daemon"
    sender = ", ".join(people[:3]) if people else where
    if source == "email":
        mail_from = _mail_from(details.get("mail_from"))
        sender = mail_from or (", ".join(people[:3]) if people else "mail")
        if snippet:
            thread = [{"who": mail_from or "mail", "ts": ts, "text": snippet}]
    urgent = any(_rule_matches(r, event, kind, key, start_ts, now)
                 for r in rules)
    if source == "alert" and _benign_only(_alert_issues(details)):
        urgent = False
    link = _event_link(event, source, gmail_account)
    raw_memory_id = event.get("memory_id")
    memory_id = raw_memory_id if (isinstance(raw_memory_id, str)
                                  and MEMORY_ID.match(raw_memory_id)) else ""
    actions = ["ack"] if source != "email" else []
    if link is not None:
        actions.append("open")  # nothing to open without a link
    return itm.make_item(
        id=f"events:{eid}", source=source,
        section="urgent" if urgent else "fyi",
        urgency="urgent" if urgent else "digest",
        sender=sender, where=where, gist=gist, ts=ts,
        link=link, thread=thread, draft=None,
        actions=actions,
        meta=dict({"source_id": _text(event.get("source_id")),
                   "kind": _text(kind)},
                  **({"key": key} if key else {}),
                  **({"memory_id": memory_id} if memory_id else {})),
    )


def _alert_issues(details):
    issues = details.get("issues")
    if not isinstance(issues, list):
        return []
    return [t for t in (_text(x) for x in issues) if t]


def _benign_only(issues):
    """True when every issue is a benign run state (and there is one)."""
    return bool(issues) and all(
        i.lower() in BENIGN_ALERT_ISSUES for i in issues)


def _key_routine(key):
    """The routine an alert key names (`attention:<r>`, `interrupted:<r>`,
    `hold:<r>:<day>`), or ''."""
    kind, _, rest = key.partition(":")
    if kind in ("attention", "interrupted"):
        return rest
    if kind == "hold":
        return rest.rsplit(":", 1)[0]
    return ""


class _RunLog:
    """The newest ERROR/FATAL line per routine in the tail of logs/run.log.

    Read at most once per load, and only when an alert row asks. A missing or
    unreadable file means no lines.
    """

    def __init__(self, base_dir):
        self.path = os.path.join(str(base_dir), "logs", "run.log")
        self._errors = None

    def _scan(self):
        errors = {}
        try:
            with open(self.path, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > LOG_TAIL_BYTES:
                    handle.seek(size - LOG_TAIL_BYTES)
                    handle.readline()  # discard a partial first line
                data = handle.read(LOG_TAIL_BYTES)
        except OSError:
            return errors
        for line in data.decode("utf-8", errors="replace").splitlines():
            match = _LOG_ERROR.match(line)
            if match:
                errors[match.group("id")] = (parse_iso(match.group("ts")),
                                             line)
        return errors

    def last_error(self, routine):
        """`(epoch or None, line)` for the routine, or None."""
        if self._errors is None:
            self._errors = self._scan()
        return self._errors.get(routine)


def _age_text(seconds):
    seconds = max(seconds, 0)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def _alert_thread(event, key, since, runlog, now):
    """The detail pane of an alert row: routine, issues, active since (local
    time, like the header clock), the newest log error of the routine written
    since the alert became active (with its age), and where to look next."""
    details = event.get("details") if isinstance(event.get("details"), dict) \
        else {}
    routine = _text(details.get("routine")) or _key_routine(key)
    issues = _alert_issues(details)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(since))
    thread = []
    if routine:
        thread.append({"who": "routine", "ts": since, "text": routine})
    if issues:
        thread.append({"who": "issues", "ts": since,
                       "text": "; ".join(issues)})
    thread.append({"who": "active since", "ts": since, "text": stamp})
    found = runlog.last_error(routine) if routine else None
    if found is not None and found[0] is not None \
            and found[0] >= since - LOG_ERROR_LEAD_S:
        thread.append({"who": f"last error, {_age_text(now - found[0])}",
                       "ts": found[0],
                       "text": itm.truncate(itm.clean_line(found[1]),
                                            LOG_LINE_MAX)})
    thread.append({"who": "hint", "ts": since, "text": ALERT_HINT})
    return thread


def _synth_alert_event(key, since):
    """An alert event for an active key that has none on file."""
    routine = _key_routine(key)
    title = (f"{routine} needs attention" if key.startswith("attention:")
             and routine else f"Alert active: {key}")
    details = {"key": key}
    if routine:
        details["routine"] = routine
    return {"id": f"alert-active:{key}", "source": "daemon", "kind": "alert",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(since)),
            "routine": routine or None, "source_id": f"daemon:{key}",
            "title": title, "people": [], "details": details}


def _alert_state(item, now, max_age_h, recovery):
    """Apply the alert rules to one alert item (see docs/inbox.md).

    Resolved (the daemon no longer lists the key as active): fyi, flagged `meta.resolved` so
    the UI hides it unless asked. Older than `max_age_h`: fyi, not urgent.
    """
    key = item["meta"].get("key", "")
    if key and recovery.resolved(key, item["ts"]):
        item = dict(item, section="fyi", urgency="digest",
                    meta=dict(item["meta"], resolved=True))
    elif (item["section"] == "urgent"
          and now - item["ts"] > float(max_age_h) * 3600):
        item = dict(item, section="fyi", urgency="digest")
    return item


def _with_thread(item, event, key, since, runlog, now, item_id=None):
    """The alert item with its thread. `thread_fixed` keeps the pane from
    reversing the entries: they read top-down in either thread order."""
    try:
        return itm.parse_item(dict(
            item, id=item_id or item["id"],
            thread=_alert_thread(event, key, since, runlog, now),
            meta=dict(item["meta"], thread_fixed=True)))
    except itm.ItemError:
        return item


def load_events(base_dir, now=None, window_h=DEFAULT_WINDOW_H,
                rules=DEFAULT_URGENT_RULES, name="events", gmail_account=0,
                alert_max_age_h=DEFAULT_ALERT_MAX_AGE_H, email_routines=None,
                events=None, recovery=None, show_rsvp_replies=False,
                runlog=None):
    """The built-in source: recent events as items. Never raises.

    Calendar events of kind `rsvp` (other people answering the owner's
    invites) are left out unless `show_rsvp_replies` is true. They are dropped
    before the newest-per-source-id step, so the event's latest non-RSVP change
    (`new`, `update`, `cancel`) still shows after someone replies.

    `email_routines` (a set of routine ids, or None for no filter) keeps only
    the Gmail events written by those routines; see `daemoncfg.label_routines`.
    `events`, `recovery` and `runlog` let a caller that kept them from an
    earlier read rebuild the items for a later `now` without reading a file
    (the window, alert age and `starts_within_h` rules depend on the time).

    Every key active in `alerts.json` is listed, whatever the age of its event,
    under one id per episode, `events:alert-active:<key>@<since>`, so a mark
    survives rotation of the event files; a re-fire changes `since` and the
    row shows again.
    """
    now = time.time() if now is None else now
    if events is None:
        try:
            events = read_events(base_dir)
        except OSError as exc:
            return SourceResult(name, [], False, None,
                                f"cannot read events: {exc.strerror or exc}")
    recovery = recovery if recovery is not None else Recovery(base_dir)
    active = recovery.active_since()
    runlog = runlog if runlog is not None else _RunLog(base_dir)
    cutoff = now - float(window_h) * 3600
    latest = {}  # one row per source_id: the newest event wins
    newest_active = {}  # active alert key -> its newest (item, event), any age
    for event in events:
        if (not show_rsvp_replies and isinstance(event, dict)
                and event.get("source") == "gcal"
                and event.get("kind") == "rsvp"):
            continue
        try:
            item = event_to_item(event, now, rules, gmail_account)
        except (itm.ItemError, TypeError, ValueError):
            continue
        if item is None:
            continue
        if item["source"] == "alert" and item["meta"].get("key") in active:
            prev = newest_active.get(item["meta"]["key"])
            if prev is None or item["ts"] >= prev[0]["ts"]:
                newest_active[item["meta"]["key"]] = (item, event)
        if item["ts"] < cutoff:
            continue
        if (email_routines is not None and item["source"] == "email"
                and event.get("routine") not in email_routines):
            continue
        sid = item["meta"].get("source_id") or item["id"]
        prev = latest.get(sid)
        if prev is None or item["ts"] >= prev[0]["ts"]:
            latest[sid] = (item, event)
    rows, alerts = [], {}
    for item, event in latest.values():
        if item["source"] == "alert":
            # The newest alert per key wins, whatever its source_id.
            key = item["meta"].get("key")
            if key:
                prev = alerts.get(key)
                if prev is None or item["ts"] >= prev[0]["ts"]:
                    alerts[key] = (item, event)
                continue
            item = _alert_state(
                _with_thread(item, event, "", item["ts"], runlog, now), now,
                alert_max_age_h, recovery)
        rows.append(item)
    # A key the daemon lists as active is always shown, however old its
    # event. The event must not predate the episode (`since`); with none on
    # file, a row is made from the key.
    episode = {}
    for key, since in active.items():
        found = newest_active.get(key)
        if found is None or found[0]["ts"] < since - MTIME_SLACK_S:
            event = _synth_alert_event(key, since)
            try:
                item = event_to_item(event, now, rules, gmail_account)
            except (itm.ItemError, TypeError, ValueError):
                continue
            if item is None:
                continue
            found = (item, event)
        alerts[key] = found
        episode[key] = f"events:alert-active:{key}@{int(since)}"
    for key, (item, event) in alerts.items():
        item = _with_thread(item, event, key, active.get(key, item["ts"]),
                            runlog, now, episode.get(key))
        rows.append(_alert_state(item, now, alert_max_age_h, recovery))
    rows.sort(key=lambda i: i["ts"], reverse=True)
    newest = max((i["ts"] for i in rows), default=None)
    age = None if newest is None else max((now - newest) / 60.0, 0.0)
    return SourceResult(name, rows, False, age, None)


# --- saved view -------------------------------------------------------------

_SAVED_KINDS = ("new", "update")
_SAVED_SOURCE = {"gmail": "email", "slack": "slack", "mila": "recording",
                 "zoom": "recording"}


def load_saved(base_dir, now=None, days=DEFAULT_SAVED_DAYS, gmail_account=0):
    """What the daemon saved to memory recently, for the Saved view.

    Returns `(rows, error)`; never raises. A row is a capture event (kind
    `new` or `update` with a `memory_id`; calendar changes, alerts and other
    streams are not captures) from the last `days` days, newest first, one
    per `source_id` (the newest capture wins). Rows are plain dicts with
    `id`, `source` (an inbox source key), `title`, `ts`, `kind`, `link`
    (http(s) or None) and `source_id`. They are never inbox items.
    """
    now = time.time() if now is None else now
    try:
        events = read_events(base_dir)
    except OSError as exc:
        return [], f"cannot read events: {exc.strerror or exc}"
    cutoff = now - float(days) * 86400
    latest = {}
    for event in events:
        kind = event.get("kind")
        ts = parse_iso(event.get("ts"))
        eid = _text(event.get("id"))
        if (kind not in _SAVED_KINDS or ts is None or not eid
                or ts < cutoff or not event.get("memory_id")):
            continue
        sid = _text(event.get("source_id")) or eid
        prev = latest.get(sid)
        if prev is not None and prev["ts"] > ts:
            continue
        source = _SAVED_SOURCE.get(event.get("source"), "other")
        latest[sid] = {
            "id": eid, "source": source, "kind": kind, "ts": ts,
            "title": itm.truncate(_text(event.get("title")) or "(untitled)",
                                  itm.GIST_MAX),
            "source_id": sid,
            "link": _event_link(event, source, gmail_account),
        }
    rows = sorted(latest.values(), key=lambda r: r["ts"], reverse=True)
    return rows[:SAVED_MAX_ROWS], None


# --- command sources --------------------------------------------------------



def _expand(argv):
    return [os.path.expanduser(a) if a.startswith("~") else a for a in argv]


def _signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _kill_group(proc):
    _signal_group(proc, signal.SIGKILL)


def _wait_group_gone(proc, grace_s):
    """Wait up to `grace_s` for every process of the child's group to exit
    (the leader alone leaving is not enough: its children may be cleaning up)."""
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        proc.poll()  # reap the leader so it does not keep the group alive
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        except OSError:
            return
        time.sleep(0.05)


def _stderr_tail(raw):
    text = itm.clean_line(raw.decode("utf-8", "replace"))
    return itm.truncate(text[-STDERR_TAIL:], STDERR_TAIL)


def _run_capped(argv, timeout_s, stdin_bytes=None, term_grace_s=0,
                max_bytes=None, cancel=None):
    """Run argv in its own process group with bounded memory and time.

    stdout is read in chunks and the whole group is killed once it passes
    OUTPUT_MAX_BYTES; stderr keeps only its last STDERR_KEEP bytes (the rest is
    drained and dropped so the child never blocks). Returns
    `(status, returncode, out, err)` with status ok, timeout or overflow.
    Raises OSError/ValueError when the program cannot start.

    With `term_grace_s` > 0 a timeout or an overflow first sends SIGTERM to
    the group and gives it that many seconds to clean up before the SIGKILL
    (a write should not be cut off mid-way when it can be asked to stop).
    The default 0 kills at once, as command sources always have.

    `max_bytes` replaces OUTPUT_MAX_BYTES as the stdout cap for this call.

    `cancel` is an optional `threading.Event`: once it is set the group is
    asked to stop (SIGTERM, then SIGKILL after `term_grace_s`, or 2 s when that
    is 0) and the status is `cancelled`.
    """
    cap = itm.OUTPUT_MAX_BYTES if max_bytes is None else max_bytes
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE if stdin_bytes is not None
        else subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True)
    out, err = bytearray(), bytearray()
    flags = {"overflow": False}

    def pump_out():
        fd = proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            if flags["overflow"]:
                continue  # draining while the group winds down
            out.extend(chunk)
            if len(out) > cap:
                flags["overflow"] = True
                if not term_grace_s:
                    _kill_group(proc)
                    return
                _signal_group(proc, signal.SIGTERM)

    def pump_err():
        fd = proc.stderr.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            err.extend(chunk)
            if len(err) > 2 * STDERR_KEEP:
                del err[:-STDERR_KEEP]

    def feed():
        try:
            proc.stdin.write(stdin_bytes)
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=pump_out, daemon=True),
               threading.Thread(target=pump_err, daemon=True)]
    if stdin_bytes is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    status = "ok"
    deadline = time.monotonic() + timeout_s
    while True:
        left = deadline - time.monotonic()
        try:
            proc.wait(timeout=max(0.0, min(left, 0.1)) if cancel is not None
                      else max(0.0, left))
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                status = "cancelled"
                _signal_group(proc, signal.SIGTERM)
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                if term_grace_s:
                    _signal_group(proc, signal.SIGTERM)
                break
    if flags["overflow"]:
        status = "overflow"
    if status == "cancelled":
        _wait_group_gone(proc, term_grace_s or 2)
    elif term_grace_s and status != "ok":
        _wait_group_gone(proc, term_grace_s)
    _kill_group(proc)  # also reaps stragglers that outlive the main process
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    for t in threads:
        t.join(timeout=2)
    for stream in (proc.stdout, proc.stderr):
        try:
            stream.close()
        except OSError:
            pass
    return status, proc.returncode, bytes(out), bytes(err)


def run_command_source(name, argv, timeout_s=20.0, show_all=False):
    """Run `<argv...> list` (with `--all` when `show_all`) and parse it.
    Never raises. The caller passes `show_all` only for a source that
    declared `supports_all`."""
    command = _expand(argv) + ["list"] + (["--all"] if show_all else [])
    try:
        status, code, out, err = _run_capped(command, timeout_s)
    except (OSError, ValueError) as exc:
        return SourceResult(name, [], False, None,
                            f"cannot start: {getattr(exc, 'strerror', None) or exc}")
    if status == "timeout":
        return SourceResult(name, [], False, None,
                            f"timed out after {timeout_s:g}s")
    if status == "overflow":
        return SourceResult(name, [], False, None,
                            f"output is larger than {itm.OUTPUT_MAX_BYTES} bytes")
    if code != 0:
        detail = _stderr_tail(err)
        return SourceResult(
            name, [], False, None,
            f"exit {code}" + (f": {detail}" if detail else ""))
    try:
        rows, stale, age, error = itm.parse_list_output(
            out.decode("utf-8", "replace"))
    except itm.ItemError as exc:
        return SourceResult(name, [], False, None, f"bad output: {exc}")
    return SourceResult(name, rows, stale, age, error)


def run_command_act(argv, request, timeout_s=20.0):
    """Phase 2: run `<argv...> act` with the request on stdin.

    Defined so sources can implement it now; nothing in phase 1 calls it.
    Returns `{"ok": bool, "message": str}` and never raises. Like `list`, it
    runs in its own process group, killed whole on timeout or oversized output.
    """
    try:
        payload = json.dumps(dict(request, v=itm.SCHEMA_VERSION)).encode("utf-8")
        status, code, out, err = _run_capped(
            _expand(argv) + ["act"], timeout_s, stdin_bytes=payload)
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "message": f"cannot run: {exc}"}
    if status == "timeout":
        return {"ok": False, "message": f"timed out after {timeout_s:g}s"}
    if status == "overflow":
        return {"ok": False, "message": "response is too large"}
    if code != 0:
        return {"ok": False, "message": f"exit {code}: {_stderr_tail(err)}"}
    try:
        return itm.parse_act_response(out.decode("utf-8", "replace"))
    except itm.ItemError as exc:
        return {"ok": False, "message": f"bad response: {exc}"}


# --- aggregate --------------------------------------------------------------

class Unit:
    """One independently refreshable source: `run(now, show_all)` returns a
    list of SourceResult (possibly empty), `watch()` the paths whose change
    makes it stale (stat-checked by `watch.Watcher`). `uses_all` marks a
    command source that declared `supports_all`. `reeval(now)`, when given,
    rebuilds the results from what the last `run` read, for the parts that
    depend on the time (no file is touched); it returns None when there is
    nothing to rebuild."""

    def __init__(self, name, run, watch=lambda: [], uses_all=False,
                 reeval=None, memory=None, answers=None):
        self.name, self.run, self.watch, self.uses_all = name, run, watch, uses_all
        self.reeval = reeval
        # `answers()` returns `{series: "declined" | "answered"}` from what
        # the unit last read (see `invites.answered_series`).
        self.answers = answers
        # `memory()` returns the `{source_id: memory_id}` the unit last read.
        self.memory = memory


def _events_unit_run(src, settings, base_dir):
    """`(run, reeval)` for the built-in events source. `run` reads the event
    files; `reeval` rebuilds the items for a later time from what `run` read
    (and from the alert state and run log tail it already loaded)."""
    kept = {}

    def memory():
        return kept.get("memory", {})

    def build(now, events, recovery, runlog, note):
        res = load_events(base_dir, now=now, window_h=src["window_h"],
                          rules=src["urgent"], name=src["name"],
                          gmail_account=settings["gmail_account"],
                          alert_max_age_h=settings.get(
                              "alert_urgent_max_age_h", DEFAULT_ALERT_MAX_AGE_H),
                          email_routines=kept.get("routines"),
                          events=events, recovery=recovery, runlog=runlog,
                          show_rsvp_replies=bool(
                              settings.get("show_rsvp_replies")))
        if note:
            res = res._replace(
                error=f"{res.error}; {note}" if res.error else note)
        return res

    def run(now, show_all):
        kept.clear()
        note = None
        label = src.get("email_require_label")
        if label:
            kept["routines"] = daemoncfg.label_routines(base_dir, label)
            if not kept["routines"]:
                note = (f"email_require_label '{label}': no Gmail routine "
                        "requires that label in its query, so no mail is shown")
        try:
            events = read_events(base_dir)
        except OSError as exc:
            kept.pop("events", None)
            kept.pop("memory", None)
            return [SourceResult(src["name"], [], False, None,
                                 f"cannot read events: {exc.strerror or exc}")]
        kept.update(events=events, recovery=Recovery(base_dir),
                    runlog=_RunLog(base_dir), note=note,
                    memory=memory_index(events))
        return [build(now, events, kept["recovery"], kept["runlog"], note)]

    def reeval(now):
        if "events" not in kept:
            return None
        return [build(now, kept["events"], kept["recovery"], kept["runlog"],
                      kept["note"])]

    return run, reeval, memory


def _invites_unit_run(src, base_dir):
    """`(run, reeval, answers)` for the pending-invite source: `[]` when there
    is no calendar cache at all (nothing to report)."""
    kept = {}

    def results(data, now):
        rows, stale, age, error = invites.build(
            data, now, src.get("invites_days", invites.DEFAULT_DAYS))
        if age is None and not error:
            return []
        return [SourceResult(f"{src['name']}.invites", rows, stale, age, error)]

    def run(now, show_all):
        kept["data"] = invites.read(base_dir)
        return results(kept["data"], now)

    def reeval(now):
        return results(kept["data"], now) if "data" in kept else None

    def answers():
        return invites.answered_series(kept.get("data") or {})

    return run, reeval, answers


def events_files(base_dir):
    state = os.path.join(str(base_dir), "state")
    return [os.path.join(state, EVENTS_FILE),
            os.path.join(state, EVENTS_FILE + ".1")]


def expand_watch(base_dir, paths):
    """Watch paths of a command source: `~` expands, relative paths are
    relative to the base directory."""
    out = []
    for path in paths:
        path = os.path.expanduser(path)
        out.append(path if os.path.isabs(path)
                   else os.path.join(str(base_dir), path))
    return out


def make_units(settings, base_dir):
    """The refreshable units of the configured sources, in order."""
    units = []
    for src in settings["sources"]:
        if src["type"] == "events":
            def watch(src=src):
                files = events_files(base_dir) + [os.path.join(
                    str(base_dir), "state", "alerts.json")]
                if src.get("email_require_label"):
                    files += sorted(glob.glob(os.path.join(
                        str(base_dir), "routines", "*.yaml")))
                return files

            run, reeval, memory = _events_unit_run(src, settings, base_dir)
            units.append(Unit(src["name"], run, watch, reeval=reeval,
                              memory=memory))
            if src.get("invites_days", invites.DEFAULT_DAYS) > 0:
                run, reeval, answers = _invites_unit_run(src, base_dir)
                units.append(Unit(
                    f"{src['name']}.invites", run,
                    lambda: [os.path.join(str(base_dir), "state",
                                          invites.SNAPSHOT_FILE)],
                    reeval=reeval, answers=answers))
        else:
            units.append(Unit(
                src["name"],
                lambda now, show_all, src=src: [run_command_source(
                    src["name"], src["argv"], src["timeout_s"],
                    show_all=bool(show_all and src.get("supports_all")))],
                lambda src=src: expand_watch(base_dir, src.get("watch", [])),
                uses_all=bool(src.get("supports_all"))))
    return units


def saved_watch(base_dir):
    """The files the Saved view depends on."""
    return lambda: events_files(base_dir)


def units_answers(units, marks=None):
    """`{series: {"snapshot": "declined" | "answered" | None, "mark": {...} |
    None}}`: what is known of your answers, by recurring series (see
    `invites.series_of`). `snapshot` is from the calendar snapshot the units
    last read (a series with a row still waiting for an answer has none);
    `mark` is the inbox RSVP of the series, `InboxStore.rsvp_series()`'s entry
    (`answer`, `at`, `event`)."""
    out = {k: {"snapshot": None, "mark": dict(v)}
           for k, v in (marks or {}).items()}
    for unit in units:
        getter = getattr(unit, "answers", None)
        if getter is None:
            continue
        for series, answer in getter().items():
            out.setdefault(series, {"mark": None})["snapshot"] = answer
    return out


CHANGED_FIELDS = 3     # fields named in an invite's `changed` line


def _twin_parts(item):
    """`(series, event id, kind)` for a calendar row of the events stream with
    a `gcal:` source id, else None."""
    meta = item["meta"]
    source_id = meta.get("source_id")
    if (item["source"] != "calendar" or not isinstance(source_id, str)
            or not source_id.startswith("gcal:")):
        return None
    event_id = source_id[len("gcal:"):]
    return invites.series_of(event_id), event_id, meta.get("kind")


def _listed_invites(batches, closed):
    """`{series: meta}` of the pending-invite rows that stand for their
    event: not the ones marked done or acknowledged (`closed`, a dict by id;
    a snoozed row still counts)."""
    return {i["meta"]["series"]: i["meta"] for b in batches for r in b
            for i in r.items
            if i["id"].startswith("invites:") and i["id"] not in closed
            and isinstance(i["meta"].get("series"), str)}


def _standing(listed, answered):
    """`{series: (snapshot answer, mark)}` of the answers that count. The
    mark of a series whose invite is listed counts only when the store would
    still hide that invite: not when the listed invite is another instance of
    the series than the one answered (`moved`), nor when a later snapshot
    still lists it as unanswered (`unconfirmed`)."""
    out = {}
    for series, state in (answered or {}).items():
        snap, mark = state.get("snapshot"), state.get("mark")
        if mark is not None and series in listed and rsvp_state(
                mark.get("at"), mark.get("event"), listed[series]) != "hidden":
            mark = None
        if snap or mark:
            out[series] = (snap, mark)
    return out


def _seen_at_close(parts, ts, closed):
    """True for a calendar row (any kind) of an invite marked done or
    acknowledged, that is not newer than the close and about the instance that
    was closed (or any instance of the series, for a mark that recorded none):
    you saw it, and chose to be finished with it."""
    series, event_id, kind = parts
    if kind not in ("new", "update", "cancel"):
        return False
    mark = (closed or {}).get(f"invites:{series}")
    return (mark is not None and mark.get("event") in (None, event_id)
            and isinstance(ts, (int, float)) and ts <= mark["at"])


def _hides_calendar_twin(item, listed, standing, closed=None):
    """True for a calendar row of the events stream that an invite makes
    redundant (the rules are in docs/inbox.md). `listed` is `_listed_invites`'
    result, `standing` is `_standing`'s.

    - `New invite` goes while the series has a pending-invite row and once it
      is answered.
    - `Updated` goes while the invite is listed and the update is about the
      listed instance (the invite shows its current time and title, see
      `_carry_changes`), and, after an inbox answer, when it is about the
      answered instance and not newer than the answer (you saw the invite as it
      was). Any other update stays.
    - `Cancelled` stays. For a declined meeting (every row declined in the
      snapshot, or the instance declined in the inbox) `Updated` and
      `Cancelled` both go.
    - Whatever the answer, every kind goes when the invite was marked done or
      acknowledged (`closed`, see `_seen_at_close`) and the row is not newer
      than the close."""
    parts = _twin_parts(item)
    if parts is None:
        return False
    series, event_id, kind = parts
    if _seen_at_close(parts, item.get("ts"), closed):
        return True
    snap, mark = standing.get(series, (None, None))
    if kind == "new":
        return series in listed or snap is not None or mark is not None
    if kind not in ("update", "cancel"):
        return False
    same = mark is not None and mark.get("event") in (None, event_id)
    if snap == "declined" or (same and mark["answer"] == "declined"):
        return True
    if kind == "cancel":
        return False
    if mark is not None:
        ts = item.get("ts")
        return (same and mark.get("at") is not None
                and isinstance(ts, (int, float)) and ts <= mark["at"])
    return (series in listed and snap is None
            and listed[series].get("event_id") == event_id)


def _changed_line(item):
    """The `changed` thread line for an `Updated` row's changed fields (the
    `field: old -> new` lines `_calendar_parts` built), or None."""
    pairs = [m for m in item["thread"]
             if isinstance(m, dict) and m.get("who") != "when"
             and isinstance(m.get("text"), str) and " -> " in m["text"]]
    if not pairs:
        return None
    text = "; ".join(f"{m['who']}: {m['text']}" for m in pairs[:CHANGED_FIELDS])
    if len(pairs) > CHANGED_FIELDS:
        text += f"; +{len(pairs) - CHANGED_FIELDS} more"
    return {"who": "changed", "ts": item["ts"],
            "text": itm.truncate(text, itm.TEXT_MAX)}


def _carry_changes(batches, listed, standing, closed=None):
    """`{series: thread line}`: the newest update of each listed invite's own
    instance that `_hides_calendar_twin` hides, to show on the invite row. A
    series with an answer that counts has none (its invite is not what you
    read)."""
    best = {}
    for batch in batches:
        for res in batch:
            for i in res.items:
                parts = _twin_parts(i)
                if (parts is None or parts[2] != "update"
                        or parts[0] not in listed or parts[0] in standing
                        or not _hides_calendar_twin(i, listed, standing, closed)):
                    continue
                line = _changed_line(i)
                if line and (parts[0] not in best
                             or i["ts"] >= best[parts[0]][0]):
                    best[parts[0]] = (i["ts"], line)
    return {k: v[1] for k, v in best.items()}


def _with_line(item, line):
    """A copy of an invite row with the `changed` line after its `when`
    line."""
    thread = [m for m in item["thread"]]
    at = next((n + 1 for n, m in enumerate(thread)
               if isinstance(m, dict) and m.get("who") == "when"), len(thread))
    thread.insert(at, line)
    return dict(item, thread=thread)


def merge(batches, answered=None, closed=None):
    """Flatten per-unit results (in source order). A duplicate item id across
    sources keeps the first and flags the later source. Calendar rows of the
    events stream that a pending invite makes redundant are dropped (see
    `_hides_calendar_twin`), and the newest change of a listed invite is added
    to its row as a `changed` line. `answered` is what `units_answers`
    returns; `closed` is `InboxStore.closed_ids()`: the invite rows marked done
    or acknowledged no longer count as listed, and the calendar rows they had
    seen are hidden. Cached rows are never changed: a row with a new line
    is a copy."""
    batches = [list(b) for b in batches]
    closed = closed or {}
    listed = _listed_invites(batches, closed)
    standing = _standing(listed, answered)
    changes = (_carry_changes(batches, listed, standing, closed)
               if listed else {})
    results = []
    seen = set()
    for batch in batches:
        for res in batch:
            dropped = 0
            kept = []
            for i in res.items:
                if i["id"] in seen:
                    dropped += 1   # a duplicate id
                elif (listed or standing or closed) and _hides_calendar_twin(
                        i, listed, standing, closed):
                    continue
                elif (changes and i["id"].startswith("invites:")
                      and i["id"] not in closed
                      and i["meta"].get("series") in changes):
                    kept.append(_with_line(i, changes[i["meta"]["series"]]))
                else:
                    kept.append(i)
            seen.update(i["id"] for i in kept)
            error = res.error
            if dropped:
                note = f"{dropped} duplicate id(s) from another source dropped"
                error = f"{error}; {note}" if error else note
            results.append(res._replace(items=kept, error=error))
    return results


def collect(settings, base_dir, now=None, show_all=False, marks=None,
            closed=None):
    """Poll every configured source. Returns a list of SourceResult.

    `show_all` adds `--all` to `list` for the command sources that declared
    `supports_all`; no other source ever receives it. Duplicate ids across
    sources keep the first and flag the later source. `marks` is
    `InboxStore.rsvp_series()`, to hide the events stream's rows about invites
    answered in the inbox, `closed` is `InboxStore.closed_ids()`.
    """
    now = time.time() if now is None else now
    units = make_units(settings, base_dir)
    batches = [list(u.run(now, show_all)) for u in units]
    results = merge(batches, units_answers(units, marks), closed)
    return annotate_memory(results, units_memory_index(units))
