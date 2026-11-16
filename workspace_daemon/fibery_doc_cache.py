"""Read-only cache of Fibery documents in local files.

For each configured document, one headless-Claude bridge call transports a
single ``read_document`` result (the same pinned, exact-arguments fetch the
other Fibery routines use) and the text is stored as
``<out_dir>/<key>.json``: ``{"v": 1, "key", "fetched_at", "text"}``, 0600 in a
0700 directory, written atomically. Nothing is ever written to Fibery.

Which documents refresh. With ``only_upcoming`` (the default) a document is a
candidate only when the calendar snapshot (``state/calendar-snapshot.json``)
has a timed, non-cancelled, non-declined event that starts within
``lookahead_h`` hours and has exactly two attendees (and the snapshot says the
count is known, ``attendees_known``), the other of whom has an
email listed in that document's ``match_emails`` (compared case-insensitively).
A candidate is fetched when its cache file is missing, unreadable or older
than ``max_age_h``. With ``only_upcoming: false`` every document is a
candidate.

Logs carry counts and document keys only: never a secret, an email or text.
Failures are reported with exception types and redacted messages.
"""
import datetime
import json
import os
import re
import stat
from pathlib import Path

from . import state
from .shell import log

CACHE_VERSION = 1
DEFAULT_OUT_DIR = "state/fibery-docs"
DEFAULT_LOOKAHEAD_H = 24
DEFAULT_MAX_AGE_H = 12
# A snapshot older than this (hours) is not trusted to say what is upcoming:
# the smaller of twice the lookahead and this cap.
SNAPSHOT_MAX_AGE_H = 6
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
READ_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__read_document$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ANY_EMAIL_RE = re.compile(r"[^@\s,;:<>()\[\]\"']+@[^@\s,;:<>()\[\]\"']+")
MAX_DOCS = 50
MAX_EMAILS = 50
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class DocsFileError(RuntimeError):
    """The docs file cannot be used (the message never carries its content)."""


# --- config ------------------------------------------------------------------

def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_docs(docs, label="docs"):
    """`(clean docs, problems)` for a list of ``{key, document_secret,
    match_emails}``. Only those three keys are read and validated; any other
    key of an entry (a display name, a link) is ignored and dropped from the
    clean entry. Problem texts never include a secret or an email."""
    problems = []
    if not isinstance(docs, list) or not docs:
        return [], [f"{label} must be a non-empty list"]
    if len(docs) > MAX_DOCS:
        return [], [f"{label} has more than {MAX_DOCS} entries"]
    clean, keys = [], set()
    for i, doc in enumerate(docs):
        where = f"{label}[{i}]"
        start = len(problems)
        if not isinstance(doc, dict):
            problems.append(f"{where} must be a mapping")
            continue
        key = doc.get("key")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            problems.append(f"{where}.key must be 1-64 chars of a-z, 0-9, _ "
                            "and - (it names the cache file)")
        elif key in keys:
            problems.append(f"{where}.key is used twice")
        else:
            keys.add(key)
        secret = doc.get("document_secret")
        if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
            problems.append(f"{where}.document_secret is required "
                            "(letters, digits, - and _ only)")
        emails = doc.get("match_emails")
        if (not isinstance(emails, list) or not emails
                or len(emails) > MAX_EMAILS
                or not all(isinstance(e, str) and EMAIL_RE.fullmatch(e.strip())
                           for e in emails)):
            problems.append(f"{where}.match_emails must be a non-empty list "
                            f"(up to {MAX_EMAILS}) of email addresses")
        if len(problems) == start:
            clean.append({
                "key": key, "document_secret": secret,
                "match_emails": sorted({e.strip().lower() for e in emails})})
    return clean, problems


def config_problems(rid, cfg):
    """Problems with a `fibery_doc_cache` maintenance block (the docs file is
    not opened here: it is read, and checked the same way, at run time)."""
    problems = []
    tool = cfg.get("read_tool")
    if not (isinstance(tool, str) and READ_TOOL_RE.fullmatch(tool)):
        problems.append(
            f"{rid}: maintenance.read_tool must be an exact MCP tool name "
            "ending in __read_document")
    has_docs, has_file = "docs" in cfg, "docs_file" in cfg
    if has_docs == has_file:
        problems.append(f"{rid}: maintenance needs exactly one of docs or "
                        "docs_file")
    if has_docs:
        problems.extend(f"{rid}: maintenance.{p}"
                        for p in check_docs(cfg["docs"])[1])
    if has_file:
        path = cfg["docs_file"]
        if (not isinstance(path, str) or not path.strip()
                or "\x00" in path or "\n" in path
                or not (path.startswith("/") or path.startswith("~"))):
            problems.append(f"{rid}: maintenance.docs_file must be an "
                            "absolute or ~ path")
    look = cfg.get("lookahead_h", DEFAULT_LOOKAHEAD_H)
    if not _num(look) or not 1 <= look <= 168:
        problems.append(f"{rid}: maintenance.lookahead_h must be a number "
                        "from 1 to 168")
    age = cfg.get("max_age_h", DEFAULT_MAX_AGE_H)
    if not _num(age) or not 0 < age <= 720:
        problems.append(f"{rid}: maintenance.max_age_h must be a number "
                        "above 0, up to 720")
    out_dir = cfg.get("out_dir", DEFAULT_OUT_DIR)
    if (not isinstance(out_dir, str) or not out_dir.strip()
            or "\x00" in out_dir or "\n" in out_dir):
        problems.append(f"{rid}: maintenance.out_dir must be a path")
    if not isinstance(cfg.get("only_upcoming", True), bool):
        problems.append(f"{rid}: maintenance.only_upcoming must be true or "
                        "false")
    if "model" in cfg and not (isinstance(cfg["model"], str)
                               and cfg["model"].strip()):
        problems.append(f"{rid}: maintenance.model must be a non-empty string")
    return problems


def _resolve(base_dir, value):
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else Path(base_dir) / path


def load_docs(cfg, base_dir):
    """The configured documents, from `docs` or from the private `docs_file`.

    The file must be a JSON list of the same shape, readable by its owner
    only (a group- or world-readable file of secrets is refused). Raises
    DocsFileError with a message that never carries the file's content."""
    if "docs" in cfg:
        docs, problems = check_docs(cfg["docs"])
        if problems:
            raise DocsFileError("docs: " + "; ".join(problems))
        return docs
    path = _resolve(base_dir, cfg["docs_file"])
    # Open first (never through a symlink), then judge the open file: what is
    # checked is what is read.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise DocsFileError(
            f"cannot read the docs file: {exc.strerror or 'error'}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DocsFileError("the docs file is not a regular file")
        if info.st_uid != os.getuid():
            raise DocsFileError("the docs file is not owned by you")
        if info.st_mode & 0o077:
            raise DocsFileError(
                "the docs file is readable by others; chmod 600 it")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = None
            raw = json.loads(handle.read())
    except DocsFileError:
        raise
    except (OSError, ValueError) as exc:
        raise DocsFileError(
            f"cannot read the docs file: {type(exc).__name__}") from None
    finally:
        if fd is not None:
            os.close(fd)
    docs, problems = check_docs(raw, "docs_file")
    if problems:
        raise DocsFileError("; ".join(problems))
    return docs


# --- selection ---------------------------------------------------------------

def _start(value):
    """A timed start as an aware datetime, or None (all-day, malformed)."""
    if not isinstance(value, str) or _DATE_ONLY.match(value):
        return None
    try:
        when = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


def _email(person):
    value = person.get("email") if isinstance(person, dict) else None
    return value.strip().lower() if isinstance(value, str) and value.strip() \
        else None


def other_party(row):
    """The lower-cased email of the one other person in a two-person event:
    the single non-self attendee who has not declined (the snapshot keeps
    declined guests but does not count them), else the organizer when the
    list names no one but the owner and the organizer is not the owner. None
    when it cannot be told."""
    people = row.get("attendees")
    if isinstance(people, list):
        not_me = [p for p in people if isinstance(p, dict) and not p.get("self")]
        live = [p for p in not_me
                if p.get("response") != "declined" and not p.get("group")
                and _email(p)]
        if len(live) == 1:
            return _email(live[0])
        if not_me:
            return None
    org = row.get("organizer")
    if isinstance(org, dict) and not org.get("self"):
        return _email(org)
    return None


def qualifying_emails(snapshot, now, lookahead_h):
    """The set of other-party emails (lower-cased) of the events that count:
    timed, not cancelled, not declined, starting within `lookahead_h` hours
    of `now`, with exactly two attendees whose number the snapshot knows
    (`attendees_known` true; older rows without it are skipped)."""
    found = set()
    events = (snapshot or {}).get("events")
    if not isinstance(events, dict):
        return found
    end = now + datetime.timedelta(hours=lookahead_h)
    for row in events.values():
        if not isinstance(row, dict):
            continue
        if row.get("status") == "cancelled" or row.get("my_rsvp") == "declined":
            continue
        count = row.get("attendee_count")
        if isinstance(count, bool) or count != 2:
            continue
        if row.get("attendees_known") is not True:
            continue    # the count is only a floor (hidden or group guests)
        start = _start(row.get("start"))
        if start is None or not now <= start <= end:
            continue
        who = other_party(row)
        if who:
            found.add(who)
    return found


def qualifying_keys(docs, snapshot, now, lookahead_h):
    """Keys of the documents with a qualifying upcoming event."""
    emails = qualifying_emails(snapshot, now, lookahead_h)
    return {d["key"] for d in docs
            if emails & {e.lower() for e in d["match_emails"]}}


# --- cache files -------------------------------------------------------------

def cache_path(out_dir, key):
    return Path(out_dir) / f"{key}.json"


def cached_at(out_dir, key):
    """When the key's cache file was fetched (aware datetime), or None."""
    try:
        doc = json.loads(cache_path(out_dir, key).read_text(encoding="utf-8"))
        if doc["v"] != CACHE_VERSION or doc["key"] != key \
                or not isinstance(doc["text"], str):
            return None
        when = datetime.datetime.fromisoformat(doc["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when


def write_cache(out_dir, key, text, now):
    """Atomically write the cache file (0600) in a 0700 directory."""
    state.ensure_private_dir(out_dir)
    body = json.dumps({
        "v": CACHE_VERSION, "key": key,
        "fetched_at": now.astimezone(datetime.timezone.utc).isoformat(),
        "text": text}, ensure_ascii=False, indent=2) + "\n"
    state.write_atomic(cache_path(out_dir, key), body, mode=0o600)


def snapshot_age_h(snapshot, now):
    """Hours since the snapshot was fetched, or None when it cannot be told."""
    try:
        when = datetime.datetime.fromisoformat(
            str(snapshot["fetched_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return (now - when).total_seconds() / 3600


# A refused login or a rate limit: stop only when a second document fails the
# same way (one document's own access problem must not block the others).
_REFUSAL_MARKERS = {
    "auth": ("unauthenticated", "invalid api key", "auth failed"),
    "rate limit": ("rate limit", "too many requests", "quota exceeded"),
}


def _refusal(exc):
    """The refusal category of a failure ("auth", "rate limit"), or None. A
    timeout, a refused or reset connection is never one."""
    text = str(exc).casefold()
    for category, markers in _REFUSAL_MARKERS.items():
        if any(m in text for m in markers):
            return category
    return None


def _redact(message, secrets):
    """A message with every known secret and anything email-shaped removed."""
    text = str(message)
    for secret in secrets:
        text = text.replace(secret, "<secret>")
    return ANY_EMAIL_RE.sub("<email>", text)[:200]


# --- run ---------------------------------------------------------------------

def run(cfg, base_dir, dry_run=False, routine_id="fibery-doc-cache", now=None):
    """Refresh the cache files that need it; returns a report.

    Raises RuntimeError after trying every document when any refresh failed
    (the runner then shows the routine as failing). Documents are tried oldest
    cache first (a missing cache first), so one that always fails cannot keep
    the others from being tried. A bridge guard stop (paused, budget) ends the
    run at once and is re-raised for the runner.

    Only two things end the run early. A real outage (the machine slept, or
    the network is down: `sleepclock.is_outage` or `text_is_outage`) stops it
    at once and raises a `BridgeOutage` carrying the outage note, so the run
    reads as an outage in `status`; the runner still counts it as an error in
    its totals. A refused login or a rate limit stops it only when a second
    document fails the same way. A timeout, a refused or a reset connection
    never stops it: that document fails and the next one is tried.

    With `only_upcoming`, a calendar snapshot older than the smaller of
    twice `lookahead_h` and SNAPSHOT_MAX_AGE_H hours is not used: nothing is
    refreshed (and this is logged) rather than guessing from old events."""
    from . import calendar_cache, claude_bridge, sleepclock
    from . import fibery_tasks_inbound as fti
    now = now or datetime.datetime.now(datetime.timezone.utc)
    docs = load_docs(cfg, base_dir)
    out_dir = _resolve(base_dir, cfg.get("out_dir", DEFAULT_OUT_DIR))
    only_upcoming = cfg.get("only_upcoming", True)
    max_age = datetime.timedelta(hours=cfg.get("max_age_h", DEFAULT_MAX_AGE_H))
    report = {"ok": True, "dry_run": bool(dry_run), "docs": len(docs),
              "qualifying": 0, "refreshed": 0, "fresh": 0, "failed": 0,
              "failed_keys": [], "stale_snapshot": False}
    if only_upcoming:
        snapshot = calendar_cache.load_snapshot(base_dir)
        if snapshot is None:
            log(f"routine={routine_id} fibery_doc_cache: no calendar "
                "snapshot; nothing to refresh")
            return report
        lookahead = cfg.get("lookahead_h", DEFAULT_LOOKAHEAD_H)
        limit = min(2 * lookahead, SNAPSHOT_MAX_AGE_H)
        age = snapshot_age_h(snapshot, now)
        if age is None or age > limit:
            report["stale_snapshot"] = True
            log(f"routine={routine_id} fibery_doc_cache: the calendar "
                "snapshot is "
                + ("unreadable" if age is None else f"{age:.1f}h old")
                + f" (limit {limit:g}h); not refreshing")
            return report
        candidates = qualifying_keys(docs, snapshot, now, lookahead)
    else:
        candidates = {d["key"] for d in docs}
    report["qualifying"] = len(candidates)
    secrets = [d["document_secret"] for d in docs]
    oldest = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    stamps = {d["key"]: cached_at(out_dir, d["key"]) for d in docs}
    refusals = {}                 # category -> keys that failed that way
    for doc in sorted((d for d in docs if d["key"] in candidates),
                      key=lambda d: stamps[d["key"]] or oldest):
        key = doc["key"]
        when = stamps[key]
        if when is not None and now - when <= max_age:
            report["fresh"] += 1
            continue
        if dry_run:
            log(f"routine={routine_id} fibery_doc_cache: dry-run would "
                f"refresh key={key}")
            continue
        fetch_cfg = {"read_tool": cfg["read_tool"],
                     "document_secret": doc["document_secret"],
                     "model": cfg.get("model", "haiku")}
        try:
            text = fti.fetch(fetch_cfg, base_dir, exact_args=True)
            fti.parse_document(text)       # sanity: it is a document
            write_cache(out_dir, key, text, now)
        except claude_bridge.GUARD_STOPS:
            raise
        except Exception as exc:
            report["failed"] += 1
            report["failed_keys"].append(key)
            detail = f"{type(exc).__name__}: {_redact(exc, secrets)}"
            log(f"routine={routine_id} fibery_doc_cache: refresh failed "
                f"key={key} ({detail})")
            if sleepclock.is_outage(exc) or sleepclock.text_is_outage(exc):
                log(f"routine={routine_id} maintenance=fibery_doc_cache "
                    "stopped: an outage; the remaining documents are not "
                    "tried")
                raise claude_bridge.BridgeOutage(
                    f"fibery_doc_cache stopped at key={key} "
                    f"({sleepclock.OUTAGE_NOTE})") from None
            category = _refusal(exc)
            if category:
                refusals.setdefault(category, set()).add(key)
                if len(refusals[category]) >= 2:
                    log(f"routine={routine_id} maintenance=fibery_doc_cache "
                        f"stopped: {category} failed on two documents; the "
                        "remaining documents are not tried")
                    raise RuntimeError(
                        f"fibery_doc_cache stopped at key={key}: "
                        f"{category} failed on two documents "
                        f"({_redact(exc, secrets)})") from None
            continue
        report["refreshed"] += 1
        log(f"routine={routine_id} fibery_doc_cache: refreshed key={key}")
    if report["failed"]:
        report["ok"] = False
        keys = ", ".join(report["failed_keys"])
        log(f"routine={routine_id} maintenance=fibery_doc_cache ERROR: "
            f"{report['failed']} refresh(es) failed: {keys}")
        raise RuntimeError(
            f"fibery_doc_cache: {report['failed']} of "
            f"{report['qualifying']} refresh(es) failed ({keys})")
    return report
