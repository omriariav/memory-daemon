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
      reactions: [eyes]        # owner reactions found by search (at most 5)
      retention_days: 14       # default 14
      reaction_checks: 10      # reaction reads per run (needs reactions_tool)
      reaction_check_hours: 72 # how long an unanswered message is re-checked

One JSON object per line, oldest message first:

    {"type": "message", "channel_id", "thread_ts", "ts", "user_id",
     "is_owner", "is_bot", "text", "seen_at"}
    {"type": "owner_reaction", "channel_id", "thread_ts", "ts", "emoji",
     "seen_at"}

Owner reactions come from two places. Search (`hasmy::<emoji>:`) finds the
listed emoji only. With `reactions_tool` (the connector's read-only
`slack_get_reactions`) on the source, each run also reads the reactions of
up to `reaction_checks` open messages: the newest message of a conversation
(a channel's top level, or one thread) when it is from someone else, carries
no recorded owner reaction, and is younger than `reaction_check_hours`. A
reaction adds no search hit, so open messages are polled, with a back-off of
a quarter of the message's age (at most 6h) between checks of the same one;
`state/comms-inbox-checks.json` keeps the last check per message.

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
KEYS = {
    "channels", "all_dms", "reactions", "retention_days", "reaction_checks",
    "reaction_check_hours",
}
DEFAULTS = {"channels": [], "all_dms": False, "reactions": [],
            "retention_days": 14, "reaction_checks": 10,
            "reaction_check_hours": 72}
CHECKS_FILE = "comms-inbox-checks.json"
MAX_CHECK_GAP = 6 * 3600
MAX_REACTIONS = 5
MAX_RETENTION_DAYS = 90
MAX_TEXT = 4000
_REACTION_LINE = re.compile(
    # Skin tones arrive as `:+1::skin-tone-2:`.
    r"^:(?P<emoji>[^\s:][^\s]{0,120}?): \u00d7 \d+ \u2014 (?P<who>.*)$"
)
_REACTOR = re.compile(r"\(([UW][A-Z0-9]+)\)")
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
    for key, lower, upper in (
        ("retention_days", 1, MAX_RETENTION_DAYS),
        ("reaction_checks", 0, 30), ("reaction_check_hours", 1, 336),
    ):
        number = value.get(key, DEFAULTS[key])
        if not (
            isinstance(number, int) and not isinstance(number, bool)
            and lower <= number <= upper
        ):
            return f"`{key}` must be an integer from {lower} to {upper}"
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


def _checks_path(base_dir):
    return Path(base_dir) / "state" / CHECKS_FILE


def _load_checks(base_dir):
    try:
        data = json.loads(_checks_path(base_dir).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def due_checks(base_dir, self_user_id, limit, hours, now=None):
    """Open messages whose reactions are due a read, newest first.

    Each is {"channel_id", "ts", "thread_ts"}. Open: the newest message of
    its conversation, not the owner's and not a bot's, with no recorded owner
    reaction, younger than `hours`. Due: never checked, or last checked at
    least a quarter of its age ago (capped at MAX_CHECK_GAP).
    """
    if base_dir is None or not limit:
        return []
    now = time.time() if now is None else now
    rows = _load(path(base_dir))
    reacted = {
        (r["channel_id"], r["ts"]) for r in rows
        if r.get("type") == "owner_reaction"
    }
    newest = {}
    for row in rows:
        if row.get("type") != "message":
            continue
        thread = row.get("thread_ts")
        conversation = (
            row["channel_id"], thread if thread and thread != row["ts"] else ""
        )
        if (
            conversation not in newest
            or float(row["ts"]) > float(newest[conversation]["ts"])
        ):
            newest[conversation] = row
    checks = _load_checks(base_dir)
    due = []
    for row in newest.values():
        age = now - float(row["ts"])
        if (
            row.get("is_owner") or row.get("is_bot")
            or (row["channel_id"], row["ts"]) in reacted
            or age > hours * 3600
        ):
            continue
        last = checks.get(f"{row['channel_id']}:{row['ts']}")
        if last is not None and now - last < min(age / 4, MAX_CHECK_GAP):
            continue
        due.append({
            "channel_id": row["channel_id"], "ts": row["ts"],
            "thread_ts": row.get("thread_ts") or row["ts"],
        })
    due.sort(key=lambda d: float(d["ts"]), reverse=True)
    return due[:limit]


def mark_checked(base_dir, keys, hours, now=None):
    """Remember when each `<channel>:<ts>` was read; forget expired ones."""
    if base_dir is None:
        return
    now = time.time() if now is None else now
    checks = _load_checks(base_dir)
    checks.update({key: now for key in keys})
    checks = {
        key: when for key, when in checks.items()
        if now - float(key.rsplit(":", 1)[1]) <= hours * 3600
    }
    try:
        state.write_atomic(
            _checks_path(base_dir), json.dumps(checks, sort_keys=True),
            mode=0o600,
        )
    except OSError:
        log("comms inbox: reaction check state could not be written")


def parse_reactions(text, owner):
    """Emoji names `owner` reacted with, from one slack_get_reactions result.

    Raises ValueError when the result has an unknown shape.
    """
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        data = text
    if isinstance(data, dict):
        data = data.get("result")
    if not isinstance(data, str):
        raise ValueError("reaction result has no text")
    body = data.strip()
    if not body.startswith("Reactions on message:"):
        if re.search(r"no reactions", body, re.I):
            return []
        raise ValueError("reaction result has an unknown shape")
    found = []
    for line in body.split("\n")[1:]:
        line = line.strip()
        if not line:
            continue
        match = _REACTION_LINE.match(line)
        if not match:
            raise ValueError("reaction result has an unknown line")
        if owner in _REACTOR.findall(match.group("who")):
            found.append(match.group("emoji"))
    return found
