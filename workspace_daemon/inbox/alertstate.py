"""Read-only recovery signals for daemon alerts (rules in docs/inbox.md).

An `attention:<routine>` alert is resolved once the run log
(`logs/run.log`, parsed by `status.read_tick_history`) shows the routine's
latest result as an "ok" tick later than the alert. That is the one success
record every routine kind has, including maintenance routines such as
calendar-cache and sheet-watch: it comes from the scheduler tick, not from the
capture ledger. A `connector:<server>` alert is resolved once the Claude usage
log (`state/claude-usage.jsonl`) holds a later successful call that used the
connector.

Everything here is best effort. The live daemon may be a different code
version than this checkout, so a missing file, an unknown shape or any error
means "unknown": `Recovery` answers False and the caller keeps only the age
rule. Nothing is written, locked or raised.
"""
import datetime
import re
from pathlib import Path

# `status` issues that one clean tick clears. Ledger issues ("2 memory sink",
# "1 Gmail triage", ...) clear only when the ledger changes, so a later ok
# tick does not prove them fixed.
TICK_ISSUE = re.compile(r"^(?:\d+ last-run errors?|last run|incomplete|overlap)$")
_UNSET = object()


def _epoch(value):
    """ISO 8601 text to epoch seconds, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
    return stamp.timestamp()


class Recovery:
    """Lazy lookups; a source is only read when an alert needs it."""

    def __init__(self, base_dir, now):
        self.base_dir = Path(base_dir)
        self.now = now
        self._routines = _UNSET
        self._connectors = _UNSET

    def routine_ok_at(self, routine):
        """Epoch of the routine's latest result when that result is ok."""
        if self._routines is _UNSET:
            self._routines = self._read_routines()
        return (self._routines or {}).get(routine)

    def connector_ok_at(self, server):
        """Epoch of the connector's latest successful call."""
        if self._connectors is _UNSET:
            self._connectors = self._read_connectors()
        return (self._connectors or {}).get(server)

    def _read_routines(self):
        try:
            from .. import status
            history = status.read_tick_history(self.base_dir / "logs" / "run.log")
            results = history["routines"]
            if not isinstance(results, dict):
                return None
            out = {}
            for routine, result in results.items():
                if (isinstance(routine, str) and isinstance(result, dict)
                        and result.get("state") == "ok"):
                    at = _epoch(result.get("at"))
                    if at is not None:
                        out[routine] = at
            return out
        except Exception:  # noqa: BLE001 - unknown shape or version: no signal
            return None

    def _read_connectors(self):
        try:
            from .. import claude_bridge, status
            entries = claude_bridge.recent_usage(self.base_dir, now=self.now)
            ok = status.connector_times(entries)["ok"]
            if not isinstance(ok, dict):
                return None
            return {server: float(at) for server, at in ok.items()
                    if isinstance(server, str)
                    and isinstance(at, (int, float))
                    and not isinstance(at, bool)}
        except Exception:  # noqa: BLE001 - unknown shape or version: no signal
            return None

    def resolved(self, key, details, ts):
        """True when a newer success shows the alert's condition is over."""
        details = details if isinstance(details, dict) else {}
        if key.startswith("attention:"):
            issues = details.get("issues")
            if not (isinstance(issues, list) and issues
                    and all(isinstance(i, str) and TICK_ISSUE.match(i)
                            for i in issues)):
                return False
            routine = details.get("routine")
            if not isinstance(routine, str) or not routine:
                routine = key[len("attention:"):]
            ok = self.routine_ok_at(routine)
        elif key.startswith("connector:"):
            server = details.get("connector")
            if not isinstance(server, str) or not server:
                server = key[len("connector:"):]
            ok = self.connector_ok_at(server)
        else:
            return False
        return ok is not None and ok > ts
