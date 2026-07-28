"""Slack source with public-channel digests, private batching, and mentions.

Routine config:

    source:
      kind: slack
      ada_channels:             # public channels summarized by Ada
        - C0123PUBLIC
      private_channels:         # direct history, batched by channel/day
        - C0456PRIVATE
      channels:                 # legacy per-thread mode
        - C0789LEGACY
      include_mentions: true
      hours: 26
      ada_days: 2               # Ada accepts whole days, 1..90
      max_results: 1000         # direct-message cap per channel

Ada returns a curated public-channel payload. Private channels are read with
the user token and grouped into one candidate per UTC day, so a conversation
made of unthreaded sentences reaches Yoetz as one coherent input. Mentions
outside configured channels remain canonical thread candidates.
"""
import datetime
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from .chat_text import redact_secrets, slack_timestamp_iso, timestamped_line
from .shell import ada_bin, log, utc_now_iso


SLACK_CLI = os.environ.get("SLACK_CLI")
REPO_DIR = Path(__file__).resolve().parents[1]


def _cli(args, timeout=60):
    command = (
        [SLACK_CLI, *args]
        if SLACK_CLI
        else [sys.executable, "-m", "workspace_daemon.slack_cli", *args]
    )
    result = subprocess.run(
        command,
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"slack-cli {args[0]} failed: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
    data = json.loads(result.stdout)
    if not data.get("ok"):
        raise RuntimeError(f"slack-cli {args[0]}: {data.get('error')}")
    return data


def _ada_summary(channel, days):
    result = subprocess.run(
        [
            ada_bin(), "slack", "channel-summary", channel,
            "--days", str(days), "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ada channel-summary failed for {channel}: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"ada channel-summary returned non-JSON for {channel}: "
            f"{result.stdout.strip()[:200]}"
        ) from exc
    if not data.get("success"):
        raise RuntimeError(
            f"ada channel-summary failed for {channel}: "
            f"{data.get('error') or data.get('message') or 'unknown error'}"
        )
    return data


def _message_day(ts):
    try:
        value = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return "date-unknown"
    return value.date().isoformat()


def configured_channels(source):
    """Every channel explicitly assigned to this source block."""
    return {
        channel
        for key in ("channels", "ada_channels", "private_channels")
        for channel in source.get(key, [])
    }


def _mention_excluded_channels(source):
    """Channels covered by a declared ingestion path anywhere in the run."""
    return configured_channels(source) | set(
        source.get("_exclude_mention_channels", [])
    )


def _legacy_candidates(source):
    """Backwards-compatible one-candidate-per-thread channel ingestion."""
    hours = str(source.get("hours", 26))
    per_channel = int(source.get("max_results", 30))
    seen = {}
    latest = {}
    for channel in source.get("channels", []):
        data = _cli(
            ["history", channel, "--hours", hours, "--limit", str(per_channel)]
        )
        for message in data.get("messages", []):
            sid = message["source_id"]
            latest[sid] = max(latest.get(sid, ""), message.get("ts") or "")
            if sid not in seen:
                text = redact_secrets(
                    (message.get("text") or "").replace("\n", " ")
                )
                seen[sid] = {
                    "title": text[:90],
                    "raw": {
                        "channel": channel,
                        "anchor": sid.split(":")[-1],
                        "source_id": sid,
                        "mode": "thread",
                    },
                }
    return seen, latest


def _private_digest_candidates(source):
    """One direct-reader candidate per private channel and UTC activity day."""
    hours = str(source.get("hours", 26))
    per_channel = int(source.get("max_results", 30))
    out = []
    for channel in source.get("private_channels", []):
        data = _cli(
            ["history", channel, "--hours", hours, "--limit", str(per_channel)]
        )
        messages = data.get("messages", [])
        if len(messages) >= per_channel:
            log(
                f"slack private WARN channel={channel}: history reached "
                f"max_results={per_channel}; older activity may be omitted"
            )
        by_day = {}
        for message in messages:
            day = _message_day(message.get("ts"))
            by_day.setdefault(day, []).append(message)
        for day, day_messages in by_day.items():
            day_messages.sort(key=lambda message: message.get("ts") or "")
            sid = f"slack:{channel}:digest:{day}"
            latest = day_messages[-1].get("ts") or ""
            first_text = next(
                (
                    redact_secrets(
                        (message.get("text") or "").replace("\n", " ")
                    )
                    for message in day_messages
                    if (message.get("text") or "").strip()
                ),
                "",
            )
            out.append({
                "id": f"{sid}@{latest}",
                "title": first_text[:90] or f"Private Slack digest for {day}",
                "raw": {
                    "channel": channel,
                    "source_id": sid,
                    "mode": "private_digest",
                    "digest_day": day,
                    "messages": day_messages,
                },
            })
    return out


def _ada_digest_candidates(source):
    """One Ada-curated candidate per public channel and capture date."""
    days = int(source.get(
        "ada_days",
        max(1, min(90, math.ceil(float(source.get("hours", 26)) / 24))),
    ))
    capture_day = utc_now_iso()[:10]
    out = []
    for channel in source.get("ada_channels", []):
        summary = _ada_summary(channel, days)
        count = int(summary.get("message_count") or 0)
        if count == 0:
            log(
                f"slack ada WARN channel={channel}: zero messages returned; "
                "verify the channel is public and visible to Ada"
            )
            continue
        timestamps = [
            row.get("timestamp")
            for key in ("key_threads", "top_messages")
            for row in (summary.get(key) or [])
            if row.get("timestamp")
        ]
        latest = max(timestamps, key=float) if timestamps else str(count)
        if count >= 100:
            log(
                f"slack ada WARN channel={channel}: summary reached the "
                "100-message service cap; older activity may be omitted"
            )
        sid = f"slack:{channel}:digest:{capture_day}"
        name = summary.get("channel_name") or channel
        out.append({
            "id": f"{sid}@{latest}",
            "title": (
                f"Slack catch-up: #{name} "
                f"({summary.get('time_period', 'unknown period')})"
            ),
            "raw": {
                "channel": channel,
                "source_id": sid,
                "mode": "ada_digest",
                "capture_day": capture_day,
                "summary": summary,
            },
        })
    return out


def candidates(source):
    """List digest candidates plus legacy threads and out-of-channel mentions."""
    out = _ada_digest_candidates(source)
    out.extend(_private_digest_candidates(source))
    seen, latest = _legacy_candidates(source)

    if source.get("include_mentions"):
        days = max(1, math.ceil(int(source.get("hours", 26)) / 24))
        data = _cli(["mentions", "--days", str(days)])
        for message in data.get("mentions", []):
            sid = message.get("source_id")
            if not sid:
                continue
            # A configured channel digest already contains this mention. The
            # runner supplies channels owned by other routines too, because a
            # digest and a thread have different source ids and cannot be
            # deduplicated later by claim routing.
            if message.get("channel_id") in _mention_excluded_channels(source):
                continue
            anchor = sid.split(":")[-1]
            latest[sid] = max(
                latest.get(sid, ""), message.get("ts") or anchor
            )
            if sid not in seen:
                seen[sid] = {
                    "title": redact_secrets(message.get("text") or "")[:90],
                    "raw": {
                        "channel": message["channel_id"],
                        "anchor": anchor,
                        "source_id": sid,
                        "via_mention": True,
                        "mode": "thread",
                    },
                }

    # Thread candidate IDs change when replies arrive. Their source IDs stay
    # stable, so the ledger reprocesses and the memory store updates one entry.
    out.extend(
        {"id": f"{sid}@{latest.get(sid, '')}", **candidate}
        for sid, candidate in seen.items()
    )
    return out


def _resolve_names(messages):
    user_ids = sorted({
        message["user"]
        for message in messages
        if message.get("user", "").startswith("U")
    })
    names = {}
    if user_ids:
        try:
            names = {
                uid: (user.get("real_name") or uid)
                for uid, user in _cli(["whois", *user_ids])["users"].items()
            }
        except Exception as exc:
            log(f"slack whois failed ({exc}); keeping raw user ids")
    return names, user_ids


def _fetch_ada_digest(candidate):
    raw = candidate["raw"]
    summary = raw["summary"]
    rows = []
    seen = set()
    for section, text_key in (
        ("key_threads", "text_preview"),
        ("top_messages", "text"),
    ):
        for row in summary.get(section) or []:
            key = (
                row.get("permalink"),
                row.get("timestamp"),
                row.get(text_key),
            )
            if key in seen:
                continue
            seen.add(key)
            text = redact_secrets(row.get(text_key) or "")
            suffix = []
            if row.get("reply_count"):
                suffix.append(f"{row['reply_count']} replies")
            if row.get("permalink"):
                suffix.append(row["permalink"])
            if suffix:
                text = f"{text} ({'; '.join(suffix)})"
            rows.append((
                row.get("timestamp"),
                row.get("user") or "unknown",
                text,
            ))
    rows.sort(key=lambda row: float(row[0] or 0))
    lines = [
        f"Channel: #{summary.get('channel_name') or raw['channel']}",
        f"Period: {summary.get('time_period') or 'unknown'}",
        f"Messages considered by Ada: {summary.get('message_count') or 0}",
    ]
    if int(summary.get("message_count") or 0) >= 100:
        lines.append(
            "Coverage warning: Ada reached its 100-message cap; "
            "older activity may be omitted."
        )
    lines.append("")
    lines.extend(
        timestamped_line(slack_timestamp_iso(ts), speaker, text)
        for ts, speaker, text in rows
    )
    links = summary.get("important_links") or []
    if links:
        lines.extend(["", "Important links:", *[f"- {link}" for link in links]])
    timestamps = [row[0] for row in rows if row[0]]
    first = min(timestamps, key=float) if timestamps else ""
    latest = max(timestamps, key=float) if timestamps else ""
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],
        "title": candidate["title"],
        "date": _message_day(latest) if latest else raw["capture_day"],
        "body": "\n".join(lines),
        "frontmatter": {
            "slack_channel": raw["channel"],
            "slack_channel_name": summary.get("channel_name", ""),
            "slack_capture_mode": "ada-channel-summary",
            "slack_summary_period": summary.get("time_period", ""),
            "message_count": int(summary.get("message_count") or 0),
            "message_limit_reached": (
                int(summary.get("message_count") or 0) >= 100
            ),
            "first_message_at": slack_timestamp_iso(first),
            "latest_message_at": slack_timestamp_iso(latest),
        },
    }


def _fetch_private_digest(candidate):
    raw = candidate["raw"]
    channel = raw["channel"]
    messages = []
    for root in raw["messages"]:
        if int(root.get("reply_count") or 0) > 0:
            messages.extend(
                _cli(["replies", channel, root["ts"]]).get("messages", [])
            )
        else:
            messages.append(root)
    by_ts = {
        message.get("ts"): message
        for message in messages
        if message.get("ts") and (message.get("text") or "").strip()
    }
    messages = sorted(
        by_ts.values(), key=lambda message: float(message["ts"])
    )
    if not messages:
        raise RuntimeError("private channel digest has no text content")
    names, user_ids = _resolve_names(messages)
    lines = [
        timestamped_line(
            slack_timestamp_iso(message.get("ts")),
            names.get(message.get("user"), message.get("user", "?")),
            message.get("text", ""),
        )
        for message in messages
    ]
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],
        "title": candidate["title"],
        "date": raw["digest_day"],
        "body": "\n".join(lines),
        "frontmatter": {
            "slack_channel": channel,
            "slack_capture_mode": "private-daily-digest",
            "slack_participants": sorted(set(names.values())) or user_ids,
            "message_count": len(messages),
            "first_message_at": slack_timestamp_iso(messages[0].get("ts")),
            "latest_message_at": slack_timestamp_iso(messages[-1].get("ts")),
            "digest_day": raw["digest_day"],
            "via_mention": False,
        },
    }


def fetch(routine, candidate):
    """Render an Ada digest, a private daily digest, or a legacy thread."""
    mode = candidate.get("raw", {}).get("mode")
    if mode == "ada_digest":
        return _fetch_ada_digest(candidate)
    if mode == "private_digest":
        return _fetch_private_digest(candidate)

    channel = candidate["raw"]["channel"]
    anchor = candidate["raw"]["anchor"]
    data = _cli(["replies", channel, anchor])
    messages = data.get("messages", [])
    if not messages:
        raise RuntimeError("empty thread")

    names, user_ids = _resolve_names(messages)
    lines = [
        timestamped_line(
            slack_timestamp_iso(message.get("ts")),
            names.get(message.get("user"), message.get("user", "?")),
            message.get("text", ""),
        )
        for message in messages
    ]
    root_ts = float(anchor)
    date = datetime.datetime.fromtimestamp(
        root_ts, datetime.timezone.utc
    ).date().isoformat()

    title = candidate["title"] or f"slack thread in {channel}"
    return {
        "id": candidate["id"],
        "source_id": candidate["raw"]["source_id"],
        "title": title,
        "date": date,
        "body": "\n".join(lines),
        "frontmatter": {
            "slack_channel": channel,
            "slack_thread_ts": anchor,
            "slack_capture_mode": "thread",
            "slack_participants": sorted(set(names.values())) or user_ids,
            "via_mention": bool(candidate["raw"].get("via_mention")),
            "first_message_at": slack_timestamp_iso(messages[0].get("ts")),
            "latest_message_at": slack_timestamp_iso(messages[-1].get("ts")),
        },
    }
