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
import re
import time
from pathlib import Path

from . import gmail, state
from .shell import log

DEFAULT_TTL_SECONDS = 14 * 24 * 3600
ID_REFRESH_GAP_SECONDS = 600
# Gmail's system label ids are their own names (INBOX, STARRED, CATEGORY_UPDATES);
# a user label's id is Label_<n>.
_SYSTEM_ID = re.compile(r"[A-Z][A-Z0-9_]*\Z")
_ids_tried = {}  # cache path -> monotonic time of the last id refetch


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
        self._ids = None
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
        ids = data.get("ids")
        if isinstance(ids, dict):
            self._ids = {
                k: v for k, v in ids.items()
                if isinstance(k, str) and isinstance(v, str) and v
            }

    def _store(self):
        if self.read_only:
            return
        try:
            state.write_atomic(
                self.path,
                json.dumps({"fetched_at": self._fetched_at,
                            "labels": self._names,
                            **({"ids": self._ids} if self._ids else {})},
                           indent=2, sort_keys=True) + "\n",
                mode=0o600,
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

    # --- label ids ---------------------------------------------------------

    def names_for_ids(self, ids):
        """Label names for Gmail label ids; ids that do not resolve are dropped.

        System ids map to themselves. A user id (`Label_<n>`) is looked up in
        the cached id map; a miss refetches the map once (at most every
        ID_REFRESH_GAP_SECONDS per process), so a label made today resolves
        and a deleted one is not refetched for every message.
        """
        found, missing = [], False
        for label_id in ids:
            label_id = str(label_id).strip()
            if (self._ids or {}).get(label_id):
                found.append(self._ids[label_id])
            elif _SYSTEM_ID.match(label_id):
                found.append(label_id)
            elif label_id:
                missing = True
        if missing and self._refresh_ids():
            return self.names_for_ids(ids)
        return found

    def _refresh_ids(self):
        key = str(self.path)
        last = _ids_tried.get(key)
        if last is not None and time.monotonic() - last < ID_REFRESH_GAP_SECONDS:
            return False
        _ids_tried[key] = time.monotonic()
        try:
            self._ids = gmail.user_label_ids()
        except Exception as exc:  # noqa: BLE001 - names are best effort
            log(f"WARN could not fetch the label id map: {type(exc).__name__}")
            return False
        if self._names is None:
            self._names = sorted(self._ids.values())
            self._fetched_at = time.time()
        self._store()
        return True
