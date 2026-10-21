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
    owner_action_channels:
      - C0EXAMPLE01
    bootstrap: 3d             # first-run look-back when no cursor exists
"""
import os
import re
from pathlib import Path

import yaml

from .shell import log

WATCH_KEYS = {
    "channels", "people", "include_mentions", "owner_action_channels",
    "bootstrap",
}
CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{8,}$")
USER_ID = re.compile(r"^[UW][A-Z0-9]{8,}$")
_BOOTSTRAP = re.compile(r"^[1-9]\d*[mhd]$")


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


def load(path):
    """Parse a watch file into source keys; raises WatchError with the reason."""
    path = Path(str(path)).expanduser()
    if not path.is_absolute():
        raise WatchError("watch path must be absolute or start with ~")
    try:
        if os.stat(path).st_mode & 0o077:
            log("slack watch file is readable by group or others; "
                "run chmod 600 on it")
        with open(path) as handle:
            data = yaml.safe_load(handle)
    except OSError as exc:
        raise WatchError(f"cannot read watch file ({exc.strerror})") from exc
    except yaml.YAMLError as exc:
        raise WatchError("watch file is not valid YAML") from exc
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
    bootstrap = data.get("bootstrap", "3d")
    if not isinstance(bootstrap, str) or not _BOOTSTRAP.match(bootstrap):
        raise WatchError("`bootstrap` must look like '12h' or '3d'")
    return {
        "channels": _ids(data, "channels", CHANNEL_ID, "channel"),
        "people": _ids(data, "people", USER_ID, "user"),
        "include_mentions": mentions,
        "owner_action_channels": _ids(
            data, "owner_action_channels", CHANNEL_ID, "channel"
        ),
        "bootstrap": bootstrap,
    }


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
        elif value not in ([], False):
            merged[key] = value
    return merged, None
