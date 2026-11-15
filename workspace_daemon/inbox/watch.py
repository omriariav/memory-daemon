"""Change detection for the inbox: stat-only checks, no file reads.

Each source depends on a few files. Every `watch_interval_s` the app asks
`Watcher.due()` which sources have a changed file (mtime or size, or a file
that appeared or vanished) and re-runs only those. A source with no files is
never due: it is refreshed by the fallback timer and by the manual key only.

`due()` does not commit: the caller commits what it actually started
(`commit`), so a change seen while a refresh was running is not lost, and a
file that keeps changing triggers at most one refresh per `debounce_s` per
source. Nothing here raises: a stat failure is just another signature.
"""
import os
import time

DEBOUNCE_S = 2.0
ERROR = ("error",)   # signature of a path that cannot be stat-ed (not "missing")


def file_signature(path, stat=os.stat):
    """`(mtime_ns, size)`, None when the path does not exist, `ERROR` on any
    other failure."""
    try:
        st = stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except (OSError, ValueError):
        return ERROR
    return (st.st_mtime_ns, st.st_size)


class Watcher:
    """`units` is a list of objects with `.name` and `.watch`, a callable
    returning the paths to watch (evaluated on every check, so a glob can
    pick up new files)."""

    def __init__(self, units, debounce_s=DEBOUNCE_S, clock=time.monotonic,
                 stat=os.stat):
        self.units = {u.name: u for u in units}
        self.debounce_s, self.clock, self._stat = debounce_s, clock, stat
        self.active = {name: True for name in self.units}
        self.committed = {}   # name -> signature at the last committed run
        self.last_run = {}    # name -> clock() at the last committed run
        self.seen = {}        # name -> signature seen by the last due() call

    def signature(self, name):
        try:
            paths = list(self.units[name].watch())
        except Exception:  # a broken watch callable never ends the session
            return ERROR
        return tuple(sorted((str(p), file_signature(p, self._stat))
                            for p in paths)) or None

    def set_active(self, name, active):
        """Inactive units are never due (the Saved view watches only while it
        is open). Turning one on takes its current state as the baseline."""
        if name in self.units:
            self.active[name] = bool(active)
            if active:
                self.commit([name])

    def due(self, now=None):
        """Names of the units whose files changed and whose debounce is over."""
        now = self.clock() if now is None else now
        out = []
        for name in self.units:
            if not self.active.get(name):
                continue
            sig = self.signature(name)
            if sig is None:           # nothing to watch
                continue
            self.seen[name] = sig
            if name not in self.committed:
                self.committed[name] = sig   # first look: the baseline
                continue
            if sig == self.committed[name]:
                continue
            if now - self.last_run.get(name, float("-inf")) < self.debounce_s:
                continue
            out.append(name)
        return out

    def commit(self, names=None, now=None):
        """Record that these units (default: all) were refreshed now, against
        their current signature. Anything that changes later is due again."""
        now = self.clock() if now is None else now
        for name in (self.units if names is None else names):
            if name not in self.units:
                continue
            sig = self.signature(name)
            if sig is not None:
                self.committed[name] = sig
            self.last_run[name] = now
