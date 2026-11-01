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
the option on does not announce every channel. The known set only grows:
leaving is not tracked, so a channel left and later rejoined is not announced
again, and a channel that disappears from a listing (archived, or a scope
briefly lost) is not announced when it comes back. DMs and archived channels
are not listed.

Cost: one bridge session per check (ids only). Only when a new channel
appears, a full listing (names, types) and, with `channel_info_tool`, a
channel search (at most MAX_INFO) follow in their own sessions.

Trust: membership comes from the `ids_only` listing, which holds channel ids
only; names and types come from the full listing, used only for new ids and
dropped when two entries disagree (a channel purpose cannot forge a name onto
a real id). Creator details are kept only when exactly one search result
names the channel by both name and permalink id. A listing that is incomplete
(empty, no "of N total" header, more than one page, or a count that does not
match) changes nothing. A new channel with no usable name (full listing
failed, or its entry unparseable or contested) waits for later checks and is
recorded without a name after NAME_TRIES. More than MAX_NEW new channels at
once is taken for a changed scope and re-baselines silently.

State: `state/comms-inbox-joined-<lane>-<hash>.json`, one file per connector
tool and owner, shared by every routine that lists the same channels (they
run under one lock), so a join is recorded once and two workspaces never
overwrite each other's set. A state write
failure is logged, never raised: the run goes on, and the next check may
record the same join again.
"""
import hashlib
import json
import re
import time
from pathlib import Path

from . import slack_watch, state
from .chat_text import redact_secrets
from .shell import log

STATE_PREFIX = "comms-inbox-joined"
LIST_LIMIT = 200
MAX_INFO = 5
MAX_NAME = 80
# A listing this many channels larger than the last is taken for a changed
# scope or workspace, not real joins: it re-baselines silently.
MAX_NEW = 20
# Checks a new channel may stay unnamed before it is recorded without a name.
NAME_TRIES = 3
LIST_PINS = {
    "types": "public_channel,private_channel",
    "exclude_archived": True,
    "limit": LIST_LIMIT,
}
_HEADER = re.compile(r"showing (\d+) of (\d+) total", re.IGNORECASE)
_BLOCK = re.compile(
    r"^### #([\w.-]{1,80})\s*\n- \*\*ID:\*\* ([CG][A-Z0-9]{8,})"
    r"\s*\n- \*\*Type:\*\* (Public|Private) Channel\s*$",
    re.MULTILINE,
)
_RESULTS = re.compile(r"^## Channels \((\d+) results?\)", re.MULTILINE)
_RESULT = re.compile(r"^### Result \d+ of \d+", re.MULTILINE)
_INFO_BLOCK = re.compile(
    r"^### Result \d+ of \d+\s*\n"
    r"Name: #([\w.-]{1,80})\s*\n"
    r"Creator: ([^\n]{1,120}?) \(<@([UW][A-Z0-9]{8,})>?\)\s*\n"
    r"Created: ([0-9: -]{10,25}[A-Z]{0,5}(?:[+-][0-9]{1,2}(?::?[0-9]{2})?)?)"
    r"\s*\n"
    # Purpose and Topic sit here when the channel has them.
    r"(?:(?:Purpose|Topic): [^\n]*\n){0,2}"
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
        "include_archived": False, "limit": 20,
        "response_format": "detailed",
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
    # Empty is never complete: the owner is always in some channel, and an
    # empty answer is how a lost scope looks.
    if not ids or shown != total or len(ids) != shown \
            or len(set(ids)) != len(ids):
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
    text = _body(text, "results")
    # A result block embedded in another result's text adds a header the
    # count does not include: then nothing in the answer is trusted.
    count = _RESULTS.search(text)
    if not count or int(count.group(1)) != len(_RESULT.findall(text)):
        return {}
    blocks = [b for b in _INFO_BLOCK.findall(text) if b[4] == channel]
    if len(blocks) != 1 or blocks[0][0] != name:
        return {}
    _name, creator, creator_id, created, _id = blocks[0]
    return {"creator_id": creator_id,
            "creator_name": redact_secrets(creator)[:MAX_NAME],
            "created": created.strip()}


def _path(base_dir, lane):
    lane = lane or "default"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", lane)[:40]
    digest = hashlib.sha256(lane.encode()).hexdigest()[:10]
    return Path(base_dir) / "state" / f"{STATE_PREFIX}-{safe}-{digest}.json"


def load(base_dir, lane):
    """{"members": set or None (no baseline yet), "unnamed": {id: tries},
    "checked": epoch or None}."""
    empty = {"members": None, "unnamed": {}, "checked": None}
    try:
        data = json.loads(_path(base_dir, lane).read_text())
        members, checked = data["members"], data.get("checked")
        if members is not None and (
            not isinstance(members, list) or not all(map(_channel, members))
        ):
            raise ValueError("bad channel list")
        unnamed = data.get("unnamed") or {}
        if not isinstance(unnamed, dict) or not all(
            _channel(k) and isinstance(v, int) for k, v in unnamed.items()
        ):
            raise ValueError("bad unnamed")
        if not isinstance(checked, (int, float)) or isinstance(checked, bool):
            checked = None
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, KeyError, TypeError):
        log("comms inbox: joined-channel state unreadable; re-baselining")
        return empty
    return {"members": None if members is None else set(members),
            "unnamed": dict(unnamed),
            "checked": checked}


def save(base_dir, lane, members, checked, unnamed=None):
    """Write the state; a failure is logged, never raised."""
    try:
        state.write_atomic(
            _path(base_dir, lane),
            json.dumps({
                "members": None if members is None else sorted(members),
                "checked": checked,
                "unnamed": dict(sorted((unnamed or {}).items())),
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
    members, checked = saved["members"], saved["checked"]
    # A check time in the future (clock change, restored file) is ignored.
    if checked is not None and 0 <= now - checked \
            < inbox["joined_check_minutes"] * 60:
        return 0
    ids = parse_ids((list_session([ids_call()]) or [None])[0])
    if ids is None:
        log("comms inbox: channel listing incomplete or unreadable; "
            "joined channels not checked this run")
        save(base_dir, lane, members, now, saved["unnamed"])
        return 0
    if members is None or len(ids - members) > MAX_NEW:
        save(base_dir, lane, ids | (members or set()), now)
        log(f"comms inbox: joined-channel baseline of {len(ids)} channel(s)")
        return 0
    new = ids - members
    unnamed = {c: n for c, n in saved["unnamed"].items() if c in new}
    if not new:
        save(base_dir, lane, members, now)
        return 0
    text = (list_session([names_call()]) or [None])[0]
    names = parse_names(text, new) if text is not None else {}
    if text is None:
        log("comms inbox: full channel listing failed")
    # A channel without a name waits for later checks, up to NAME_TRIES.
    for channel in new - set(names):
        unnamed[channel] = unnamed.get(channel, 0) + 1
    ready = {c for c in new if c in names or unnamed[c] >= NAME_TRIES}
    if not ready:
        save(base_dir, lane, members, now, unnamed)
        return 0
    infos = {}
    lookups = [c for c in sorted(ready) if c in names][:MAX_INFO]
    if info_session and lookups:
        found = info_session([info_call(names[c][0]) for c in lookups])
        for channel, text in zip(lookups, found):
            info = parse_info(text, channel, names[channel][0])
            if info:
                infos[channel] = info
    rows = rows_for(ready, names, infos, now)
    added = record(rows)
    written = ready if added == len(rows) else set()
    save(base_dir, lane, members | written, now,
         {c: n for c, n in unnamed.items() if c not in written})
    log(f"comms inbox: {len(ready)} newly joined channel(s), {added} recorded"
        + (f", {len(new - ready)} waiting for a name" if new - ready else ""))
    return added
