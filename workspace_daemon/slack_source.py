"""Slack source: list recent threads in watched channels (plus @mentions) via
the local `slack-cli` tool, then fetch full threads on demand.

Routine config:

    source:
      kind: slack
      channels:                 # channel IDs to sweep (names allowed in comments)
        - C0123ABCD
      include_mentions: true    # also sweep workspace-wide @mentions
      hours: 26                 # lookback window (overlap a 24h schedule slightly)
      max_results: 30           # cap per channel

`slack-cli` (a separate small tool) owns the token and the Slack Web API calls;
mentions are delegated by it to the `ada` CLI. Every candidate is identified by
the canonical thread anchor `slack:<channel>:<thread_ts>`, which doubles as the
memory-store source id, so re-sweeps of an active thread update one entry in
place instead of duplicating.

Slack has no equivalent of Gmail triage: `actions` are meaningless here and the
config layer rejects them for this source kind.
"""
import datetime
import json
import math
import os
import subprocess

from .shell import log

SLACK_CLI = os.environ.get("SLACK_CLI", os.path.expanduser("~/bin/slack-cli"))


def _cli(args, timeout=60):
    r = subprocess.run([SLACK_CLI, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"slack-cli {args[0]} failed: {(r.stderr or r.stdout).strip()[:200]}")
    d = json.loads(r.stdout)
    if not d.get("ok"):
        raise RuntimeError(f"slack-cli {args[0]}: {d.get('error')}")
    return d


def candidates(source):
    """One candidate per thread anchor across watched channels and mentions."""
    hours = str(source.get("hours", 26))
    per_channel = int(source.get("max_results", 30))
    seen = {}

    for channel in source.get("channels", []):
        d = _cli(["history", channel, "--hours", hours, "--limit", str(per_channel)])
        for m in d.get("messages", []):
            sid = m["source_id"]
            if sid not in seen:
                text = (m.get("text") or "").replace("\n", " ")
                seen[sid] = {"id": sid, "title": text[:90], "raw": {
                    "channel": channel, "anchor": sid.split(":")[-1]}}

    if source.get("include_mentions"):
        d = _cli(["mentions", "--days", str(max(1, math.ceil(int(source.get("hours", 26)) / 24)))])
        for m in d.get("mentions", []):
            sid = m.get("source_id")
            if sid and sid not in seen:
                seen[sid] = {"id": sid, "title": (m.get("text") or "")[:90], "raw": {
                    "channel": m["channel_id"], "anchor": sid.split(":")[-1],
                    "via_mention": True}}

    return list(seen.values())


def fetch(routine, candidate):
    """Expand the thread and render a readable conversation body."""
    channel = candidate["raw"]["channel"]
    anchor = candidate["raw"]["anchor"]
    d = _cli(["replies", channel, anchor])
    msgs = d.get("messages", [])
    if not msgs:
        raise RuntimeError("empty thread")

    # Resolve senders once per thread; unknown ids stay as raw ids.
    user_ids = sorted({m["user"] for m in msgs if m.get("user", "").startswith("U")})
    names = {}
    if user_ids:
        try:
            names = {uid: (u.get("real_name") or uid)
                     for uid, u in _cli(["whois", *user_ids])["users"].items()}
        except Exception as exc:  # name resolution is cosmetic, never fatal
            log(f"slack whois failed ({exc}); keeping raw user ids")

    lines = [f"{names.get(m.get('user'), m.get('user', '?'))}: {m.get('text', '')}"
             for m in msgs]
    root_ts = float(anchor)
    date = datetime.datetime.fromtimestamp(root_ts, datetime.timezone.utc).date().isoformat()

    title = candidate["title"] or f"slack thread in {channel}"
    return {
        "id": candidate["id"],  # slack:<channel>:<thread_ts> — already canonical
        "title": title,
        "date": date,
        "body": "\n".join(lines),
        "frontmatter": {
            "slack_channel": channel,
            "slack_thread_ts": anchor,
            "slack_participants": sorted(set(names.values())) or user_ids,
            "via_mention": bool(candidate["raw"].get("via_mention")),
        },
    }
