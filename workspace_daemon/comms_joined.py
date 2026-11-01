"""Comms inbox: one line per channel the owner newly joins.

With `joined_channels: true` in a `comms_inbox` block and `channels_tool` (the
connector's read-only `slack_list_user_channels`) on the source, a run lists
the owner's public and private channels at most every
`joined_check_minutes`, compares them with the last listing, and records a
`channel_joined` line for each new one:

    {"type": "channel_joined", "channel_id", "name", "channel_type": "channel",
     "private", "ts", "thread_ts", "seen_at"}

plus, when `channel_info_tool` (`slack_search_channels`) finds the channel,
"creator_id", "creator_name" and "created" (the connector's own text, e.g.
"2026-11-01 13:57:21 IST"). `ts` (and `thread_ts`) is when the join was
detected, not when it happened: the listing carries no join time.

The first listing only records the current channels (no lines), so turning
the option on does not announce every channel. A channel left and later
rejoined is announced again. DMs and archived channels are not listed.

Trust: membership comes from the `ids_only` listing, which holds channel ids
only; names and types come from a full listing in the same session, used only
for ids the first listing holds, and dropped when two entries disagree (a
channel purpose cannot forge a name onto a real id). Creator details are kept
only when exactly one search result's permalink names the channel. A listing
that is incomplete (more than one page, or a count that does not match its
header) changes nothing. State: `state/comms-inbox-joined.json`.
"""
import json
import re
import time
from pathlib import Path

from . import state
from .shell import log

STATE_FILE = "comms-inbox-joined.json"
DEFAULT_MINUTES = 60
LIST_LIMIT = 200
MAX_INFO = 5
MAX_NAME = 80
LIST_PINS = {
    "types": "public_channel,private_channel",
    "exclude_archived": True,
    "limit": LIST_LIMIT,
}
_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{8,}$")
_HEADER = re.compile(r"showing (\d+)(?: of (\d+) total)?", re.IGNORECASE)
_BLOCK = re.compile(
    r"^### #([a-z0-9][a-z0-9._-]{0,79})\s*\n- \*\*ID:\*\* ([CG][A-Z0-9]{8,})"
    r"\s*\n- \*\*Type:\*\* (Public|Private) Channel\s*$",
    re.MULTILINE,
)
_INFO_BLOCK = re.compile(
    r"^Name: #([a-z0-9][a-z0-9._-]{0,79})\s*\n"
    r"Creator: ([^\n]{1,120}?) \(<@([UW][A-Z0-9]{6,})>?\)\s*\n"
    r"Created: ([0-9: -]{10,25}[A-Z]{0,5})\s*\n"
    r"Permalink: \[link\]\([^)\s]*?/archives/([CG][A-Z0-9]{8,})\)",
    re.MULTILINE,
)


def _body(text, key):
    """The text of a connector result: its JSON `key` field, or the text."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = text
    if isinstance(data, dict):
        data = data.get(key)
    return data if isinstance(data, str) else ""


def list_calls():
    """The two planned listing calls: ids only, then full (names, types)."""
    return [dict(LIST_PINS, format="ids_only"), dict(LIST_PINS)]


def info_call(name):
    return {
        "keywords": [name], "natural_language_query": "",
        "channel_types": "public_channel,private_channel",
        "include_archived": False, "limit": 5, "response_format": "detailed",
    }


def parse_ids(text):
    """The listed channel ids, or None when the listing is not complete."""
    text = _body(text, "result")
    match = _HEADER.search(text)
    if not match or "more results available" in text.lower():
        return None
    shown = int(match.group(1))
    total = int(match.group(2) or shown)
    ids = [
        line.strip() for line in text.splitlines()
        if _CHANNEL_ID.match(line.strip())
    ]
    if shown != total or len(ids) != shown or len(set(ids)) != len(ids):
        return None
    return set(ids)


def parse_names(text, ids):
    """{id: (name, private)} for listed ids whose full entries all agree."""
    found = {}
    for name, channel, kind in _BLOCK.findall(_body(text, "result")):
        if channel in ids:
            found.setdefault(channel, set()).add((name, kind == "Private"))
    return {c: next(iter(v)) for c, v in found.items() if len(v) == 1}


def parse_info(text, channel):
    """Creator details of `channel` from a channel search, or {}."""
    blocks = [
        b for b in _INFO_BLOCK.findall(_body(text, "results"))
        if b[4] == channel
    ]
    if len(blocks) != 1:
        return {}
    _name, creator, creator_id, created, _id = blocks[0]
    return {"creator_id": creator_id, "creator_name": creator[:MAX_NAME],
            "created": created.strip()}


def _path(base_dir):
    return Path(base_dir) / "state" / STATE_FILE


def load(base_dir):
    """{"members": set or None (no baseline yet), "checked": epoch or None}."""
    empty = {"members": None, "checked": None}
    try:
        data = json.loads(_path(base_dir).read_text())
        members, checked = data["members"], data.get("checked")
        if members is not None and (not isinstance(members, list) or not all(
            isinstance(c, str) and _CHANNEL_ID.match(c) for c in members
        )):
            raise ValueError("bad members")
        if not isinstance(checked, (int, float)) or isinstance(checked, bool):
            checked = None
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, KeyError, TypeError):
        log("comms inbox: joined-channel state unreadable; re-baselining")
        return empty
    return {"members": None if members is None else set(members),
            "checked": checked}


def save(base_dir, members, checked):
    state.write_atomic(
        _path(base_dir),
        json.dumps({"members": None if members is None else sorted(members),
                    "checked": checked}, sort_keys=True),
        mode=0o600,
    )


def due(saved, minutes, now):
    return saved["checked"] is None or now - saved["checked"] >= minutes * 60


def rows_for(new, names, infos, now):
    """channel_joined rows for the ids in `new`, oldest id first."""
    stamp = f"{now:.6f}"
    rows = []
    for channel in sorted(new):
        name, private = names.get(channel, (None, None))
        row = {
            "type": "channel_joined", "channel_id": channel,
            "thread_ts": stamp, "ts": stamp, "channel_type": "channel",
            "name": name, "private": private,
        }
        row.update(infos.get(channel, {}))
        rows.append(row)
    return rows


def check(base_dir, inbox, list_session, info_session, record, now=None):
    """List the owner's channels when due; record newly joined ones.

    `list_session(calls)` and `info_session(calls)` return one text (or None)
    per call; `info_session` may be None. `record(rows)` returns the number
    of lines written. Returns the number of channel_joined lines recorded.
    A failed or incomplete listing changes nothing but the check time, so it
    is retried after `joined_check_minutes`; joins that could not be written
    are retried on the next run.
    """
    now = time.time() if now is None else now
    saved = load(base_dir)
    if not due(saved, inbox.get("joined_check_minutes", DEFAULT_MINUTES), now):
        return 0
    texts = list_session(list_calls())
    ids = parse_ids(texts[0]) if texts else None
    if ids is None:
        log("comms inbox: channel listing incomplete or unreadable; "
            "joined channels not checked this run")
        save(base_dir, saved["members"], now)
        return 0
    if saved["members"] is None:
        save(base_dir, ids, now)
        log(f"comms inbox: joined-channel baseline of {len(ids)} channel(s)")
        return 0
    new = ids - saved["members"]
    if not new:
        save(base_dir, ids, now)
        return 0
    names = parse_names(texts[1] if len(texts) > 1 else None, ids)
    infos = {}
    lookups = [c for c in sorted(new) if c in names][:MAX_INFO]
    if info_session and lookups:
        found = info_session([info_call(names[c][0]) for c in lookups])
        for channel, text in zip(lookups, found):
            info = parse_info(text, channel)
            if info:
                infos[channel] = info
    rows = rows_for(new, names, infos, now)
    added = record(rows)
    if added == len(rows):
        save(base_dir, ids, now)
    log(f"comms inbox: {len(new)} newly joined channel(s), {added} recorded")
    return added
