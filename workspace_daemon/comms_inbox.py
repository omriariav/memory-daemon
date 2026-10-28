"""Comms inbox: a local record of what the Slack searches listed.

The `slack_mcp` source can keep `state/comms-inbox.jsonl` for a configured set
of conversations, so a reply tracker reads new messages (and the owner's own
replies and reactions) from disk instead of fetching Slack again.

Every routine with a `comms_inbox` block shares the files below, and each run
prunes them by the longest `retention_days` of the lanes active in the last
day. Runs that write them must not overlap: capture routines all run under
the daemon's `run` lock, which serializes them. It reuses
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
      track_reactions: [track-example]   # "watch this conversation" (at most 3)
      follow_threads: true     # every channel thread the owner posted in
      thread_polls: 10         # thread reads per run for followed threads

One JSON object per line, oldest message first:

    {"type": "message", "channel_id", "thread_ts", "ts", "user_id",
     "is_owner", "is_bot", "text", "seen_at"}
    {"type": "owner_reaction", "channel_id", "thread_ts", "ts", "emoji",
     "seen_at"}
    {"type": "peer_reaction", "channel_id", "thread_ts", "ts", "emoji",
     "user_id", "seen_at"}
    {"type": "track_request", "channel_id", "thread_ts", "ts", "emoji",
     "seen_at"}

A `track_reactions` emoji the owner puts on any message (found by a
`hasmy::<emoji>:` search looking back `retention_days`, no cursor) writes a
`track_request` line for that message and starts tracking its conversation:
the thread rooted at `thread_ts`, plus the whole conversation from that
message on when it is a DM or group DM. Messages of tracked conversations are
recorded like those of `channels`, until the track request is older than
`retention_days` (removing the reaction is not seen). A tracked channel that
no other query lists is searched by an extra `tracked` query (channels in
chunks, at most MAX_TRACKED_QUERIES), from a per-channel scan mark kept with
the tracked set in `state/comms-inbox-tracked.json`.

An empty block (`comms_inbox: {}`) is valid: that routine records only the
tracked and followed conversations its own searches list, and publishes its
searched channels so the routine that polls followed threads skips them.

Owner reactions (and peer reactions, below) come from two places. Search (`hasmy::<emoji>:`) finds the
listed emoji only. With `reactions_tool` (the connector's read-only
`slack_get_reactions`) on the source, each run also reads the reactions of
up to `reaction_checks` open messages: the newest message of a conversation
(a channel's top level, or one thread) younger than `reaction_check_hours`,
when it is from someone else with no recorded owner reaction, or the owner's
(not a bot's) with no recorded peer reaction. A reaction adds no search hit,
so open messages are polled, with a back-off of a quarter of the message's
age (at most 6h) between checks of the same one;
`state/comms-inbox-checks.json` keeps the last check per message. The owner's
own messages get at most half of `reaction_checks` (unused slots go to the
others), polled newest first. A reaction by someone other than the owner on
an owner message is recorded as a `peer_reaction` (one line per emoji and
user), so a reply tracker can see an emoji-only acknowledgement.

`follow_threads` adds a record-only `from:<@owner>` search (its own scan
mark, look-back `retention_days` on the first run): every channel message of
the owner, root or reply, makes its thread followed, outside DMs (those are
`all_dms`). A followed thread's messages are recorded from any query, and it
stays followed until `retention_days` after its last known activity (each
new message extends it). Replies in channels the source searches arrive by
search; a followed thread elsewhere is read with `read_thread_tool` (newest
`max_thread_replies` replies), at most `thread_polls` per run in one bridge
session, again only after a quarter of the time since its last activity has
passed since its last read (at most 6h). State: `state/comms-inbox-follow.json`.
Every routine with a `comms_inbox` block shares that state: it records the
followed threads its own searches list, and publishes the channels it
searches (`covered`, ignored after COVERED_TTL), so no routine polls a thread
another routine's search already lists. Only routines with `follow_threads`
run the owner search and the polls.

`thread_ts` is the thread root (equal to `ts` for a top-level message); an
`owner_reaction` or `peer_reaction` names the message it was put on. Lines
are never changed: messages are deduped on (channel_id, ts), reactions on
(channel_id, ts, emoji) and peer reactions also on user_id, so an edit or a removed reaction is not reflected. Lines whose `ts` is
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
    "reaction_check_hours", "track_reactions", "follow_threads",
    "thread_polls",
}
DEFAULTS = {"channels": [], "all_dms": False, "reactions": [],
            "retention_days": 14, "reaction_checks": 10,
            "reaction_check_hours": 72, "track_reactions": [],
            "follow_threads": False, "thread_polls": 10}
FOLLOW_FILE = "comms-inbox-follow.json"
MAX_FOLLOWED = 500
COVERED_TTL = 86400
TRACKED_FILE = "comms-inbox-tracked.json"
MAX_TRACK_REACTIONS = 3
MAX_TRACKED_QUERIES = 2
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
    for key in ("all_dms", "follow_threads"):
        if not isinstance(value.get(key, False), bool):
            return f"`{key}` must be true or false"
    for key, most in (
        ("reactions", MAX_REACTIONS), ("track_reactions", MAX_TRACK_REACTIONS),
    ):
        names = value.get(key, [])
        if (
            not isinstance(names, list) or len(names) > most
            or not all(isinstance(n, str) and EMOJI.match(n) for n in names)
            or len(set(names)) != len(names)
        ):
            return (
                f"`{key}` must be a list of at most {most} distinct emoji "
                "names (no colons)"
            )
    if set(value.get("reactions", [])) & set(value.get("track_reactions", [])):
        return "an emoji cannot be in both `reactions` and `track_reactions`"

    for key, lower, upper in (
        ("retention_days", 1, MAX_RETENTION_DAYS),
        ("reaction_checks", 0, 30), ("reaction_check_hours", 1, 336),
        ("thread_polls", 0, 30),
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
    cfg["track_reactions"] = list(cfg["track_reactions"])
    return cfg


def path(base_dir):
    return Path(base_dir) / "state" / FILE_NAME


def _key(row):
    if row.get("type") in ("owner_reaction", "track_request"):
        return (row["type"], row["channel_id"], row["ts"], row["emoji"])
    if row.get("type") == "peer_reaction":
        return (row["type"], row["channel_id"], row["ts"], row["emoji"],
                row["user_id"])
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


def peer_row(hit, emoji, user_id):
    return dict(reaction_row(hit, emoji), type="peer_reaction", user_id=user_id)


def track_row(hit, emoji):
    return dict(reaction_row(hit, emoji), type="track_request")


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


def due_checks(base_dir, limit, hours, now=None):
    """Open messages whose reactions are due a read, newest first.

    Each is {"channel_id", "ts", "thread_ts", "own"}. Open: the newest
    message of its conversation, not a bot's, younger than `hours`, and either
    someone else's with no recorded owner reaction, or the owner's ("own")
    with no recorded peer reaction. Due: never checked, or last checked at
    least a quarter of its age ago (capped at MAX_CHECK_GAP). The owner's
    messages take at most limit // 2 slots; slots either side leaves unused go
    to the other.
    """
    if base_dir is None or not limit:
        return []
    now = time.time() if now is None else now
    rows = _load(path(base_dir))
    reacted = {
        (r["channel_id"], r["ts"]) for r in rows
        if r.get("type") == "owner_reaction"
    }
    peer_reacted = {
        (r["channel_id"], r["ts"]) for r in rows
        if r.get("type") == "peer_reaction"
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
    others, own = [], []
    for row in newest.values():
        age = now - float(row["ts"])
        mine = bool(row.get("is_owner"))
        if (
            row.get("is_bot") or age > hours * 3600
            or (row["channel_id"], row["ts"]) in (
                peer_reacted if mine else reacted
            )
        ):
            continue
        last = checks.get(f"{row['channel_id']}:{row['ts']}")
        if last is not None and now - last < min(age / 4, MAX_CHECK_GAP):
            continue
        (own if mine else others).append({
            "channel_id": row["channel_id"], "ts": row["ts"],
            "thread_ts": row.get("thread_ts") or row["ts"], "own": mine,
        })
    for group in (others, own):
        group.sort(key=lambda d: float(d["ts"]), reverse=True)
    take_own = min(len(own), max(limit // 2, limit - len(others)))
    due = others[:limit - take_own] + own[:take_own]
    due.sort(key=lambda d: float(d["ts"]), reverse=True)
    return due


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


def parse_reactors(text):
    """[(emoji, [user ids])] from one slack_get_reactions result.

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
        found.append((match.group("emoji"), _REACTOR.findall(match.group("who"))))
    return found


def parse_reactions(text, owner):
    """Emoji names `owner` reacted with, from one slack_get_reactions result.

    Raises ValueError when the result has an unknown shape.
    """
    return [e for e, users in parse_reactors(text) if owner in users]


# --- tracked conversations ---------------------------------------------------

def _tracked_path(base_dir):
    return Path(base_dir) / "state" / TRACKED_FILE


def load_tracked(base_dir):
    """{"conversations": [{channel_id, thread_ts, since, whole}],
    "scanned": {channel: epoch}}; unreadable counts as empty."""
    empty = {"conversations": [], "scanned": {}}
    if base_dir is None:
        return empty
    try:
        data = json.loads(_tracked_path(base_dir).read_text())
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        log("comms inbox: tracked state is unreadable; starting empty")
        return empty
    out = {"conversations": [], "scanned": {}}
    if not isinstance(data, dict):
        return out
    conversations = data.get("conversations")
    for entry in conversations if isinstance(conversations, list) else []:
        try:
            item = {
                "channel_id": str(entry["channel_id"]),
                "thread_ts": str(entry["thread_ts"]),
                "since": str(entry["since"]),
                "whole": entry.get("whole") is True,
            }
            float(item["thread_ts"]), float(item["since"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        out["conversations"].append(item)
    scanned = data.get("scanned")
    for channel, when in (scanned.items() if isinstance(scanned, dict) else ()):
        if isinstance(when, (int, float)) and not isinstance(when, bool):
            out["scanned"][str(channel)] = when
    return out


def save_tracked(base_dir, tracked, retention_days, now=None):
    """Write the tracked set, dropping requests past retention."""
    if base_dir is None:
        return
    floor = (time.time() if now is None else now) - retention_days * 86400
    live = [c for c in tracked["conversations"] if float(c["since"]) >= floor]
    channels = {c["channel_id"] for c in live}
    data = {
        "conversations": live,
        "scanned": {
            k: v for k, v in tracked["scanned"].items() if k in channels
        },
    }
    try:
        state.write_atomic(
            _tracked_path(base_dir), json.dumps(data, sort_keys=True),
            mode=0o600,
        )
    except OSError:
        log("comms inbox: tracked state could not be written")


def add_tracked(tracked, hit, whole):
    """Track the conversation of `hit`; True when it was new.

    The thread rooted at the hit's root is tracked from the hit on; `whole`
    (a direct conversation) also tracks the channel's top level. A second
    request on a known conversation keeps the earlier `since`.
    """
    for item in tracked["conversations"]:
        if item["channel_id"] == hit["channel"] and (
            item["thread_ts"] == hit["root"] or (whole and item["whole"])
        ):
            if float(hit["ts"]) < float(item["since"]):
                item["since"] = hit["ts"]
            item["whole"] = item["whole"] or whole
            return False
    tracked["conversations"].append({
        "channel_id": hit["channel"], "thread_ts": hit["root"],
        "since": hit["ts"], "whole": bool(whole),
    })
    return True


def is_tracked(tracked, hit):
    """Whether `hit` belongs to a tracked conversation (at or after `since`)."""
    for item in tracked["conversations"]:
        if item["channel_id"] != hit["channel"]:
            continue
        if float(hit["ts"]) < float(item["since"]) and not (
            hit["ts"] == item["thread_ts"]
        ):
            continue
        if item["whole"] or hit["root"] == item["thread_ts"]:
            return True
    return False


# --- followed threads --------------------------------------------------------

def _follow_path(base_dir):
    return Path(base_dir) / "state" / FOLLOW_FILE


def load_follow(base_dir):
    """{"threads": {"<channel>:<root>": {"last", "checked"}}, "scanned":
    epoch or None}; `last` is the newest known message ts, `checked` the
    epoch of the last thread read. Unreadable counts as empty."""
    empty = {"threads": {}, "scanned": None, "covered": {}}
    if base_dir is None:
        return empty
    try:
        data = json.loads(_follow_path(base_dir).read_text())
    except FileNotFoundError:
        return empty
    except (OSError, ValueError):
        log("comms inbox: follow state is unreadable; starting empty")
        return empty
    if not isinstance(data, dict):
        return empty
    out = {"threads": {}, "scanned": None, "covered": {}}
    covered = data.get("covered")
    for lane, entry in (covered.items() if isinstance(covered, dict) else ()):
        try:
            days = entry.get("retention_days", DEFAULTS["retention_days"])
            out["covered"][str(lane)] = {
                "at": float(entry["at"]),
                "channels": [str(c) for c in entry["channels"]],
                "retention_days": int(days),
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    scanned = data.get("scanned")
    if isinstance(scanned, (int, float)) and not isinstance(scanned, bool):
        out["scanned"] = scanned
    threads = data.get("threads")
    for key, entry in (threads.items() if isinstance(threads, dict) else ()):
        try:
            channel, root = str(key).split(":", 1)
            float(root), float(entry["last"])
            checked = entry.get("checked")
            if checked is not None:
                float(checked)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        out["threads"][f"{channel}:{root}"] = {
            "last": str(entry["last"]), "checked": checked,
        }
    return out


def save_follow(base_dir, follow, retention_days, now=None):
    """Write the follow state, dropping threads idle past retention."""
    if base_dir is None:
        return
    floor = (time.time() if now is None else now) - retention_days * 86400
    live = {
        key: entry for key, entry in follow["threads"].items()
        if float(entry["last"]) >= floor
    }
    if len(live) > MAX_FOLLOWED:
        newest = sorted(live, key=lambda k: float(live[k]["last"]))
        live = {key: live[key] for key in newest[-MAX_FOLLOWED:]}
    data = {
        "threads": live, "scanned": follow["scanned"],
        "covered": {
            routine: entry
            for routine, entry in follow.get("covered", {}).items()
            if entry["at"] >= (time.time() if now is None else now)
            - COVERED_TTL
        },
    }
    try:
        state.write_atomic(
            _follow_path(base_dir), json.dumps(data, sort_keys=True),
            mode=0o600,
        )
    except OSError:
        log("comms inbox: follow state could not be written")


def follow_thread(follow, channel, root, ts):
    """Follow `channel`/`root` (or extend it) with activity at `ts`; True
    when the thread was new."""
    key = f"{channel}:{root}"
    entry = follow["threads"].get(key)
    if entry is None:
        follow["threads"][key] = {"last": ts, "checked": None}
        return True
    if float(ts) > float(entry["last"]):
        entry["last"] = ts
    return False


def is_followed(follow, hit):
    return f"{hit['channel']}:{hit['root']}" in follow["threads"]


def keep_days(follow, now=None):
    """Days the shared inbox files keep: the longest retention any lane
    with a live coverage entry asks for."""
    floor = (time.time() if now is None else now) - COVERED_TTL
    days = [
        entry.get("retention_days", DEFAULTS["retention_days"])
        for entry in follow["covered"].values() if entry["at"] >= floor
    ]
    return max(days or [DEFAULTS["retention_days"]])


def covered_channels(follow, now=None):
    """Channels some routine's search lists (published within COVERED_TTL)."""
    floor = (time.time() if now is None else now) - COVERED_TTL
    return {
        channel for entry in follow["covered"].values()
        if entry["at"] >= floor for channel in entry["channels"]
    }


def due_polls(follow, covered, limit, now=None):
    """Followed threads to read now, most recently active first.

    Threads in `covered` channels are listed by search and never read. Due:
    never read, or read at least a quarter of the time since the thread's
    last activity ago (at most MAX_CHECK_GAP).
    """
    if not limit:
        return []
    now = time.time() if now is None else now
    due = []
    for key, entry in follow["threads"].items():
        channel, root = key.split(":", 1)
        if channel in covered:
            continue
        idle = max(0.0, now - float(entry["last"]))
        checked = entry.get("checked")
        if checked is not None and now - checked < min(idle / 4, MAX_CHECK_GAP):
            continue
        due.append((float(entry["last"]), channel, root))
    due.sort(reverse=True)
    return [(channel, root) for _last, channel, root in due[:limit]]
