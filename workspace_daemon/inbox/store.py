"""Local inbox state: done, snooze-until and ack per item id.

`<base>/state/inbox-state.json`, written atomically with mode 0600. Every
mutation re-reads the file first, so two inbox windows do not overwrite each
other's marks. Corrupt or unreadable state reads as empty and the broken file
is kept aside as `.bad` before the next write.
"""
import json
import os
import time
from pathlib import Path

from .. import state as daemon_state

FILE_NAME = "inbox-state.json"
SCHEMA_VERSION = 1
# A done/ack mark must outlive the longest event window, or the row would
# come back. Snoozes are kept until due, then like any other mark.
RETENTION_S = 14 * 86400
MAX_ENTRIES = 5000


class InboxStore:
    def __init__(self, base_dir=None, clock=time.time):
        """`base_dir=None` keeps marks in memory only (demo mode, tests)."""
        self.path = (Path(base_dir) / "state" / FILE_NAME
                     if base_dir is not None else None)
        self.clock = clock
        self.marks = {}
        self._corrupt = False
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
            for field in ("done", "ack", "snooze_until", "updated"):
                val = mark.get(field)
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    clean[field] = float(val)
            if clean:
                marks[key] = clean
        return marks, False

    def load(self):
        self.marks, self._corrupt = self._read()
        return self.marks

    def _save(self):
        self._prune()
        if self.path is None:
            return
        if self._corrupt:
            try:
                os.replace(self.path, str(self.path) + ".bad")
            except OSError:
                pass
            self._corrupt = False
        daemon_state.ensure_private_dir(self.path.parent)
        text = json.dumps({"v": SCHEMA_VERSION, "items": self.marks},
                          indent=1, sort_keys=True)
        daemon_state.write_atomic(self.path, text, mode=0o600)

    def _mutate(self, item_id, change):
        self.marks, self._corrupt = self._read()
        mark = dict(self.marks.get(item_id, {}))
        change(mark)
        mark["updated"] = self.clock()
        self.marks[item_id] = mark
        self._save()

    # -- marks --
    def done(self, item_id):
        self._mutate(item_id, lambda m: m.update(done=self.clock()))

    def ack(self, item_id):
        self._mutate(item_id, lambda m: m.update(ack=self.clock()))

    def snooze(self, item_id, until):
        self._mutate(item_id, lambda m: m.update(snooze_until=float(until)))

    def restore(self, item_id):
        """Drop every mark (undo)."""
        self.marks, self._corrupt = self._read()
        if self.marks.pop(item_id, None) is not None:
            self._save()

    # -- queries --
    def is_hidden(self, item_id, now=None):
        now = self.clock() if now is None else now
        mark = self.marks.get(item_id)
        if not mark:
            return False
        if "done" in mark or "ack" in mark:
            return True
        return mark.get("snooze_until", 0) > now

    def snoozed_until(self, item_id, now=None):
        now = self.clock() if now is None else now
        until = (self.marks.get(item_id) or {}).get("snooze_until", 0)
        return until if until > now else None

    def visible(self, rows, now=None):
        """Items that are not done, acked or still snoozed."""
        now = self.clock() if now is None else now
        return [r for r in rows if not self.is_hidden(r["id"], now)]

    # -- housekeeping --
    def _prune(self, now=None):
        now = self.clock() if now is None else now
        for key in [k for k, m in self.marks.items()
                    if m.get("snooze_until", 0) <= now
                    and now - m.get("updated", 0) > RETENTION_S]:
            del self.marks[key]
        if len(self.marks) > MAX_ENTRIES:
            oldest = sorted(self.marks, key=lambda k:
                            self.marks[k].get("updated", 0))
            for key in oldest[: len(self.marks) - MAX_ENTRIES]:
                del self.marks[key]
