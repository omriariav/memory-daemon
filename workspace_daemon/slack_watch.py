"""Slack watch file: the private list of what a Slack source should cover.

A routine points at the file with `watch: <absolute or ~ path>`; the ids stay
out of the routine and out of the repository. The loader returns exactly the
keys the `slack` source already understands (plus `people` and `bootstrap` for
the connector-backed source), so switching `kind:` later keeps the same
candidate ids.

    channels:                 # channels, private channels and DMs
      - C0EXAMPLE01
      - {id: C0EXAMPLE02, name: example-team}   # names are local labels only
    people:                   # authors whose messages are watched anywhere
      - U0EXAMPLEA
    include_mentions: true
    all_dms: true             # slack_mcp only: every DM and group DM of the owner
    save_reactions:           # slack_mcp only: emoji names that mean "save this"
      - save-for-later-example
    owner_action_channels:
      - C0EXAMPLE01
    bootstrap: 3d             # first-run look-back when no cursor exists
    discover_from: ~/path/slack-discovered.yaml   # channels found by discovery
    ignore_channels: [C0EXAMPLE09]                # never added by discovery

`discover_from` names a separate private file kept by the `slack_discovery`
maintenance routine; its channels are appended to the hand-written `channels`
at load (a hand-written entry always wins, an `ignore_channels` entry is never
merged in). A missing discovered file adds nothing, and a corrupt one is
treated as empty with a warning (the discovery routine rebuilds it).

The discovered file is YAML written by the daemon, never by hand:

    channels:
      - {id: C0EXAMPLE05, name: example-name, last_active: 2026-10-20,
         reason: posted}
"""
import datetime
import os
import re
from pathlib import Path

import yaml

from . import state
from .shell import log

WATCH_KEYS = {
    "channels", "people", "include_mentions", "owner_action_channels",
    "bootstrap", "all_dms", "discover_from", "ignore_channels",
    "save_reactions",
}
# Emoji names (without colons) the owner reacts with to save a message.
SAVE_REACTION = re.compile(r"^[a-z0-9][a-z0-9_+'-]{0,99}$")
MAX_SAVE_REACTIONS = 3
DISCOVERED_ID = re.compile(r"^[CG][A-Z0-9]{8,}$")
DISCOVERED_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
DISCOVERED_REASONS = ("posted", "mentioned")
CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{8,}$")
USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
_BOOTSTRAP = re.compile(r"^[1-9]\d*[mhd]$")


_WARNED = set()  # warn once per process, not on every tick


class WatchError(ValueError):
    pass


def _ids(data, key, pattern, label):
    values = data.get(key)
    if values is None:
        return []
    if not isinstance(values, list):
        raise WatchError(f"`{key}` must be a list")
    out = []
    for entry in values:
        if isinstance(entry, dict):
            if set(entry) - {"id", "name"} or "id" not in entry:
                raise WatchError(
                    f"`{key}` mapping entries need `id` and optional `name`"
                )
            name = entry.get("name")
            if name is not None and not isinstance(name, str):
                raise WatchError(f"`{key}` entry names must be strings")
            entry = entry["id"]
        if not isinstance(entry, str) or not pattern.match(entry):
            raise WatchError(f"`{key}` has an entry that is not a {label} id")
        if entry not in out:
            out.append(entry)
    return out


def _private_path(value, label):
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise WatchError(f"{label} path must be absolute or start with ~")
    return path


def _read_yaml(path, label):
    """Parsed YAML of a private file (None when empty); warns on loose modes."""
    try:
        if os.stat(path).st_mode & 0o077 and str(path) not in _WARNED:
            _WARNED.add(str(path))
            log(f"slack {label} is readable by group or others; "
                "run chmod 600 on it")
        with open(path) as handle:
            return yaml.safe_load(handle)
    except OSError as exc:
        raise WatchError(f"cannot read {label} ({exc.strerror})") from exc
    except yaml.YAMLError as exc:
        raise WatchError(f"{label} is not valid YAML") from exc


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return None
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.date.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.isoformat() == value else None
    return None


def read_discovered(path):
    """Entries of a discovered-channels file, [] when it does not exist yet.

    Each entry is {"id", "last_active" (date), "reason", "name" (optional)}.
    A malformed file raises WatchError: it is never half-trusted.
    """
    path = _private_path(path, "discovered file")
    if not path.exists():
        return []
    data = _read_yaml(path, "discovered file")
    if data is None:
        return []
    if not isinstance(data, dict) or set(data) - {"channels"}:
        raise WatchError("discovered file must be a mapping with `channels`")
    rows = data.get("channels") or []
    if not isinstance(rows, list):
        raise WatchError("discovered `channels` must be a list")
    out, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) - {
            "id", "name", "last_active", "reason"
        }:
            raise WatchError("discovered entries need id, last_active, reason")
        cid = row.get("id")
        active = _as_date(row.get("last_active"))
        if not isinstance(cid, str) or not DISCOVERED_ID.match(cid):
            raise WatchError("discovered entry has an id that is not a channel")
        if active is None or row.get("reason") not in DISCOVERED_REASONS:
            raise WatchError("discovered entry has a bad last_active or reason")
        name = row.get("name")
        if name is not None and not (
            isinstance(name, str) and DISCOVERED_NAME.match(name)
        ):
            raise WatchError("discovered entry has a bad name")
        if cid in seen:
            raise WatchError("discovered file lists a channel twice")
        seen.add(cid)
        entry = {"id": cid, "last_active": active, "reason": row["reason"]}
        if name:
            entry["name"] = name
        out.append(entry)
    return out


def quarantine_discovered(path, stamp):
    """Move a corrupt discovered file aside; returns the new file name."""
    path = _private_path(path, "discovered file")
    aside = path.with_name(f"{path.name}.corrupt-{stamp}")
    os.replace(path, aside)
    return aside.name


def write_discovered(path, entries):
    """Atomically write the discovered file (mode 600), sorted by id."""
    path = _private_path(path, "discovered file")
    rows = [dict(sorted(entry.items())) for entry in
            sorted(entries, key=lambda e: e["id"])]
    text = (
        "# Written by the slack_discovery maintenance routine; do not edit.\n"
        + yaml.safe_dump({"channels": rows}, sort_keys=False)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    state.write_atomic(path, text, mode=0o600)


def _parse(path):
    """Validated watch-file data (before discovery is merged)."""
    path = _private_path(path, "watch")
    data = _read_yaml(path, "watch file")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise WatchError("watch file top level must be a mapping")
    unknown = set(data) - WATCH_KEYS
    if unknown:
        raise WatchError(
            f"watch file has unknown key(s): {', '.join(sorted(map(str, unknown)))}"
        )
    mentions = data.get("include_mentions", False)
    if not isinstance(mentions, bool):
        raise WatchError("`include_mentions` must be true or false")
    all_dms = data.get("all_dms", False)
    if not isinstance(all_dms, bool):
        raise WatchError("`all_dms` must be true or false")
    reactions = data.get("save_reactions")
    if reactions is None:
        reactions = []
    if (
        not isinstance(reactions, list)
        or len(reactions) > MAX_SAVE_REACTIONS
        or not all(
            isinstance(name, str) and SAVE_REACTION.match(name)
            for name in reactions
        )
        or len(set(reactions)) != len(reactions)
    ):
        raise WatchError(
            f"`save_reactions` must be a list of at most {MAX_SAVE_REACTIONS} "
            "distinct emoji names (letters, digits, _ + ' -; no colons)"
        )
    bootstrap = data.get("bootstrap", "3d")
    if not isinstance(bootstrap, str) or not _BOOTSTRAP.match(bootstrap):
        raise WatchError("`bootstrap` must look like '12h' or '3d'")
    hand = _ids(data, "channels", CHANNEL_ID, "channel")
    ignore = _ids(data, "ignore_channels", CHANNEL_ID, "channel")
    discover_from = data.get("discover_from")
    if discover_from is not None and not (
        isinstance(discover_from, str) and discover_from
    ):
        raise WatchError("`discover_from` must be a path string")
    return {
        "channels": hand,
        "ignore_channels": ignore,
        "discover_from": discover_from,
        "people": _ids(data, "people", USER_ID, "user"),
        "include_mentions": mentions,
        "all_dms": all_dms,
        "save_reactions": reactions,
        "owner_action_channels": _ids(
            data, "owner_action_channels", CHANNEL_ID, "channel"
        ),
        "bootstrap": bootstrap,
    }


def hand_written(path):
    """(channels, ignore_channels) typed by hand in a watch file."""
    parsed = _parse(path)
    return parsed["channels"], parsed["ignore_channels"]


def load(path):
    """Parse a watch file into source keys; raises WatchError with the reason.

    Channels of the file named by `discover_from` follow the hand-written
    ones, minus anything already listed and anything in `ignore_channels`.
    """
    parsed = _parse(path)
    ignore = set(parsed.pop("ignore_channels"))
    discover_from = parsed.pop("discover_from")
    if discover_from:
        path = _private_path(discover_from, "discovered file")
        try:
            found = read_discovered(path)
        except WatchError as exc:
            if f"bad:{path}" not in _WARNED:
                _WARNED.add(f"bad:{path}")
                log(f"slack discovered file ignored: {exc}")
            found = []
        known = set(parsed["channels"]) | ignore
        parsed["channels"] = parsed["channels"] + sorted(
            entry["id"] for entry in found if entry["id"] not in known
        )
        parsed["discover_from"] = discover_from
    return parsed


def expand(source):
    """Return `source` with its watch file applied, or (source, error).

    Inline watch keys next to `watch:` are an error: one place owns the list.
    Empty lists are omitted so the result matches a hand-written block.
    """
    ref = source.get("watch")
    if ref is None:
        return source, None
    if not isinstance(ref, str) or not ref:
        return source, "watch must be a path string"
    clash = sorted(
        key for key in WATCH_KEYS - {"bootstrap"} if key in source
    )
    if clash:
        return source, (
            f"watch cannot be combined with inline {', '.join(clash)}"
        )
    try:
        loaded = load(ref)
    except WatchError as exc:
        return source, str(exc)
    merged = dict(source)
    for key, value in loaded.items():
        if key == "bootstrap":
            # Only the connector-backed source has a look-back of its own;
            # an inline value wins over the file's.
            if source.get("kind") == "slack_mcp" and key not in source:
                merged[key] = value
        elif key == "discover_from":
            # Kept only so validation can cross-check the discovery routine.
            merged["_watch_discover_from"] = str(Path(value).expanduser())
        elif key == "all_dms":
            # Only the connector-backed source can search every DM.
            if value and source.get("kind") == "slack_mcp":
                merged[key] = value
        elif key == "save_reactions":
            # Only the connector-backed source can search by reaction.
            if value and source.get("kind") == "slack_mcp":
                merged[key] = list(value)
        elif value not in ([], False):
            merged[key] = value
    return merged, None
