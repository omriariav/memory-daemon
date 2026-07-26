"""Cached Gmail user-label catalog.

The catalog is ~940 names and changes maybe monthly, but every run that applies
labels was fetching it — about a second of an hourly job spent re-learning
something that almost never moves.

The trap with any TTL is staleness: create a label, reference it in a routine,
and get a false "does not exist in Gmail" until the cache expires. That is the
same shape as a bug this project already shipped once, so a miss is
self-healing — `resolve()` refetches before reporting a name as unknown. The
only case that pays the refetch cost is the one where the cache is provably out
of date.
"""
import json
import time
from pathlib import Path

from . import gmail, state
from .shell import log

DEFAULT_TTL_SECONDS = 14 * 24 * 3600


def cache_file(base_dir):
    return Path(base_dir) / "state" / "labels.json"


class Catalog:
    """User-label names, read through a TTL cache and refetched on a miss."""

    def __init__(self, base_dir, ttl=DEFAULT_TTL_SECONDS, force_refresh=False,
                 read_only=False):
        self.path = cache_file(base_dir)
        self.ttl = ttl
        # A dry run promises no state write, and it deliberately runs without the
        # run lock, so it must not persist anything.
        self.read_only = read_only
        self._names = None
        self._fetched_at = 0.0
        self._announced = False
        if not force_refresh:
            self._load()

    # --- cache io ---------------------------------------------------------

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            names, fetched_at = data["labels"], float(data["fetched_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return  # unreadable or malformed: treat as absent, refetch on use
        if not isinstance(names, list):
            return
        self._names, self._fetched_at = names, fetched_at

    def _store(self):
        if self.read_only:
            return
        try:
            state.write_atomic(
                self.path,
                json.dumps({"fetched_at": self._fetched_at, "labels": self._names},
                           indent=2, sort_keys=True) + "\n",
            )
        except OSError as exc:
            # A cache is an optimisation; failing to persist it must not fail a run.
            log(f"WARN could not write the label cache: {exc}")

    @property
    def age(self):
        # A backwards clock jump (laptop resume before NTP) would make age
        # negative and freeze the cache; treat that as expired instead.
        return abs(time.time() - self._fetched_at)

    def _expired(self):
        return self._names is None or self.age > self.ttl

    # --- public api -------------------------------------------------------

    def refresh(self):
        self._names = gmail.user_labels()
        self._fetched_at = time.time()
        self._announced = True
        self._store()
        log(f"fetched {len(self._names)} user labels")
        return self._names

    def names(self):
        """The catalog, refetching only when the cache is missing or stale."""
        if self._expired():
            return self.refresh()
        if not self._announced:
            # names() is called once per label lookup; say this once per run.
            log(f"using {len(self._names)} cached user labels "
                f"(age {self.age / 3600:.0f}h, ttl {self.ttl / 3600:.0f}h)")
            self._announced = True
        return self._names

    def resolve(self, name):
        """Canonical casing for `name`, or None. Refetches once before giving up.

        A miss is the one reliable signal that the cache is behind, so it is
        also the one case where paying for a fetch is clearly worth it.
        """
        before = self._fetched_at
        match = self._match(name, self.names())
        if match:
            return match
        if self._fetched_at != before:
            # names() just fetched (cold or expired cache), so the catalog is
            # already current — the name is genuinely absent, not stale.
            return None
        log(f"label {name!r} not in the cached catalog — refreshing to be sure")
        return self._match(name, self.refresh())

    @staticmethod
    def _match(name, names):
        return {n.lower(): n for n in names}.get(name.lower())
