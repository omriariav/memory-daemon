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
the option on does not announce every channel. A channel must be missing
from two listings in a row to count as left; one left and later rejoined is
announced again. DMs and archived channels are not listed.

Cost: one bridge session per check (ids only). Only when a new channel
appears, a full listing (names, types) and, with `channel_info_tool`, a
channel search (at most MAX_INFO) follow in their own sessions.

Trust: membership comes from the `ids_only` listing, which holds channel ids
only; names and types come from the full listing, used only for new ids and
dropped when two entries disagree (a channel purpose cannot forge a name onto
a real id). Creator details are kept only when exactly one search result
names the channel by both name and permalink id. A listing that is incomplete
(no "of N total" header, more than one page, or a count that does not match)
changes nothing; when the full listing fails, the new channels wait for the
next check rather than being recorded without a name.

State: `state/comms-inbox-joined-<lane>.json`, one file per routine, so two
lanes (or workspaces) never overwrite each other's membership. A state write
failure is logged, never raised: the run goes on, and the next check may
record the same join again.
"""
import json
import re
import time
from pathlib import Path

from . import slack_watch, state
from .shell import log

STATE_PREFIX = "comms-inbox-joined"
LIST_LIMIT = 200
MAX_INFO = 5
MAX_NAME = 80
LIST_PINS = {
    "types": "public_channel,private_channel",
    "exclude_archived": True,
    "limit": LIST_LIMIT,
}
_HEADER = re.compile(r"showing (\d+) of (\d+) total", re.IGNORECASE)
_BLOCK = re.compile(
    r"^### #([a-z0-9][a-z0-9._-]{0,79})\s*\n- \*\*ID:\*\* ([CG][A-Z0-9]{8,})"
    r"\s*\n- \*\*Type:\*\* (Public|Private) Channel\s*$",
    re.MULTILINE,
)
_INFO_BLOCK = re.compile(
    r"^### Result \d+ of \d+\s*\n"
    r"Name: #([a-z0-9][a-z0-9._-]{0,79})\s*\n"
    r"Creator: ([^\n]{1,120}?) \(<@([UW][A-Z0-9]{6,})>?\)\s*\n"
    r"Created: ([0-9: -]{10,25}[A-Z]{0,5})\s*\n"
    r"Permalink: \[link\]\([^)\s]*?/archives/([CG][A-Z0-9]{8,})\)",
    re.MULTILINE,
)


def _channel(value):
    return (isinstance(value, str) and slack_watch.CHANNEL_ID.match(value)
            and not value.startswith("D"))


def _body(text, key):
    """The text of a connector result: its JSON `key` field, or the text."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = text
    if isinstance(data, dict):
        data = data.get(key)
    return data if isinstance(data, str) else ""


def ids_call():
    return dict(LIST_PINS, format="ids_only")


def names_call():
    return dict(LIST_PINS, format="full")


def info_call(name):
    return {
        "keywords": [name], "natural_language_query": f"the {name} channel",
        "channel_types": "public_channel,private_channel",
        "include_archived": False, "limit": 5, "response_format": "detailed",
    }


def parse_ids(text):
    """The listed channel ids, or None when the listing is not complete."""
    text = _body(text, "result")
    match = _HEADER.search(text)
    if not match or "more results available" in text.lower():
        return None
    shown, total = int(match.group(1)), int(match.group(2))
    ids = [
        line.strip() for line in text.splitlines() if _channel(line.strip())
    ]
    if shown != total or len(ids) != shown or len(set(ids)) != len(ids):
        return None
    return set(ids)


def parse_names(text, ids):
    """{id: (name, private)} for `ids` whose full entries all agree."""
    found = {}
    for name, channel, kind in _BLOCK.findall(_body(text, "result")):
        if channel in ids:
            found.setdefault(channel, set()).add((name, kind == "Private"))
    return {c: next(iter(v)) for c, v in found.items() if len(v) == 1}


def parse_info(text, channel, name):
    """Creator details of `channel` (named `name`) from a search, or {}."""
    blocks = [
        b for b in _INFO_BLOCK.findall(_body(text, "results"))
        if b[4] == channel
    ]
    if len(blocks) != 1 or blocks[0][0] != name:
        return {}
    _name, creator, creator_id, created, _id = blocks[0]
    return {"creator_id": creator_id, "creator_name": creator[:MAX_NAME],
            "created": created.strip()}


def _path(base_dir, lane):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", lane or "default")[:60]
    return Path(base_dir) / "state" / f"{STATE_PREFIX}-{safe}.json"


def load(base_dir, lane):
    """{"members": set or None (no baseline yet), "missing": set,
    "checked": epoch or None}."""
    empty = {"members": None, "missing": set(), "checked": None}
    try:
        data = json.loads(_path(base_dir, lane).read_text())
        members, checked = data["members"], data.get("checked")
        missing = data.get("missing") or []
        for group in (members or [], missing):
            if not isinstance(group, list) or not all(map(_channel, group)):
                raise ValueError("bad channel list")
        if not isinstance(checked, (int, float)) or isinstance(checked, bool):
            checked = None
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, KeyError, TypeError):
        log("comms inbox: joined-channel state unreadable; re-baselining")
        return empty
    return {"members": None if members is None else set(members),
            "missing": set(missing), "checked": checked}


def save(base_dir, lane, members, missing, checked):
    """Write the state; a failure is logged, never raised."""
    try:
        state.write_atomic(
            _path(base_dir, lane),
            json.dumps({
                "members": None if members is None else sorted(members),
                "missing": sorted(missing), "checked": checked,
            }, sort_keys=True),
            mode=0o600,
        )
    except OSError as exc:
        log(f"comms inbox: joined-channel state not saved: {exc}")


def rows_for(new, names, infos, now):
    """channel_joined rows for the ids in `new`, by id."""
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


def check(base_dir, lane, inbox, list_session, info_session, record,
          now=None):
    """List the owner's channels when due; record newly joined ones.

    `list_session(calls)` and `info_session(calls)` return one text (or None)
    per call; `info_session` may be None. `record(rows)` returns the number
    of lines written. Returns the number of channel_joined lines recorded.
    A failed or incomplete listing changes nothing but the check time, so it
    is retried after `joined_check_minutes`; joins that could not be written
    (or named) are retried at the next check.
    """
    now = time.time() if now is None else now
    saved = load(base_dir, lane)
    members, missing = saved["members"], saved["missing"]
    if saved["checked"] is not None \
            and now - saved["checked"] < inbox["joined_check_minutes"] * 60:
        return 0
    ids = parse_ids((list_session([ids_call()]) or [None])[0])
    if ids is None:
        log("comms inbox: channel listing incomplete or unreadable; "
            "joined channels not checked this run")
        save(base_dir, lane, members, missing, now)
        return 0
    if members is None:
        save(base_dir, lane, ids, set(), now)
        log(f"comms inbox: joined-channel baseline of {len(ids)} channel(s)")
        return 0
    # Left only after two listings in a row without it.
    absent = members - ids
    kept = (members & ids) | (absent - missing)
    new = ids - members
    if not new:
        save(base_dir, lane, kept, absent - missing, now)
        return 0
    texts = list_session([names_call()]) or [None]
    if texts[0] is None:
        log("comms inbox: full channel listing failed; "
            f"{len(new)} new channel(s) wait for the next check")
        save(base_dir, lane, kept, absent - missing, now)
        return 0
    names = parse_names(texts[0], new)
    infos = {}
    lookups = [c for c in sorted(new) if c in names][:MAX_INFO]
    if info_session and lookups:
        found = info_session([info_call(names[c][0]) for c in lookups])
        for channel, text in zip(lookups, found):
            info = parse_info(text, channel, names[channel][0])
            if info:
                infos[channel] = info
    rows = rows_for(new, names, infos, now)
    added = record(rows)
    written = new if added == len(rows) else set()
    save(base_dir, lane, kept | written, absent - missing, now)
    log(f"comms inbox: {len(new)} newly joined channel(s), {added} recorded")
    return added
