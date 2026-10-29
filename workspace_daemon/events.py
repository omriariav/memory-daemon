"""Event stream: one JSON line per memory a capture created or updated, per
calendar invite, change or cancellation, and per operational alert.

`state/events.jsonl` lets a reader (a notifier, a dashboard) follow captures
without polling the memory store. `memory_sink.capture` (source from the
`source_id` prefix, e.g. slack), the `calendar_cache` routine (source gcal)
and `alerts.check` (source daemon, kind "alert") write it; every other reader must treat it as read-only.

One JSON object per line, oldest first:

    {"v": 1, "id", "ts", "source", "kind", "routine", "memory_id",
     "source_id", "title", "people"}

`kind` is "new" (entry created) or "update" (existing entry refreshed). Calendar
events add "cancel" and carry `memory_id` null, `source_id` `gcal:<event id>`
and a trailing optional "details" object (omitted when empty):
`{"start", "end"}` ISO times, plus `{"changed": {field: [old, new]}}` on update.
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


def source_of(source_id):
    """The source prefix of a canonical source id (`slack:C1:1.0` -> slack)."""
    prefix, sep, _ = str(source_id or "").partition(":")
    prefix = prefix.strip()
    return prefix if sep and prefix else "unknown"


def event_id(source_id, memory_id, kind, ts):
    raw = f"{source_id}|{memory_id}|{kind}|{ts}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build(routine_id, kind, memory_id, source_id, title, people, ts=None,
          details=None):
    ts = ts or utc_now_iso()
    event = {
        "v": SCHEMA_VERSION,
        "id": event_id(source_id, memory_id, kind, ts),
        "ts": ts,
        "source": source_of(source_id),
        "kind": kind,
        "routine": routine_id,
        "memory_id": memory_id,
        "source_id": source_id,
        "title": title,
        "people": list(people or []),
    }
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
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)  # closing also drops the flock
    except (OSError, TypeError, ValueError) as exc:
        log(f"events WARN append failed: {type(exc).__name__}: {exc}")
        return False
    return True
