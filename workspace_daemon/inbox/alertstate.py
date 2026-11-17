"""Read-only recovery signal for daemon alerts (rules in docs/inbox.md).

The daemon keeps its own record of which alert conditions are active:
`state/alerts.json` maps each active alert key to the time it was sent (or to
`{"pending": time}` while a send is unconfirmed), and `alerts.check` drops a
key on the first tick that finds its condition cleared. So an alert whose key
is no longer in that file has recovered, for every alert kind and every
routine kind (maintenance routines included), and the daemon, not this
module, decides what "recovered" means. A key that is still there is active
even if the alert's wording changed.

Everything here is best effort. The live daemon may be a different code
version, so a missing file, a read error or any unfamiliar shape means
"unknown": `Recovery` answers False and the caller keeps only the age rule.
Nothing is written, locked or raised.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ALERT_FILE = "alerts.json"
MAX_BYTES = 1024 * 1024
# The file is written before an alert is sent, so a file older than the alert
# cannot say anything about it (a copy, a clock jump): slack in seconds.
MTIME_SLACK_S = 2.0
_UNSET = object()


def _valid(value):
    return ((isinstance(value, str) and bool(value))
            or (isinstance(value, dict) and isinstance(value.get("pending"), str)
                and bool(value["pending"])))


def _epoch(value):
    """Epoch seconds of a key's value (its time, or its pending time), or None."""
    if isinstance(value, dict):
        value = value.get("pending")
    if not isinstance(value, str):
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


class Recovery:
    """Lazy lookup; the file is only read when an alert needs it."""

    def __init__(self, base_dir):
        self.path = Path(base_dir) / "state" / ALERT_FILE
        self._state = _UNSET

    def _read(self):
        """({active key: since epoch or None}, file mtime), or None when the
        state is unknown."""
        try:
            with open(self.path, "rb") as handle:
                mtime = os.fstat(handle.fileno()).st_mtime
                data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                return None
            doc = json.loads(data.decode("utf-8"))
            if not isinstance(doc, dict) or not all(
                    isinstance(k, str) and _valid(v) for k, v in doc.items()):
                return None
            return {k: _epoch(v) for k, v in doc.items()}, mtime
        except Exception:  # noqa: BLE001 - missing, unreadable or unknown shape
            return None

    def resolved(self, key, ts):
        """True when the daemon no longer lists `key` as active."""
        if self._state is _UNSET:
            self._state = self._read()
        if self._state is None:
            return False
        active, mtime = self._state
        return mtime >= ts - MTIME_SLACK_S and key not in active

    def active_since(self):
        """`{key: since epoch}` of the keys the daemon lists as active.

        A key whose time is unreadable, or later than the file's own mtime (a
        copy, a clock jump), is left out; an unknown state gives `{}`.
        """
        if self._state is _UNSET:
            self._state = self._read()
        if self._state is None:
            return {}
        active, mtime = self._state
        return {key: since for key, since in active.items()
                if since is not None and mtime >= since - MTIME_SLACK_S}
