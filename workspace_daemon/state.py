"""Processed-item state, keyed by source item id.

The ledger is the only thing standing between a re-run and a duplicate note, so
it is written atomically and flushed after every item rather than once at the
end of a run: an hourly LaunchAgent on a laptop gets interrupted (sleep, reboot,
launchd timeout), and a run that dies halfway must not forget what it just did.
"""
import fcntl
import json
import os
import time
from pathlib import Path

from .shell import log


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
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise

    # Durability of the rename itself. The content is already visible at `path`,
    # so a failure here only weakens the power-loss guarantee — it must NOT be
    # raised, or the caller would treat a completed write as "did not happen"
    # and later overwrite the new file with its older in-memory state.
    try:
        _fsync_dir(path.parent)
    except OSError as exc:
        log(f"WARN wrote {path} but could not fsync its directory: {exc}")


TEMP_MAX_AGE_SECONDS = 3600


def sweep_temp_files(directory, max_age=TEMP_MAX_AGE_SECONDS):
    """Delete orphaned write_atomic temp files.

    SIGTERM and SIGKILL unwind nothing, so the cleanup in write_atomic never
    runs when launchd stops a job mid-write. The real file is untouched in that
    case, but the temp lingers; without this they accumulate silently over
    months of interrupted hourly runs. The age guard keeps this from racing a
    write that is genuinely in flight.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return 0
    now = time.time()
    removed = 0
    for tmp in directory.glob(".*.tmp"):
        try:
            if now - tmp.stat().st_mtime < max_age:
                continue
            tmp.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        log(f"swept {removed} orphaned temp file(s) from {directory}")
    return removed


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
        for _ in range(5):
            # O_RDWR|O_CREAT, never "w": open(path, "w") truncates before the
            # lock is even attempted, so a losing contender would erase the
            # holder's pid — the one diagnostic this file carries.
            fh = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                holder = fh.read().strip() or "unknown pid"
                fh.close()
                raise AlreadyRunning(
                    f"another run (pid {holder}) holds {self.path}; skipping this one"
                )
            # flock binds to the inode, not the path. If the lock file was
            # deleted or replaced between open and flock, we now hold an
            # exclusive lock on an orphan inode and guard nothing at all.
            try:
                same = os.fstat(fh.fileno()).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                self._fh = fh
                fh.truncate(0)
                fh.write(f"{os.getpid()}\n")
                fh.flush()
                return self
            fh.close()  # raced with a delete/replace — retry on the new inode
        raise AlreadyRunning(f"{self.path} kept changing underneath us; skipping this run")

    def still_held(self):
        """True while our lock still guards the path we took it on.

        Once the lock file is deleted our flock survives only on an orphan inode,
        and any other run is then free to create the path and lock it. There is
        no rendezvous left to defend, so the honest move is for the holder to
        notice and stop rather than race on.
        """
        if not self._fh:
            return False
        try:
            return os.fstat(self._fh.fileno()).st_ino == os.stat(self.path).st_ino
        except OSError:
            return False

    def check(self):
        if not self.still_held():
            raise AlreadyRunning(
                f"{self.path} was deleted or replaced mid-run; stopping so a "
                f"concurrent run cannot double-process"
            )

    def __exit__(self, *exc):
        if self._fh:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False
