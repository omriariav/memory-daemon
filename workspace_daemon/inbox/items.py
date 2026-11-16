"""Inbox item format v1: validation, sanitising and source-output parsing.

The format is documented in docs/inbox.md. Anything that comes from outside
(an events line, a command source's stdout) is untrusted display text: it is
stripped of control characters, ANSI escapes and bidi overrides before it can
reach a terminal.
"""
import json
import math
import re

SCHEMA_VERSION = 1

SOURCES = ("slack", "email", "calendar", "recording", "alert", "drive", "other")
SECTIONS = ("urgent", "needs_reply", "waiting", "fyi")
URGENCY_LEVELS = ("urgent", "digest")
ACTIONS = ("reply", "ack", "done", "snooze", "nudge", "open")

GIST_MAX = 120
ID_MAX = 200
FIELD_MAX = 200
LINK_MAX = 2000
TEXT_MAX = 8000
THREAD_MAX = 100
OUTPUT_MAX_BYTES = 4 * 1024 * 1024
ITEMS_MAX = 2000

# Bidi embeddings, overrides and isolates can reorder surrounding text to
# spoof it; plain marks (LRM, RLM, ALM) are harmless and kept.
_BIDI_CONTROLS = "‪‫‬‭‮⁦⁧⁨⁩"
_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC
    r"|\x1b[@-Z\\-_]"              # two-byte escapes
)
_CONTROLS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f" + _BIDI_CONTROLS + "  ]"
)
_WS = re.compile(r"\s+")
# http(s) URL with no whitespace, C0/C1 controls or DEL anywhere. fullmatch
# (not `$`) so a trailing newline cannot slip through.
_LINK = re.compile(r"https?://[^\s/?#\x00-\x1f\x7f-\x9f][^\s\x00-\x1f\x7f-\x9f]*")


class ItemError(ValueError):
    """An item or a source output does not follow the format."""


def clean_line(value):
    """One-line display text: no escapes, controls or bidi overrides."""
    text = _ANSI.sub("", str(value if value is not None else ""))
    text = _CONTROLS.sub("", text.replace("\t", " ").replace("\n", " ")
                         .replace("\r", " "))
    return _WS.sub(" ", text).strip()


def clean_text(value):
    """Multi-line display text: newlines and tabs kept, everything else as
    `clean_line`."""
    text = _ANSI.sub("", str(value if value is not None else ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROLS.sub("", text)


def truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _has_controls(text):
    return bool(_CONTROLS.search(text)) or "\n" in text or "\r" in text


def valid_link(link):
    """True for a plain http(s) URL (no spaces, controls or DEL)."""
    return (isinstance(link, str) and len(link) <= LINK_MAX
            and _LINK.fullmatch(link) is not None)


def validate_item(item):
    """Problems with one item, as strings. Empty list means valid.

    Strict: this is the contract. Use `parse_item` for untrusted input.
    """
    if not isinstance(item, dict):
        return ["item is not an object"]
    problems = []

    def need(key, typ, label):
        if key not in item:
            problems.append(f"missing '{key}'")
            return False
        if not isinstance(item[key], typ) or isinstance(item[key], bool):
            problems.append(f"'{key}' must be {label}")
            return False
        return True

    if item.get("v") != SCHEMA_VERSION or isinstance(item.get("v"), bool):
        problems.append(f"'v' must be {SCHEMA_VERSION}")
    if need("id", str, "a string"):
        if not item["id"] or len(item["id"]) > ID_MAX:
            problems.append(f"'id' must be 1..{ID_MAX} characters")
        elif _has_controls(item["id"]):
            problems.append("'id' has control characters")
    for key, allowed in (("source", SOURCES), ("section", SECTIONS),
                         ("urgency", URGENCY_LEVELS)):
        if need(key, str, "a string") and item[key] not in allowed:
            problems.append(f"'{key}' must be one of {', '.join(allowed)}")
    for key in ("sender", "where"):
        if need(key, str, "a string"):
            if len(item[key]) > FIELD_MAX:
                problems.append(f"'{key}' is longer than {FIELD_MAX}")
            if _has_controls(item[key]):
                problems.append(f"'{key}' must be one line of plain text")
    if need("gist", str, "a string"):
        if len(item["gist"]) > GIST_MAX:
            problems.append(f"'gist' is longer than {GIST_MAX}")
        if _has_controls(item["gist"]):
            problems.append("'gist' must be one line of plain text")
    if "ts" not in item or not _is_number(item["ts"]) or item["ts"] < 0:
        problems.append("'ts' must be a non-negative epoch number")
    if "link" not in item:
        problems.append("missing 'link'")
    elif item["link"] is not None:
        link = item["link"]
        if not valid_link(link):
            problems.append("'link' must be null or an http(s) URL")
    if "thread" not in item or not isinstance(item["thread"], list):
        problems.append("'thread' must be a list")
    else:
        if len(item["thread"]) > THREAD_MAX:
            problems.append(f"'thread' has more than {THREAD_MAX} entries")
        for i, msg in enumerate(item["thread"][:THREAD_MAX]):
            if not (isinstance(msg, dict)
                    and isinstance(msg.get("who"), str)
                    and _is_number(msg.get("ts"))
                    and isinstance(msg.get("text"), str)):
                problems.append(f"thread[{i}] needs who (str), ts, text (str)")
            elif len(msg["text"]) > TEXT_MAX:
                problems.append(f"thread[{i}].text is longer than {TEXT_MAX}")
    if "draft" not in item:
        problems.append("missing 'draft'")
    elif item["draft"] is not None and (
            not isinstance(item["draft"], str)
            or len(item["draft"]) > TEXT_MAX):
        problems.append(f"'draft' must be null or a string up to {TEXT_MAX}")
    actions = item.get("actions")
    if not isinstance(actions, list):
        problems.append("'actions' must be a list")
    else:
        for a in actions:
            if a not in ACTIONS or not isinstance(a, str):
                problems.append(f"unknown action {a!r}")
        if len(set(map(str, actions))) != len(actions):
            problems.append("'actions' has duplicates")
    if not isinstance(item.get("meta"), dict):
        problems.append("'meta' must be an object")
    return problems


def sanitize_item(item):
    """A copy of an untrusted item with display text cleaned.

    Single-line fields are flattened; `gist` is also cut to GIST_MAX, so a
    chatty source is shortened rather than rejected. Unknown keys are
    dropped. Structure problems are left for `validate_item`.
    """
    if not isinstance(item, dict):
        return item
    out = {}
    for key in ("v", "source", "section", "urgency", "ts", "link", "actions",
                "meta"):
        if key in item:
            out[key] = item[key]
    for key in ("id", "sender", "where"):
        if isinstance(item.get(key), str):
            out[key] = clean_line(item[key])
        elif key in item:
            out[key] = item[key]
    if isinstance(item.get("gist"), str):
        out["gist"] = truncate(clean_line(item["gist"]), GIST_MAX)
    elif "gist" in item:
        out["gist"] = item["gist"]
    if isinstance(item.get("thread"), list):
        thread = []
        for msg in item["thread"]:
            if isinstance(msg, dict):
                msg = dict(msg)
                if isinstance(msg.get("who"), str):
                    msg["who"] = clean_line(msg["who"])
                if isinstance(msg.get("text"), str):
                    msg["text"] = clean_text(msg["text"])
                thread.append({k: msg[k] for k in ("who", "ts", "text")
                               if k in msg})
            else:
                thread.append(msg)
        out["thread"] = thread
    elif "thread" in item:
        out["thread"] = item["thread"]
    if isinstance(item.get("draft"), str):
        out["draft"] = clean_text(item["draft"])
    elif "draft" in item:
        out["draft"] = item["draft"]
    return out


def parse_item(raw):
    """Sanitise, then validate. Returns the clean item or raises ItemError."""
    item = sanitize_item(raw)
    problems = validate_item(item)
    if problems:
        raise ItemError("; ".join(problems))
    return item


def make_item(*, id, source, section, urgency, sender, where, gist, ts,
              link=None, thread=None, draft=None, actions=(), meta=None):
    """Build a valid item (used by built-in sources). Text is cleaned."""
    return parse_item({
        "v": SCHEMA_VERSION, "id": id, "source": source, "section": section,
        "urgency": urgency, "sender": sender, "where": where, "gist": gist,
        "ts": ts, "link": link, "thread": list(thread or []), "draft": draft,
        "actions": list(actions), "meta": dict(meta or {}),
    })


def parse_list_output(text):
    """Parse a command source's `list` stdout.

    Returns `(items, stale, age_min, error)`. Raises ItemError when the
    output as a whole is unusable. Items that fail validation are dropped
    and described in the returned error string, so one bad row does not hide
    the rest.
    """
    if len(text.encode("utf-8", "replace")) > OUTPUT_MAX_BYTES:
        raise ItemError(f"output is larger than {OUTPUT_MAX_BYTES} bytes")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ItemError(f"output is not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise ItemError("output is not a JSON object")
    if doc.get("v") != SCHEMA_VERSION or isinstance(doc.get("v"), bool):
        raise ItemError(f"output 'v' must be {SCHEMA_VERSION}")
    rows = doc.get("items")
    if not isinstance(rows, list):
        raise ItemError("output 'items' must be a list")
    stale = doc.get("stale", False)
    if not isinstance(stale, bool):
        raise ItemError("output 'stale' must be a boolean")
    age = doc.get("age_min")
    if age is not None and (not _is_number(age) or age < 0):
        raise ItemError("output 'age_min' must be a number or null")
    err = doc.get("error")
    if err is not None and not isinstance(err, str):
        raise ItemError("output 'error' must be a string or null")
    if len(rows) > ITEMS_MAX:
        rows = rows[:ITEMS_MAX]
        trimmed = f"only the first {ITEMS_MAX} items are read"
        err = f"{err}; {trimmed}" if err else trimmed

    items, seen, bad = [], set(), []
    for i, row in enumerate(rows):
        try:
            item = parse_item(row)
        except ItemError as exc:
            bad.append(f"item {i}: {exc}")
            continue
        if item["id"] in seen:
            bad.append(f"item {i}: duplicate id")
            continue
        seen.add(item["id"])
        items.append(item)
    notes = []
    if err:
        notes.append(clean_line(err))
    if bad:
        notes.append(f"{len(bad)} item(s) dropped: {truncate(bad[0], 160)}")
    return items, stale, age, "; ".join(notes) or None


def parse_act_request(text):
    """Validate the stdin of `act` (used by tests and phase 2 sources)."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ItemError(f"request is not JSON ({exc})") from None
    if (not isinstance(doc, dict) or doc.get("v") != SCHEMA_VERSION
            or doc.get("action") not in ACTIONS
            or not isinstance(doc.get("id"), str) or not doc["id"]
            or not isinstance(doc.get("meta"), dict)
            or ("text" in doc and not isinstance(doc["text"], str))):
        raise ItemError("request needs v, action, id, meta (object), "
                        "optional text")
    return doc


def parse_act_response(text):
    """Validate the stdout of `act`: `{"ok": bool, "message": str}`."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ItemError(f"response is not JSON ({exc})") from None
    if (not isinstance(doc, dict) or not isinstance(doc.get("ok"), bool)
            or not isinstance(doc.get("message"), str)):
        raise ItemError("response needs ok (bool) and message (str)")
    return {"ok": doc["ok"], "message": clean_line(doc["message"])}


MEMORY_ID_MAX = 200


def save_request(item, force=False):
    """The stdin of `save`: `{"v": 1, "action": "save", "item": <v1 item>}`,
    plus `"force": true` when the user chose "save anyway" after a duplicate."""
    doc = {"v": SCHEMA_VERSION, "action": "save", "item": item}
    if force:
        doc["force"] = True
    return doc


def _candidate(raw):
    """The `candidate` of a duplicate answer: `{"id", "title", "score"}`, any
    part may be missing; text is cleaned to one line. None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ItemError("response candidate must be an object or null")
    out = {}
    for key in ("id", "title"):
        val = raw.get(key)
        if val is not None and not isinstance(val, str):
            raise ItemError(f"response candidate.{key} must be a string")
        out[key] = truncate(clean_line(val), FIELD_MAX) if val else None
    score = raw.get("score")
    if score is not None and not _is_number(score):
        raise ItemError("response candidate.score must be a number")
    out["score"] = score
    return out


def parse_save_response(text):
    """Validate the stdout of the save command.

    `{"ok": bool, "message": str, "memory_id": str|null, "duplicate": bool}`
    and, with `ok: false, duplicate: true`, an optional `candidate`
    (`{"id", "title", "score"}`, the memory it looks like). `memory_id`,
    `duplicate` and `candidate` may be left out (null, false, null). Returns
    those five keys, text cleaned to one line. Raises ItemError.
    """
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ItemError(f"response is not JSON ({exc})") from None
    if (not isinstance(doc, dict) or not isinstance(doc.get("ok"), bool)
            or not isinstance(doc.get("message"), str)):
        raise ItemError("response needs ok (bool) and message (str)")
    memory_id = doc.get("memory_id")
    if memory_id is not None and not isinstance(memory_id, str):
        raise ItemError("response memory_id must be a string or null")
    duplicate = doc.get("duplicate", False)
    if not isinstance(duplicate, bool):
        raise ItemError("response duplicate must be a boolean")
    candidate = _candidate(doc.get("candidate"))
    return {"ok": doc["ok"], "message": clean_line(doc["message"]),
            "candidate": candidate,
            "memory_id": (truncate(clean_line(memory_id), MEMORY_ID_MAX)
                          if memory_id is not None else None),
            "duplicate": duplicate}
