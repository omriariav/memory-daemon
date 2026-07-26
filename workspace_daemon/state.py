"""Processed-item state, keyed by source item id.

The ledger is the only thing standing between a re-run and a duplicate note, so
it is written atomically and flushed after every item rather than once at the
end of a run: an hourly LaunchAgent on a laptop gets interrupted (sleep, reboot,
launchd timeout), and a run that dies halfway must not forget what it just did.
"""
import fcntl
import json
import os
from pathlib import Path


class StateError(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def state_file(base_dir):
    return Path(base_dir) / "state" / "processed.json"


def load(base_dir):
    path = state_file(base_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        # Loading happens before the per-routine exception boundary, so a
        # corrupt ledger would otherwise abort every routine with a bare
        # traceback. Say what is wrong and what deleting it would cost.
        raise StateError(f"{path} is not valid JSON ({exc}). {_REPAIR_HINT}") from exc
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc}") from exc

    # Shape matters as much as syntax: `[]`, `null` or a stray string all parse
    # cleanly and then fail much later with an unhelpful AttributeError.
    if not isinstance(data, dict):
        raise StateError(
            f"{path} must contain a JSON object, found {type(data).__name__}. {_REPAIR_HINT}"
        )
    malformed = [k for k, v in data.items() if not isinstance(v, dict)]
    if malformed:
        raise StateError(
            f"{path}: every entry must be an object; malformed keys: "
            f"{', '.join(malformed[:5])}. {_REPAIR_HINT}"
        )
    return data


_REPAIR_HINT = (
    "Repair it by hand if you can — deleting it makes every item look "
    "unprocessed, so the next run will re-summarize and re-triage everything "
    "its queries still match."
)


def _fsync_dir(path):
    """Durably commit a rename. Syncing the file alone does not survive power loss."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, text):
    """Write via a unique temp file + os.replace so a crash cannot truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp, mode)  # replacing must not widen the file's permissions
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _serialize(entries):
    return json.dumps(entries, indent=2, sort_keys=True) + "\n"


def save(base_dir, entries):
    write_atomic(state_file(base_dir), _serialize(entries))


def last_run(base_dir, routine_id):
    """Most recent processed_at for a routine, or None if it never processed anything."""
    stamps = [
        entry.get("processed_at")
        for entry in load(base_dir).values()
        if entry.get("rule_id") == routine_id and entry.get("processed_at")
    ]
    return max(stamps) if stamps else None


class Store:
    """The processed ledger, persisted as each item completes."""

    def __init__(self, base_dir, dry_run=False):
        self.path = state_file(base_dir)
        self.dry_run = dry_run
        self.entries = load(base_dir)

    def __contains__(self, item_id):
        return item_id in self.entries

    def __len__(self):
        return len(self.entries)

    def get(self, item_id):
        return self.entries.get(item_id)

    def items(self):
        return list(self.entries.items())

    def record(self, item_id, entry):
        """Persist first, commit to memory only on success.

        Mutating the dict before the write means a failed write is silently
        committed by the next item's successful one — an entry lands in the
        ledger for work whose follow-up never ran.
        """
        if self.dry_run:
            return
        merged = dict(self.entries)
        merged[item_id] = entry
        write_atomic(self.path, _serialize(merged))
        self.entries = merged


class RunLock:
    """Exclusive lock for the duration of a run.

    launchd will not overlap a StartInterval job with itself, but a manual
    `daemon.py run` alongside the scheduled one will. Two concurrent runs both
    query the same queue, both see the same unprocessed item, and both
    summarize it — duplicate notes and double the LLM spend — while their
    whole-snapshot ledger writes clobber each other.
    """

    def __init__(self, base_dir):
        self.path = Path(base_dir) / "state" / "run.lock"
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise AlreadyRunning(
                f"another run holds {self.path}; skipping this one"
            )
        self._fh.write(f"{os.getpid()}\n")
        self._fh.flush()
        return self

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False
