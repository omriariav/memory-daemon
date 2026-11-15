"""Inbox sources: the built-in events reader and external command sources.

Phase 1 only calls `list`. A source never raises into the UI: every failure
becomes a `SourceResult.error` string.
"""
import json
import os
import re
import signal
import subprocess
import time
from collections import namedtuple
from datetime import datetime, timezone

from . import items as itm

SourceResult = namedtuple(
    "SourceResult", "name items stale age_min error", defaults=(None,)
)

EVENTS_FILE = "events.jsonl"
MAX_EVENT_LINE = 64 * 1024
MAX_EVENTS_BYTES = 16 * 1024 * 1024
DEFAULT_WINDOW_H = 48.0
STDERR_TAIL = 200

# Alert keys that are urgent unless the config says otherwise.
DEFAULT_URGENT_RULES = (
    {"source": "daemon", "key_prefix": "budget-hit:"},
    {"source": "daemon", "key_prefix": "connector:"},
    {"source": "daemon", "key_prefix": "attention:"},
)

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

def read_events(base_dir):
    """Every parseable event line, oldest first, from the rotation then the
    live file. Read-only; takes no lock (writers append whole lines, so a
    torn last line is simply skipped)."""
    state_dir = os.path.join(str(base_dir), "state")
    out = []
    for name in (EVENTS_FILE + ".1", EVENTS_FILE):
        path = os.path.join(state_dir, name)
        try:
            with open(path, "rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                if size > MAX_EVENTS_BYTES:
                    handle.seek(size - MAX_EVENTS_BYTES)
                    handle.readline()  # drop the partial first line
                data = handle.read(MAX_EVENTS_BYTES)
        except FileNotFoundError:
            continue
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
        sid = _text(event.get("source_id"))
        thread = sid.partition(":")[2]
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
        where = "mail"
    elif source == "recording":
        gist = title
        where = _text(event.get("source")) or "recording"
    else:
        gist = title
        where = "daemon"
    sender = ", ".join(people[:3]) if people else where
    urgent = any(_rule_matches(r, event, kind, key, start_ts, now)
                 for r in rules)
    link = _event_link(event, source, gmail_account)
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
        meta={"source_id": _text(event.get("source_id")), "kind": _text(kind)},
    )


def load_events(base_dir, now=None, window_h=DEFAULT_WINDOW_H,
                rules=DEFAULT_URGENT_RULES, name="events", gmail_account=0):
    """The built-in source: recent events as items. Never raises."""
    now = time.time() if now is None else now
    try:
        events = read_events(base_dir)
    except OSError as exc:
        return SourceResult(name, [], False, None,
                            f"cannot read events: {exc.strerror or exc}")
    cutoff = now - float(window_h) * 3600
    latest = {}  # one row per source_id: the newest event wins
    for event in events:
        try:
            item = event_to_item(event, now, rules, gmail_account)
        except (itm.ItemError, TypeError, ValueError):
            continue
        if item is None or item["ts"] < cutoff:
            continue
        sid = item["meta"].get("source_id") or item["id"]
        prev = latest.get(sid)
        if prev is None or item["ts"] >= prev["ts"]:
            latest[sid] = item
    rows = sorted(latest.values(), key=lambda i: i["ts"], reverse=True)
    newest = max((i["ts"] for i in rows), default=None)
    age = None if newest is None else max((now - newest) / 60.0, 0.0)
    return SourceResult(name, rows, False, age, None)


# --- command sources --------------------------------------------------------

def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _stderr_tail(raw):
    text = itm.clean_line(raw.decode("utf-8", "replace"))
    return itm.truncate(text[-STDERR_TAIL:], STDERR_TAIL)


def run_command_source(name, argv, timeout_s=20.0):
    """Run `<argv...> list` and parse it. Never raises."""
    argv = [os.path.expanduser(a) if a.startswith("~") else a for a in argv]
    try:
        proc = subprocess.Popen(
            argv + ["list"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return SourceResult(name, [], False, None,
                            f"cannot start: {getattr(exc, 'strerror', None) or exc}")
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return SourceResult(name, [], False, None,
                            f"timed out after {timeout_s:g}s")
    if proc.returncode != 0:
        detail = _stderr_tail(err)
        return SourceResult(
            name, [], False, None,
            f"exit {proc.returncode}" + (f": {detail}" if detail else ""))
    if len(out) > itm.OUTPUT_MAX_BYTES:
        return SourceResult(name, [], False, None,
                            f"output is larger than {itm.OUTPUT_MAX_BYTES} bytes")
    try:
        rows, stale, age, error = itm.parse_list_output(
            out.decode("utf-8", "replace"))
    except itm.ItemError as exc:
        return SourceResult(name, [], False, None, f"bad output: {exc}")
    return SourceResult(name, rows, stale, age, error)


def run_command_act(argv, request, timeout_s=20.0):
    """Phase 2: run `<argv...> act` with the request on stdin.

    Defined so sources can implement it now; nothing in phase 1 calls it.
    Returns `{"ok": bool, "message": str}` and never raises.
    """
    argv = [os.path.expanduser(a) if a.startswith("~") else a for a in argv]
    try:
        payload = json.dumps(dict(request, v=itm.SCHEMA_VERSION))
        proc = subprocess.run(
            argv + ["act"], input=payload.encode("utf-8"),
            capture_output=True, timeout=timeout_s, start_new_session=True,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"timed out after {timeout_s:g}s"}
    except (OSError, ValueError, TypeError) as exc:
        return {"ok": False, "message": f"cannot run: {exc}"}
    if proc.returncode != 0:
        return {"ok": False,
                "message": f"exit {proc.returncode}: {_stderr_tail(proc.stderr)}"}
    try:
        return itm.parse_act_response(proc.stdout.decode("utf-8", "replace"))
    except itm.ItemError as exc:
        return {"ok": False, "message": f"bad response: {exc}"}


# --- aggregate --------------------------------------------------------------

def collect(settings, base_dir, now=None):
    """Poll every configured source. Returns a list of SourceResult.

    Duplicate ids across sources keep the first and flag the later source.
    """
    now = time.time() if now is None else now
    results = []
    seen = set()
    for src in settings["sources"]:
        if src["type"] == "events":
            res = load_events(base_dir, now=now, window_h=src["window_h"],
                              rules=src["urgent"], name=src["name"],
                              gmail_account=settings["gmail_account"])
        else:
            res = run_command_source(src["name"], src["argv"], src["timeout_s"])
        kept = [i for i in res.items if i["id"] not in seen]
        dropped = len(res.items) - len(kept)
        seen.update(i["id"] for i in kept)
        error = res.error
        if dropped:
            note = f"{dropped} duplicate id(s) from another source dropped"
            error = f"{error}; {note}" if error else note
        results.append(res._replace(items=kept, error=error))
    return results
