"""Acts: what the inbox tells a command source after a local mark (0.10.0).

No Textual import. A command source opts in per source (`acts: [...]`, see
settings.py); with no `acts:` nothing here runs and the inbox behaves as it
did before. After the 5 s undo window the inbox calls `<argv> act` in a worker
(`sources.run_command_act`) with a request built here, and keeps two small
files of its own:

- `state/inbox-ops-log.jsonl`: every op sent and how it ended (the audit
  screen, `L`), capped at LOG_MAX_ROWS rows.
- `state/inbox-ops-pending.jsonl`: ops that could not be delivered (a timeout,
  a failure, a bad reply), retried with the SAME `op_id` at most every
  RETRY_EVERY_S seconds, capped at PENDING_MAX entries, entries older than
  PENDING_MAX_AGE_S dropped (the caller tells the user).

The source must be idempotent on `op_id`. See docs/inbox.md, "Acts".
"""
import fcntl
import json
import os
import tempfile
import threading
import time
import uuid

from .. import state as daemon_state

# What `acts:` may list on a command source. `reopen` and `unmute` are not
# listed on their own: they are sent as the inverse of `close`, `drop` and
# `mute`. 0.12.0 (tasks) adds `drop` and `due`; the inverse of `due` is `due`
# with the date it had before (`inverse_args`).
ACT_SETTINGS = ("close", "snooze", "mute", "waits_on", "relay", "drop", "due")
INVERSE = {"close": "reopen", "mute": "unmute", "drop": "reopen", "due": "due"}
# The act that undoes a local mark of each kind, for the audit screen.
OPS_LOG_NAME = "inbox-ops-log.jsonl"
PENDING_NAME = "inbox-ops-pending.jsonl"
LOG_MAX_ROWS = 500
LOG_COMPACT_BYTES = 512 * 1024
LOG_COMPACT_EVERY = 50      # appends between two checks of the row count
PENDING_MAX = 50
PENDING_MAX_AGE_S = 7 * 86400
RETRY_EVERY_S = 300
META_MAX_BYTES = 8192        # an item's meta bigger than this is not sent or kept
LOG_META_MAX_BYTES = 1024    # nor is one bigger than this kept in the ops log
NOTE_MAX = 300               # a relay note, characters
WHO_MAX = 120
MUTE_CHOICES = (("1d", "1 day", 86400), ("1w", "1 week", 7 * 86400),
                ("never", "untrack", None))

# Outcomes (`ActOutcome.status`).
OK, SHADOW, CONFLICT, PENDING = "ok", "shadow", "conflict", "pending"
# A failure whose op could not even be written to the pending file: it will
# never be retried.
LOST = "lost"
# Acts that are one-off events, not a state: a newer op for the same item does
# not replace a pending one of these.
EVENT_ACTS = ("relay",)


def new_op_id():
    return str(uuid.uuid4())


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def small_meta(meta, limit=META_MAX_BYTES):
    """`meta` unchanged when it is an object of at most `limit` bytes as JSON,
    else `{}` (the pending and log files stay small)."""
    if not isinstance(meta, dict):
        return {}
    try:
        size = len(json.dumps(meta, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return {}
    return meta if size <= limit else {}


def seen_of(ts, meta):
    """What the user had seen: the item's time and `meta.last_seen_ts`."""
    last = meta.get("last_seen_ts") if isinstance(meta, dict) else None
    return {"ts": float(ts) if _num(ts) else None,
            "last_seen_ts": float(last) if _num(last) else None}


def build_request(op_id, action, item_id, meta, ts, args):
    """The stdin of `act` (without `v`, which `run_command_act` adds)."""
    return {"op_id": op_id, "action": action, "id": item_id,
            "meta": small_meta(meta), "args": dict(args or {}),
            "seen": seen_of(ts, meta)}


def inverse_args(row):
    """The args of the inverse of a logged op: `due` goes back to the date it
    had (`prev_due`, null for no date); the others take none."""
    if row.get("action") == "due":
        return {"date": row.get("prev_due")}
    return {}


def clean_note(text):
    """A relay note as one line of at most NOTE_MAX characters."""
    text = " ".join(str(text or "").split())
    return text[:NOTE_MAX]


class _Jsonl:
    """A small JSON-lines file in `<base>/state/`: mode 0600, a threading
    lock plus an `fcntl` lock on a sidecar so several panes never interleave,
    atomic rewrites, damaged lines skipped."""

    NAME = ""

    def __init__(self, base_dir):
        self.path = None if base_dir is None else (
            os.path.join(str(base_dir), "state", self.NAME))
        self._lock = threading.RLock()

    def _read(self):
        if self.path is None:
            return list(getattr(self, "_mem", []))
        rows = []
        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            pass
        return rows

    def _write(self, rows):
        if self.path is None:
            self._mem = list(rows)
            return
        directory = os.path.dirname(self.path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{self.NAME}.",
                                   suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, ensure_ascii=False,
                                            sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:  # make the rename itself durable; best effort
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _locked(self):
        return _Locked(self)


class _Locked:
    def __init__(self, owner):
        self.owner, self.fd = owner, None

    def __enter__(self):
        self.owner._lock.acquire()
        if self.owner.path is not None:
            directory = os.path.dirname(self.owner.path)
            daemon_state.ensure_private_dir(directory)
            self.fd = os.open(self.owner.path + ".lock",
                              os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)       # closing drops the flock
        self.owner._lock.release()
        return False


class OpsLog(_Jsonl):
    """The ops the inbox sent, oldest first on disk. A row:
    `{"at", "op_id", "action", "id", "who", "where", "source", "args",
    "meta", "result", "message", "inverse_of"?}` where `result` is `ok`,
    `shadow`, `conflict`, `pending` (not synced) or `failed`."""

    NAME = OPS_LOG_NAME
    _since = 0

    def append(self, row):
        with self._locked():
            if self.path is None:
                rows = self._read() + [row]
                self._write(rows[-LOG_MAX_ROWS:])
                return
            with open(self.path, "a", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(json.dumps(row, ensure_ascii=False,
                                        sort_keys=True) + "\n")
            self._since += 1
            try:
                big = os.path.getsize(self.path) > LOG_COMPACT_BYTES
            except OSError:
                big = False
            if not big and self._since < LOG_COMPACT_EVERY:
                return
            self._since = 0
            rows = self._read()
            if len(rows) > LOG_MAX_ROWS:
                self._write(rows[-LOG_MAX_ROWS:])

    def rows(self, limit=LOG_MAX_ROWS):
        """The newest `limit` rows, newest first."""
        with self._locked():
            rows = self._read()
        return list(reversed(rows[-limit:]))


class PendingOps(_Jsonl):
    """Ops that were not delivered. An entry: `{"op_id", "request" (the exact
    stdin of `act`), "source", "action", "id", "who", "where", "queued_at",
    "last_try", "tries", "message"}`."""

    NAME = PENDING_NAME

    @staticmethod
    def _fresh(rows, now):
        """`(kept, expired, over_cap)`: entries older than PENDING_MAX_AGE_S go;
        then the oldest go past PENDING_MAX."""
        kept = [r for r in rows if _num(r.get("queued_at"))
                and now - r["queued_at"] <= PENDING_MAX_AGE_S
                and isinstance(r.get("op_id"), str)
                and isinstance(r.get("request"), dict)]
        expired = len(rows) - len(kept)
        over = max(0, len(kept) - PENDING_MAX)
        return kept[over:], expired, over

    @staticmethod
    def _supersedes(entry, old):
        """A newer op on an item makes an older pending one on the same item
        stale (the user changed their mind): it would be retried later and
        undo or contradict the newer decision. Events (relay) never are."""
        return (old.get("id") == entry.get("id")
                and old.get("action") not in EVENT_ACTS
                and entry.get("action") not in EVENT_ACTS)

    def add(self, entry, now=None):
        """Store `entry` (a new op replaces one with the same op_id, and drops
        the older pending ops of the same item, see `_supersedes`). Returns
        `(expired, over_cap)`: how many old entries were dropped for age or the
        cap."""
        now = time.time() if now is None else now
        with self._locked():
            rows = [r for r in self._read()
                    if r.get("op_id") != entry["op_id"]
                    and not self._supersedes(entry, r)] + [entry]
            kept, expired, over = self._fresh(rows, now)
            self._write(kept)
        return expired, over

    def prune(self, now=None):
        """Drop entries older than a week (and any past the cap). Returns the
        number dropped, 0 when the file was already clean."""
        now = time.time() if now is None else now
        with self._locked():
            rows = self._read()
            kept, expired, over = self._fresh(rows, now)
            if expired or over or len(kept) != len(rows):
                self._write(kept)
        return expired + over

    def all(self):
        with self._locked():
            return self._read()

    def count(self):
        return len(self.all())

    def due(self, now=None):
        """Entries not tried for RETRY_EVERY_S seconds."""
        now = time.time() if now is None else now
        return [r for r in self.all()
                if not int(r.get("tries") or 0)        # kept at quit: never tried
                or now - (r.get("last_try") or r.get("queued_at") or 0)
                >= RETRY_EVERY_S]

    def drop_for_id(self, item_id, keep_op=None):
        """Drop the pending state ops of an item (a newer op, such as an
        inverse, replaced them). Returns how many."""
        with self._locked():
            rows = self._read()
            kept = [r for r in rows if r.get("op_id") == keep_op
                    or r.get("id") != item_id
                    or r.get("action") in EVENT_ACTS]
            if len(kept) != len(rows):
                self._write(kept)
            return len(rows) - len(kept)

    def note(self, op_id, message):
        """Keep the reason of the latest failed try."""
        with self._locked():
            rows = self._read()
            for row in rows:
                if row.get("op_id") == op_id:
                    row["message"] = str(message)[:200]
            self._write(rows)

    def remove(self, op_id):
        with self._locked():
            rows = self._read()
            kept = [r for r in rows if r.get("op_id") != op_id]
            if len(kept) != len(rows):
                self._write(kept)

    def touch(self, op_id, now, message):
        """One more failed try: stamp it and keep the reason."""
        with self._locked():
            rows = self._read()
            for row in rows:
                if row.get("op_id") == op_id:
                    row["last_try"] = now
                    row["tries"] = int(row.get("tries") or 0) + 1
                    row["message"] = str(message)[:200]
            self._write(rows)


# --- the nudge template (`nudge_template`) ----------------------------------
DEFAULT_NUDGE_TEMPLATE = "Hi {first}, any news on {topic}?"
NUDGE_FIELDS = ("first", "who", "topic")
NUDGE_TEMPLATE_MAX = 500
TOPIC_MAX = 80


def template_problem(template):
    """Why `template` is not a usable `nudge_template`, or None. It may use
    only the fields in NUDGE_FIELDS, with no format specs or indexing."""
    import string
    if (not isinstance(template, str) or not template.strip()
            or len(template) > NUDGE_TEMPLATE_MAX
            or any(ord(c) < 32 and c != "\n" or ord(c) == 127
                   for c in template)):
        return (f"must be 1 to {NUDGE_TEMPLATE_MAX} characters of text "
                "without control characters")
    try:
        parts = list(string.Formatter().parse(template))
    except ValueError as exc:
        return f"is not a valid template ({exc})"
    for _text, field, spec, conv in parts:
        if field is None:
            continue
        if field not in NUDGE_FIELDS or spec or conv:
            return ("may use only {first}, {who} and {topic} (no format "
                    "specs); write a literal brace as {{ or }}")
    return None


_PUNCT = "\"'.,;:!?()[]{}<>*_~`|/\\-–—…"


def first_name(who):
    """The first word of a person's first name, without punctuation, or ""
    when `who` is not a person (a channel such as `#launch`, a name with no
    letters, or You)."""
    who = " ".join(str(who or "").split())
    if not who or who[0] == "#" or who.lower() in ("you", "me"):
        return ""
    word = who.split(" ")[0].strip(_PUNCT)
    return word if any(c.isalpha() for c in word) else ""


def render_nudge(template, who, gist):
    """The nudge text for a row: `{first}` is the first word of the person's
    first name (`there` for a channel or any name that is not a person),
    `{who}` the whole name, `{topic}` the row's gist cut to one line of at
    most TOPIC_MAX characters."""
    who = " ".join(str(who or "").split())
    topic = " ".join(str(gist or "").split())
    if len(topic) > TOPIC_MAX:
        topic = topic[:TOPIC_MAX - 1].rstrip() + "…"
    return template.format(first=first_name(who) or "there", who=who,
                           topic=topic or "this")
