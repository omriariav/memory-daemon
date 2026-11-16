"""Local inbox state: done, snooze-until, ack, RSVP answer and saved per item id.

`<base>/state/inbox-state.json`, written atomically with mode 0600. Several
inbox panes (processes) and several threads of one pane may write at once, so
every read-modify-write holds a `threading.Lock` and an `fcntl` lock on the
sidecar `inbox-state.lock`, re-reads the file under the lock, and writes
through a uniquely named temp file in the same directory. Marks written by
another pane are therefore never lost. Corrupt or unreadable state reads as
empty and the broken file is kept aside as `.bad` before the next write.
"""
import fcntl
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from .. import state as daemon_state

FILE_NAME = "inbox-state.json"
LOCK_NAME = "inbox-state.lock"
SCHEMA_VERSION = 1
# RSVP answers a mark may hold (the word the bridge sent).
RSVP_ANSWERS = ("accepted", "declined", "tentative")
# A mark is kept while its item is still listed by some source, and for this
# long after the item was last listed (or the mark was last changed). Active
# snoozes are never pruned by age. Age pruning happens only in `note_listed`
# after a successful poll: a write made before the first poll of a session
# must not drop marks whose items that poll is about to list again.
RETENTION_S = 7 * 86400
LISTED_REFRESH_S = 3600  # how often a still-listed item's mark is re-stamped
MAX_ENTRIES = 5000


class InboxStore:
    def __init__(self, base_dir=None, clock=time.time):
        """`base_dir=None` keeps marks in memory only (demo mode, tests)."""
        self.path = (Path(base_dir) / "state" / FILE_NAME
                     if base_dir is not None else None)
        self.clock = clock
        self.marks = {}
        self._corrupt = False
        self._lock = threading.RLock()
        self.load()

    # -- io --
    def _read(self):
        if self.path is None:
            return self.marks, False
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}, False
        except (OSError, ValueError):
            return {}, True
        raw = doc.get("items") if isinstance(doc, dict) else None
        if not isinstance(raw, dict):
            return {}, True
        marks = {}
        for key, mark in raw.items():
            if not isinstance(key, str) or not isinstance(mark, dict):
                continue
            clean = {}
            for field in ("done", "ack", "snooze_until", "updated",
                          "last_listed", "saved"):
                val = mark.get(field)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    clean[field] = float(val)
            if mark.get("rsvp") in RSVP_ANSWERS:
                clean["rsvp"] = mark["rsvp"]
            if clean:
                marks[key] = clean
        return marks, False

    def load(self):
        """Re-read the file (another pane may have written)."""
        with self._lock:
            self.marks, self._corrupt = self._read()
            return self.marks

    def _write(self):
        self._cap()
        if self.path is None:
            return
        if self._corrupt:
            try:
                os.replace(self.path, str(self.path) + ".bad")
            except OSError:
                pass
            self._corrupt = False
        text = json.dumps({"v": SCHEMA_VERSION, "items": self.marks},
                          indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=f".{FILE_NAME}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
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
            dfd = os.open(str(self.path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    def _transaction(self, change):
        """Re-read under both locks, apply `change(marks)`, write if it
        returns True."""
        with self._lock:
            if self.path is None:
                if change(self.marks):
                    self._write()
                return
            daemon_state.ensure_private_dir(self.path.parent)
            lock_fd = os.open(str(self.path.parent / LOCK_NAME),
                              os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                self.marks, self._corrupt = self._read()
                if change(self.marks):
                    self._write()
            finally:
                os.close(lock_fd)  # closing drops the flock

    # -- marks --
    def _mark(self, item_id, **fields):
        def change(marks):
            mark = dict(marks.get(item_id, {}))
            mark.update(fields)
            mark["updated"] = self.clock()
            marks[item_id] = mark
            return True
        self._transaction(change)

    def done(self, item_id):
        self._mark(item_id, done=self.clock())

    def ack(self, item_id):
        self._mark(item_id, ack=self.clock())

    def snooze(self, item_id, until):
        self._mark(item_id, snooze_until=float(until))

    def rsvp(self, item_id, answer):
        """The invite was answered (`accepted`, `declined`, `tentative`): hide
        it until the calendar cache shows the answer and the row is gone."""
        if answer not in RSVP_ANSWERS:
            raise ValueError(f"unknown RSVP answer {answer!r}")
        self._mark(item_id, rsvp=answer)

    def mark_saved(self, item_id):
        """The item was saved to memory. It stays listed, with a glyph."""
        self._mark(item_id, saved=self.clock())

    def restore(self, item_id):
        """Drop every mark (undo)."""
        self._transaction(lambda marks: marks.pop(item_id, None) is not None)

    def note_listed(self, item_ids, prune=False):
        """Record that these ids are still listed by a source, so their marks
        outlive the item's time in the source's window; with `prune`, then
        drop the marks that have not been listed for RETENTION_S. Pass
        `prune=True` only after a poll in which every source answered (a source
        that is down lists nothing). Cheap: it touches the disk only when a
        marked id needs a fresh stamp or a mark has expired."""
        ids = set(item_ids)
        now = self.clock()

        def stale(marks):
            return [k for k in ids if k in marks and
                    now - marks[k].get("last_listed", 0) > LISTED_REFRESH_S]

        def expired(marks, stamped=()):
            return [k for k, m in marks.items()
                    if k not in stamped and k not in ids
                    and m.get("snooze_until", 0) <= now
                    and now - max(m.get("last_listed", 0),
                                  m.get("updated", 0)) > RETENTION_S]

        if not stale(self.marks) and not (prune and expired(self.marks)):
            return  # nothing to do: skip the lock and the disk

        def change(marks):
            todo = stale(marks)
            for key in todo:
                marks[key]["last_listed"] = now
            gone = expired(marks) if prune else []
            for key in gone:
                del marks[key]
            return bool(todo or gone)
        self._transaction(change)

    # -- queries --
    def is_hidden(self, item_id, now=None):
        now = self.clock() if now is None else now
        mark = self.marks.get(item_id)
        if not mark:
            return False
        if "done" in mark or "ack" in mark or "rsvp" in mark:
            return True
        return mark.get("snooze_until", 0) > now

    def is_saved(self, item_id):
        return "saved" in (self.marks.get(item_id) or {})

    def rsvp_answer(self, item_id):
        return (self.marks.get(item_id) or {}).get("rsvp")

    def snoozed_until(self, item_id, now=None):
        now = self.clock() if now is None else now
        until = (self.marks.get(item_id) or {}).get("snooze_until", 0)
        return until if until > now else None

    def visible(self, rows, now=None):
        """Items that are not done, acked or still snoozed."""
        now = self.clock() if now is None else now
        return [r for r in rows if not self.is_hidden(r["id"], now)]

    # -- housekeeping --
    def _cap(self):
        """Bound the file size on every write (never by age; see the module
        notes): drop the least recently listed marks past MAX_ENTRIES."""
        if len(self.marks) > MAX_ENTRIES:
            oldest = sorted(self.marks, key=lambda k: max(
                self.marks[k].get("last_listed", 0),
                self.marks[k].get("updated", 0)))
            for key in oldest[: len(self.marks) - MAX_ENTRIES]:
                del self.marks[key]
