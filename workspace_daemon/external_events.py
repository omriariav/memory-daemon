"""Maintenance kind `external_events`: feed state/events.jsonl from a local
command you configure.

The command (argv, no shell) prints `{"events": [...]}` on stdout. Each event
is validated, then the whole batch is appended through the same `events`
module every other producer uses. One invalid event rejects the batch.
"""
import collections
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

from . import events as event_stream
from . import memory_sink, state
from .chat_text import redact_secrets
from .shell import log

DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 1800
DEFAULT_MAX_EVENTS = 200
MAX_STDOUT_BYTES = 8 * 1024 * 1024
STDERR_KEEP_BYTES = 64 * 1024
STDERR_TAIL_CHARS = 500
REAP_GRACE_S = 5
CURSOR_MAX_BYTES = 256 * 1024
CURSOR_ENV = "MEMORY_DAEMON_CURSOR_FILE"
DRY_RUN_ENV = "MEMORY_DAEMON_DRY_RUN"
SOURCE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
KEY_MAX = 256
TITLE_MAX = 300
DETAILS_MAX_BYTES = 8 * 1024
PERSON_MAX = 128
# `ts` is deliberately not accepted: the daemon stamps emit time so the file
# stays oldest-first. A script that wants to report when something happened
# puts it in details (details.occurred_at by convention).
EVENT_KEYS = {"kind", "key", "title", "people", "details"}
RESERVED_SOURCES = memory_sink.RESERVED_EVENT_SOURCES


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def config_problems(rid, cfg):
    """Validation problems for a `maintenance` block of this kind."""
    problems = []
    command = cfg.get("command")
    if (
        not isinstance(command, list) or not command
        or any(not isinstance(a, str) or not a for a in command)
    ):
        problems.append(
            f"{rid}: maintenance.command must be a non-empty list of "
            "non-empty strings (argv, no shell)"
        )
    source = cfg.get("source")
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        problems.append(
            f"{rid}: maintenance.source is required and must match "
            f"{SOURCE_RE.pattern}"
        )
    elif source in RESERVED_SOURCES:
        problems.append(
            f"{rid}: maintenance.source {source!r} is used by a built-in "
            "producer; pick another name"
        )
    if "timeout_s" in cfg:
        value = cfg["timeout_s"]
        if not _is_int(value) or not 1 <= value <= MAX_TIMEOUT_S:
            problems.append(
                f"{rid}: maintenance.timeout_s must be an integer from 1 to "
                f"{MAX_TIMEOUT_S}"
            )
    if "max_events" in cfg:
        value = cfg["max_events"]
        if not _is_int(value) or value < 1:
            problems.append(
                f"{rid}: maintenance.max_events must be a positive integer"
            )
    if "env" in cfg:
        env = cfg["env"]
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not ENV_NAME_RE.fullmatch(k)
            or not isinstance(v, str)
            for k, v in env.items()
        ):
            problems.append(
                f"{rid}: maintenance.env must map variable names to strings"
            )
    return problems


def _one_line_text(value, label, limit):
    if (
        not isinstance(value, str) or not value.strip() or len(value) > limit
        or CONTROL_RE.search(value)
    ):
        raise ValueError(
            f"{label} must be a non-empty single-line string of at most "
            f"{limit} chars (no control characters)"
        )
    return value


def validate_event(raw):
    """Return the normalized event fields or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("event must be an object")
    unknown = set(raw) - EVENT_KEYS
    if unknown:
        raise ValueError(f"unknown field(s) {', '.join(sorted(unknown))}")
    kind = raw.get("kind")
    if not isinstance(kind, str) or not KIND_RE.fullmatch(kind):
        raise ValueError(f"kind must match {KIND_RE.pattern}")
    key = _one_line_text(raw.get("key"), "key", KEY_MAX)
    title = _one_line_text(raw.get("title"), "title", TITLE_MAX)
    people = raw.get("people", [])
    if not isinstance(people, list) or any(
        not isinstance(p, str) or len(p) > PERSON_MAX
        or not SLUG_RE.fullmatch(p)
        for p in people
    ):
        raise ValueError("people must be a list of kebab-case slugs")
    details = raw.get("details")
    if "details" in raw:
        if not isinstance(details, dict):
            raise ValueError("details must be an object")
        try:
            size = len(json.dumps(
                details, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise ValueError("details must be JSON-serializable") from None
        if size > DETAILS_MAX_BYTES:
            raise ValueError(f"details exceeds {DETAILS_MAX_BYTES} bytes")
    return {
        "kind": kind, "key": key, "title": title, "people": people,
        "details": details,
    }


def parse_output(stdout, max_events):
    """Validate command stdout.

    Returns (events, has_cursor, cursor). Raises RuntimeError on any problem.
    """
    try:
        data = json.loads(stdout)
    except ValueError:
        raise RuntimeError(
            "command stdout is not JSON: "
            + redact_secrets(stdout.strip()[:1000])[:200]) from None
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise RuntimeError('stdout must be a JSON object {"events": [...]}')
    extra = set(data) - {"events", "cursor"}
    if extra:
        raise RuntimeError(
            f"stdout has unknown key(s) {', '.join(sorted(extra))}")
    has_cursor = "cursor" in data
    cursor = data.get("cursor")
    if has_cursor:
        try:
            size = len(json.dumps(cursor, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError):
            raise RuntimeError("cursor is not JSON-serializable") from None
        if size > CURSOR_MAX_BYTES:
            raise RuntimeError(f"cursor exceeds {CURSOR_MAX_BYTES} bytes")
    items = data["events"]
    if len(items) > max_events:
        raise RuntimeError(
            f"command returned {len(items)} events, over max_events="
            f"{max_events}; none emitted"
        )
    out, seen = [], {}
    for index, raw in enumerate(items):
        try:
            event = validate_event(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid event at index {index}: {exc}") from None
        pair = (event["kind"], event["key"])
        if pair in seen:
            raise RuntimeError(
                f"invalid event at index {index}: duplicate kind/key "
                f"{pair[0]}/{pair[1]!r} (first at index {seen[pair]})"
            )
        seen[pair] = index
        out.append(event)
    return out, has_cursor, cursor


def cursor_path(base_dir, routine_id):
    return Path(base_dir) / "state" / "external-events" / f"{routine_id}.cursor"


def _save_cursor(path, cursor):
    state.ensure_private_dir(path.parent)
    state.write_atomic(
        path, json.dumps(cursor, ensure_ascii=False, allow_nan=False) + "\n",
        mode=0o600,
    )


def _kill_group(pid):
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _tail(raw, chars=STDERR_TAIL_CHARS):
    text = " ".join(raw.decode("utf-8", errors="replace").split())
    text = redact_secrets(text)
    return text[-chars:]


class _Pump(threading.Thread):
    """Drain one pipe, keeping at most `keep` bytes (head or tail)."""

    def __init__(self, pipe, keep, tail):
        super().__init__(daemon=True)
        self.pipe, self.keep, self.tail = pipe, keep, tail
        self.fd = pipe.fileno()
        self.chunks = collections.deque()
        self.size = 0
        self.overflow = False

    def run(self):
        # The reader owns closing its pipe: closing an fd another thread is
        # blocked reading would let the number be reused under it. A reader
        # held by a process that escaped the kill is abandoned and closes the
        # pipe itself once that process lets go.
        try:
            self._drain()
        finally:
            try:
                self.pipe.close()
            except OSError:
                pass

    def _drain(self):
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                return
            if not chunk:
                return
            if self.tail:
                self.chunks.append(chunk)
                self.size += len(chunk)
                while self.size - len(self.chunks[0]) >= self.keep:
                    self.size -= len(self.chunks.popleft())
            elif self.size + len(chunk) > self.keep:
                self.overflow = True  # keep draining, drop the rest
            else:
                self.chunks.append(chunk)
                self.size += len(chunk)

    def data(self):
        data = b"".join(self.chunks)
        return data[-self.keep:] if self.tail else data


def _run_command(cfg, base_dir, extra_env=None):
    argv = [os.path.expanduser(a) for a in cfg["command"]]
    env = dict(os.environ)
    env.update(cfg.get("env") or {})
    env.update(extra_env or {})
    timeout = cfg.get("timeout_s", DEFAULT_TIMEOUT_S)
    cwd = str(base_dir) if base_dir and Path(base_dir).is_dir() else str(Path.home())
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot start command: {exc}") from None
    out = _Pump(proc.stdout, MAX_STDOUT_BYTES, tail=False)
    err = _Pump(proc.stderr, STDERR_KEEP_BYTES, tail=True)
    out.start()
    err.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        # A child that kept a pipe open after the command exited holds the
        # readers too: that counts against the same timeout.
        for pump in (out, err):
            if not timed_out:
                pump.join(max(0.0, deadline - time.monotonic()))
                timed_out = pump.is_alive()
        if timed_out:
            _kill_group(proc.pid)
            try:
                proc.wait(timeout=REAP_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
            for pump in (out, err):
                pump.join(REAP_GRACE_S)
    finally:
        # Stray children the command left behind.
        _kill_group(proc.pid)
    tail = _tail(err.data())
    suffix = f": {tail}" if tail else ""
    if timed_out:
        raise RuntimeError(f"command timed out after {timeout}s{suffix}")
    if proc.poll() is None:
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"command exited {proc.returncode}{suffix}")
    if out.overflow:
        raise RuntimeError(f"command stdout exceeds {MAX_STDOUT_BYTES} bytes")
    try:
        return out.data().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"command stdout is not valid UTF-8 ({exc})") from None


def run(cfg, base_dir, dry_run=False, routine_id=None):
    """Run the command, validate its events, emit them. Raises on failure.

    The daemon keeps the cursor: it is saved only after every event of the
    batch was emitted, never on a dry run.
    """
    source = cfg["source"]
    cursor_file = cursor_path(base_dir, routine_id)
    stdout = _run_command(cfg, base_dir, {
        CURSOR_ENV: str(cursor_file),
        DRY_RUN_ENV: "1" if dry_run else "0",
    })
    parsed, has_cursor, cursor = parse_output(
        stdout, cfg.get("max_events", DEFAULT_MAX_EVENTS))
    nonce = uuid.uuid4().hex
    built = [
        event_stream.build(
            routine_id, item["kind"], None, f"{source}:{item['key']}",
            item["title"], item["people"], details=item["details"],
            salt=f"{nonce}:{index}",
        )
        for index, item in enumerate(parsed)
    ]
    emitted = 0
    for event in built:
        if dry_run:
            log(
                f"routine={routine_id} maintenance=external_events would emit "
                + json.dumps(event, ensure_ascii=False, sort_keys=True)
            )
        elif event_stream.emit(base_dir, event):
            emitted += 1
        else:
            break
    if not dry_run:
        if emitted != len(built):
            raise RuntimeError(
                f"emitted {emitted} of {len(built)} events; cursor not "
                "advanced, so the next run repeats the emitted ones"
            )
        if has_cursor:
            try:
                _save_cursor(cursor_file, cursor)
            except OSError as exc:
                raise RuntimeError(
                    f"events emitted but cursor not saved: {exc}") from None
    return {"events": len(built), "emitted": emitted, "dry_run": dry_run}
