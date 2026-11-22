"""WhatsApp messages from a local forwarder spool, one memory per chat-day.

The daemon never reads WhatsApp. A separate local forwarder writes new
messages, and copies of their media files, as numbered *batches* into a
private spool folder (``b<seq>-<stamp>/batch.json`` plus ``media/``, format
``whatsapp-forwarder-spool`` version 1) and deletes batches after a few days
whether or not anyone read them. This adapter is a spool *consumer*:

* Ingest (during listing). New batches are read in name order. Every message
  is upserted by its stable ``id`` into a private, durable archive
  (``archive_dir``): ``chats/chat-<chat_key>/<date>.json`` holds the merged
  records of one chat-day, and each spooled file with status ``copied`` is
  copied to ``media/<date>/<message id><ext>`` (0600, directories 0700). The
  last finished batch is remembered, so a restart resumes after it. Because
  copies and records land in the archive before the cursor moves, and long
  before any ledger entry exists, the spool's pruning can never lose a file
  or shrink a conversation that was already seen. The spool itself is never
  written.
* Capture unit. One candidate per chat per local day (the spool's
  timezone). Its memory source id ``whatsapp:chat-<chat_key>:<date>`` is
  opaque (the forwarder's chat key, never a phone number or JID), so later
  messages, a late file (``update: "media"``) or a re-sent duplicate update
  the same memory entry. The candidate id adds a digest of the chat-day's
  content, so only changed content is reconsidered.
* Quiet period. A chat-day is offered for analysis only once it had no new
  or changed message for ``quiet_minutes``, or once its oldest uncaptured
  change has waited ``max_wait_hours``; until then it stays listed as
  deferred so no other routine claims it.
* Text from media, locally. Voice notes and audio are transcribed with
  whisper.cpp (``whisper-cli``) after ffmpeg converts them to 16 kHz mono
  WAV; PDFs give a bounded ``pdftotext`` excerpt. Results are cached next to
  the stored file; any failure degrades to "no transcript".

Logs carry counts, opaque ids and error classes only: never message text,
names, chat labels, JIDs or file names.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import time
from pathlib import Path

from . import state
from .chat_text import redact_secrets
from .shell import log, optional_bin, run

FORMAT = "whatsapp-forwarder-spool"
VERSION = 1
ARCHIVE_VERSION = 1

BATCH_RE = re.compile(r"^b[0-9]{10}-[0-9]{8}T[0-9]{6}Z$")
MESSAGE_ID_RE = re.compile(r"^wa1-[0-9a-f]{40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# media.path is relative to the spool: "<batch>/media/wa-<row key>.<ext>".
MEDIA_PATH_RE = re.compile(
    r"^(?P<batch>b[0-9]{10}-[0-9]{8}T[0-9]{6}Z)/media/"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,120})$"
)
EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
MEDIA_KINDS = ("image", "video", "audio", "document", "gif", "sticker")
SPOOL_STATUSES = (
    "copied", "not_selected", "too_large", "not_downloaded", "unreadable",
    "blocked_type",
)
UNKNOWN_SENDER_RE = re.compile(r"^Unknown(?:-\d{1,6})?$")

DEFAULTS = {
    "quiet_minutes": 60,
    "max_wait_hours": 6,
    "copy_media": True,
    "max_file_mb": 100,
    "max_total_mb": 10240,
    "transcribe_audio": False,
    "whisper_model": None,
    "whisper_language": "auto",
    "whisper_threads": None,
    "transcribe_timeout_seconds": 900,
    "transcript_max_chars": 8000,
    "pdf_text": True,
    "pdf_max_pages": 20,
    "pdf_max_chars": 4000,
    "max_body_chars": 60000,
    "chat_keys": [],
    "exclude_chat_keys": [],
    "chat_labels": [],
    "exclude_chat_labels": [],
}
SOURCE_KEYS = set(DEFAULTS) | {"spool_dir", "archive_dir"}

# Text from media. Each extractor maps a stored file to (text, None) or
# (None, reason). A later image or video description step (a vision model)
# plugs in here as another entry, e.g. "image": _describe_image, and is
# cached and rendered exactly like a transcript.
_CACHE_SUFFIX = {"transcript": ".transcript.txt", "pdf": ".pdf.txt"}


def _now():
    return time.time()


def settings(source):
    cfg = dict(DEFAULTS)
    cfg.update({key: value for key, value in source.items() if key in DEFAULTS})
    cfg["spool_dir"] = source.get("spool_dir")
    cfg["archive_dir"] = source.get("archive_dir")
    return cfg


# --- validation ----------------------------------------------------------------

def _bounded_number(value, lower, upper, integer=True):
    if isinstance(value, bool):
        return False
    kinds = (int,) if integer else (int, float)
    return isinstance(value, kinds) and lower <= value <= upper


def _inside(child, parent):
    try:
        Path(child).relative_to(parent)
    except ValueError:
        return False
    return True


def _git_tree(path):
    """The git work tree containing ``path`` (or its nearest parent), else None."""
    current = Path(path)
    while not current.exists() and current != current.parent:
        current = current.parent
    for directory in [current, *current.parents]:
        if (directory / ".git").exists():
            return directory
    return None


def _private_dir_problem(path, what):
    """Why an existing directory is unsafe to use, else None."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{what} cannot be inspected ({type(exc).__name__})"
    if stat.S_ISLNK(info.st_mode):
        return f"{what} must not be a symlink"
    if not stat.S_ISDIR(info.st_mode):
        return f"{what} is not a directory"
    if info.st_uid != os.getuid():
        return f"{what} belongs to another user"
    if info.st_mode & 0o077:
        return (
            f"{what} is accessible by group/others "
            f"(mode {stat.S_IMODE(info.st_mode):o}); chmod 700 it"
        )
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return f"{what} is not readable and writable by you"
    return None


def path_problems(source):
    """Filesystem problems with the spool and archive folders (empty = fine)."""
    problems = []
    spool = source.get("spool_dir")
    archive = source.get("archive_dir")
    if isinstance(spool, str) and spool.startswith("/"):
        if not Path(spool).exists():
            problems.append(
                "spool_dir does not exist (the forwarder creates it on its "
                "first run with the spool on)"
            )
        else:
            problem = _private_dir_problem(spool, "spool_dir")
            if problem:
                problems.append(problem)
    if isinstance(archive, str) and archive.startswith("/"):
        problem = _private_dir_problem(archive, "archive_dir")
        if problem:
            problems.append(problem)
        tree = _git_tree(archive)
        if tree is not None:
            problems.append(
                "archive_dir is inside a git work tree; message text and "
                "media must stay out of every repository"
            )
    return problems


def validate(prefix, source, routine=None):
    """Config problems for one whatsapp source block (empty list = valid)."""
    problems = []
    spool = source.get("spool_dir")
    archive = source.get("archive_dir")
    for key, value in (("spool_dir", spool), ("archive_dir", archive)):
        if not isinstance(value, str) or not value.startswith("/"):
            problems.append(f"{prefix}.{key} must be an absolute path")
    if (
        isinstance(spool, str) and isinstance(archive, str)
        and spool.startswith("/") and archive.startswith("/")
    ):
        spool_path = Path(os.path.normpath(spool))
        archive_path = Path(os.path.normpath(archive))
        if _inside(archive_path, spool_path) or _inside(spool_path, archive_path):
            problems.append(
                f"{prefix}.archive_dir and spool_dir must not contain each "
                "other (the spool is never written)"
            )
        store = ((routine or {}).get("memory") or {}).get("store")
        if isinstance(store, str) and store.startswith("/") and _inside(
            archive_path, Path(os.path.normpath(store))
        ):
            problems.append(
                f"{prefix}.archive_dir must be outside memory.store (the "
                "store's folders are git repositories)"
            )
    for key, lower, upper in (
        ("quiet_minutes", 0, 1440),
        ("max_file_mb", 1, 2048),
        ("max_total_mb", 1, 1048576),
        ("transcribe_timeout_seconds", 10, 7200),
        ("transcript_max_chars", 200, 200000),
        ("pdf_max_pages", 1, 500),
        ("pdf_max_chars", 200, 200000),
        ("max_body_chars", 2000, 500000),
    ):
        if key in source and not _bounded_number(source[key], lower, upper):
            problems.append(
                f"{prefix}.{key} must be an integer from {lower} to {upper}"
            )
    if "max_wait_hours" in source and not _bounded_number(
        source["max_wait_hours"], 0.25, 72, integer=False
    ):
        problems.append(
            f"{prefix}.max_wait_hours must be a number from 0.25 to 72 "
            "(capture long before the spool prunes)"
        )
    if (
        _bounded_number(source.get("quiet_minutes"), 0, 1440)
        and _bounded_number(source.get("max_wait_hours"), 0.25, 72, False)
        and source["quiet_minutes"] > source["max_wait_hours"] * 60
    ):
        problems.append(
            f"{prefix}.quiet_minutes must not exceed max_wait_hours"
        )
    for key in ("copy_media", "transcribe_audio", "pdf_text"):
        if key in source and not isinstance(source[key], bool):
            problems.append(f"{prefix}.{key} must be true or false")
    if source.get("transcribe_audio") is True:
        model = source.get("whisper_model")
        if not isinstance(model, str) or not model.startswith("/"):
            problems.append(
                f"{prefix}.whisper_model must be an absolute path to a "
                "whisper.cpp ggml model when transcribe_audio is true"
            )
    elif source.get("whisper_model") is not None and not (
        isinstance(source["whisper_model"], str)
        and source["whisper_model"].startswith("/")
    ):
        problems.append(f"{prefix}.whisper_model must be an absolute path")
    language = source.get("whisper_language")
    if language is not None and not (
        isinstance(language, str)
        and re.fullmatch(r"auto|[a-z]{2,3}", language)
    ):
        problems.append(
            f"{prefix}.whisper_language must be 'auto' or a language code "
            "such as 'he' or 'en'"
        )
    threads = source.get("whisper_threads")
    if threads is not None and not _bounded_number(threads, 1, 64):
        problems.append(f"{prefix}.whisper_threads must be an integer from 1 to 64")
    for key in ("chat_keys", "exclude_chat_keys"):
        values = source.get(key)
        if values is not None and not (
            isinstance(values, list)
            and all(_bounded_number(v, 0, 2 ** 53) for v in values)
        ):
            problems.append(
                f"{prefix}.{key} must be a list of chat keys (integers, the "
                "first column of the forwarder's --list-conversations)"
            )
    for key in ("chat_labels", "exclude_chat_labels"):
        values = source.get(key)
        if values is not None and not (
            isinstance(values, list)
            and all(isinstance(v, str) and v.strip() for v in values)
        ):
            problems.append(f"{prefix}.{key} must be a list of chat labels")
    for key in ("query", "catch_up"):
        if key in source:
            problems.append(f"{prefix}.{key} is not supported by whatsapp")
    if routine is None or routine.get("enabled", True) is not False:
        # Filesystem state only matters for a routine that can run; the
        # shipped examples are disabled and name placeholder paths.
        problems.extend(f"{prefix}: {p}" for p in path_problems(source))
    return problems


# --- archive -------------------------------------------------------------------

def _private_tree(directory):
    """Create ``directory`` and any missing parents, each one 0700.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode to the last
    folder only; the intermediate ones (``media/``, ``chats/``) would get
    the umask's 0755.
    """
    directory = Path(directory)
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for folder in reversed(missing):
        state.ensure_private_dir(folder)


def _write_json(path, payload):
    _private_tree(path.parent)
    state.write_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        mode=0o600,
    )


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _cursor_path(archive, spool):
    digest = hashlib.sha256(
        os.path.normpath(spool).encode("utf-8")
    ).hexdigest()[:12]
    return Path(archive) / f"spool-cursor-{digest}.json"


def chat_ref(chat_key):
    return f"chat-{int(chat_key)}"


def identity(chat_key, date):
    return f"whatsapp:{chat_ref(chat_key)}:{date}"


def _day_relpath(chat_key, date):
    return f"chats/{chat_ref(chat_key)}/{date}.json"


def _media_relpath(message_id, date, extension):
    return f"media/{date}/{message_id}{extension}"


def _content_digest(day):
    """Digest of what the model and the store would see for one chat-day."""
    rows = []
    for message in sorted(day["messages"].values(), key=_order):
        media = message.get("media") or None
        rows.append([
            message["id"], message.get("timestamp"), message.get("epoch"),
            message.get("sender"), bool(message.get("from_me")),
            message.get("type"), message.get("text"),
            None if media is None else [
                media.get("kind"), media.get("status"),
                media.get("original_name"), media.get("mime"),
                media.get("size"),
            ],
            bool(message.get("stored_path")),
        ])
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _order(message):
    return (message.get("epoch") or 0, message.get("id") or "")


def _clean_record(message):
    """The fields kept in the archive; JIDs, row and stanza keys are dropped."""
    media = message.get("media")
    kept_media = None
    if isinstance(media, dict):
        kept_media = {
            key: media.get(key)
            for key in ("kind", "status", "path", "original_name", "mime", "size")
        }
    return {
        "id": message["id"],
        "id_source": message.get("id_source"),
        "chat_key": message["chat_key"],
        "chat_label": str(message.get("chat_label") or ""),
        "chat_type": str(message.get("chat_type") or "other"),
        "sender": str(message.get("sender") or ""),
        "from_me": message.get("from_me") is True,
        "timestamp": message["timestamp"],
        "epoch": message["epoch"],
        "type": str(message.get("type") or "text"),
        "text": str(message.get("text") or ""),
        "backfill": message.get("backfill") is True,
        "media": kept_media,
    }


def _valid_message(message):
    if not isinstance(message, dict):
        return False
    if not isinstance(message.get("id"), str) or not MESSAGE_ID_RE.match(
        message["id"]
    ):
        return False
    chat_key = message.get("chat_key")
    if isinstance(chat_key, bool) or not isinstance(chat_key, int) or chat_key < 0:
        return False
    timestamp = message.get("timestamp")
    epoch = message.get("epoch")
    if (
        not isinstance(timestamp, str) or not DATE_RE.match(timestamp[:10])
        or isinstance(epoch, bool) or not isinstance(epoch, int)
    ):
        return False
    try:
        datetime.datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    media = message.get("media")
    return media is None or isinstance(media, dict)


def _wanted(cfg, chat_key, label):
    """Whether a chat passes the source's include and exclude filters."""
    include_keys, include_labels = cfg["chat_keys"], cfg["chat_labels"]
    if (include_keys or include_labels) and not (
        chat_key in include_keys or label in include_labels
    ):
        return False
    return not (
        chat_key in cfg["exclude_chat_keys"]
        or label in cfg["exclude_chat_labels"]
    )


class _MediaSkip(Exception):
    """A spooled file that is not copied; ``code`` says why."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _spool_file(spool, relpath):
    """The spool file named by ``media.path``, checked to stay in the spool."""
    match = MEDIA_PATH_RE.match(relpath or "")
    if not match:
        raise _MediaSkip("bad_path")
    media_dir = Path(spool) / match.group("batch") / "media"
    path = media_dir / match.group("name")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _MediaSkip("missing") from None
    if not stat.S_ISREG(info.st_mode):
        raise _MediaSkip("not_regular")
    if os.path.realpath(path) != os.path.join(
        os.path.realpath(media_dir), match.group("name")
    ):
        raise _MediaSkip("bad_path")
    return path, info.st_size


def _copy_file(source_path, size, destination):
    """Copy byte for byte into ``destination`` (0600), atomically."""
    if destination.exists():
        if destination.stat().st_size == size:
            os.chmod(destination, 0o600)
            return False
    _private_tree(destination.parent)
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source_path, flags)
    try:
        target_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as reader, \
                    os.fdopen(target_fd, "wb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(temp, 0o600)
            if temp.stat().st_size != size:
                raise _MediaSkip("changed")
            os.replace(temp, destination)
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
    finally:
        os.close(source_fd)
    return True


# A copy that failed for one of these reasons is retried while the spool
# still holds the file. The others are final: size and quota limits, a bad
# path, and "missing" (a batch is complete when it appears, so a file that is
# not there was pruned).
_RETRY_COPY = ("changed", "copy_failed")
_LOCAL_FIELDS = ("first_epoch", "changed_epoch", "stored_path", "store_note")


def _content(record):
    """A record without local bookkeeping or the batch-specific file path."""
    value = {k: v for k, v in record.items() if k not in _LOCAL_FIELDS}
    value.pop("backfill", None)
    if isinstance(value.get("media"), dict):
        value["media"] = {
            k: v for k, v in value["media"].items() if k != "path"
        }
    return value


def _retry_copy(record):
    note = str(record.get("store_note") or "")
    return (
        (record.get("media") or {}).get("status") == "copied"
        and not record.get("stored_path")
        and (not note or note.split(":")[0] in _RETRY_COPY)
    )


def _index_entry(day, copy_media):
    messages = day["messages"].values()
    return {
        "chat_key": day["chat_key"],
        "date": day["date"],
        "label": day.get("chat_label", ""),
        "digest": _content_digest(day),
        "changed": max((m.get("changed_epoch") or 0) for m in messages),
        "pending_copies": copy_media and any(_retry_copy(m) for m in messages),
    }


class _Ingest:
    """One listing's view of the archive plus the batches read into it."""

    def __init__(self, cfg, dry_run):
        self.cfg = cfg
        self.dry_run = dry_run
        self.spool = Path(cfg["spool_dir"])
        self.archive = Path(cfg["archive_dir"])
        self.index_path = self.archive / "chats" / "index.json"
        index = _read_json(self.index_path, {})
        self.index = index if isinstance(index, dict) else {}
        self.index.setdefault("v", ARCHIVE_VERSION)
        self.index.setdefault("days", {})
        self.index.setdefault("media_bytes", 0)
        self.days = {}
        self.last_merged = None
        self.dirty = set()    # chat-days to write back
        self.changed = set()  # chat-days whose content changed
        self.counts = dict.fromkeys((
            "batches", "messages", "new", "updated", "duplicates", "invalid",
            "filtered", "copied", "reused", "too_large", "archive_full",
            "copy_failed",
        ), 0)

    def day(self, chat_key, date):
        key = identity(chat_key, date)
        if key not in self.days:
            loaded = _read_json(self.archive / _day_relpath(chat_key, date), None)
            if not isinstance(loaded, dict) or not isinstance(
                loaded.get("messages"), dict
            ):
                loaded = {
                    "v": ARCHIVE_VERSION, "chat_key": chat_key, "date": date,
                    "messages": {},
                }
            self.days[key] = loaded
        return self.days[key]

    def copy(self, record, date):
        """Copy one spooled file into the archive. True when now stored."""
        media = record.get("media") or {}
        if (
            media.get("status") != "copied" or not self.cfg["copy_media"]
            or record.get("stored_path")
        ):
            return False
        try:
            source_path, size = _spool_file(self.spool, media.get("path"))
            if size > self.cfg["max_file_mb"] * 1024 * 1024:
                raise _MediaSkip("too_large")
            if (
                self.index["media_bytes"] + size
                > self.cfg["max_total_mb"] * 1024 * 1024
            ):
                raise _MediaSkip("archive_full")
            extension = Path(source_path.name).suffix.lower()
            if not EXTENSION_RE.match(extension):
                extension = ".bin"
            relpath = _media_relpath(record["id"], date, extension)
            if self.dry_run:
                return False
            copied = _copy_file(source_path, size, self.archive / relpath)
        except _MediaSkip as skip:
            record["store_note"] = skip.code
            key = skip.code if skip.code in self.counts else "copy_failed"
            self.counts[key] += 1
            return False
        except OSError as exc:
            record["store_note"] = f"copy_failed:{type(exc).__name__}"
            self.counts["copy_failed"] += 1
            return False
        record["stored_path"] = relpath
        record.pop("store_note", None)
        if copied:
            self.index["media_bytes"] += size
            self.counts["copied"] += 1
        else:
            self.counts["reused"] += 1
        return True

    def merge(self, message, batch_epoch, timezone):
        """Upsert one spool record by id; a later record is a newer version."""
        record = _clean_record(message)
        date = record["timestamp"][:10]
        key = identity(record["chat_key"], date)
        day = self.day(record["chat_key"], date)
        day["chat_label"] = record["chat_label"]
        day["chat_type"] = record["chat_type"]
        if timezone:
            day["timezone"] = timezone
        existing = day["messages"].get(record["id"])
        if existing is None:
            record["first_epoch"] = record["changed_epoch"] = batch_epoch
            self.copy(record, date)
            day["messages"][record["id"]] = record
            self.counts["new"] += 1
            self.changed.add(key)
            self.dirty.add(key)
            return
        if message.get("update") == "media":
            # Only the file is news. The contract does not promise that the
            # follow-up repeats text, sender and time, so keep ours.
            new_media = record["media"]
            record = {
                k: v for k, v in existing.items()
                if k not in ("stored_path", "store_note")
            }
            record["media"] = new_media
        older_media = existing.get("media") or {}
        if existing.get("stored_path") and (
            (record.get("media") or {}).get("status") != "copied"
            or (record.get("media") or {}).get("size") == older_media.get("size")
        ):
            # A re-sent record never loses a copy we already hold.
            record["media"] = older_media
            record["stored_path"] = existing["stored_path"]
        record["first_epoch"] = existing.get("first_epoch", batch_epoch)
        same = _content(record) == _content(existing)
        if same and not _retry_copy(existing):
            self.counts["duplicates"] += 1
            return
        if same:
            # The same message again (a re-send) whose file we still lack:
            # take the fresh spool path and try once more.
            record["changed_epoch"] = existing.get("changed_epoch", batch_epoch)
            record["store_note"] = existing.get("store_note")
            record.pop("stored_path", None)
            day["messages"][record["id"]] = record
            self.dirty.add(key)
            if self.copy(record, date):
                record["changed_epoch"] = batch_epoch
                self.changed.add(key)
            self.counts["duplicates"] += 1
            return
        record["changed_epoch"] = batch_epoch
        self.copy(record, date)
        day["messages"][record["id"]] = record
        self.counts["updated"] += 1
        self.changed.add(key)
        self.dirty.add(key)

    def read_batches(self):
        """Merge every batch after the cursor; returns the last one merged."""
        cursor = _read_json(_cursor_path(self.archive, self.spool), {})
        last = cursor.get("last_batch") if isinstance(cursor, dict) else None
        names = sorted(
            entry.name for entry in os.scandir(self.spool)
            if BATCH_RE.match(entry.name) and entry.is_dir(follow_symlinks=False)
        )
        pending = [name for name in names if last is None or name > last]
        if last and pending and not any(name <= last for name in names):
            if int(pending[0][1:11]) - int(last[1:11]) > 1:
                log(
                    f"whatsapp spool WARN: batches after seq {int(last[1:11])} "
                    f"up to seq {int(pending[0][1:11])} are gone (pruned "
                    "before they were read?)"
                )
        for name in pending:
            self._merge_batch(name)
            self.counts["batches"] += 1
            self.last_merged = last = name
        return last

    def _merge_batch(self, name):
        payload = json.loads(
            (self.spool / name / "batch.json").read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            raise RuntimeError(f"whatsapp spool batch {name} is not a {FORMAT} file")
        if payload.get("version") != VERSION:
            raise RuntimeError(
                f"whatsapp spool batch {name} has format version "
                f"{payload.get('version')!r}; this reader supports {VERSION}"
            )
        batch_epoch = payload.get("created_epoch")
        messages = payload.get("messages")
        if isinstance(batch_epoch, bool) or not isinstance(batch_epoch, int) or (
            not isinstance(messages, list)
        ):
            raise RuntimeError(
                f"whatsapp spool batch {name} has no created_epoch or messages"
            )
        timezone = payload.get("timezone")
        timezone = timezone if isinstance(timezone, str) else None
        for message in messages:
            self.counts["messages"] += 1
            if not _valid_message(message):
                self.counts["invalid"] += 1
                continue
            if not _wanted(
                self.cfg, message["chat_key"], str(message.get("chat_label") or "")
            ):
                self.counts["filtered"] += 1
                continue
            self.merge(message, batch_epoch, timezone)

    def retry_copies(self):
        """Retry earlier failed copies whose spool file may still exist."""
        for key, meta in list(self.index["days"].items()):
            if not meta.get("pending_copies"):
                continue
            day = self.day(meta["chat_key"], meta["date"])
            for record in day["messages"].values():
                if not _retry_copy(record):
                    continue
                self.dirty.add(key)  # persist the new outcome either way
                if self.copy(record, meta["date"]):
                    record["changed_epoch"] = int(_now())
                    self.changed.add(key)

    def save(self, last):
        """Persist chat-days, then the index, then the cursor (in that order)."""
        for key in sorted(self.dirty):
            day = self.days[key]
            self.index["days"][key] = _index_entry(day, self.cfg["copy_media"])
            if not self.dry_run:
                _write_json(
                    self.archive / _day_relpath(day["chat_key"], day["date"]),
                    day,
                )
        if self.dry_run:
            return
        if self.dirty or not self.index_path.exists():
            _write_json(self.index_path, self.index)
        if last is not None:
            _write_json(_cursor_path(self.archive, self.spool), {
                "spool": os.path.normpath(str(self.spool)),
                "last_batch": last,
            })


def _check_runtime_paths(cfg, dry_run):
    problems = path_problems(cfg)
    if problems:
        raise RuntimeError("whatsapp source: " + "; ".join(problems))
    archive = Path(cfg["archive_dir"])
    if not archive.exists():
        if dry_run:
            return
        if not archive.parent.is_dir():
            raise RuntimeError(
                "whatsapp source: archive_dir's parent folder does not exist"
            )
        state.ensure_private_dir(archive)


def ingest(source):
    """Read new spool batches into the archive; returns the ingest state."""
    cfg = settings(source)
    dry_run = bool(source.get("_dry_run") or source.get("_count_only"))
    _check_runtime_paths(cfg, dry_run)
    work = _Ingest(cfg, dry_run)
    work.retry_copies()
    last = None
    try:
        last = work.read_batches()
    except Exception:
        # Keep what the earlier batches of this run already merged (their
        # copies exist); the failing batch is read again on the next run.
        work.save(work.last_merged)
        raise
    work.save(last)
    counts = work.counts
    if counts["batches"] or work.dirty:
        log(
            "whatsapp spool: "
            + " ".join(f"{key}={value}" for key, value in counts.items())
            + f" chat_days_changed={len(work.changed)}"
            + (f" last_batch={last}" if last else "")
            + (" [dry-run]" if dry_run else "")
        )
    return work


# --- listing -------------------------------------------------------------------

def _settled(entry):
    return isinstance(entry, dict) and not (
        entry.get("memory_error") or entry.get("memory_held")
    )


def _epoch_of(value):
    try:
        return datetime.datetime.strptime(
            str(value), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def _last_captures(processed):
    """Newest settled processing time per whatsapp source id."""
    found = {}
    if processed is None:
        return found
    for _item_id, entry in processed.items():
        if entry.get("source_kind") != "whatsapp" or not _settled(entry):
            continue
        source_id = entry.get("source_id")
        at = _epoch_of(entry.get("processed_at"))
        if isinstance(source_id, str) and at is not None:
            found[source_id] = max(found.get(source_id, 0), at)
    return found


def candidates(source):
    """One candidate per chat-day with uncaptured content; see the docstring."""
    cfg = settings(source)
    work = ingest(source)
    processed = source.get("_processed")
    dry_run = work.dry_run
    now = _now()
    quiet = cfg["quiet_minutes"] * 60
    max_wait = cfg["max_wait_hours"] * 3600
    last_capture = _last_captures(processed)
    ready, waiting = [], []
    settled = 0
    for key, meta in sorted(work.index["days"].items()):
        if not _wanted(cfg, meta.get("chat_key"), meta.get("label", "")):
            continue
        candidate_id = f"{key}@{meta['digest']}"
        existing = processed.get(candidate_id) if processed is not None else None
        if existing is not None and _settled(existing):
            settled += 1
            continue
        day = work.day(meta["chat_key"], meta["date"])
        changes = [
            m.get("changed_epoch") or 0 for m in day["messages"].values()
        ]
        if not changes:
            continue
        since = last_capture.get(key, 0)
        uncaptured = [epoch for epoch in changes if epoch > since] or changes
        quiet_for = now - max(changes)
        waited = now - min(uncaptured)
        due = (
            existing is not None  # a failed capture retries without waiting
            or quiet_for >= quiet
            or waited >= max_wait
        )
        candidate = {
            "id": candidate_id,
            # Logged by the runner: never a chat label or a name.
            "title": f"WhatsApp {chat_ref(meta['chat_key'])} {meta['date']}",
            "raw": {
                "source_id": key,
                "chat_key": meta["chat_key"],
                "date": meta["date"],
                "digest": meta["digest"],
                "_dry_run": dry_run,
                # The merged chat-day travels with the candidate so a dry run
                # (which writes nothing) and the real run read the same data.
                "_day": day,
            },
        }
        (ready if due else waiting).append(candidate)
    limit = int(source.get("max_results") or 0)
    ready.sort(key=lambda c: (c["raw"]["date"], c["raw"]["chat_key"]))
    if limit and len(ready) > limit:
        waiting.extend(ready[limit:])
        ready = ready[:limit]
    for candidate in waiting:
        candidate["raw"]["_deferred"] = True
    log(
        f"whatsapp: chat_days ready={len(ready)} waiting={len(waiting)} "
        f"settled={settled}"
    )
    return ready + waiting


# --- text from media -------------------------------------------------------------

def ffmpeg_bin():
    return optional_bin("ffmpeg", "WORKSPACE_DAEMON_FFMPEG_BIN")


def whisper_cli_bin():
    return optional_bin("whisper-cli", "WORKSPACE_DAEMON_WHISPER_CLI_BIN")


def pdftotext_bin():
    return optional_bin("pdftotext", "WORKSPACE_DAEMON_PDFTOTEXT_BIN")


def _transcribe(cfg, path, workdir):
    ffmpeg, whisper = ffmpeg_bin(), whisper_cli_bin()
    if not ffmpeg or not whisper:
        return None, "tool_missing"
    model = cfg.get("whisper_model")
    if not model or not Path(model).is_file():
        return None, "model_missing"
    timeout = cfg["transcribe_timeout_seconds"]
    wav = workdir / "audio.wav"
    try:
        run([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(wav),
        ], timeout=timeout)
    except Exception as exc:
        return None, f"convert_failed:{type(exc).__name__}"
    command = [
        whisper, "-m", str(model), "-l", cfg["whisper_language"],
        "-f", str(wav), "-nt", "-np", "-otxt", "-of", str(workdir / "out"),
    ]
    if cfg.get("whisper_threads"):
        command += ["-t", str(cfg["whisper_threads"])]
    try:
        run(command, timeout=timeout)
        text = (workdir / "out.txt").read_text(encoding="utf-8").strip()
    except Exception as exc:
        return None, f"transcribe_failed:{type(exc).__name__}"
    return (text, None) if text else (None, "empty")


def _pdf_text(cfg, path, _workdir):
    binary = pdftotext_bin()
    if not binary:
        return None, "tool_missing"
    try:
        result = run([
            binary, "-q", "-enc", "UTF-8", "-l", str(cfg["pdf_max_pages"]),
            str(path), "-",
        ], timeout=120)
    except Exception as exc:
        return None, f"pdf_failed:{type(exc).__name__}"
    text = re.sub(r"[ \t]+\n", "\n", result.stdout or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return (text[:200000], None) if text else (None, "no_text")


_EXTRACTORS = {"transcript": _transcribe, "pdf": _pdf_text}
# Missing tools and models are an installation state, not a property of the
# file: they are retried on the next capture instead of being remembered.
_RETRYABLE = {"tool_missing", "model_missing"}


def _extraction_kind(cfg, message):
    media = message.get("media") or {}
    if media.get("kind") == "audio" and cfg["transcribe_audio"]:
        return "transcript"
    if media.get("kind") == "document" and cfg["pdf_text"]:
        name = str(media.get("original_name") or message.get("stored_path") or "")
        if media.get("mime") == "application/pdf" or name.lower().endswith(".pdf"):
            return "pdf"
    return None


def _extract(cfg, archive, message, dry_run, counts):
    """(kind, text, reason) for one message's stored file; cached on disk."""
    kind = _extraction_kind(cfg, message)
    if kind is None or not message.get("stored_path"):
        return None, None, None
    stored = archive / message["stored_path"]
    cache = stored.with_name(stored.name + _CACHE_SUFFIX[kind])
    failed = stored.with_name(stored.name + f".{kind}.failed")
    if cache.exists():
        return kind, cache.read_text(encoding="utf-8"), None
    if failed.exists():
        return kind, None, failed.read_text(encoding="utf-8").strip() or "failed"
    if dry_run:
        return kind, None, "dry_run"
    if not stored.is_file():
        return kind, None, "stored_file_missing"
    scratch = archive / ".tmp"
    _private_tree(scratch)
    workdir = Path(tempfile.mkdtemp(dir=scratch))
    try:
        text, reason = _EXTRACTORS[kind](cfg, stored, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if text:
        state.write_atomic(cache, text + "\n", mode=0o600)
        counts[kind] += 1
        return kind, text, None
    counts["failed"] += 1
    if reason not in _RETRYABLE:
        state.write_atomic(failed, reason + "\n", mode=0o600)
    return kind, None, reason


# --- fetch -----------------------------------------------------------------------

def _hhmm(message):
    return message["timestamp"][11:16]


def _sender(message):
    name = " ".join(str(message.get("sender") or "Unknown").split())[:80]
    return f"{name} [owner]" if message.get("from_me") else name


def _size_label(size):
    if not isinstance(size, int) or size < 0:
        return None
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def _attachment_facts(message, archive, exact=False):
    media = message.get("media") or {}
    kind = media.get("kind")
    facts = [kind if kind in MEDIA_KINDS else "file"]
    name = " ".join(str(media.get("original_name") or "").split())[:120]
    if name:
        facts.append(f'"{name}"')
    mime = media.get("mime")
    if isinstance(mime, str) and re.fullmatch(r"[\w.+-]{1,60}/[\w.+-]{1,80}", mime):
        facts.append(mime)
    size = media.get("size")
    if exact and isinstance(size, int) and size >= 0:
        facts.append(f"{size} bytes")
    elif _size_label(size):
        facts.append(_size_label(size))
    if message.get("stored_path"):
        where = f"stored at {archive / message['stored_path']}"
    elif media.get("status") == "copied":
        where = f"not stored ({message.get('store_note') or 'pending copy'})"
    else:
        status = media.get("status")
        status = status if status in SPOOL_STATUSES else "unknown"
        where = f"not copied by the forwarder ({status})"
    return facts, where


def _bounded(text, limit):
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


_EXTRACT_LABEL = {
    "transcript": "transcript (local speech-to-text, may contain errors)",
    "pdf": "PDF text excerpt",
}


def _render(messages, extracted, archive, cfg):
    """(prompt body, verbatim block) for one chat-day."""
    prompt, verbatim = [], []
    for message in messages:
        text = message.get("text") or ""
        prompt.append(f"[{_hhmm(message)}] {_sender(message)}: {text}")
        sender = " ".join(str(message.get("sender") or "").split())
        verbatim.append(
            f"- {message['timestamp']} · {sender} · {message['id']}"
            + (" · from_me" if message.get("from_me") else "")
        )
        if text:
            verbatim.extend(f"  > {line}" for line in text.split("\n"))
        if message.get("media"):
            facts, where = _attachment_facts(message, archive)
            prompt.append(
                f"  attachment: {', '.join(facts)}"
                + (", stored" if message.get("stored_path") else f", {where}")
            )
            exact_facts, _where = _attachment_facts(message, archive, exact=True)
            verbatim.append(f"  attachment: {' · '.join(exact_facts)} · {where}")
        kind, extract, reason = extracted.get(message["id"], (None, None, None))
        if kind and extract:
            limit = (
                cfg["transcript_max_chars"] if kind == "transcript"
                else cfg["pdf_max_chars"]
            )
            shown, cut = _bounded(extract, limit)
            label = _EXTRACT_LABEL[kind] + (" (truncated)" if cut else "")
            prompt.append(f"  {label}:")
            prompt.extend(f"    {line}" for line in shown.split("\n"))
            verbatim.append(f"  {label}:")
            verbatim.extend(f"  | {line}" for line in shown.split("\n"))
        elif kind and reason:
            note = f"  {kind}: none ({reason})"
            prompt.append(note)
            verbatim.append(note)
    return prompt, verbatim


def _bounded_prompt(lines, limit):
    """Keep the newest whole lines within ``limit`` characters."""
    total, kept = 0, []
    for line in reversed(lines):
        total += len(line) + 1
        if total > limit and kept:
            break
        kept.append(line)
    kept.reverse()
    omitted = len(lines) - len(kept)
    if omitted:
        kept.insert(0, (
            f"[{omitted} earlier line(s) of this day are omitted from this "
            "prompt; the stored entry keeps every message verbatim]"
        ))
    return "\n".join(kept)


def _participants(messages):
    names = []
    for message in messages:
        name = " ".join(str(message.get("sender") or "").split())[:80]
        if (
            message.get("from_me") or not name or UNKNOWN_SENDER_RE.match(name)
            or name in names
        ):
            continue
        names.append(name)
    return names


def fetch(_routine, source, candidate):
    cfg = settings(source)
    raw = candidate["raw"]
    archive = Path(cfg["archive_dir"])
    dry_run = bool(raw.get("_dry_run"))
    day = raw.get("_day")
    if not isinstance(day, dict):
        day = _read_json(archive / _day_relpath(raw["chat_key"], raw["date"]), None)
        if not isinstance(day, dict):
            raise RuntimeError(f"whatsapp archive has no record of {raw['source_id']}")
    messages = sorted(day["messages"].values(), key=_order)
    if not dry_run and any(_retry_copy(m) for m in messages):
        # Last chance before the ledger settles this chat-day: a file the
        # listing could not copy is retried while the spool still has it.
        # The index digest is left alone on purpose: this capture already
        # shows the stored file, so it must not count as a new change.
        work = _Ingest(cfg, dry_run=False)
        stored = [work.copy(m, raw["date"]) for m in messages if _retry_copy(m)]
        if any(stored):
            _write_json(archive / _day_relpath(raw["chat_key"], raw["date"]), day)
            _write_json(work.index_path, work.index)
    counts = {"transcript": 0, "pdf": 0, "failed": 0}
    extracted = {}
    for message in messages:
        kind, text, reason = _extract(cfg, archive, message, dry_run, counts)
        if kind:
            extracted[message["id"]] = (kind, text, reason)
    if extracted:
        reasons = sorted({
            reason for _k, _t, reason in extracted.values() if reason
        })
        log(
            f"whatsapp {raw['source_id']} media text: "
            f"transcribed={counts['transcript']} pdf={counts['pdf']} "
            f"failed={counts['failed']}"
            + (f" unavailable={','.join(reasons)}" if reasons else "")
        )
    prompt_lines, verbatim_lines = _render(messages, extracted, archive, cfg)
    label = " ".join(str(day.get("chat_label") or "").split())[:120]
    chat_type = str(day.get("chat_type") or "other")
    timezone = str(day.get("timezone") or "")
    header = (
        f"Chat: {label or chat_ref(raw['chat_key'])} · {chat_type} · "
        f"{chat_ref(raw['chat_key'])} · {raw['date']}"
        + (f" ({timezone})" if timezone else "")
    )
    verbatim = (
        "## WhatsApp messages (verbatim)\n\n"
        "Each message: `- <time> · <sender> · <message id>` (plus "
        "` · from_me` for the owner's own), its text on `  > ` lines exactly "
        "as sent, then attachments and locally extracted text.\n\n"
        f"{header}\n\n" + "\n".join(verbatim_lines)
    )
    participants = _participants(messages)
    attachments = [
        {
            "message_id": message["id"],
            "kind": (message.get("media") or {}).get("kind"),
            "status": (message.get("media") or {}).get("status"),
            "stored_path": (
                str(archive / message["stored_path"])
                if message.get("stored_path") else None
            ),
        }
        for message in messages if message.get("media")
    ]
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],
        "source_kind": "whatsapp",
        "title": f"WhatsApp: {label or chat_ref(raw['chat_key'])} {raw['date']}",
        "date": raw["date"],
        # Chats can carry pasted codes and tokens: redact before the model,
        # the store or a vault note sees the text.
        "body": redact_secrets(
            _bounded_prompt(prompt_lines, cfg["max_body_chars"])
        ),
        # Appended verbatim to the stored entry by the memory sink, after the
        # model's summary, so original text survives the paraphrase.
        "memory_appendix": redact_secrets(verbatim),
        "frontmatter": {
            "whatsapp_chat_ref": chat_ref(raw["chat_key"]),
            "whatsapp_chat_label": label,
            "whatsapp_chat_type": chat_type,
            "whatsapp_date": raw["date"],
            "whatsapp_timezone": timezone or None,
            "whatsapp_message_ids": [message["id"] for message in messages],
            "whatsapp_participants": participants,
            "whatsapp_attachments": attachments,
            "whatsapp_content_hash": raw.get("digest"),
            "message_count": len(messages),
            "first_message_at": messages[0]["timestamp"] if messages else None,
            "latest_message_at": messages[-1]["timestamp"] if messages else None,
        },
    }
