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
      ignore_reactors: [U0EXAMPLE09]   # never recorded as peer reactions (at most 20)
      track_reactions: [track-example]   # "watch this conversation" (at most 3)
      track_lookback_days: 7   # track emoji count on messages this recent
                               # (default retention_days)
      watch_channels: [C0EXAMPLE02]   # every top-level post by anyone (at most 30)
      bot_text_users: [U0EXAMPLED]    # apps whose DM text is read in full (at most 10)
      seen_reactions: [eyes]   # owner emoji that mean "seen", not "handled" (at most 5)
      follow_threads: true     # every channel thread the owner posted in
      thread_polls: 10         # thread reads per run for followed threads
      joined_channels: true    # a line per newly joined channel (needs
                               # channels_tool; see comms_joined.py)
      joined_check_minutes: 60 # how often channels are listed (15 to 1440)

One JSON object per line, oldest message first:

    {"type": "message", "channel_id", "thread_ts", "ts", "user_id",
     "is_owner", "is_bot", "text", "seen_at"}

plus, on any line type, the optional "channel_type" and "peer_is_bot"
described below.
    {"type": "owner_reaction", "channel_id", "thread_ts", "ts", "emoji",
     "seen_at"}
    {"type": "peer_reaction", "channel_id", "thread_ts", "ts", "emoji",
     "user_id", "seen_at"}
    {"type": "track_request", "channel_id", "thread_ts", "ts", "emoji",
     "seen_at"}
    {"type": "channel_joined", "channel_id", "name", "private", "ts",
     "thread_ts", "seen_at"} (+ creator details; see comms_joined.py, and
     "watch_suggested": true when suggested for a watch; see watch_joins.py)

Every line (any type) written since channel types were added also carries
`channel_type`: "im" (a direct message), "mpim" (a group DM) or "channel"
(public or private). It comes from the search hit's own label (`DM` on a
`D...` id, `Group DM`); a line without a label (a thread or reaction read)
takes the type any other line of that channel in the inbox carries, and has
none when no line does. Lines of an "im" also carry `peer_is_bot` once a
message from someone other than the owner is in the inbox: true when every
such message carries the bot marker or is Slackbot (an app), false
otherwise. The value is
fixed when the line is written, so read it from the newest line of the
conversation. Limits: an app the owner uses that posts in a person's DM
before the person does counts as the peer until they reply; only lines
within `retention_days` count, so a person silent longer than that whose
agent then posts reads as a bot, and a channel whose labelled lines all
expired gives unlabelled lines no type; a `DM` label on a non-`D` id (which
the `dms` query still records) is typed "channel", as in dm_kind. Lines written before this have neither
field; readers treat a missing field as unknown.

A `track_reactions` emoji the owner puts on any message (found by a
`hasmy::<emoji>:` search looking back `track_lookback_days`, default
`retention_days`, no cursor; Slack filters on the message date, so a track
emoji put on an older message is not seen) writes a
`track_request` line for that message and starts tracking its conversation:
the thread rooted at `thread_ts`, plus the whole conversation from that
message on when it is a DM or group DM. Messages of tracked conversations are
recorded like those of `channels`, until the track request is older than
`retention_days` (removing the reaction is not seen). A tracked channel that
no other query lists is searched by an extra `tracked` query (channels in
chunks, at most MAX_TRACKED_QUERIES), from a per-channel scan mark kept with
the tracked set in `state/comms-inbox-tracked.json`.

`watch_channels` records every TOP-LEVEL message (`thread_ts == ts`) of anyone
in those public or private channels as an ordinary "message" line (channel_type
"channel"), whether or not the owner took part. Bots are left out unless the
source sets `include_bots`; thread replies are not recorded (the owner's own
threads are covered by `follow_threads`, and `channels` still records
everything it lists). Hits come from the routine's own searches: a watched
channel in the routine's `channels` list (the watch file's, discovered ones
included) is already searched, so its hits are simply kept. A watched channel
the routine does not search gets an inbox-only `watched` query (`in:<#C...>`,
channels in chunks of 15, at most MAX_WATCHED_QUERIES per run and within
`max_queries_per_run`) from a per-channel scan mark kept in
`state/comms-inbox-watched.json` (first run: `retention_days` back; the mark
advances like a cursor and rewinds by the source's `overlap`). Its hits are
recorded only: they never reach capture or analysis, so they cost one search
each and no model call. Another routine searching the same channel does not
help, because a routine records only the hits of its own searches.

`bot_text_users` names apps (user ids) whose messages arrive in a DM with an
empty search text (the Google Drive app posts its comment notifications as
attachments, which the search result's `Text:` does not carry). A DM line of
such a user with no text is completed from `read_channel_tool`: each run reads
one history page (newest first) of up to MAX_BOT_TEXT_READS DMs, from the
oldest empty line of the app in that DM, and records the app's messages with
their full rendered text (secret-redacted, cut at MAX_TEXT) and `is_bot` true.
Lines are otherwise never changed, with one exception: a stored line of a
`bot_text_users` user whose text is "" gains the text read later (that is also
the one-time backfill of lines written before the option existed). A message
whose text still reads empty is tried MAX_BOT_TEXT_TRIES times
(`state/comms-inbox-bottext.json`) and then left. Needs `read_channel_tool`
and `all_dms` (inbox and source). Other apps stay as they were.

`seen_reactions` lists owner emoji that mean "seen", not "handled". Such a
reaction is still recorded as an `owner_reaction`, but it does not stop the
reaction polling of its message (which continues, with the usual back-off and
age limit, until an owner reaction outside the list is recorded). Messages of
a `bot_text_users` app are polled too, although they are bot messages, and each
of them separately (not only the newest of the DM), within `reaction_checks`.
Both need `reactions_tool`. The default (empty list) is the earlier behaviour.

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
user), so a reply tracker can see an emoji-only acknowledgement. Reactors in
`ignore_reactors` are skipped. The bot marker is not used: a person's agent
posts under that person's own id, so it cannot tell a bot from a person.

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
import os
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
    "thread_polls", "ignore_reactors", "joined_channels",
    "joined_check_minutes", "track_lookback_days", "suggest_watch",
    "watch_channels", "bot_text_users", "seen_reactions",
}
DEFAULTS = {"channels": [], "all_dms": False, "reactions": [],
            "retention_days": 14, "reaction_checks": 10,
            "reaction_check_hours": 72, "track_reactions": [],
            "follow_threads": False, "thread_polls": 10,
            "ignore_reactors": [], "joined_channels": False,
            "joined_check_minutes": 60, "track_lookback_days": None,
            "suggest_watch": None, "watch_channels": [],
            "bot_text_users": [], "seen_reactions": []}
FOLLOW_FILE = "comms-inbox-follow.json"
MAX_FOLLOWED = 500
COVERED_TTL = 86400
TRACKED_FILE = "comms-inbox-tracked.json"
MAX_TRACK_REACTIONS = 3
MAX_TRACKED_QUERIES = 2
WATCHED_FILE = "comms-inbox-watched.json"
MAX_WATCH_CHANNELS = 30
MAX_WATCHED_QUERIES = 2
BOT_TEXT_FILE = "comms-inbox-bottext.json"
MAX_BOT_TEXT_USERS = 10
MAX_BOT_TEXT_READS = 4
MAX_BOT_TEXT_TRIES = 3
CHECKS_FILE = "comms-inbox-checks.json"
MAX_CHECK_GAP = 6 * 3600
MAX_REACTIONS = 5
MAX_IGNORE_REACTORS = 20
MAX_RETENTION_DAYS = 90
MAX_TEXT = 4000
MAX_FILES = 10
SLACKBOT = "USLACKBOT"
_REACTION_LINE = re.compile(
    # Skin tones arrive as `:+1::skin-tone-2:`.
    r"^:(?P<emoji>[^\s:][^\s]{0,120}?): \u00d7 (?P<count>\d+) \u2014 "
    r"(?P<who>.*)$"
)
_REACTOR = re.compile(r"\(([UW][A-Z0-9]+)\)")
# One reactor entry: a display name, then the id in parentheses at its END.
_REACTOR_ENTRY = re.compile(r"[^\n]*? \(([UW][A-Z0-9]+)\)")
CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{8,}$")
WATCH_CHANNEL_ID = re.compile(r"^[CG][A-Z0-9]{8,}$")
USER_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")
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
    for key in ("all_dms", "follow_threads", "joined_channels"):
        if not isinstance(value.get(key, False), bool):
            return f"`{key}` must be true or false"
    for key, most in (
        ("reactions", MAX_REACTIONS), ("track_reactions", MAX_TRACK_REACTIONS),
        ("seen_reactions", MAX_REACTIONS),
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
    watch = value.get("watch_channels", [])
    if (
        not isinstance(watch, list) or len(watch) > MAX_WATCH_CHANNELS
        or not all(isinstance(c, str) and WATCH_CHANNEL_ID.match(c)
                   for c in watch)
        or len(set(watch)) != len(watch)
    ):
        return (
            f"`watch_channels` must be a list of at most {MAX_WATCH_CHANNELS}"
            " distinct channel ids (C... or G...)"
        )
    bots = value.get("bot_text_users", [])
    if (
        not isinstance(bots, list) or len(bots) > MAX_BOT_TEXT_USERS
        or not all(isinstance(u, str) and USER_ID.match(u) for u in bots)
        or len(set(bots)) != len(bots)
    ):
        return (
            f"`bot_text_users` must be a list of at most {MAX_BOT_TEXT_USERS}"
            " distinct Slack user ids"
        )
    ignore = value.get("ignore_reactors", [])
    if (
        not isinstance(ignore, list) or len(ignore) > MAX_IGNORE_REACTORS
        or not all(isinstance(u, str) and USER_ID.match(u) for u in ignore)
        or len(set(ignore)) != len(ignore)
    ):
        return (
            f"`ignore_reactors` must be a list of at most {MAX_IGNORE_REACTORS}"
            " distinct Slack user ids"
        )
    if set(value.get("reactions", [])) & set(value.get("track_reactions", [])):
        return "an emoji cannot be in both `reactions` and `track_reactions`"
    if set(value.get("seen_reactions", [])) & set(
        value.get("track_reactions", [])
    ):
        return "an emoji cannot be in both `seen_reactions` and `track_reactions`"
    suggest = value.get("suggest_watch")
    if suggest is not None and not (
        isinstance(suggest, str) and suggest
        # os.path leaves an unknown ~user as is (pathlib raises).
        and os.path.isabs(os.path.expanduser(suggest))
    ):
        return "`suggest_watch` must be an absolute or ~ path to a watch file"

    for key, lower, upper in (
        ("retention_days", 1, MAX_RETENTION_DAYS),
        ("reaction_checks", 0, 30), ("reaction_check_hours", 1, 336),
        ("thread_polls", 0, 30), ("joined_check_minutes", 15, 1440),
    ):
        number = value.get(key, DEFAULTS[key])
        if not (
            isinstance(number, int) and not isinstance(number, bool)
            and lower <= number <= upper
        ):
            return f"`{key}` must be an integer from {lower} to {upper}"
    lookback = value.get("track_lookback_days")
    cap = value.get("retention_days", DEFAULTS["retention_days"])
    if lookback is not None and not (
        isinstance(lookback, int) and not isinstance(lookback, bool)
        and 1 <= lookback <= cap
    ):
        return (f"`track_lookback_days` must be an integer from 1 to {cap} "
                "(retention_days)")
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
    cfg["ignore_reactors"] = list(cfg["ignore_reactors"])
    cfg["watch_channels"] = list(cfg["watch_channels"])
    cfg["bot_text_users"] = list(cfg["bot_text_users"])
    cfg["seen_reactions"] = list(cfg["seen_reactions"])
    return cfg


def path(base_dir):
    return Path(base_dir) / "state" / FILE_NAME


def _key(row):
    if row.get("type") in ("owner_reaction", "track_request"):
        return (row["type"], row["channel_id"], row["ts"], row["emoji"])
    if row.get("type") == "channel_joined":
        return (row["type"], row["channel_id"], row["ts"])
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


def dm_kind(hit):
    """'im' or 'mpim' for a hit in a direct conversation, else None.

    Only the hit's own label and id say so: `D...` with label DM is an IM;
    label Group DM is an mpim. A legacy `G...` private channel carries
    neither label, so it is never taken for a DM.
    """
    if hit.get("label") == "DM" and hit["channel"].startswith("D"):
        return "im"
    if hit.get("label") == "Group DM":
        return "mpim"
    return None


def channel_type(hit):
    """dm_kind, or "channel" for any other labelled hit; None without a label."""
    if not (hit.get("label") or "").strip():
        return None
    return dm_kind(hit) or "channel"


def _size(value):
    """A byte count from an int or text such as `12345`, `3.4 MB`; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    found = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(bytes?|b|kb|mb|gb)?\s*", str(value or ""),
        re.I,
    )
    if not found:
        return None
    unit = (found.group(2) or "b").lower()
    scale = {"kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}.get(unit, 1)
    return int(float(found.group(1)) * scale)


def clean_files(files):
    """`files` as [{name, filetype, size, permalink}] (what is known), capped.

    `permalink` is kept only when the source supplies one; slack_mcp does not.

    At most MAX_FILES entries; each text field is secret-redacted and cut.
    Entries without any usable field are dropped.
    """
    out = []
    for item in files if isinstance(files, (list, tuple)) else ():
        if not isinstance(item, dict):
            continue
        entry = {}
        for key, limit in (("name", 200), ("filetype", 120), ("permalink", 300)):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = redact_secrets(value.strip())[:limit]
        size = _size(item.get("size"))
        if size is not None:
            entry["size"] = size
        if entry:
            out.append(entry)
        if len(out) >= MAX_FILES:
            break
    return out


def message_row(hit, self_user_id):
    row = {
        "type": "message",
        "channel_id": hit["channel"],
        "thread_ts": hit["root"],
        "ts": hit["ts"],
        "user_id": hit["user"],
        "is_owner": hit["user"] == self_user_id,
        "is_bot": bool(hit["is_bot"]),
        "text": redact_secrets(hit["text"])[:MAX_TEXT],
    }
    kind = channel_type(hit)
    if kind:
        row["channel_type"] = kind
    files = clean_files(hit.get("files"))
    if files:
        row["files"] = files
    return row


def dm_channels(base_dir, since):
    """{channel: (channel_type, newest ts)} of IM/group-DM channels with an
    inbox line at or after epoch `since`."""
    out = {}
    for row in _load(path(base_dir)):
        if row.get("channel_type") not in ("im", "mpim"):
            continue
        ts = float(row["ts"])
        if ts < since:
            continue
        old = out.get(row["channel_id"])
        if old is None or ts > old[1]:
            out[row["channel_id"]] = (row["channel_type"], ts)
    return out


def reaction_row(hit, emoji):
    row = {
        "type": "owner_reaction",
        "channel_id": hit["channel"],
        "thread_ts": hit["root"],
        "ts": hit["ts"],
        "emoji": emoji,
    }
    kind = channel_type(hit)
    if kind:
        row["channel_type"] = kind
    return row


def peer_row(hit, emoji, user_id):
    return dict(reaction_row(hit, emoji), type="peer_reaction", user_id=user_id)


def track_row(hit, emoji):
    return dict(reaction_row(hit, emoji), type="track_request")


def _annotate(added, rows):
    """Label `added` with channel_type and IM peer_is_bot from all `rows`.

    The inbox itself is the record: a line keeps the type its own hit label
    gave it, and an unlabelled line takes the type another line of its
    channel carries. An IM peer is a bot when every non-owner message of that
    IM in the inbox carries the bot marker (or is Slackbot).
    """
    kinds, peers = {}, {}
    for row in rows:
        if row.get("channel_type"):
            kinds[row["channel_id"]] = row["channel_type"]
    for row in rows:
        if kinds.get(row["channel_id"]) == "im" \
                and row.get("type", "message") == "message" \
                and not row.get("is_owner"):
            bot = bool(row.get("is_bot")) or row.get("user_id") == SLACKBOT
            peers.setdefault(row["channel_id"], set()).add(bot)
    for row in added:
        kind = row.get("channel_type") or kinds.get(row["channel_id"])
        if kind:
            row["channel_type"] = kind
        if kind == "im" and row["channel_id"] in peers:
            row["peer_is_bot"] = peers[row["channel_id"]] == {True}


def record(base_dir, rows, retention_days, now=None, exclude_channels=(),
           text_users=()):
    """Add `rows` (deduped) and drop expired lines; returns lines added.

    A stored message of a `text_users` user with no text gains the text of a
    row that has some (the only change a stored message text ever gets).

    Rows of `exclude_channels` are never added, and any already stored are
    dropped. Rewrites the file only when something changed. A write failure is logged
    and swallowed: the inbox is a convenience, never a reason to fail a run.
    """
    if base_dir is None:
        return 0
    file = path(base_dir)
    floor = (time.time() if now is None else now) - retention_days * 86400
    existing = _load(file)
    skip = set(exclude_channels or ())
    kept = [
        row for row in existing
        if float(row["ts"]) >= floor and row.get("channel_id") not in skip
    ]
    known = {_key(row) for row in kept}
    stamp = utc_now_iso()
    added = []
    patched = False
    by_key = {_key(row): row for row in kept}
    for row in rows:
        if float(row["ts"]) < floor or row.get("channel_id") in skip:
            continue
        if _key(row) in known:
            # A message first stored without its files (seen by search)
            # gains them when a channel read later reports them.
            old = by_key.get(_key(row))
            if old is not None and row.get("files") and not old.get("files"):
                old["files"] = row["files"]
                patched = True
            if (
                old is not None and row.get("type", "message") == "message"
                and row.get("text") and not old.get("text")
                and old.get("user_id") in text_users
            ):
                old["text"] = row["text"]
                patched = True
            continue
        known.add(_key(row))
        added.append(dict(row, seen_at=stamp))
    if added:
        _annotate(added, kept + added)
    if (
        not added and not patched and len(kept) == len(existing)
        and file.exists()
    ):
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


def due_checks(base_dir, limit, hours, now=None, seen=(), bot_users=()):
    """Open messages whose reactions are due a read, newest first.

    Each is {"channel_id", "ts", "thread_ts", "own"}. Open: the newest
    message of its conversation, not a bot's, younger than `hours`, and either
    someone else's with no recorded owner reaction, or the owner's ("own")
    with no recorded peer reaction. Due: never checked, or last checked at
    least a quarter of its age ago (capped at MAX_CHECK_GAP). The owner's
    messages take at most limit // 2 slots; slots either side leaves unused go
    to the other.

    An owner reaction in `seen` does not close a message. A message of a
    `bot_users` user is pollable although a bot's, one conversation each.
    """
    if base_dir is None or not limit:
        return []
    now = time.time() if now is None else now
    rows = _load(path(base_dir))
    reacted = {
        (r["channel_id"], r["ts"]) for r in rows
        if r.get("type") == "owner_reaction" and r.get("emoji") not in seen
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
        if row.get("user_id") in bot_users and conversation[1] == "":
            conversation = (row["channel_id"], row["ts"])
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
            (row.get("is_bot") and row.get("user_id") not in bot_users)
            or age > hours * 3600
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


def _reactor_ids(who, count):
    """Ids at the end of each `Name (ID)` entry of a reactor list.

    Fail closed: a display name may hold text like `(U...)`, so every id-like
    token must be accounted for: the number of such tokens, the entries and
    the stated `\u00d7 N` must agree, else ValueError (a truncated list too).
    """
    entries = re.split(r"(?<=\)), ", who)
    ids = []
    for entry in entries:
        match = _REACTOR_ENTRY.fullmatch(entry)
        if not match:
            raise ValueError("reaction result has an unknown reactor list")
        ids.append(match.group(1))
    if not (len(_REACTOR.findall(who)) == len(ids) == count):
        raise ValueError("reaction result has an ambiguous reactor list")
    return ids


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
        found.append((match.group("emoji"), _reactor_ids(
            match.group("who"), int(match.group("count"))
        )))
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


# --- watched channels and bot text -------------------------------------------

def _json_state(file):
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_watched(base_dir):
    """{channel: epoch of its last complete inbox-only scan}."""
    if base_dir is None:
        return {}
    scanned = _json_state(Path(base_dir) / "state" / WATCHED_FILE).get("scanned")
    if not isinstance(scanned, dict):
        return {}
    return {
        str(k): v for k, v in scanned.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }


def save_watched(base_dir, scanned, retention_days, now=None):
    """Write the scan marks, dropping those older than retention."""
    if base_dir is None:
        return
    floor = (time.time() if now is None else now) - retention_days * 86400
    data = {"scanned": {k: v for k, v in scanned.items() if v >= floor}}
    if data["scanned"] == load_watched(base_dir):
        return
    try:
        state.write_atomic(
            Path(base_dir) / "state" / WATCHED_FILE,
            json.dumps(data, sort_keys=True), mode=0o600,
        )
    except OSError:
        log("comms inbox: watched-channel state could not be written")


def is_watched_post(hit, watch_channels, include_bots):
    """Whether `hit` is a top-level message of a watched channel."""
    return (
        hit["channel"] in watch_channels and hit["root"] == hit["ts"]
        and (include_bots or not hit["is_bot"])
    )


def load_bot_tries(base_dir):
    """{"<channel>:<ts>": reads that still found no text}."""
    if base_dir is None:
        return {}
    data = _json_state(Path(base_dir) / "state" / BOT_TEXT_FILE)
    return {
        str(k): v for k, v in data.items()
        if isinstance(v, int) and not isinstance(v, bool)
    }


def save_bot_tries(base_dir, tries, retention_days, now=None):
    """Write the try counts, dropping messages older than retention."""
    if base_dir is None:
        return
    floor = (time.time() if now is None else now) - retention_days * 86400
    keep = {
        k: v for k, v in tries.items() if float(k.rsplit(":", 1)[1]) >= floor
    }
    try:
        state.write_atomic(
            Path(base_dir) / "state" / BOT_TEXT_FILE,
            json.dumps(keep, sort_keys=True), mode=0o600,
        )
    except OSError:
        log("comms inbox: bot text state could not be written")


def empty_bot_messages(base_dir, users, extra_rows=()):
    """{DM channel: [ts]} of messages by `users` stored with no text.

    `extra_rows` (rows about to be recorded) count like stored ones. Only DM
    channels (`D...`) are listed, and a message already tried
    MAX_BOT_TEXT_TRIES times is left out.
    """
    if base_dir is None or not users:
        return {}
    tries = load_bot_tries(base_dir)
    out = {}
    for row in _load(path(base_dir)) + list(extra_rows):
        if (
            row.get("type", "message") != "message"
            or row.get("user_id") not in users or row.get("text")
            or not str(row.get("channel_id", "")).startswith("D")
            or tries.get(f"{row['channel_id']}:{row['ts']}", 0)
            >= MAX_BOT_TEXT_TRIES
        ):
            continue
        stamps = out.setdefault(row["channel_id"], [])
        if row["ts"] not in stamps:
            stamps.append(row["ts"])
    return out
