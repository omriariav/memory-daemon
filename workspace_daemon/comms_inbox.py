"""Comms inbox: a local record of what the Slack searches listed.

The `slack_mcp` source can keep `state/comms-inbox.jsonl` for a configured set
of conversations, so a reply tracker reads new messages (and the owner's own
replies and reactions) from disk instead of fetching Slack again. It reuses
the hits every run's searches already return: no extra read, no model call,
and no settle delay (a message is recorded in the run that first lists it).

Watch file (or source) block:

    comms_inbox:
      channels: [C0EXAMPLE01, D0EXAMPLE03]   # conversations to record
      all_dms: true            # also every DM and group DM the `dms` query lists
      reactions: [eyes]        # owner reactions worth recording (at most 5)
      retention_days: 14       # default 14

One JSON object per line, oldest message first:

    {"type": "message", "channel_id", "thread_ts", "ts", "user_id",
     "is_owner", "is_bot", "text", "seen_at"}
    {"type": "owner_reaction", "channel_id", "thread_ts", "ts", "emoji",
     "seen_at"}

`thread_ts` is the thread root (equal to `ts` for a top-level message); an
`owner_reaction` names the message it was put on. Lines are never changed:
messages are deduped on (channel_id, ts) and reactions on (channel_id, ts,
emoji), so an edit or a removed reaction is not reflected. Lines whose `ts` is
older than the retention window are dropped. The file is rewritten atomically
(mode 600), so a reader never sees a partial line.
"""
import json
import re
import time
from pathlib import Path

from . import state
from .chat_text import redact_secrets
from .shell import log, utc_now_iso

FILE_NAME = "comms-inbox.jsonl"
KEYS = {"channels", "all_dms", "reactions", "retention_days"}
DEFAULTS = {"channels": [], "all_dms": False, "reactions": [],
            "retention_days": 14}
MAX_REACTIONS = 5
MAX_RETENTION_DAYS = 90
MAX_TEXT = 4000
CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{8,}$")
EMOJI = re.compile(r"^[a-z0-9][a-z0-9_+'-]{0,99}$")


def problem(value):
    """Why `value` is not a valid `comms_inbox` block, or None."""
    if not isinstance(value, dict):
        return "must be a mapping"
    unknown = set(value) - KEYS
    if unknown:
        return f"has unknown key(s): {', '.join(sorted(map(str, unknown)))}"
    channels = value.get("channels", [])
    if not isinstance(channels, list) or not all(
        isinstance(c, str) and CHANNEL_ID.match(c) for c in channels
    ):
        return "`channels` must be a list of Slack channel ids"
    if not isinstance(value.get("all_dms", False), bool):
        return "`all_dms` must be true or false"
    if not channels and value.get("all_dms") is not True:
        return "needs `channels` and/or `all_dms: true`"
    reactions = value.get("reactions", [])
    if (
        not isinstance(reactions, list) or len(reactions) > MAX_REACTIONS
        or not all(isinstance(n, str) and EMOJI.match(n) for n in reactions)
        or len(set(reactions)) != len(reactions)
    ):
        return (
            f"`reactions` must be a list of at most {MAX_REACTIONS} distinct "
            "emoji names (no colons)"
        )
    days = value.get("retention_days", DEFAULTS["retention_days"])
    if not (
        isinstance(days, int) and not isinstance(days, bool)
        and 1 <= days <= MAX_RETENTION_DAYS
    ):
        return f"`retention_days` must be an integer from 1 to {MAX_RETENTION_DAYS}"
    return None


def settings(value):
    """The block merged over its defaults, or None when absent or invalid."""
    if value is None or problem(value):
        return None
    cfg = dict(DEFAULTS)
    cfg.update(value)
    cfg["channels"] = list(cfg["channels"])
    cfg["reactions"] = list(cfg["reactions"])
    return cfg


def path(base_dir):
    return Path(base_dir) / "state" / FILE_NAME


def _key(row):
    if row.get("type") == "owner_reaction":
        return ("owner_reaction", row["channel_id"], row["ts"], row["emoji"])
    return ("message", row["channel_id"], row["ts"])


def _load(file):
    rows = []
    try:
        text = file.read_text()
    except FileNotFoundError:
        return rows
    except OSError:
        log("comms inbox: existing file is unreadable; starting empty")
        return rows
    for line in text.splitlines():
        try:
            row = json.loads(line)
            float(row["ts"])
            _key(row)
        except (ValueError, TypeError, KeyError):
            continue
        rows.append(row)
    return rows


def message_row(hit, self_user_id):
    return {
        "type": "message",
        "channel_id": hit["channel"],
        "thread_ts": hit["root"],
        "ts": hit["ts"],
        "user_id": hit["user"],
        "is_owner": hit["user"] == self_user_id,
        "is_bot": bool(hit["is_bot"]),
        "text": redact_secrets(hit["text"])[:MAX_TEXT],
    }


def reaction_row(hit, emoji):
    return {
        "type": "owner_reaction",
        "channel_id": hit["channel"],
        "thread_ts": hit["root"],
        "ts": hit["ts"],
        "emoji": emoji,
    }


def record(base_dir, rows, retention_days, now=None):
    """Add `rows` (deduped) and drop expired lines; returns lines added.

    Rewrites the file only when something changed. A write failure is logged
    and swallowed: the inbox is a convenience, never a reason to fail a run.
    """
    if base_dir is None:
        return 0
    file = path(base_dir)
    floor = (time.time() if now is None else now) - retention_days * 86400
    existing = _load(file)
    kept = [row for row in existing if float(row["ts"]) >= floor]
    known = {_key(row) for row in kept}
    stamp = utc_now_iso()
    added = []
    for row in rows:
        if float(row["ts"]) < floor or _key(row) in known:
            continue
        known.add(_key(row))
        added.append(dict(row, seen_at=stamp))
    if not added and len(kept) == len(existing) and file.exists():
        return 0
    merged = sorted(kept + added, key=lambda r: float(r["ts"]))
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in merged)
    try:
        state.write_atomic(file, text, mode=0o600)
    except OSError:
        log("comms inbox: write failed; nothing recorded this run")
        return 0
    return len(added)
