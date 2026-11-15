"""Event stream: one JSON line per memory a capture created or updated, per
calendar invite, change or cancellation, and per operational alert.

`state/events.jsonl` lets a reader (a notifier, a dashboard) follow captures
without polling the memory store. `memory_sink.capture` (source from the
`source_id` prefix, e.g. slack), the `calendar_cache` routine (source gcal)
`external_events` routines (a user-chosen source),
`alerts.check` (source daemon, kind "alert") and the Mila source's language
check (also a daemon alert) write it; every other reader must treat it as
read-only.

One JSON object per line, oldest first:

    {"v": 1, "id", "ts", "source", "kind", "routine", "memory_id",
     "source_id", "title", "people"}

`kind` is "new" (entry created) or "update" (existing entry refreshed). Calendar
events add "cancel" and "rsvp" and carry `memory_id` null, `source_id`
`gcal:<event id>` and a trailing optional "details" object (omitted when
empty): `{"start", "end"}` ISO times, plus `{"changed": {field: [old, new]}}`
on update or `{"changed": {attendee: [old, new]}}` on rsvp (keyed by name,
or by email when the name is empty or shared), `"instances"` (every
instance id) when one event stands for several instances of a series, and
`"untrusted": true` (titles and names are written by others).
`kind` "diff" (source of the captured entry, e.g. zoom) means a capture of the
same meeting disagrees with an earlier one: `details` is
`{"entry_ids": [new, earlier], "differs": [one line each], "untrusted": true}`.
A capture of a meeting matched to a calendar event adds `details`
`{"calendar_event_id": str, "attendee_count": int}` on "new" and "update":
the event's human attendees including the owner, rooms excluded. The count is
omitted when the attendee list is unknown. Declined guests are not counted.
When the count is exactly 2, `other_attendee` is the other person's
lower-case email (never 0 for unknown).
`id` is a hash of (source_id, memory_id, kind, ts), so a reader can drop a
repeated line. Writers in separate process groups serialize on
`state/events.lock`. The file is rotated to `events.jsonl.1` (one
generation) once it reaches MAX_BYTES.
"""
import fcntl
import hashlib
import json
import os

from .shell import log, utc_now_iso
from .state import ensure_private_dir

FILE_NAME = "events.jsonl"
LOCK_NAME = "events.lock"
MAX_BYTES = 5 * 1024 * 1024
SCHEMA_VERSION = 1
EVENT_ID_MAX = 256


def _clean_details(details):
    """Drop malformed calendar_event_id / attendee_count; keep other keys."""
    if not isinstance(details, dict):
        return None
    out = dict(details)
    event_id = out.get("calendar_event_id")
    if "calendar_event_id" in out and not (
        isinstance(event_id, str) and event_id.strip()
        and len(event_id) <= EVENT_ID_MAX
    ):
        del out["calendar_event_id"]
    count = out.get("attendee_count")
    if "attendee_count" in out and not (
        isinstance(count, int) and not isinstance(count, bool) and count >= 0
    ):
        del out["attendee_count"]
    other = out.get("other_attendee")
    if "other_attendee" in out and not (
        isinstance(other, str) and "@" in other and len(other) <= 254
    ):
        del out["other_attendee"]
    return out or None


def source_of(source_id):
    """The source prefix of a canonical source id (`slack:C1:1.0` -> slack)."""
    prefix, sep, _ = str(source_id or "").partition(":")
    prefix = prefix.strip()
    return prefix if sep and prefix else "unknown"


def event_id(source_id, memory_id, kind, ts, salt=None):
    raw = f"{source_id}|{memory_id}|{kind}|{ts}"
    if salt is not None:
        raw += f"|{salt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build(routine_id, kind, memory_id, source_id, title, people, ts=None,
          details=None, salt=None):
    ts = ts or utc_now_iso()
    event = {
        "v": SCHEMA_VERSION,
        "id": event_id(source_id, memory_id, kind, ts, salt),
        "ts": ts,
        "source": source_of(source_id),
        "kind": kind,
        "routine": routine_id,
        "memory_id": memory_id,
        "source_id": source_id,
        "title": title,
        "people": list(people or []),
    }
    details = _clean_details(details)
    if details:
        event["details"] = details
    return event


def emit(base_dir, event):
    """Append one event line; True on success. Never raises: a failure is logged."""
    try:
        state_dir = ensure_private_dir(os.path.join(base_dir, "state"))
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        target = str(state_dir / FILE_NAME)
        lock_fd = os.open(
            str(state_dir / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if os.stat(target).st_size >= MAX_BYTES:
                    os.replace(target, target + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(
                target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600
            )
            try:
                start = os.fstat(fd).st_size
                written = os.write(fd, line)
                if written != len(line):
                    # Drop the fragment so a retry starts on a clean line.
                    os.ftruncate(fd, start)
                    raise OSError(
                        f"short write ({written} of {len(line)} bytes)")
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)  # closing also drops the flock
    except (OSError, TypeError, ValueError) as exc:
        log(f"events WARN append failed: {type(exc).__name__}: {exc}")
        return False
    return True
