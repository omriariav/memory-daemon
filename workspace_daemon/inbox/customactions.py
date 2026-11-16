"""The inbox's generic `actions:` hook: user-defined, confirmed write actions
backed by a configured command. No Textual import; every failure becomes an
`Outcome`.

One action is up to three calls of the same command (JSON on stdin, one JSON
line on stdout, no shell, item text never in argv):

  choices   {"v": 1, "action": "choices", "name": <label>, "item": {...}}
            -> {"v": 1, "ok": true, "choices": [{"id", "label", "detail"}],
                "message": null}            read-only
  prepare   {"v": 1, "action": "prepare", "name", "item", "choice": <id>}
            -> {"ok", "rows": [[label, value], ...], "state": "<opaque>",
                "message", "duplicate"}     read-only, does a fresh read
  run       {"v": 1, "action": "run", "name", "item", "choice", "state"}
            -> {"ok", "message"}            the only call that writes

Everything a command prints is untrusted display text: it is validated and
cleaned here before any widget sees it, and cut to fixed caps. The commands
run under the capped runner (own process group, SIGTERM then SIGKILL after
TERM_GRACE_S on a timeout, a cap on the raw output). `choices` and `prepare`
can be cancelled; `run` never is, and is never retried.
"""
import json
import re
from collections import namedtuple

from . import items as itm
from . import sources

PROTOCOL_VERSION = 1
MAX_CHOICES = 50
CHOICE_ID_MAX = 200
CHOICE_LABEL_MAX = 80
CHOICE_DETAIL_MAX = 200
MAX_ROWS = 20
ROW_LABEL_MAX = 40
ROW_VALUE_MAX = 1000
STATE_MAX_BYTES = 64 * 1024
MESSAGE_MAX = 300
MAX_RAW_BYTES = 512 * 1024
TERM_GRACE_S = 5

_ID_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

Choices = namedtuple("Choices", "ok choices message")
Prepared = namedtuple("Prepared", "ok rows state message duplicate")
Ran = namedtuple("Ran", "ok message")


def request(action, name, item, choice=None, state=None):
    """The stdin of one call."""
    doc = {"v": PROTOCOL_VERSION, "action": action, "name": name,
           "item": item}
    if action in ("prepare", "run"):
        doc["choice"] = choice
    if action == "run":
        doc["state"] = state
    return doc


def _load(text):
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise itm.ItemError(f"response is not JSON ({exc})") from None
    if not isinstance(doc, dict):
        raise itm.ItemError("response is not a JSON object")
    if "v" in doc and (doc["v"] != PROTOCOL_VERSION
                       or isinstance(doc["v"], bool)):
        raise itm.ItemError(f"response 'v' must be {PROTOCOL_VERSION}")
    return doc


def _message(doc):
    message = doc.get("message")
    if message is not None and not isinstance(message, str):
        raise itm.ItemError("response message must be a string or null")
    return (itm.truncate(itm.clean_line(message), MESSAGE_MAX)
            if message else None)


def parse_choices(text):
    """`{"ok": bool, "choices": [{"id": str, "label": str, "detail"?: str}],
    "message"?: str|null}` -> `Choices` (at most MAX_CHOICES, a repeated id
    keeps its first row). Raises `ItemError`.

    The `id` is an opaque handle the command gets back in `prepare` and `run`,
    so it is passed through byte for byte, never cleaned or cut: a row whose id
    is not a string of 1 to CHOICE_ID_MAX characters, or holds a control
    character, is skipped. Only `label` and `detail`, which are shown, are
    cleaned and cut."""
    doc = _load(text)
    ok = doc.get("ok")
    if not isinstance(ok, bool):
        raise itm.ItemError("response needs ok (bool)")
    message = _message(doc)
    raw = doc.get("choices", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise itm.ItemError("response choices must be a list")
    choices, seen = [], set()
    for n, row in enumerate(raw):
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            raise itm.ItemError(f"choices[{n}] needs id (str) and label (str)")
        detail = row.get("detail")
        if detail is not None and not isinstance(detail, str):
            raise itm.ItemError(f"choices[{n}].detail must be a string")
        cid = row.get("id")
        if (not isinstance(cid, str) or not 1 <= len(cid) <= CHOICE_ID_MAX
                or _ID_CONTROLS.search(cid) or itm._CONTROLS.search(cid)
                or cid in seen):
            continue
        if len(choices) >= MAX_CHOICES:
            break
        seen.add(cid)
        choices.append({
            "id": cid,
            "label": itm.truncate(itm.clean_line(row["label"]),
                                  CHOICE_LABEL_MAX)
            or itm.truncate(itm.clean_line(cid), CHOICE_LABEL_MAX) or "(choice)",
            "detail": itm.truncate(itm.clean_line(detail or ""),
                                   CHOICE_DETAIL_MAX)})
    return Choices(ok, choices if ok else [], message)


def parse_prepared(text):
    """`{"ok": bool, "rows": [[label, value], ...], "state": str|null,
    "message"?: str|null, "duplicate"?: bool}` -> `Prepared`. `state` is
    opaque and goes back to `run` unchanged (at most STATE_MAX_BYTES of
    UTF-8). `rows` and `state` may be left out or null (an `ok: false`
    answer carries neither). Raises `ItemError`."""
    doc = _load(text)
    ok = doc.get("ok")
    if not isinstance(ok, bool):
        raise itm.ItemError("response needs ok (bool)")
    duplicate = doc.get("duplicate", False)
    if duplicate is None:
        duplicate = False
    if not isinstance(duplicate, bool):
        raise itm.ItemError("response duplicate must be a boolean")
    message = _message(doc)
    raw_rows = doc.get("rows")
    if raw_rows is None:
        raw_rows = []
    if not isinstance(raw_rows, list):
        raise itm.ItemError("response rows must be a list")
    rows = []
    for n, row in enumerate(raw_rows[:MAX_ROWS]):
        if (not isinstance(row, (list, tuple)) or len(row) != 2
                or not all(isinstance(x, str) for x in row)):
            raise itm.ItemError(f"rows[{n}] must be [label, value] (strings)")
        label = itm.truncate(itm.clean_line(row[0]), ROW_LABEL_MAX)
        value = itm.truncate(itm.clean_text(row[1]).strip(), ROW_VALUE_MAX)
        rows.append((label, value))
    state = doc.get("state")
    if state is None:
        state = ""
    if not isinstance(state, str):
        raise itm.ItemError("response state must be a string or null")
    if len(state.encode("utf-8")) > STATE_MAX_BYTES:
        raise itm.ItemError(f"response state is over {STATE_MAX_BYTES} bytes")
    return Prepared(ok, rows, state, message, duplicate)


def parse_ran(text):
    """`{"ok": bool, "message"?: str|null}` -> `Ran`. Raises `ItemError`."""
    doc = _load(text)
    ok = doc.get("ok")
    if not isinstance(ok, bool):
        raise itm.ItemError("response needs ok (bool)")
    return Ran(ok, _message(doc))


def _call(cfg, doc, cancel=None):
    """Run the command once. Returns `(None, text)` or `(reason, None)`."""
    try:
        payload = json.dumps(doc).encode("utf-8")
        status, code, out, err = sources._run_capped(
            sources._expand(cfg["argv"]), cfg["timeout_s"],
            stdin_bytes=payload, term_grace_s=TERM_GRACE_S,
            max_bytes=MAX_RAW_BYTES, cancel=cancel)
    except (OSError, ValueError, TypeError) as exc:
        return ("cannot run: "
                f"{getattr(exc, 'strerror', None) or exc}"), None
    if status == "cancelled":
        return "cancelled", None
    if status == "timeout":
        return f"timed out after {cfg['timeout_s']:g}s", None
    if status == "overflow":
        return "response is too large", None
    if code != 0:
        detail = sources._stderr_tail(err)
        return f"exit {code}" + (f": {detail}" if detail else ""), None
    return None, out.decode("utf-8", "replace")


def run_choices(cfg, item, cancel=None):
    """The choices for one item. Never raises."""
    reason, text = _call(cfg, request("choices", cfg["label"], item), cancel)
    if reason:
        return Choices(False, [], reason)
    try:
        res = parse_choices(text)
    except itm.ItemError as exc:
        return Choices(False, [], f"bad response: {exc}")
    if not res.ok:
        return Choices(False, [], res.message
                       or "the action command reported a failure")
    if not res.choices:
        return Choices(False, [], res.message or "No choices for this item")
    return res


def run_prepare(cfg, item, choice, cancel=None):
    """The confirmation rows for one choice. Never raises. A `duplicate`
    answer comes back with `duplicate` True whatever its `ok`."""
    reason, text = _call(
        cfg, request("prepare", cfg["label"], item, choice), cancel)
    if reason:
        return Prepared(False, [], "", reason, False)
    try:
        res = parse_prepared(text)
    except itm.ItemError as exc:
        return Prepared(False, [], "", f"bad response: {exc}", False)
    if res.duplicate:
        return res._replace(ok=False)
    if not res.ok:
        return res._replace(rows=[], state="", message=res.message
                            or "the action command reported a failure")
    return res


def run_run(cfg, item, choice, state):
    """Do the action. Never raises, never retries, never cancelled: a timeout
    asks the command to stop (SIGTERM, SIGKILL after TERM_GRACE_S) and says
    the action may still have completed."""
    reason, text = _call(
        cfg, request("run", cfg["label"], item, choice, state))
    if reason:
        if reason.startswith("timed out"):
            reason += "; it may still have completed"
        return Ran(False, reason)
    try:
        res = parse_ran(text)
    except itm.ItemError as exc:
        return Ran(False, f"bad response: {exc}")
    return Ran(res.ok, res.message or (
        "Done" if res.ok else "the action command reported a failure"))
