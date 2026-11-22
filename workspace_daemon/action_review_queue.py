"""Queue of meeting action candidates awaiting the owner's review.

One JSON line per meeting, keyed by `meeting_entry_id` (the meeting's source id
when the entry id is unknown). The file is 0600, every change happens under an
exclusive flock on `<queue>.lock`, and a re-run never duplicates a meeting: an
identical row is skipped, a changed one replaces the old line in place.

Row schema (v1):

    {"v": 1, "meeting_entry_id", "review_todo_id", "source_id",
     "source_kind", "recording_id" (only when known), "title", "date", "at",
     "candidates": [{"text", "quote", "due" (only when set), "owner",
                     "status": "valid"|"ungrounded"|"invalid", "reason"}]}

Candidate text lives only here and in the review todo, never in logs.
"""
import fcntl
import json
import os
import tempfile

SCHEMA_VERSION = 1


def row_key(row):
    return row.get("meeting_entry_id") or row.get("source_id")


def build_row(*, meeting_entry_id, review_todo_id, source_id, source_kind,
              recording_id, title, date, at, candidates):
    row = {
        "v": SCHEMA_VERSION,
        "meeting_entry_id": meeting_entry_id,
        "review_todo_id": review_todo_id,
        "source_id": source_id,
        "source_kind": source_kind,
    }
    if recording_id:
        row["recording_id"] = recording_id
    row.update(title=title, date=date, at=at, candidates=candidates)
    return row


def _same(a, b):
    return ({k: v for k, v in a.items() if k != "at"}
            == {k: v for k, v in b.items() if k != "at"})


def upsert(path, row):
    """Add or replace the meeting's row. Returns "added", "replaced" or
    "unchanged". Raises OSError on I/O trouble (the caller counts it)."""
    key = row_key(row)
    if not key:
        raise ValueError("queue row needs a meeting entry id or source id")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lines, found = [], None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            pass
        for index, line in enumerate(lines):
            try:
                old = json.loads(line)
            except ValueError:
                continue  # kept verbatim, never matched
            if isinstance(old, dict) and row_key(old) == key:
                found = index
                if _same(old, row):
                    _private(path)
                    return "unchanged"
                break
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=False)
        if found is None:
            _append(path, encoded + "\n")
            return "added"
        lines[found] = encoded
        # A stray duplicate of the same key (hand edit) is dropped too.
        kept = [
            line for index, line in enumerate(lines)
            if index == found or not _is_key(line, key)
        ]
        _rewrite(path, directory, "\n".join(kept) + "\n")
        return "replaced"
    finally:
        os.close(lock_fd)  # closing also drops the flock


def _is_key(line, key):
    try:
        old = json.loads(line)
    except ValueError:
        return False
    return isinstance(old, dict) and row_key(old) == key


def _private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _append(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        start = os.fstat(fd).st_size
        data = text.encode("utf-8")
        written = os.write(fd, data)
        if written != len(data):
            os.ftruncate(fd, start)
            raise OSError(f"short write ({written} of {len(data)} bytes)")
    finally:
        os.close(fd)


def _rewrite(path, directory, text):
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".queue-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
