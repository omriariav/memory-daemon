"""The inbox's Context hook: a configured command that returns extra context
for one item (what a person is, what a ticket says, what a doc contains).

No Textual import; every failure becomes an `Outcome`. The command is any
program, run without a shell with the item as JSON on stdin and nothing else
(item text is never put in argv), under the same process-group, timeout and
output-cap handling as the save command (a timeout asks the group to stop with
SIGTERM and kills it SAVE_TERM_GRACE_S later). It must be read-only: the
inbox runs it on its own whenever the Context pane is open and the selection
changes.

Everything the command prints is untrusted display text. It is validated and
cleaned here (`parse_context_response`) before any widget sees it, and cut to
fixed caps: at most MAX_SECTIONS sections, MAX_LINES lines each, MAX_LINE_CHARS
characters a line, MAX_TOTAL_CHARS in all, and the raw answer is limited to
MAX_RAW_BYTES.

`ContextCache` keeps results in memory per (item id, item ts) for a TTL.
"""
import json
import threading
import time
from collections import namedtuple

from . import items as itm
from . import sources

PROTOCOL_VERSION = 1
MAX_SECTIONS = 6
MAX_LINES = 40
MAX_LINE_CHARS = 300
TITLE_MAX = 80
MAX_TOTAL_CHARS = 20000
MAX_RAW_BYTES = 256 * 1024
MESSAGE_MAX = 300
TERM_GRACE_S = 5
CACHE_TTL_S = 600.0
ERROR_TTL_S = 30.0
CACHE_MAX_ENTRIES = 200

Outcome = namedtuple("Outcome", "ok sections message")


def context_request(item):
    """The stdin of the command: `{"v": 1, "action": "context", "item": ...}`."""
    return {"v": PROTOCOL_VERSION, "action": "context", "item": item}


def parse_context_response(text):
    """Validate and clean the command's stdout.

    `{"v": 1, "ok": bool, "sections": [{"title": str, "lines": [str]}],
    "message": str|null}`. `sections` and `message` may be left out. Returns an
    `Outcome` (sections cleaned and capped). Raises `ItemError` when the answer
    does not follow the format.
    """
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise itm.ItemError(f"response is not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise itm.ItemError("response is not a JSON object")
    if doc.get("v") != PROTOCOL_VERSION or isinstance(doc.get("v"), bool):
        raise itm.ItemError(f"response 'v' must be {PROTOCOL_VERSION}")
    ok = doc.get("ok")
    if not isinstance(ok, bool):
        raise itm.ItemError("response needs ok (bool)")
    message = doc.get("message")
    if message is not None and not isinstance(message, str):
        raise itm.ItemError("response message must be a string or null")
    message = (itm.truncate(itm.clean_line(message), MESSAGE_MAX)
               if message else None)
    raw_sections = doc.get("sections", [])
    if raw_sections is None:
        raw_sections = []
    if not isinstance(raw_sections, list):
        raise itm.ItemError("response sections must be a list")
    sections, budget = [], MAX_TOTAL_CHARS
    for n, sec in enumerate(raw_sections[:MAX_SECTIONS]):
        if (not isinstance(sec, dict) or not isinstance(sec.get("title"), str)
                or not isinstance(sec.get("lines"), list)
                or not all(isinstance(x, str) for x in sec["lines"])):
            raise itm.ItemError(
                f"sections[{n}] needs title (str) and lines (list of str)")
        if budget <= 0:
            break
        title = itm.truncate(itm.clean_line(sec["title"]), TITLE_MAX) \
            or "(untitled)"
        budget -= len(title)
        lines = []
        for raw in sec["lines"][:MAX_LINES]:
            if budget <= 0:
                break
            line = itm.truncate(itm.clean_line(raw), MAX_LINE_CHARS)
            budget -= len(line)
            lines.append(line)
        sections.append({"title": title, "lines": lines})
    return Outcome(ok, sections, message)


def run_context(cfg, item):
    """Run the context command on one v1 item dict. Never raises.

    Returns an `Outcome`: `ok` with the cleaned sections, or not ok with a
    one-line reason in `message` (the command's own message for `ok: false`,
    else a fixed kind such as a timeout or an exit code)."""
    try:
        payload = json.dumps(context_request(item)).encode("utf-8")
        status, code, out, err = sources._run_capped(
            sources._expand(cfg["argv"]), cfg["timeout_s"],
            stdin_bytes=payload, term_grace_s=TERM_GRACE_S,
            max_bytes=MAX_RAW_BYTES)
    except (OSError, ValueError, TypeError) as exc:
        return Outcome(False, [], "cannot run: "
                       f"{getattr(exc, 'strerror', None) or exc}")
    if status == "timeout":
        return Outcome(False, [], f"timed out after {cfg['timeout_s']:g}s")
    if status == "overflow":
        return Outcome(False, [], "response is too large")
    if code != 0:
        detail = sources._stderr_tail(err)
        return Outcome(False, [], f"exit {code}"
                       + (f": {detail}" if detail else ""))
    try:
        res = parse_context_response(out.decode("utf-8", "replace"))
    except itm.ItemError as exc:
        return Outcome(False, [], f"bad response: {exc}")
    if res.ok:
        return res
    return Outcome(False, [], res.message
                   or "the context command reported a failure")


class ContextCache:
    """In-memory results keyed by (item id, item ts), valid for `ttl_s`
    (errors for the shorter `error_ttl_s`, so a broken command is retried soon
    but not on every keystroke). Oldest entries go first past `max_entries`."""

    def __init__(self, ttl_s=CACHE_TTL_S, error_ttl_s=ERROR_TTL_S,
                 max_entries=CACHE_MAX_ENTRIES, clock=time.time):
        self.ttl_s, self.error_ttl_s = ttl_s, error_ttl_s
        self.max_entries, self.clock = max_entries, clock
        self._rows = {}
        self._lock = threading.Lock()

    def get(self, key, ts):
        """The cached entry `{"ok", "sections", "message", "at"}` or None
        (absent or expired; an expired entry is dropped)."""
        with self._lock:
            row = self._rows.get((key, ts))
            if row is None:
                return None
            ttl = self.ttl_s if row["ok"] else self.error_ttl_s
            if self.clock() - row["at"] >= ttl:
                del self._rows[(key, ts)]
                return None
            self._rows[(key, ts)] = self._rows.pop((key, ts))  # most recent
            return dict(row)

    def put(self, key, ts, outcome):
        row = {"ok": outcome.ok, "sections": outcome.sections,
               "message": outcome.message, "at": self.clock()}
        with self._lock:
            self._rows.pop((key, ts), None)
            self._rows[(key, ts)] = row
            while len(self._rows) > self.max_entries:
                del self._rows[next(iter(self._rows))]
        return dict(row)

    def drop(self, key, ts):
        with self._lock:
            self._rows.pop((key, ts), None)

    def __len__(self):
        return len(self._rows)
