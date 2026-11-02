"""Watch suggestions for the channels the owner joins.

With `suggest_watch: <watch file>` in a `comms_inbox` block that also sets
`joined_channels: true`, each newly joined channel that watch file does not
already list for good (its hand-written `channels` and `ignore_channels`,
and the joined file's own lists) becomes a suggestion in the file the watch file names with
`joined_from`, and its `channel_joined` line carries `"watch_suggested":
true`. Nothing is watched until the owner approves:

    daemon.py joined --routine <id>                    # list suggestions
    daemon.py joined --routine <id> add <channel>      # watch it
    daemon.py joined --routine <id> dismiss <channel>  # never suggest again

`<id>` is the routine whose watch file has `joined_from`. An approved channel
moves to the joined file's `channels`, which the watch file merges at load
like discovered channels; a dismissed one is never suggested again and is
kept out of the watch even when discovery finds it. A channel in
`ignore_channels` cannot be approved. A discovered channel is still
suggested: discovery drops idle channels, and approving one keeps it. A
suggestion is made once; a join whose
inbox line was not written yet is flagged again when it is retried. A
failure to read or write the joined file is logged and never stops the run;
the channel_joined line is recorded either way.

Every read-modify-write of the joined file (the daemon's suggestions, the
CLI's approvals) holds an exclusive lock on `<joined file>.lock`, so neither
overwrites the other's change.

The joined file is YAML written by the daemon, never by hand:

    suggested: [{id: C0EXAMPLE07, name: example-name, joined: 2026-11-02}]
    channels:  [{id: C0EXAMPLE08, name: other-name, approved: 2026-11-02}]
    dismissed: [{id: C0EXAMPLE09, name: old-name}]
"""
import contextlib
import datetime
import fcntl
import os

from . import slack_watch
from .shell import log


class JoinedError(ValueError):
    pass


def joined_path(watch_path):
    """The `joined_from` file of a watch file; raises JoinedError if none."""
    parsed = slack_watch._parse(watch_path)
    if not parsed["joined_from"]:
        raise JoinedError(f"watch file {watch_path} has no `joined_from`")
    return slack_watch._private_path(parsed["joined_from"], "joined file")


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(f"{path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing also drops the flock


def _name(value):
    return value if isinstance(value, str) \
        and slack_watch.JOINED_NAME.match(value) else None


def suggest(watch_path, rows, today=None):
    """Add uncovered joined channels of `rows` as suggestions; flag the rows.

    A row whose channel is already suggested (a join retried because its
    inbox line was not written) is flagged again. Returns the number of new
    suggestions. Never raises: any problem is logged and leaves the rows
    unflagged.
    """
    today = today or datetime.date.today()
    try:
        path = joined_path(watch_path)
        hand, ignore = slack_watch.hand_written(watch_path)
        with _locked(path):
            lists = slack_watch.read_joined(path)
            # Only lasting coverage counts: discovery drops idle channels.
            covered = set(hand) | set(ignore)
            # A suggestion the watch has since come to cover is settled.
            stale = [e for e in lists["suggested"] if e["id"] in covered]
            for entry in stale:
                lists["suggested"].remove(entry)
            pending = {e["id"] for e in lists["suggested"]}
            known = covered | {
                e["id"] for key in slack_watch.JOINED_LISTS
                for e in lists[key]
            }
            flag, fresh = [], []
            for row in rows:
                cid = row.get("channel_id")
                if row.get("type") != "channel_joined":
                    continue
                if cid in pending:
                    flag.append(row)
                    continue
                if cid in known:
                    continue
                entry = {"id": cid, "joined": today}
                if _name(row.get("name")):
                    entry["name"] = row["name"]
                lists["suggested"].append(entry)
                fresh.append(row)
                flag.append(row)
                known.add(cid)
            if fresh or stale:
                slack_watch.write_joined(path, lists)
    except (JoinedError, slack_watch.WatchError, OSError) as exc:
        log(f"comms inbox: watch suggestions skipped: {exc}")
        return 0
    for row in flag:
        row["watch_suggested"] = True
    if fresh:
        log(f"comms inbox: {len(fresh)} joined channel(s) suggested for "
            "watching")
    return len(fresh)


_WORD = {"channels": "watched", "dismissed": "dismissed"}


def _move(watch_path, channel, target, today):
    path = joined_path(watch_path)
    if target == "channels" and channel in slack_watch.hand_written(
        watch_path
    )[1]:
        raise JoinedError(
            f"{channel} is in the watch file's ignore_channels; "
            "remove it there to watch it"
        )
    with _locked(path):
        lists = slack_watch.read_joined(path)
        for key in slack_watch.JOINED_LISTS:
            for entry in lists[key]:
                if entry["id"] != channel:
                    continue
                if key == target:
                    return f"{channel} is already {_WORD[target]}"
                lists[key].remove(entry)
                moved = {k: v for k, v in entry.items() if k in ("id", "name")}
                if slack_watch.JOINED_LISTS[target]:
                    moved[slack_watch.JOINED_LISTS[target]] = today
                lists[target].append(moved)
                slack_watch.write_joined(path, lists)
                return f"{channel} {_WORD[target]}"
    raise JoinedError(f"{channel} is not in {path.name}")


def approve(watch_path, channel, today=None):
    """Watch a suggested (or dismissed) channel; returns a message."""
    return _move(watch_path, channel, "channels",
                 today or datetime.date.today())


def dismiss(watch_path, channel, today=None):
    """Never suggest `channel` again and keep it out of the watch."""
    return _move(watch_path, channel, "dismissed",
                 today or datetime.date.today())


def listing(watch_path):
    """Lines describing the joined file for `daemon.py joined`."""
    path = joined_path(watch_path)
    lists = slack_watch.read_joined(path)
    lines = []
    for key, title in (("suggested", "suggested"), ("channels", "watched"),
                       ("dismissed", "dismissed")):
        lines.append(f"{title} ({len(lists[key])}):")
        for entry in lists[key]:
            when = entry.get("joined") or entry.get("approved")
            lines.append(
                f"  {entry['id']}  #{entry.get('name') or '?'}"
                + (f"  {when.isoformat()}" if when else "")
            )
    return lines
