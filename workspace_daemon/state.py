"""Processed-item state, keyed by source item id.

The ledger is the only thing standing between a re-run and a duplicate note, so
it is written atomically and flushed after every item rather than once at the
end of a run: an hourly LaunchAgent on a laptop gets interrupted (sleep, reboot,
launchd timeout), and a run that dies halfway must not forget what it just did.
"""
import fcntl
import json
import os
import re
import stat
import time
from pathlib import Path

from .shell import log
from .time_utils import is_rfc3339_instant


class StateError(Exception):
    pass


class AlreadyRunning(Exception):
    pass


def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def ensure_private_file(path):
    """Migrate an existing sensitive runtime file to owner-only access."""
    path = Path(path)
    if path.exists():
        os.chmod(path, 0o600)
    return path


def state_file(base_dir):
    return Path(base_dir) / "state" / "processed.json"


def schedule_file(base_dir):
    return Path(base_dir) / "state" / "schedule.json"


def cursor_file(base_dir):
    return Path(base_dir) / "state" / "cursors.json"


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


def write_atomic(path, text, mode=None):
    """Write via a unique temp file + os.replace so a crash cannot truncate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    preserved_mode = path.stat().st_mode & 0o777 if path.exists() else None
    target_mode = mode if mode is not None else preserved_mode
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        create_mode = target_mode if target_mode is not None else 0o666
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            create_mode,
        )
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if target_mode is not None:
            # Apply permissions before replace so the destination is never
            # briefly visible with the process umask's broader default.
            os.chmod(tmp, target_mode)
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


def _directory_identity(directory, follow_symlinks=True):
    """Return the stable device/inode identity of a directory."""
    metadata = os.stat(directory, follow_symlinks=follow_symlinks)
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"not a directory: {directory}")
    return metadata.st_dev, metadata.st_ino


def _create_directory_beneath_pinned_parents(directory):
    """Create a canonical absolute directory without following new symlinks.

    ``directory`` has already been resolved against the symlinks that were part
    of the configured path at the start of the operation. Walk that canonical
    path from a pinned root descriptor and open every component with
    ``O_NOFOLLOW``. If an ancestor is renamed while we walk, later work remains
    attached to its original inode; if a symlink is inserted, opening it fails.
    The caller subsequently compares the resulting identity with the configured
    path again, so creation beneath a renamed ancestor cannot authorize a write.
    """
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError(f"directory must be absolute: {directory}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(directory.anchor, directory_flags | no_follow)
    try:
        for component in directory.parts[1:]:
            try:
                child_fd = os.open(
                    component,
                    directory_flags | no_follow,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o777, dir_fd=directory_fd)
                except FileExistsError:
                    # A concurrent creator is acceptable only if the component
                    # can now be opened as a real directory without following a
                    # symlink.
                    pass
                child_fd = os.open(
                    component,
                    directory_flags | no_follow,
                    dir_fd=directory_fd,
                )
            os.close(directory_fd)
            directory_fd = child_fd

        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise NotADirectoryError(f"not a directory: {directory}")
        return opened.st_dev, opened.st_ino
    finally:
        os.close(directory_fd)


def ensure_directory_identity(directory):
    """Return a vault's identity, securely creating it when it is absent."""
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError(f"directory must be absolute: {directory}")

    # Capture the configured path's canonical meaning before testing existence.
    # If a missing path is redirected after that observation, the descriptor
    # walk below follows the captured path and rejects newly introduced links.
    canonical = directory.resolve(strict=False)
    try:
        configured_identity = _directory_identity(directory)
    except FileNotFoundError:
        return _create_directory_beneath_pinned_parents(canonical)

    # Existing configured symlinks remain supported, but a retarget between
    # resolution and identity capture must not silently redefine the vault.
    canonical_identity = _directory_identity(canonical, follow_symlinks=False)
    if configured_identity != canonical_identity:
        raise OSError(f"directory changed while resolving: {directory}")
    return configured_identity


def _open_directory_fd(directory, create=False, expected_identity=None):
    """Open and verify a canonical directory, returning a pinned descriptor.

    The caller supplies an immutable, already-resolved path. Do not resolve it
    again here: a rename-plus-symlink swap between those two resolutions would
    bless the attacker's new target. Direct open preserves ordinary pathname
    semantics for execute-only parent directories. Comparing the directory's
    device/inode identity before and after open catches changed intermediate
    components without depending on pathname casing or platform-specific fd
    paths; O_NOFOLLOW covers the final component. Once verified,
    descriptor-relative operations cannot be redirected by later changes.
    """
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError(f"directory must be absolute: {directory}")
    if create:
        directory.mkdir(parents=True, exist_ok=True)
    if expected_identity is None:
        expected_identity = _directory_identity(
            directory, follow_symlinks=False
        )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(
        directory,
        directory_flags | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(directory_fd)
        if (opened.st_dev, opened.st_ino) != expected_identity:
            raise OSError(
                f"directory changed while opening: {directory}"
            )
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def write_atomic_at(directory, filename, text, mode, expected_identity=None):
    """Atomically write one direct child of an already-resolved directory.

    The directory is held open throughout creation and replacement, so a
    symlink cannot redirect the destination between containment validation and
    the durable write.
    """
    if not filename or Path(filename).name != filename or filename in {".", ".."}:
        raise ValueError(f"filename must be one direct path component: {filename!r}")

    directory = Path(directory)
    directory_fd = _open_directory_fd(
        directory,
        create=expected_identity is None,
        expected_identity=expected_identity,
    )
    tmp_name = f".{filename}.{os.getpid()}.tmp"
    replaced = False
    try:
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory_fd,
        )
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w") as file:
                fd = None
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
        finally:
            if fd is not None:
                os.close(fd)
        os.replace(
            tmp_name,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
    except BaseException:
        try:
            os.unlink(tmp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        if replaced:
            try:
                os.fsync(directory_fd)
            except OSError as exc:
                log(
                    f"WARN wrote {directory / filename} but could not fsync "
                    f"its directory: {exc}"
                )
        os.close(directory_fd)


TEMP_MAX_AGE_SECONDS = 3600

# The exact shape write_atomic produces: `.<real name>.<pid>.tmp`.
TEMP_NAME = re.compile(r"^\..+\.\d+\.tmp$")


def sweep_temp_files(directory, max_age=TEMP_MAX_AGE_SECONDS):
    """Delete orphaned write_atomic temp files.

    SIGTERM and SIGKILL unwind nothing, so the cleanup in write_atomic never
    runs when launchd stops a job mid-write. The real file is untouched in that
    case, but the temp lingers; without this they accumulate silently over
    months of interrupted hourly runs. The age guard keeps this from racing a
    write that is genuinely in flight.
    """
    directory = Path(directory)
    try:
        expected_identity = _directory_identity(directory)
        resolved_directory = directory.resolve(strict=True)
        directory_fd = _open_directory_fd(
            resolved_directory,
            expected_identity=expected_identity,
        )
    except (FileNotFoundError, NotADirectoryError):
        return 0
    now = time.time()
    removed = 0
    try:
        for name in os.listdir(directory_fd):
            # Only our own shape: `.<real name>.<pid>.tmp`. A broad suffix match
            # also catches editor swap files and sync-conflict artifacts, and
            # deleting somebody else's data is worse than leaving a stale temp.
            if not TEMP_NAME.match(name):
                continue
            try:
                metadata = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if now - metadata.st_mtime < max_age:
                    continue
                os.unlink(name, dir_fd=directory_fd)
                removed += 1
            except OSError:
                continue
    finally:
        os.close(directory_fd)
    if removed:
        log(f"swept {removed} orphaned temp file(s) from {resolved_directory}")
    return removed


def _serialize(entries):
    return json.dumps(entries, indent=2, sort_keys=True) + "\n"


def save(base_dir, entries):
    write_atomic(state_file(base_dir), _serialize(entries), mode=0o600)


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
        if not dry_run:
            ensure_private_dir(self.path.parent)
            ensure_private_file(self.path)
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
        write_atomic(self.path, _serialize(merged), mode=0o600)
        self.entries = merged

    def record_resolving(self, item_id, entry, source_id):
        """Record success and atomically clear failed versions of one source.

        Versioned sources may produce a new candidate id after their content is
        corrected while retaining one stable source id. Keeping an older
        rejection in that case would leave status permanently red even though
        the source was later captured successfully.
        """
        if self.dry_run:
            return
        merged = {
            key: value
            for key, value in self.entries.items()
            if not (
                key != item_id
                and source_id is not None
                and (
                    value.get("calendar_match_rejected")
                    or value.get("memory_error")
                    or value.get("expand_fallback")
                )
                and (
                    value.get("memory_source_id") == source_id
                    or value.get("source_id") == source_id
                )
            )
        }
        merged[item_id] = entry
        write_atomic(self.path, _serialize(merged), mode=0o600)
        self.entries = merged


class ScheduleStore:
    """Small durable record of when each routine was last attempted.

    Cadence is based on attempts, not successes: a broken external dependency
    should be retried on the routine's declared interval rather than on every
    coordinator tick. Manual `run` commands never update this state.
    """

    def __init__(self, base_dir, dry_run=False):
        self.path = schedule_file(base_dir)
        self.dry_run = dry_run
        if not dry_run:
            ensure_private_dir(self.path.parent)
            ensure_private_file(self.path)
        self.entries = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise StateError(f"{self.path} is not valid schedule state: {exc}") from exc
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise StateError(f"{self.path} must contain an object of routine records")
        return data

    def due(self, routine, now=None):
        from . import config  # avoid a module cycle at import time

        now = time.time() if now is None else float(now)
        last = (self.entries.get(routine["id"]) or {}).get("last_attempted_epoch")
        if not isinstance(last, (int, float)):
            return True
        elapsed = now - float(last)
        # Wall clocks can jump backwards after sleep or time synchronization.
        # Treat that as due rather than freezing the routine until the future
        # timestamp is reached again.
        return (
            elapsed < 0
            or config.next_due_epoch(routine, float(last), now) <= now
        )

    def mark_attempted(self, routine_ids, now=None):
        if self.dry_run:
            return
        from .shell import utc_now_iso

        now = time.time() if now is None else float(now)
        # Capture and long-running census ticks use separate process locks.
        # Serialize their short schedule updates and reload inside the lock so
        # neither process can overwrite the other's newer routine timestamps.
        lock_path = self.path.with_name("schedule.lock")
        ensure_private_dir(lock_path.parent)
        with os.fdopen(
            os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "r+"
        ) as handle:
            os.chmod(lock_path, 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            merged = dict(self._load())
            stamp = utc_now_iso()
            for rid in routine_ids:
                merged[rid] = {
                    "last_attempted_at": stamp,
                    "last_attempted_epoch": now,
                }
            write_atomic(self.path, _serialize(merged), mode=0o600)
            self.entries = merged


class CursorStore:
    """Durable last-successful source scans for queue-style catch-up.

    The checkpoint is the instant a scan started, not when it finished. Messages
    arriving during a long run therefore remain newer than the checkpoint and
    are eligible next time. Callers add an overlap when reading; the processed
    ledger absorbs that repeated source slice.
    """

    def __init__(self, base_dir, dry_run=False):
        self.path = cursor_file(base_dir)
        self.dry_run = dry_run
        if not dry_run:
            ensure_private_dir(self.path.parent)
            ensure_private_file(self.path)
        self.entries = self._load()

    @staticmethod
    def key(routine_id, source_id):
        return f"{routine_id}:{source_id}"

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise StateError(f"{self.path} is not valid cursor state: {exc}") from exc
        if not isinstance(data, dict):
            raise StateError(f"{self.path} must contain an object of source records")
        for key, record in data.items():
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("kind"), str)
                or not record["kind"]
                or not is_rfc3339_instant(record.get("last_successful_scan_at"))
            ):
                raise StateError(
                    f"{self.path}: cursor record {key!r} must contain a "
                    "non-empty kind and RFC3339 last_successful_scan_at"
                )
        return data

    def checkpoint(self, routine_id, source_id, kind):
        key = self.key(routine_id, source_id)
        record = self.entries.get(key)
        if record is None:
            return None
        if record.get("kind") != kind:
            raise StateError(
                f"{self.path}: cursor record {key!r} has kind "
                f"{record.get('kind')!r}, expected {kind!r}"
            )
        value = record.get("last_successful_scan_at")
        return value

    def mark_successful(self, sources, checkpoint):
        """Advance several source cursors in one atomic state write."""
        self.mark_successful_at([
            (*source, checkpoint)
            for source in sources
        ])

    def mark_successful_at(self, sources):
        """Atomically advance source cursors that have distinct checkpoints."""
        if self.dry_run or not sources:
            return
        merged = dict(self.entries)
        for routine_id, source_id, kind, checkpoint in sources:
            merged[self.key(routine_id, source_id)] = {
                "kind": kind,
                "last_successful_scan_at": checkpoint,
            }
        write_atomic(self.path, _serialize(merged), mode=0o600)
        self.entries = merged


class RunLock:
    """Exclusive lock for the duration of a run.

    launchd will not overlap a StartInterval job with itself, but a manual
    `daemon.py run` alongside the scheduled one will. Two concurrent runs both
    query the same queue, both see the same unprocessed item, and both
    summarize it — duplicate notes and double the LLM spend — while their
    whole-snapshot ledger writes clobber each other.
    """

    def __init__(self, base_dir, name="run"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(name)):
            raise ValueError(f"invalid run-lock name {name!r}")
        self.path = Path(base_dir) / "state" / f"{name}.lock"
        self._fh = None

    def __enter__(self):
        ensure_private_dir(self.path.parent)
        for _ in range(5):
            # O_RDWR|O_CREAT, never "w": open(path, "w") truncates before the
            # lock is even attempted, so a losing contender would erase the
            # holder's pid — the one diagnostic this file carries.
            fh = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
            os.chmod(self.path, 0o600)
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
