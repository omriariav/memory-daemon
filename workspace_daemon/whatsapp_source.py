"""WhatsApp messages from a local forwarder spool, one memory per chat-day.

The daemon never reads WhatsApp. A separate local forwarder writes new
messages, and copies of their media files, as numbered *batches* into a
private spool folder (``b<seq>-<stamp>/batch.json`` plus ``media/``, format
``whatsapp-forwarder-spool`` version 1) and deletes batches after a few days
whether or not anyone read them. This adapter is a spool *consumer*:

* Ingest (during listing). New batches are read in name order. Every message
  is upserted by its stable ``id`` into a private, durable archive
  (``archive_dir``): ``chats/<chat>/<date>.json`` holds the merged records of
  one chat-day, and each spooled file with status ``copied`` is copied to
  ``media/<date>/<message id><ext>`` (0600, folders 0700). Voice notes and
  PDFs then get local text extraction under a per-run time budget. The last
  finished batch is remembered, so a restart resumes after it. Copies and
  records land in the archive before the position moves and long before the
  ledger settles a day, so the spool's pruning can never lose a file or
  shrink a conversation that was already seen. The spool is never written.
* Chat identity. A chat's id is a keyed hash (HMAC-SHA256 with a secret in
  the daemon's state directory) of its JID, so two chats can never become
  one and a rebuilt archive gets the same ids. ``chat_key``, a database row
  key reused after a reinstall, is only a link: within one ``store_id`` it
  aliases a chat's new JID (a move to ``@lid``) to its existing id.
* Capture unit. One candidate per archive chat per local day (the spool's
  timezone), memory source id ``whatsapp:<chat>:<date>``: opaque, never a
  phone number or JID. Later messages, a late file, a finished transcript or
  a re-sent message update the same memory; the candidate id adds a digest of
  the day's content so only changed content is reconsidered.
* Quiet period. A chat-day is offered for analysis once it had no change for
  ``quiet_minutes`` and no media text is still pending, or once its oldest
  uncaptured change has waited ``max_wait_hours``. Until then it is listed
  as deferred so no other routine claims it.

Logs carry counts, batch names, opaque ids and error classes only: never
message text, names, chat labels, JIDs or file names.
"""
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from . import state
from .chat_text import redact_secrets
from .shell import log, optional_bin, run

FORMAT = "whatsapp-forwarder-spool"
VERSION = 1
ARCHIVE_VERSION = 3

BATCH_RE = re.compile(r"^b[0-9]{10}-[0-9]{8}T[0-9]{6}Z$")
MESSAGE_ID_RE = re.compile(r"^wa1-[0-9a-f]{40}$")
CHAT_ID_RE = re.compile(r"^wac1-[0-9a-f]{32}$")
STORE_ID_RE = re.compile(r"^was1-[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
LOCAL_CHAT_RE = re.compile(r"^c[0-9a-f]{16}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# media.path is relative to the spool: "<batch>/media/wa-<row key>.<ext>".
MEDIA_PATH_RE = re.compile(
    r"^(?P<batch>b[0-9]{10}-[0-9]{8}T[0-9]{6}Z)/media/"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,120})$"
)
STORED_MEDIA_RE = re.compile(r"^wa1-[0-9a-f]{40}\.[A-Za-z0-9]{1,10}$")
EXTENSION_RE = re.compile(r"^\.[A-Za-z0-9]{1,10}$")
MEDIA_KINDS = ("image", "video", "audio", "document", "gif", "sticker")
SPOOL_STATUSES = (
    "copied", "not_selected", "too_large", "not_downloaded", "unreadable",
    "blocked_type", "gave_up",
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
    "transcribe_timeout_seconds": 300,
    "transcript_max_chars": 8000,
    "pdf_text": True,
    "pdf_max_pages": 20,
    "pdf_max_chars": 4000,
    "extract_budget_seconds": 300,
    "max_body_chars": 60000,
    "archive_keep_days": None,
    "recapture_after_rebuild": False,
    "chat_keys": [],
    "exclude_chat_keys": [],
    "chat_labels": [],
    "exclude_chat_labels": [],
}
SOURCE_KEYS = set(DEFAULTS) | {"spool_dir", "archive_dir"}

PDF_TIMEOUT_SECONDS = 60
EXTRACT_MAX_ATTEMPTS = 3
EXTRACT_BACKOFF_SECONDS = 15 * 60
STALE_TEMP_SECONDS = 2 * 3600
MEDIA_RECOUNT_SECONDS = 24 * 3600

# Copy outcomes that are retried while the spool still holds the file. A
# limit (too_large, archive_full) is retried too, so raising a limit takes
# effect; "missing" (a batch is complete when it appears, so the file was
# pruned), a bad path and a non-regular file are final.
_RETRY_COPY = ("", "changed", "copy_failed", "sha_mismatch", "too_large",
               "archive_full")

# Text from media. Each extractor maps a stored file to
# (text, reason, transient). A later image or video description step (a
# vision model) plugs in here as another entry, e.g. "image":
# _describe_image, plus one line in _extraction_kind; it is budgeted, cached,
# retried and rendered exactly like a transcript.
_CACHE_SUFFIX = {"transcript": ".transcript.txt", "pdf": ".pdf.txt"}
# A missing tool or model is an installation state, not a property of the
# file: such messages are retried once the tool is there.
_UNAVAILABLE = {"tool_missing", "model_missing"}
_EXTRACT_FINAL = ("done", "failed")

# Seconds of extraction spent by this process (one daemon tick).
_extract_spent = 0.0


def _now():
    return time.time()


def settings(source):
    cfg = dict(DEFAULTS)
    cfg.update({key: value for key, value in source.items() if key in DEFAULTS})
    cfg["spool_dir"] = source.get("spool_dir")
    cfg["archive_dir"] = source.get("archive_dir")
    return cfg


class Listing(list):
    """Candidates of one listing. ``degraded`` names a problem that left the
    listing incomplete (the runner logs it as an error and keeps lower-ranked
    routines away), while the chat-days it could offer are still offered."""
    degraded = None


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
        if _git_tree(archive) is not None:
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
        ("transcribe_timeout_seconds", 10, 1800),
        ("transcript_max_chars", 200, 200000),
        ("pdf_max_pages", 1, 500),
        ("pdf_max_chars", 200, 200000),
        ("extract_budget_seconds", 0, 3600),
        ("max_body_chars", 2000, 500000),
    ):
        if key in source and not _bounded_number(source[key], lower, upper):
            problems.append(
                f"{prefix}.{key} must be an integer from {lower} to {upper}"
            )
    keep = source.get("archive_keep_days")
    if keep is not None and not _bounded_number(keep, 1, 36500):
        problems.append(
            f"{prefix}.archive_keep_days must be an integer from 1 to 36500 "
            "(leave it out to keep the archive)"
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
    for key in ("copy_media", "transcribe_audio", "pdf_text",
                "recapture_after_rebuild"):
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


# --- archive files ---------------------------------------------------------------

def _private_tree(directory):
    """Create ``directory`` and any missing parents, each one 0700.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode to the last
    folder only; the intermediate ones would get the umask's 0755.
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
        os.path.normpath(str(spool)).encode("utf-8")
    ).hexdigest()[:12]
    return Path(archive) / f"spool-cursor-{digest}.json"


def identity(chat, date):
    return f"whatsapp:{chat}:{date}"


def _day_relpath(chat, date):
    return f"chats/{chat}/{date}.json"


def _media_relpath(message_id, date, extension):
    return f"media/{date}/{message_id}{extension}"


def _stamp_epoch(batch_name):
    """UTC creation time from a batch name, as Unix seconds."""
    return int(datetime.datetime.strptime(
        batch_name[12:], "%Y%m%dT%H%M%SZ"
    ).replace(tzinfo=datetime.timezone.utc).timestamp())


def _order(message):
    return (message.get("epoch") or 0, message.get("id") or "")


def _content_digest(day):
    """Digest of what the model and the store would see for one chat-day."""
    rows = []
    for message in sorted(day["messages"].values(), key=_order):
        media = message.get("media") or None
        extract = message.get("extract") or {}
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
            # Pending and unavailable text change nothing a capture shows
            # beyond a note; only a finished outcome is a new version.
            extract.get("state") if extract.get("state") in _EXTRACT_FINAL
            else None,
        ])
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:16]


def _clean_record(message):
    """The fields kept in the archive; JIDs, row and stanza keys are dropped."""
    media = message.get("media")
    kept_media = None
    if isinstance(media, dict):
        kept_media = {
            key: media.get(key)
            for key in ("kind", "status", "reason", "path", "original_name",
                        "mime", "size", "sha256")
        }
    return {
        "id": message["id"],
        "id_source": message.get("id_source"),
        "chat_key": message["chat_key"],
        "store_id": message.get("store_id"),
        "chat_id": message.get("chat_id") if isinstance(
            message.get("chat_id"), str) and CHAT_ID_RE.match(
            message["chat_id"]) else None,
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


def _wanted(cfg, chat_keys, label):
    """Whether a chat passes the include and exclude filters.

    ``chat_keys`` are the chat's keys in the current WhatsApp store only
    (keys are database row keys: a key from an earlier store may name
    another chat now). Without store ids, a record's own key in its batch.
    """
    include_keys, include_labels = cfg["chat_keys"], cfg["chat_labels"]
    if (include_keys or include_labels) and not (
        set(chat_keys) & set(include_keys) or label in include_labels
    ):
        return False
    return not (
        set(chat_keys) & set(cfg["exclude_chat_keys"])
        or label in cfg["exclude_chat_labels"]
    )


# --- daemon state (identity secret, aliases, capture ids) ------------------------

_base_dir = None


def configure(base_dir):
    """The daemon's base directory; its state/ holds the identity secret."""
    global _base_dir
    _base_dir = base_dir


def state_dir():
    if _base_dir is None:
        raise RuntimeError(
            "whatsapp source needs the daemon's state directory "
            "(whatsapp_source.configure(base_dir))"
        )
    return Path(_base_dir) / "state" / "whatsapp"


def _identity_key(directory, dry_run):
    """The random secret behind archive chat ids, created once (0600).

    It lives in the daemon's state directory next to the ledger, never in
    the archive and never in a log. Losing it changes every chat id, which
    is why it sits with the ledger: losing one without the other is the
    case the capture-ids check (see candidates) guards against.
    """
    path = directory / "chat-id.key"
    try:
        value = path.read_text(encoding="ascii").strip()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return bytes.fromhex(value)
        raise RuntimeError(
            "whatsapp identity key state/whatsapp/chat-id.key is malformed; "
            "restore it from a backup (a new key renames every chat)"
        )
    except FileNotFoundError:
        pass
    key = secrets.token_bytes(32)
    if dry_run:
        return key  # a preview only; nothing is written
    _private_tree(directory)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _identity_key(directory, dry_run)
    with os.fdopen(fd, "w") as handle:
        handle.write(key.hex() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return key


class _Identity:
    """Archive chat ids.

    The primary id of a chat is a keyed hash (HMAC-SHA256 with the daemon's
    own secret) of its JID: independent of the order messages are seen in,
    so a rebuilt archive gets the same ids, and two chats with different
    JIDs can never become one. ``chat_key`` is only ever a link: when a
    record carries a ``store_id`` and its (store_id, chat_key) already
    belongs to chat X while its JID-derived id has never been seen, that new
    id becomes an alias of X (a chat moving from a phone-number JID to an
    ``@lid`` one within one WhatsApp database). Without a store_id there is
    no key-based linking at all.
    """

    def __init__(self, directory, dry_run):
        self.path = directory / "identity.json"
        self.dry_run = dry_run
        self.key = _identity_key(directory, dry_run)
        data = _read_json(self.path, None)
        if not isinstance(data, dict) or data.get("v") != 1:
            data = {"v": 1, "keys": {}, "aliases": {}, "chats": [],
                    "stores": []}
        self.data = data
        self.known = set(data["chats"])
        self.dirty = False
        self.conflicts = 0

    def own_id(self, jid):
        digest = hmac.new(
            self.key, b"chat\n" + jid.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return "c" + digest[:16]

    def note_store(self, store):
        if store and store not in self.data["stores"]:
            self.data["stores"].append(store)
            self.dirty = True

    def current_store(self):
        stores = self.data["stores"]
        return stores[-1] if stores else None

    def peek(self, jid):
        own = self.own_id(jid)
        return self.data["aliases"].get(own, own)

    def resolve(self, store, chat_key, jid):
        own = self.own_id(jid)
        aliases, keys = self.data["aliases"], self.data["keys"]
        local = aliases.get(own, own)
        if store:
            store_key = f"{store}|{chat_key}"
            linked = keys.get(store_key)
            if linked is None:
                keys[store_key] = local
                self.dirty = True
            elif linked != local:
                if own not in self.known and own not in aliases:
                    aliases[own] = linked
                    local = linked
                else:
                    # Two chats that both exist claim one key: never merge.
                    # The key names the newest one from now on.
                    self.conflicts += 1
                    keys[store_key] = local
                self.dirty = True
        if local not in self.known:
            self.known.add(local)
            self.data["chats"].append(local)
            self.dirty = True
        return local

    def keys_in_store(self, store, local=None):
        """Chat keys seen in one store (for one chat, or for all)."""
        found = set()
        for store_key, chat in self.data["keys"].items():
            name, _sep, key = store_key.rpartition("|")
            if name == store and (local is None or chat == local):
                found.add(int(key))
        return found

    def save(self):
        if self.dirty and not self.dry_run:
            _write_json(self.path, self.data)
            self.dirty = False


def _id_prefix(message_id):
    return message_id[4:20]


def _load_captures():
    data = _read_json(state_dir() / "captures.json", None)
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        data = {"v": 1, "days": {}}
    return data


def _record_capture(source_id, digest, message_ids):
    """Remember which messages a capture of this chat-day contains.

    Written when the item is fetched for analysis, keyed by its digest; the
    ledger says which digest was the last one settled. A later version of
    the day must contain every one of them before it may replace that
    capture (it might otherwise be a partial day rebuilt from what is left
    in the spool).
    """
    data = _load_captures()
    versions = data["days"].setdefault(source_id, {})
    versions.pop(digest, None)
    versions[digest] = sorted(_id_prefix(m) for m in message_ids)
    while len(versions) > 3:
        versions.pop(next(iter(versions)))
    _write_json(state_dir() / "captures.json", data)


# --- media copies ----------------------------------------------------------------

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


def _sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_file(source_path, size, destination, expected_sha=None):
    """Copy byte for byte into ``destination`` (0600), atomically.

    Returns (copied, size of the file it replaced). With ``expected_sha`` a
    copy whose content does not match is thrown away (``sha_mismatch``).
    """
    replaced = 0
    if destination.exists():
        current = destination.stat().st_size
        if current == size and (
            not expected_sha or _sha256_of(destination) == expected_sha
        ):
            os.chmod(destination, 0o600)
            return False, 0
        replaced = current
    _private_tree(destination.parent)
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source_path, flags)
    try:
        target_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            digest = hashlib.sha256()
            with os.fdopen(source_fd, "rb", closefd=False) as reader, \
                    os.fdopen(target_fd, "wb") as writer:
                for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(temp, 0o600)
            if temp.stat().st_size != size:
                raise _MediaSkip("changed")
            if expected_sha and digest.hexdigest() != expected_sha:
                raise _MediaSkip("sha_mismatch")
            os.replace(temp, destination)
        except BaseException:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
    finally:
        os.close(source_fd)
    return True, replaced


def _note_code(record):
    return str(record.get("store_note") or "").split(":")[0]


def _retry_copy(record):
    return (
        (record.get("media") or {}).get("status") == "copied"
        and not record.get("stored_path")
        and _note_code(record) in _RETRY_COPY
    )


# --- media text ------------------------------------------------------------------

def ffmpeg_bin():
    return optional_bin("ffmpeg", "WORKSPACE_DAEMON_FFMPEG_BIN")


def whisper_cli_bin():
    return optional_bin("whisper-cli", "WORKSPACE_DAEMON_WHISPER_CLI_BIN")


def pdftotext_bin():
    return optional_bin("pdftotext", "WORKSPACE_DAEMON_PDFTOTEXT_BIN")


def _tool(command, timeout, label):
    """Run one tool; (result, None) or (None, (reason, transient)).

    A non-zero exit is a property of the input (deterministic); a timeout,
    a kill by signal or an OS error (no memory, too many files) is not.
    """
    try:
        result = run(command, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, (f"{label}_timeout", True)
    except (OSError, MemoryError) as exc:
        return None, (f"{label}_error:{type(exc).__name__}", True)
    if result.returncode < 0:
        return None, (f"{label}_killed", True)
    if result.returncode != 0:
        return None, (f"{label}_failed", False)
    return result, None


def _transcribe(cfg, path, workdir):
    ffmpeg, whisper = ffmpeg_bin(), whisper_cli_bin()
    if not ffmpeg or not whisper:
        return None, "tool_missing", False
    model = cfg.get("whisper_model")
    if not model or not Path(model).is_file():
        return None, "model_missing", False
    timeout = cfg["transcribe_timeout_seconds"]
    wav = workdir / "audio.wav"
    _result, problem = _tool([
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        str(wav),
    ], timeout, "convert")
    if problem:
        return None, problem[0], problem[1]
    command = [
        whisper, "-m", str(model), "-l", cfg["whisper_language"],
        "-f", str(wav), "-nt", "-np", "-otxt", "-of", str(workdir / "out"),
    ]
    if cfg.get("whisper_threads"):
        command += ["-t", str(cfg["whisper_threads"])]
    _result, problem = _tool(command, timeout, "transcribe")
    if problem:
        return None, problem[0], problem[1]
    try:
        text = (workdir / "out.txt").read_text(encoding="utf-8").strip()
    except OSError as exc:
        return None, f"transcribe_output:{type(exc).__name__}", True
    return (text, None, False) if text else (None, "empty", False)


def _pdf_text(cfg, path, _workdir):
    binary = pdftotext_bin()
    if not binary:
        return None, "tool_missing", False
    result, problem = _tool([
        binary, "-q", "-enc", "UTF-8", "-l", str(cfg["pdf_max_pages"]),
        str(path), "-",
    ], PDF_TIMEOUT_SECONDS, "pdf")
    if problem:
        return None, problem[0], problem[1]
    text = re.sub(r"[ \t]+\n", "\n", result.stdout or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return (text[:200000], None, False) if text else (None, "no_text", False)


_EXTRACTORS = {"transcript": _transcribe, "pdf": _pdf_text}


def _extraction_kind(cfg, message):
    if not cfg["extract_budget_seconds"]:
        return None  # a budget of 0 turns media text extraction off
    media = message.get("media") or {}
    if media.get("kind") == "audio" and cfg["transcribe_audio"]:
        return "transcript"
    if media.get("kind") == "document" and cfg["pdf_text"]:
        name = str(media.get("original_name") or message.get("stored_path") or "")
        if media.get("mime") == "application/pdf" or name.lower().endswith(".pdf"):
            return "pdf"
    return None


def _tools_ready(cfg):
    model = cfg.get("whisper_model")
    return {
        "transcript": bool(
            cfg["transcribe_audio"] and ffmpeg_bin() and whisper_cli_bin()
            and model and Path(model).is_file()
        ),
        "pdf": bool(cfg["pdf_text"] and pdftotext_bin()),
    }


def _extract_paths(archive, message, kind):
    stored = Path(archive) / message["stored_path"]
    return (
        stored,
        stored.with_name(stored.name + _CACHE_SUFFIX[kind]),
        stored.with_name(stored.name + f".{kind}.failed"),
        stored.with_name(stored.name + f".{kind}.attempts"),
    )


def _run_extraction(cfg, archive, message, kind, now, counts):
    """The extraction state of one stored file, running it when due."""
    global _extract_spent
    stored, cache, failed, attempts_path = _extract_paths(archive, message, kind)
    if cache.exists():
        return {"kind": kind, "state": "done"}
    if failed.exists():
        reason = failed.read_text(encoding="utf-8").strip() or "failed"
        return {"kind": kind, "state": "failed", "reason": reason}
    if not stored.is_file():
        return {"kind": kind, "state": "failed", "reason": "stored_file_missing"}
    attempts = _read_json(attempts_path, {})
    attempts = attempts if isinstance(attempts, dict) else {}
    count = int(attempts.get("count") or 0)
    if count and now - float(attempts.get("last") or 0) < (
        EXTRACT_BACKOFF_SECONDS * 2 ** (count - 1)
    ):
        return {"kind": kind, "state": "pending",
                "reason": str(attempts.get("reason") or "retry")}
    if _extract_spent >= cfg["extract_budget_seconds"]:
        counts["deferred"] += 1
        return {"kind": kind, "state": "pending", "reason": "budget"}
    scratch = Path(archive) / ".tmp"
    _private_tree(scratch)
    workdir = Path(tempfile.mkdtemp(dir=scratch))
    started = time.monotonic()
    try:
        text, reason, transient = _EXTRACTORS[kind](cfg, stored, workdir)
    except Exception as exc:  # an extractor bug must not fail the listing
        text, reason, transient = None, f"error:{type(exc).__name__}", True
    finally:
        _extract_spent += time.monotonic() - started
        shutil.rmtree(workdir, ignore_errors=True)
    if text:
        state.write_atomic(cache, text + "\n", mode=0o600)
        try:
            attempts_path.unlink()
        except FileNotFoundError:
            pass
        counts[kind] += 1
        return {"kind": kind, "state": "done"}
    if reason in _UNAVAILABLE:
        counts["unavailable"] += 1
        return {"kind": kind, "state": "unavailable", "reason": reason}
    counts["failed"] += 1
    if transient and count + 1 < EXTRACT_MAX_ATTEMPTS:
        _write_json(attempts_path, {
            "count": count + 1, "last": now, "reason": reason,
        })
        return {"kind": kind, "state": "pending", "reason": reason}
    final = reason if not transient else f"gave_up:{reason}"
    state.write_atomic(failed, final + "\n", mode=0o600)
    try:
        attempts_path.unlink()
    except FileNotFoundError:
        pass
    return {"kind": kind, "state": "failed", "reason": final}


# --- ingest ----------------------------------------------------------------------

class _BatchError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _day_keys(day):
    """The (store|chat key) pairs a chat-day's records were spooled under."""
    return sorted({
        f"{m.get('store_id') or ''}|{m.get('chat_key')}"
        for m in day["messages"].values()
    })


def _index_entry(chat, day, mtime):
    messages = list(day["messages"].values())
    states = [(m.get("extract") or {}).get("state") for m in messages]
    return {
        "chat": chat,
        "date": day["date"],
        "label": day.get("chat_label", ""),
        "keys": _day_keys(day),
        "digest": _content_digest(day),
        "changed": max((m.get("changed_epoch") or 0) for m in messages),
        "pending_copies": any(_retry_copy(m) for m in messages),
        "pending_extract": "pending" in states,
        "unavailable_extract": "unavailable" in states,
        # The day file's mtime when this entry was computed: a day file
        # written after it (a crash before the index) is re-indexed.
        "mtime": mtime,
    }


def _mtime(path):
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


class _Ingest:
    """One listing's view of the archive plus the batches read into it."""

    def __init__(self, cfg, dry_run, ident=None):
        self.cfg = cfg
        self.dry_run = dry_run
        self.spool = Path(cfg["spool_dir"])
        self.archive = Path(cfg["archive_dir"])
        self.chats_dir = self.archive / "chats"
        self.index_path = self.chats_dir / "index.json"
        index = _read_json(self.index_path, {})
        if not isinstance(index, dict) or index.get("v") != ARCHIVE_VERSION:
            index = {}
        self.index = index
        self.index.setdefault("v", ARCHIVE_VERSION)
        self.index.setdefault("days", {})
        self.index.setdefault("media_bytes", 0)
        self.identity = ident
        self.cursor_path = _cursor_path(self.archive, self.spool)
        cursor = _read_json(self.cursor_path, None)
        self.cursor = cursor if isinstance(cursor, dict) else {}
        self.days = {}
        self.touched = set()  # chat-days whose index entry is recomputed
        self.dirty = set()    # chat-days to write back
        self.changed = set()  # chat-days whose content changed
        self.index_dirty = False
        self.batch_error = None
        self.last_merged = None
        self.max_created = int(self.cursor.get("max_created_epoch") or 0)
        self.store = None
        self.counts = dict.fromkeys((
            "batches", "messages", "new", "updated", "duplicates", "invalid",
            "no_chat_jid", "filtered", "expired", "copied", "reused",
            "too_large", "archive_full", "copy_failed", "transcript", "pdf",
            "failed", "unavailable", "deferred",
        ), 0)

    # -- days --
    def day_path(self, chat, date):
        return self.archive / _day_relpath(chat, date)

    def day(self, chat, date):
        key = identity(chat, date)
        if key not in self.days:
            loaded = _read_json(self.day_path(chat, date), None)
            if not isinstance(loaded, dict) or not isinstance(
                loaded.get("messages"), dict
            ):
                loaded = {
                    "v": ARCHIVE_VERSION, "chat": chat, "date": date,
                    "messages": {},
                }
            self.days[key] = loaded
        return self.days[key]

    def reconcile(self):
        """Re-index every day file the index lacks or has stale.

        The index is derived data: a day file written after its index entry
        (a crash between the two, even if the batch is pruned before the
        next run) is loaded and indexed again.
        """
        days = self.index["days"]
        seen = set()
        for path in self.chats_dir.glob("*/*.json"):
            chat, date = path.parent.name, path.stem
            if not LOCAL_CHAT_RE.match(chat) or not DATE_RE.match(date):
                continue
            key = identity(chat, date)
            seen.add(key)
            meta = days.get(key)
            if meta is not None and meta.get("expired"):
                continue
            if meta is None or meta.get("mtime") != _mtime(path):
                self.day(chat, date)
                self.touched.add(key)
        for key, meta in list(days.items()):
            if key not in seen and not meta.get("expired"):
                # Its file is gone (a retention run interrupted between the
                # day file and the index): keep a tombstone, so a late record
                # cannot start a partial day under the same source id.
                days[key] = {"chat": meta.get("chat"), "date": meta.get("date"),
                             "expired": True}
                self.index_dirty = True

    # -- housekeeping --
    def sweep_temp(self, now):
        """Remove temp files and extraction folders a killed run left."""
        if not self.archive.is_dir():
            return
        for path in self.archive.rglob(".*.tmp"):
            try:
                if path.is_file() and now - path.stat().st_mtime > STALE_TEMP_SECONDS:
                    path.unlink()
            except OSError:
                pass
        scratch = self.archive / ".tmp"
        if scratch.is_dir():
            for path in scratch.iterdir():
                try:
                    if now - path.stat().st_mtime > STALE_TEMP_SECONDS:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass

    def recount_media(self, now):
        """Recount the archive's media bytes now and then (self-correcting)."""
        if now - float(self.index.get("media_counted") or 0) < MEDIA_RECOUNT_SECONDS:
            return
        total = 0
        media = self.archive / "media"
        if media.is_dir():
            for path in media.rglob("wa1-*"):
                if STORED_MEDIA_RE.match(path.name) and path.is_file():
                    total += path.stat().st_size
        self.index["media_bytes"] = total
        self.index["media_counted"] = now
        self.index_dirty = True

    # -- copies --
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
            expected = media.get("sha256")
            expected = expected if isinstance(expected, str) and (
                SHA256_RE.match(expected)) else None
            copied, replaced = _copy_file(
                source_path, size, self.archive / relpath, expected,
            )
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
            self.index["media_bytes"] += size - replaced
            self.index_dirty = True
            self.counts["copied"] += 1
        else:
            self.counts["reused"] += 1
        return True

    def retry_copies(self, now):
        """Retry copies that may succeed now (transient, or a raised limit)."""
        if not self.cfg["copy_media"]:
            return
        for key, meta in list(self.index["days"].items()):
            if not meta.get("pending_copies") or meta.get("expired"):
                continue
            day = self.day(meta["chat"], meta["date"])
            self.touched.add(key)
            for record in day["messages"].values():
                if not _retry_copy(record):
                    continue
                note = record.get("store_note")
                if self.copy(record, meta["date"]):
                    record["changed_epoch"] = int(now)
                    self.changed.add(key)
                if record.get("store_note") != note or record.get("stored_path"):
                    self.dirty.add(key)

    # -- merge --
    def merge(self, message, batch_epoch, timezone):
        jid = message.get("chat_jid")
        if not isinstance(jid, str) or not jid.strip() or len(jid) > 300:
            # Without the chat's JID there is no identity that cannot merge
            # two chats (chat_key is a reusable row key), so the record is
            # not archived; the count shows how many.
            self.counts["no_chat_jid"] += 1
            return
        label = str(message.get("chat_label") or "")
        if not _wanted(self.cfg, {message["chat_key"]}, label):
            self.counts["filtered"] += 1
            return
        chat = self.identity.resolve(self.store, message["chat_key"], jid)
        record = _clean_record(dict(message, store_id=self.store))
        date = record["timestamp"][:10]
        key = identity(chat, date)
        if (self.index["days"].get(key) or {}).get("expired"):
            # Removed by archive_keep_days after its capture: a late record
            # must not start a partial day that overwrites the stored entry.
            self.counts["expired"] += 1
            return
        day = self.day(chat, date)
        self.touched.add(key)
        for field, value in (
            ("chat_label", record["chat_label"]),
            ("chat_type", record["chat_type"]),
            ("timezone", timezone),
        ):
            if value and day.get(field) != value:
                day[field] = value
                self.dirty.add(key)
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
            # Only the file is news: keep our text, sender and time.
            new_media = record["media"]
            record = {
                k: v for k, v in existing.items()
                if k not in ("stored_path", "store_note")
            }
            record["media"] = new_media
        older_media = existing.get("media") or {}
        newer_media = record.get("media") or {}
        if existing.get("stored_path") and (
            newer_media.get("status") != "copied"
            or newer_media.get("size") == older_media.get("size")
        ):
            # A re-sent or closing record never loses a copy we hold.
            record["media"] = older_media
            record["stored_path"] = existing["stored_path"]
        if existing.get("extract") and record.get("stored_path") == existing.get(
            "stored_path"
        ):
            record["extract"] = existing["extract"]
        record["first_epoch"] = existing.get("first_epoch", batch_epoch)
        same = _content(record) == _content(existing)
        if same and not _retry_copy(existing):
            self.counts["duplicates"] += 1
            return
        if same:
            # The same message again whose file we still lack: take the
            # fresh spool path and try once more.
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

    # -- batches --
    def sequence_problem(self, names):
        """Why the batch sequence looks reset, else None.

        Compared with the newest creation time ever read (not the cursor
        batch's), so a clock that stepped back does not alarm.
        """
        last = self.cursor.get("last_batch")
        if not last or not names or not BATCH_RE.match(str(last)):
            return None
        newest = self.max_created or _stamp_epoch(last)
        older_but_later = [
            name for name in names
            if name < last and _stamp_epoch(name) > newest
        ]
        gone_and_behind = last not in names and names[-1] < last
        if older_but_later or gone_and_behind:
            return (
                f"batch sequence went backwards (below {last[:11]}; the "
                "forwarder's state was probably reset): check it, then delete "
                f"{self.cursor_path.name} in archive_dir to read the spool "
                "from its start"
            )
        return None

    def read_batches(self):
        """Merge every batch after the cursor; returns the last one merged."""
        last = self.cursor.get("last_batch")
        names = sorted(
            entry.name for entry in os.scandir(self.spool)
            if BATCH_RE.match(entry.name) and entry.is_dir(follow_symlinks=False)
        )
        problem = self.sequence_problem(names)
        if problem:
            # Degraded like an unreadable batch: nothing new is read, the
            # archived days keep flowing.
            self.batch_error = problem
            log(f"whatsapp spool ERROR: {problem.split(' (')[0]}; "
                "position held")
            return None
        pending = [name for name in names if last is None or name > last]
        if last and pending and not any(name <= last for name in names):
            if int(pending[0][1:11]) - int(last[1:11]) > 1:
                log(
                    f"whatsapp spool WARN: batches after seq {int(last[1:11])} "
                    f"up to seq {int(pending[0][1:11])} are gone (pruned "
                    "before they were read?)"
                )
        for name in pending:
            try:
                created = self._merge_batch(name)
            except _BatchError as exc:
                # Hold the position here; days already archived still flow.
                self.batch_error = f"batch {name} not read ({exc.code})"
                log(
                    f"whatsapp spool ERROR: batch {name} not read "
                    f"({exc.code}); position held before it"
                )
                break
            self.counts["batches"] += 1
            self.last_merged = (name, created)
            self.max_created = max(self.max_created, created,
                                   _stamp_epoch(name))
        return self.last_merged

    def _merge_batch(self, name):
        path = self.spool / name / "batch.json"
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _BatchError(f"unreadable:{type(exc).__name__}") from None
        except UnicodeDecodeError:
            raise _BatchError("bad_encoding") from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise _BatchError("bad_json") from None
        if not isinstance(payload, dict) or payload.get("format") != FORMAT:
            raise _BatchError("not_a_spool_batch")
        if payload.get("version") != VERSION:
            version = payload.get("version")
            raise _BatchError(
                f"unsupported_version_{version}"
                if isinstance(version, int) else "unsupported_version"
            )
        batch_epoch = payload.get("created_epoch")
        messages = payload.get("messages")
        if isinstance(batch_epoch, bool) or not isinstance(batch_epoch, int) or (
            not isinstance(messages, list)
        ):
            raise _BatchError("bad_header")
        store = payload.get("store_id")
        self.store = store if isinstance(store, str) and STORE_ID_RE.match(
            store) else None
        # Recorded per batch, so a store change is known even when every
        # message of the new store is filtered out.
        self.identity.note_store(self.store)
        timezone = payload.get("timezone")
        timezone = timezone if isinstance(timezone, str) else None
        for message in messages:
            self.counts["messages"] += 1
            if not _valid_message(message):
                self.counts["invalid"] += 1
                continue
            self.merge(message, batch_epoch, timezone)
        return batch_epoch

    # -- media text --
    def extract_texts(self, now):
        """Local text extraction for stored media, within the time budget."""
        if self.dry_run or not self.cfg["extract_budget_seconds"]:
            return
        ready = _tools_ready(self.cfg)
        keys = set(self.touched) | {
            key for key, meta in self.index["days"].items()
            if not meta.get("expired") and (
                meta.get("pending_extract")
                or (meta.get("unavailable_extract") and any(ready.values()))
            )
        }
        for key in sorted(keys, key=lambda k: k.rsplit(":", 1)[-1]):
            meta = self.index["days"].get(key)
            chat, date = key.split(":")[1], key.rsplit(":", 1)[1]
            if not meta and key not in self.days:
                continue
            day = self.day(chat, date)
            # Recompute its index entry even when nothing changes now.
            self.touched.add(key)
            for record in sorted(day["messages"].values(), key=_order):
                kind = _extraction_kind(self.cfg, record)
                if not kind or not record.get("stored_path"):
                    continue
                current = record.get("extract") or {}
                if current.get("state") in _EXTRACT_FINAL:
                    continue
                if current.get("state") == "unavailable" and not ready[kind]:
                    continue
                result = _run_extraction(
                    self.cfg, self.archive, record, kind, now, self.counts,
                )
                if result != current:
                    record["extract"] = result
                    if result.get("state") in _EXTRACT_FINAL:
                        # A finished transcript is a change like a new
                        # message: it restarts the quiet period.
                        record["changed_epoch"] = int(now)
                        self.changed.add(key)
                    self.dirty.add(key)

    # -- retention --
    def expire(self, keep_days, processed, now):
        """Delete captured chat-days (and their media) older than keep_days."""
        if keep_days is None or processed is None or self.dry_run:
            return 0
        cutoff = (
            datetime.date.fromtimestamp(now) - datetime.timedelta(days=keep_days)
        ).isoformat()
        removed = 0
        for key, meta in sorted(self.index["days"].items()):
            if meta.get("expired") or meta.get("date", "9999") >= cutoff:
                continue
            entry = processed.get(f"{key}@{meta.get('digest')}")
            if not _settled(entry):
                continue  # never delete a day whose content is not captured
            day = self.day(meta["chat"], meta["date"])
            for record in day["messages"].values():
                if not record.get("stored_path"):
                    continue
                stored = self.archive / record["stored_path"]
                for path in [stored] + [
                    stored.with_name(stored.name + suffix)
                    for suffix in (".transcript.txt", ".pdf.txt",
                                   ".transcript.failed", ".pdf.failed",
                                   ".transcript.attempts", ".pdf.attempts")
                ]:
                    try:
                        size = path.stat().st_size
                        path.unlink()
                    except FileNotFoundError:
                        continue
                    if path == stored:
                        self.index["media_bytes"] = max(
                            0, self.index["media_bytes"] - size)
                try:
                    stored.parent.rmdir()
                except OSError:
                    pass
            try:
                self.day_path(meta["chat"], meta["date"]).unlink()
            except FileNotFoundError:
                pass
            self.index["days"][key] = {
                "chat": meta["chat"], "date": meta["date"], "expired": True,
            }
            self.days.pop(key, None)
            self.touched.discard(key)
            self.dirty.discard(key)
            self.index_dirty = True
            removed += 1
        return removed

    # -- persistence --
    def save(self):
        """Persist identity, chat-days, the index, then the cursor.

        The index is recomputed for every day touched this run (duplicates
        included) and records each day file's mtime, so a crash anywhere
        before the cursor moves is healed by reading the same batches again
        or, if they are gone, by reconcile().
        """
        if not self.dry_run:
            self.identity.save()
            for key in sorted(self.dirty):
                day = self.days[key]
                _write_json(self.day_path(key.split(":")[1], day["date"]), day)
        for key in sorted(self.touched):
            day = self.days.get(key)
            if day is None or not day["messages"]:
                continue
            chat = key.split(":")[1]
            mtime = _mtime(self.day_path(chat, day["date"]))
            entry = _index_entry(chat, day, mtime)
            if self.index["days"].get(key) != entry:
                self.index["days"][key] = entry
                self.index_dirty = True
        if self.dry_run:
            return
        if self.index_dirty or not self.index_path.exists():
            _write_json(self.index_path, self.index)
            self.index_dirty = False
        if self.last_merged is not None:
            name, created = self.last_merged
            self.cursor = {
                "spool": os.path.normpath(str(self.spool)),
                "last_batch": name,
                "last_created_epoch": created,
                "max_created_epoch": self.max_created,
            }
            _write_json(self.cursor_path, self.cursor)


def _content(record):
    """A record without local bookkeeping or the batch-specific file path."""
    value = {
        k: v for k, v in record.items()
        if k not in ("first_epoch", "changed_epoch", "stored_path",
                     "store_note", "backfill", "extract", "store_id")
    }
    if isinstance(value.get("media"), dict):
        value["media"] = {
            k: v for k, v in value["media"].items() if k != "path"
        }
    return value


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
    now = _now()
    ident = _Identity(state_dir(), dry_run)
    work = _Ingest(cfg, dry_run, ident)
    if not dry_run:
        work.sweep_temp(now)
        work.recount_media(now)
    work.reconcile()
    work.retry_copies(now)
    try:
        work.read_batches()
    except Exception:
        # Keep what earlier batches of this run merged (their copies
        # exist); the position stays before the failing point.
        work.save()
        raise
    work.extract_texts(now)
    work.save()
    counts = work.counts
    if work.dirty or any(counts.values()):
        last = work.last_merged[0] if work.last_merged else None
        log(
            "whatsapp spool: "
            + " ".join(f"{key}={value}" for key, value in counts.items())
            + f" chat_days_changed={len(work.changed)}"
            + (f" last_batch={last}" if last else "")
            + (f" identity_conflicts={ident.conflicts}"
               if ident.conflicts else "")
            + (" [dry-run]" if dry_run else "")
        )
    if counts["no_chat_jid"]:
        log(f"whatsapp spool WARN: {counts['no_chat_jid']} record(s) without "
            "a chat JID were not archived (no safe chat identity)")
    if len(ident.data["stores"]) > 1:
        current = ident.keys_in_store(ident.current_store())
        for key in cfg["chat_keys"]:
            if key not in current:
                log(
                    f"whatsapp WARN: chat_keys entry {key} has not appeared "
                    "since the WhatsApp store changed (chat keys are per "
                    "store; check the forwarder's --list-conversations)"
                )
    return work


# --- listing ---------------------------------------------------------------------

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
    """Newest settled capture per whatsapp source id: (time, candidate id)."""
    found = {}
    if processed is None:
        return found
    for item_id, entry in processed.items():
        if entry.get("source_kind") != "whatsapp" or not _settled(entry):
            continue
        source_id = entry.get("source_id")
        at = _epoch_of(entry.get("processed_at"))
        if isinstance(source_id, str) and at is not None and (
            at >= found.get(source_id, (0, None))[0]
        ):
            found[source_id] = (at, item_id)
    return found


def _current_keys(work, chat, meta):
    """The chat's keys in the current store (or, without store ids, the
    keys its records of this day were spooled under)."""
    store = work.identity.current_store()
    if store:
        return work.identity.keys_in_store(store, chat)
    keys = set()
    for value in meta.get("keys") or []:
        name, _sep, key = value.rpartition("|")
        if not name:
            keys.add(int(key))
    return keys


def candidates(source):
    """One candidate per chat-day with uncaptured content; see the docstring."""
    cfg = settings(source)
    processed = source.get("_processed")
    work = ingest(source)
    now = _now()
    expired = work.expire(cfg["archive_keep_days"], processed, now)
    if expired:
        work.save()
        log(f"whatsapp archive: removed {expired} captured chat-day(s) "
            f"older than {cfg['archive_keep_days']} days")
    quiet = cfg["quiet_minutes"] * 60
    max_wait = cfg["max_wait_hours"] * 3600
    last_capture = _last_captures(processed)
    captured_ids = _load_captures()["days"] if last_capture else {}
    ready, waiting = [], []
    settled = shrinking = 0
    for key, meta in sorted(work.index["days"].items()):
        if meta.get("expired"):
            continue
        chat = meta["chat"]
        if not _wanted(cfg, _current_keys(work, chat, meta),
                       meta.get("label") or ""):
            continue
        candidate_id = f"{key}@{meta['digest']}"
        existing = processed.get(candidate_id) if processed is not None else None
        if existing is not None and _settled(existing):
            settled += 1
            continue
        day = work.day(chat, meta["date"])
        since, last_id = last_capture.get(key, (0, None))
        if last_id and not cfg["recapture_after_rebuild"]:
            before = (captured_ids.get(key) or {}).get(last_id.split("@")[-1])
            present = {_id_prefix(m) for m in day["messages"]}
            if before is not None and not set(before) <= present:
                # This day holds fewer messages than its last capture (an
                # archive rebuilt from what is left in the spool): wait
                # until it holds all of them again.
                shrinking += 1
                continue
        changes = [m.get("changed_epoch") or 0 for m in day["messages"].values()]
        if not changes:
            continue
        uncaptured = [epoch for epoch in changes if epoch > since]
        quiet_for = now - max(changes)
        waited = now - min(uncaptured) if uncaptured else 0
        due = (
            existing is not None  # a failed capture retries without waiting
            or (quiet_for >= quiet and not (
                meta.get("pending_extract") and cfg["extract_budget_seconds"]))
            or (bool(uncaptured) and waited >= max_wait)
        )
        candidate = {
            "id": candidate_id,
            # Logged by the runner: never a chat label or a name.
            "title": f"WhatsApp {chat} {meta['date']}",
            "raw": {
                "source_id": key,
                "chat": chat,
                "date": meta["date"],
                "digest": meta["digest"],
                "_dry_run": work.dry_run,
                # The merged chat-day travels with the candidate so a dry run
                # (which writes nothing) and the real run read the same data.
                "_day": day,
            },
        }
        (ready if due else waiting).append(candidate)
    limit = int(source.get("max_results") or 0)
    ready.sort(key=lambda c: (c["raw"]["date"], c["raw"]["chat"]))
    if limit and len(ready) > limit:
        waiting.extend(ready[limit:])
        ready = ready[:limit]
    for candidate in waiting:
        candidate["raw"]["_deferred"] = True
    if shrinking:
        log(
            f"whatsapp WARN: {shrinking} chat-day(s) hold fewer messages than "
            "their last capture (a rebuilt archive?); they are captured again "
            "once they hold all of them"
        )
    log(
        f"whatsapp: chat_days ready={len(ready)} waiting={len(waiting)} "
        f"settled={settled}"
    )
    listing = Listing(ready + waiting)
    listing.degraded = work.batch_error
    return listing


# --- fetch -----------------------------------------------------------------------

def _hhmm(message):
    return message["timestamp"][11:16]


def _clean_name(value, limit):
    """A display name or label on one line, redacted like message text."""
    return redact_secrets(" ".join(str(value or "").split())[:limit])


def _sender(message):
    name = _clean_name(message.get("sender") or "Unknown", 80)
    return f"{name} [owner]" if message.get("from_me") else name


def _size_label(size):
    if not isinstance(size, int) or size < 0:
        return None
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def _attachment_facts(message, name, archive, exact=False):
    media = message.get("media") or {}
    kind = media.get("kind")
    facts = [kind if kind in MEDIA_KINDS else "file"]
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
    status = media.get("status")
    if message.get("stored_path"):
        where = f"stored at {Path(archive) / message['stored_path']}"
    elif status == "copied":
        where = f"not stored ({message.get('store_note') or 'pending copy'})"
    elif status == "gave_up":
        reason = media.get("reason")
        reason = reason if reason in ("copy_failed", "expired", "watch_full") \
            else None
        where = ("not available (the forwarder stopped waiting for the file"
                 + (f": {reason})" if reason else ")"))
    else:
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


def _redacted_view(message, archive, cfg):
    """The message's text, file name and extracted text, each redacted once.

    Both the prompt and the stored verbatim block are rendered from these,
    so they can never disagree about what was redacted (a pattern spanning a
    line break is caught on the raw text, before any prefix is added).
    """
    media = message.get("media") or {}
    name = " ".join(str(media.get("original_name") or "").split())[:120]
    extract = message.get("extract") or {}
    if _extraction_kind(cfg, message) is None:
        extract = {}  # extraction is off for this kind (or the budget is 0)
    extracted = None
    if extract.get("state") == "done" and message.get("stored_path"):
        _stored, cache, _f, _a = _extract_paths(archive, message, extract["kind"])
        try:
            extracted = redact_secrets(cache.read_text(encoding="utf-8"))
        except OSError:
            extract = {"kind": extract["kind"], "state": "failed",
                       "reason": "cache_missing"}
    return {
        "text": redact_secrets(message.get("text") or ""),
        "name": redact_secrets(name) if name else "",
        "extract": extract,
        "extracted": extracted,
    }


def _render(messages, archive, cfg):
    """(prompt lines, verbatim lines) for one chat-day."""
    prompt, verbatim = [], []
    for message in messages:
        view = _redacted_view(message, archive, cfg)
        text = view["text"]
        lines = text.split("\n")
        # Continuation lines are indented, so a line inside a message that
        # looks like "[12:00] Someone: ..." never reads as its own message.
        prompt.append(f"[{_hhmm(message)}] {_sender(message)}: {lines[0]}")
        prompt.extend(f"    {line}" for line in lines[1:])
        sender = _clean_name(message.get("sender"), 200)
        verbatim.append(
            f"- {message['timestamp']} · {sender} · {message['id']}"
            + (" · from_me" if message.get("from_me") else "")
        )
        if text:
            verbatim.extend(f"  > {line}" for line in lines)
        if message.get("media"):
            facts, where = _attachment_facts(message, view["name"], archive)
            prompt.append(
                f"  attachment: {', '.join(facts)}"
                + (", stored" if message.get("stored_path") else f", {where}")
            )
            exact, _where = _attachment_facts(
                message, view["name"], archive, exact=True)
            verbatim.append(f"  attachment: {' · '.join(exact)} · {where}")
        extract = view["extract"]
        kind = extract.get("kind")
        if not kind:
            continue
        if view["extracted"]:
            limit = (
                cfg["transcript_max_chars"] if kind == "transcript"
                else cfg["pdf_max_chars"]
            )
            shown, cut = _bounded(view["extracted"], limit)
            label = _EXTRACT_LABEL[kind] + (" (truncated)" if cut else "")
            prompt.append(f"  {label}:")
            prompt.extend(f"    {line}" for line in shown.split("\n"))
            verbatim.append(f"  {label}:")
            verbatim.extend(f"  | {line}" for line in shown.split("\n"))
        else:
            word = "pending" if extract.get("state") == "pending" else "none"
            note = f"  {kind}: {word} ({extract.get('reason') or 'unknown'})"
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
        name = _clean_name(message.get("sender"), 80)
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
        day = _read_json(archive / _day_relpath(raw["chat"], raw["date"]), None)
        if not isinstance(day, dict):
            raise RuntimeError(
                f"whatsapp archive has no record of {raw['source_id']}")
    if not dry_run and cfg["copy_media"] and any(
        _retry_copy(m) for m in day["messages"].values()
    ):
        # Last chance before the ledger settles this chat-day: a file the
        # listing could not copy is retried while the spool still has it.
        # Work on the day file as it is now (never write back the listing's
        # snapshot). The index digest is left alone on purpose: this capture
        # already shows the stored file, so it must not count as a new
        # change; its recorded mtime follows the file.
        work = _Ingest(cfg, dry_run=False)
        path = work.day_path(raw["chat"], raw["date"])
        current = _read_json(path, None)
        if isinstance(current, dict) and isinstance(current.get("messages"), dict):
            day = current
            stored = [work.copy(m, raw["date"])
                      for m in day["messages"].values() if _retry_copy(m)]
            if any(stored):
                _write_json(path, day)
                entry = work.index["days"].get(raw["source_id"])
                if isinstance(entry, dict):
                    entry["mtime"] = _mtime(path)
                _write_json(work.index_path, work.index)
    messages = sorted(day["messages"].values(), key=_order)
    if not dry_run:
        _record_capture(raw["source_id"], raw.get("digest"),
                        [m["id"] for m in messages])
    prompt_lines, verbatim_lines = _render(messages, archive, cfg)
    chat = raw["chat"]
    label = _clean_name(day.get("chat_label"), 120)
    chat_type = str(day.get("chat_type") or "other")
    timezone = str(day.get("timezone") or "")
    header = (
        f"Chat: {label or chat} · {chat_type} · {chat} · {raw['date']}"
        + (f" ({timezone})" if timezone else "")
    )
    verbatim = (
        "## WhatsApp messages (verbatim)\n\n"
        "Each message: `- <time> · <sender> · <message id>` (plus "
        "` · from_me` for the owner's own), its text on `  > ` lines exactly "
        "as sent, then attachments and locally extracted text.\n\n"
        f"{header}\n\n" + "\n".join(verbatim_lines)
    )
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
        "title": f"WhatsApp: {label or chat} {raw['date']}",
        "date": raw["date"],
        # Every field was redacted once in _redacted_view (chats carry codes
        # and tokens); the joined views are not redacted again.
        "body": _bounded_prompt(prompt_lines, cfg["max_body_chars"]),
        # Appended verbatim to the stored entry by the memory sink, after the
        # model's summary, so original text survives the paraphrase.
        "memory_appendix": verbatim,
        "frontmatter": {
            "whatsapp_chat_ref": chat,
            "whatsapp_chat_label": label,
            "whatsapp_chat_type": chat_type,
            "whatsapp_date": raw["date"],
            "whatsapp_timezone": timezone or None,
            "whatsapp_message_ids": [message["id"] for message in messages],
            "whatsapp_participants": _participants(messages),
            "whatsapp_attachments": attachments,
            "whatsapp_content_hash": raw.get("digest"),
            "message_count": len(messages),
            "first_message_at": messages[0]["timestamp"] if messages else None,
            "latest_message_at": messages[-1]["timestamp"] if messages else None,
        },
    }
