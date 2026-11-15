"""Inbox sources: the built-in events reader and external command sources.

Phase 1 only calls `list`. A source never raises into the UI: every failure
becomes a `SourceResult.error` string.
"""
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
from .alertstate import Recovery

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
STDERR_TAIL = 200       # characters of stderr shown in an error
STDERR_KEEP = 64 * 1024  # bytes of stderr kept while the child runs

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
        meta=dict({"source_id": _text(event.get("source_id")),
                   "kind": _text(kind)},
                  **({"key": key} if key else {})),
    )


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


def load_events(base_dir, now=None, window_h=DEFAULT_WINDOW_H,
                rules=DEFAULT_URGENT_RULES, name="events", gmail_account=0,
                alert_max_age_h=DEFAULT_ALERT_MAX_AGE_H, email_routines=None):
    """The built-in source: recent events as items. Never raises.

    `email_routines` (a set of routine ids, or None for no filter) keeps only
    the Gmail events written by those routines; see `daemoncfg.label_routines`.
    """
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
        if (email_routines is not None and item["source"] == "email"
                and event.get("routine") not in email_routines):
            continue
        sid = item["meta"].get("source_id") or item["id"]
        prev = latest.get(sid)
        if prev is None or item["ts"] >= prev["ts"]:
            latest[sid] = item
    recovery = Recovery(base_dir)
    rows, alerts = [], {}
    for item in latest.values():
        if item["source"] == "alert":
            # The newest alert per key wins, whatever its source_id.
            key = item["meta"].get("key")
            if key:
                prev = alerts.get(key)
                if prev is None or item["ts"] >= prev["ts"]:
                    alerts[key] = item
                continue
            item = _alert_state(item, now, alert_max_age_h, recovery)
        rows.append(item)
    rows.extend(_alert_state(item, now, alert_max_age_h, recovery)
                for item in alerts.values())
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


def _run_capped(argv, timeout_s, stdin_bytes=None):
    """Run argv in its own process group with bounded memory and time.

    stdout is read in chunks and the whole group is killed once it passes
    OUTPUT_MAX_BYTES; stderr keeps only its last STDERR_KEEP bytes (the rest is
    drained and dropped so the child never blocks). Returns
    `(status, returncode, out, err)` with status ok, timeout or overflow.
    Raises OSError/ValueError when the program cannot start.
    """
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
            out.extend(chunk)
            if len(out) > itm.OUTPUT_MAX_BYTES:
                flags["overflow"] = True
                _kill_group(proc)
                return

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
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        status = "timeout"
    if flags["overflow"]:
        status = "overflow"
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

def _events_results(src, settings, base_dir, now):
    """The built-in events source, plus its pending-invite source."""
    email_routines, note = None, None
    label = src.get("email_require_label")
    if label:
        email_routines = daemoncfg.label_routines(base_dir, label)
        if not email_routines:
            note = (f"email_require_label '{label}': no Gmail routine requires "
                    "that label in its query, so no mail is shown")
    res = load_events(base_dir, now=now, window_h=src["window_h"],
                      rules=src["urgent"], name=src["name"],
                      gmail_account=settings["gmail_account"],
                      alert_max_age_h=settings.get(
                          "alert_urgent_max_age_h", DEFAULT_ALERT_MAX_AGE_H),
                      email_routines=email_routines)
    if note:
        res = res._replace(error=f"{res.error}; {note}" if res.error else note)
    out = [res]
    days = src.get("invites_days", invites.DEFAULT_DAYS)
    if days and days > 0:
        rows, stale, age, error = invites.load(base_dir, now, days)
        if age is not None or error:  # no cache at all: nothing to report
            out.append(SourceResult(f"{src['name']}.invites", rows, stale,
                                    age, error))
    return out


def collect(settings, base_dir, now=None, show_all=False):
    """Poll every configured source. Returns a list of SourceResult.

    `show_all` adds `--all` to `list` for the command sources that declared
    `supports_all`; no other source ever receives it. Duplicate ids across
    sources keep the first and flag the later source.
    """
    now = time.time() if now is None else now
    results = []
    seen = set()
    for src in settings["sources"]:
        if src["type"] == "events":
            batch = _events_results(src, settings, base_dir, now)
        else:
            batch = [run_command_source(
                src["name"], src["argv"], src["timeout_s"],
                show_all=bool(show_all and src.get("supports_all")))]
        for res in batch:
            kept = [i for i in res.items if i["id"] not in seen]
            dropped = len(res.items) - len(kept)
            seen.update(i["id"] for i in kept)
            error = res.error
            if dropped:
                note = f"{dropped} duplicate id(s) from another source dropped"
                error = f"{error}; {note}" if error else note
            results.append(res._replace(items=kept, error=error))
    return results
