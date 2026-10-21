"""Daily discovery of the Slack channels the owner is active in.

A maintenance routine (`maintenance.kind: slack_discovery`) asks the Slack
connector, through the same least-privilege bridge as the `slack_mcp` source,
two read-only searches over the last `active_days`: messages the owner posted
(`from:<@owner>`) and messages that mention the owner (keywords `<@owner>`).
Every public or private channel (never a DM) with a hit is recorded in a
separate private file, which a watch file merges with `discover_from`. A
channel with no such activity for `idle_days` is dropped.

    maintenance:
      kind: slack_discovery
      discovered: ~/path/slack-discovered.yaml
      watch: ~/path/slack-watch.yaml   # optional: hand-written channels and
                                       # ignore_channels are never added
      search_tool: mcp__<server>__slack_search_public_and_private
      self_user_id: U0EXAMPLEA
      active_days: 14
      idle_days: 30
      max_search_pages: 5
      max_channels: 45
      min_mentions: 2        # distinct mentions that count as activity

Searches are newest first, so a capped search still sees the most recent
activity. A run that cannot read every page it started (an unparseable page,
a missing result) changes nothing. Only hits every valid reading of a result
page agrees on count, so a message quoting a result block cannot add a
channel. Only channel ids, counts and dates are logged, never message text.
"""
import datetime

from . import claude_bridge, slack_mcp, slack_watch
from .shell import log

DEFAULTS = {
    "model": "haiku",
    "active_days": 14,
    "idle_days": 30,
    "max_search_pages": 5,
    "min_seconds_between_searches": 3,
    "max_channels": 45,
    "min_mentions": 2,
}
DAY = 86400


def settings(cfg):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    for key in ("discovered", "watch", "search_tool", "self_user_id"):
        out[key] = cfg.get(key)
    return out


def _queries(cfg):
    owner = cfg["self_user_id"]
    return [
        {"reason": "posted", "filters": f"from:<@{owner}>", "keywords": None},
        {"reason": "mentioned", "filters": None,
         "keywords": [f"<@{owner}>"]},
    ]


# Newest first (unlike the source): a page cap then keeps the latest activity.
PINS = dict(slack_mcp.PINNED_SEARCH, sort_dir="desc")


def _args(query, after, cursor=None):
    args = dict(PINS, after=after, limit=slack_mcp.SEARCH_LIMIT)
    if query["filters"]:
        args["filters"] = query["filters"]
    if query["keywords"]:
        args["keywords"] = query["keywords"]
    if cursor:
        args["cursor"] = cursor
    return args


def _channel_name(label):
    name = label[1:] if label.startswith("#") else ""
    return name if slack_watch.DISCOVERED_NAME.match(name) else None


def _usable(query, hit, cfg, after, now):
    """Whether a hit is the owner's activity in a public or private channel."""
    ts = float(hit["ts"])
    if ts < after or ts > now + slack_mcp.FUTURE_SLACK_SECONDS:
        return False
    if hit["is_bot"] or hit["label"] in slack_mcp.DM_LABELS:
        return False
    if not slack_watch.DISCOVERED_ID.match(hit["channel"]):
        return False
    owner = cfg["self_user_id"]
    if query["reason"] == "posted":
        return hit["user"] == owner
    return f"<@{owner}" in hit["text"]


def _search(cfg, now):
    """{channel id: {"ts", "reason", "name"}} of the newest owner activity.

    A channel qualifies on any post by the owner, or on `min_mentions`
    distinct messages mentioning the owner.
    """
    queries = _queries(cfg)
    after = int(now - cfg["active_days"] * DAY)
    pending = {i: _args(q, after) for i, q in enumerate(queries)}
    posted, mentioned, names = {}, {}, {}
    for _page in range(cfg["max_search_pages"]):
        order = sorted(pending)
        calls = [pending[i] for i in order]
        slack_mcp._pace(cfg)
        texts = slack_mcp._session(
            cfg, cfg["search_tool"], dict(PINS), calls, "search",
            [cfg["search_tool"]], pace=True,
        )
        following = {}
        for index, text in zip(order, texts):
            if text is None:
                raise claude_bridge.BridgeError(
                    "a planned discovery search returned no result"
                )
            query = queries[index]
            hits, cursor = slack_mcp.parse_search(text)  # raises: no changes
            trusted = getattr(hits, "trusted", None)
            for hit in (hits if trusted is None else trusted):
                if not _usable(query, hit, cfg, after, now):
                    continue
                stamp = float(hit["ts"])
                channel = hit["channel"]
                name = _channel_name(hit["label"])
                if name:
                    names.setdefault(channel, name)
                if query["reason"] == "posted":
                    posted[channel] = max(posted.get(channel, 0.0), stamp)
                else:
                    mentioned.setdefault(channel, set()).add(stamp)
            if cursor and hits:
                following[index] = _args(query, after, cursor)
        pending = following
        if not pending:
            break
    activity = {}
    for channel in set(posted) | set(mentioned):
        options = []
        if channel in posted:
            options.append((posted[channel], 1, "posted"))
        if len(mentioned.get(channel, ())) >= cfg["min_mentions"]:
            options.append((max(mentioned[channel]), 0, "mentioned"))
        if options:
            stamp, _tie, reason = max(options)
            activity[channel] = {
                "ts": stamp, "reason": reason, "name": names.get(channel),
            }
    return activity


def _day(stamp):
    return datetime.datetime.fromtimestamp(
        stamp, datetime.timezone.utc
    ).date()


def merge(existing, activity, today, cfg, hand=(), ignore=()):
    """(entries, added ids, removed ids) after applying one run's activity.

    Pure: `existing` are read entries, `activity` is {channel: {"ts",
    "reason", "name"}}. Hand-written channels and ignored ones are never
    recorded; an entry idle longer than `idle_days` is dropped, and the
    `max_channels` most recently active are kept.
    """
    hand, ignore = set(hand), set(ignore)
    entries = {e["id"]: dict(e) for e in existing}
    added = []
    for channel, found in activity.items():
        if channel in hand or channel in ignore:
            continue
        when = _day(found["ts"])
        entry = entries.get(channel)
        if entry is None:
            entry = entries[channel] = {"id": channel}
            added.append(channel)
        elif when < entry["last_active"]:
            continue
        entry["last_active"] = when
        entry["reason"] = found["reason"]
        if found.get("name"):
            entry["name"] = found["name"]
    cutoff = today - datetime.timedelta(days=cfg["idle_days"])
    removed = [
        cid for cid, e in entries.items()
        if cid in hand or cid in ignore or e["last_active"] < cutoff
    ]
    for cid in removed:
        del entries[cid]
    ranked = sorted(
        entries.values(), key=lambda e: (-e["last_active"].toordinal(), e["id"])
    )
    for entry in ranked[cfg["max_channels"]:]:
        del entries[entry["id"]]
        removed.append(entry["id"])
    before = {e["id"] for e in existing}
    return (
        sorted(entries.values(), key=lambda e: e["id"]),
        sorted(cid for cid in added if cid in entries),
        sorted(cid for cid in removed if cid in before),
    )


def run(cfg, base_dir, dry_run=False, now=None):
    """Refresh the discovered file; returns {"ok", "dry_run", "added",
    "removed", "kept"}. Raises (changing nothing) on any failed search."""
    cfg = settings(cfg)
    if dry_run:
        log("slack_discovery dry-run: would run 2 searches; no bridge call "
            "made and no file written")
        return {"ok": True, "dry_run": True, "added": [], "removed": [],
                "kept": 0}
    now = slack_mcp._now() if now is None else now
    slack_mcp.configure(base_dir)
    hand, ignore = (
        slack_watch.hand_written(cfg["watch"]) if cfg["watch"] else ([], [])
    )
    try:
        existing = slack_watch.read_discovered(cfg["discovered"])
    except slack_watch.WatchError as exc:
        # Never fail forever on a damaged file: keep it aside and rebuild.
        stamp = _day(now).strftime("%Y%m%d") + f"-{int(now) % 86400}"
        aside = slack_watch.quarantine_discovered(cfg["discovered"], stamp)
        log(f"slack_discovery: discovered file unusable ({exc}); "
            f"moved aside as {aside} and rebuilding")
        existing = []
    activity = _search(cfg, now)
    entries, added, removed = merge(
        existing, activity, _day(now), cfg, hand, ignore
    )
    if entries != sorted(existing, key=lambda e: e["id"]):
        slack_watch.write_discovered(cfg["discovered"], entries)
    return {"ok": True, "dry_run": False, "added": added,
            "removed": removed, "kept": len(entries)}
