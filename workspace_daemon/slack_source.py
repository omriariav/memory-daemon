"""Slack source with public summaries, direct channel batching, and mentions.

Routine config:

    source:
      kind: slack
      ada_channels:             # public channels summarized by Ada
        - C0123PUBLIC
      direct_channels:          # public/private history, batched by channel/day
        - C0234DIRECT
      private_channels:         # direct history, batched by channel/day
        - C0456PRIVATE
      channels:                 # legacy per-thread mode
        - C0789LEGACY
      include_mentions: true
      hours: 26
      ada_days: 2               # Ada accepts whole days, 1..90
      max_results: 1000         # direct-message cap per channel

Ada returns a curated public-channel payload. Direct and legacy private channels
are read with the user token and grouped into one candidate per UTC day, so a
conversation made of unthreaded sentences reaches Yoetz as one coherent input.
Mentions outside configured channels remain canonical thread candidates.
"""
import datetime
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from .chat_text import redact_secrets, slack_timestamp_iso, timestamped_line
from . import slack_census
from .shell import ada_bin, log, utc_now_iso


SLACK_CLI = os.environ.get("SLACK_CLI")
REPO_DIR = Path(__file__).resolve().parents[1]
# Bump when recurring candidate construction or enrichment changes in a way
# that requires replaying from catch_up_after rather than only the live overlap.
CATCH_UP_SCHEMA = 2
DEFAULT_CENSUS_TIMEOUT = 60 * 60


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
        for key in (
            "channels", "ada_channels", "direct_channels", "private_channels"
        )
        for channel in source.get(key, [])
    }


def _census_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_DIR / path


def _duration_seconds(value):
    raw = str(value)
    unit = raw[-1:] if raw else ""
    if not raw[:-1].isdigit() or unit not in {"m", "h", "d"}:
        raise RuntimeError(
            "Slack active-conversation refresh_every must look like "
            "'15m', '4h', or '1d'"
        )
    return int(raw[:-1]) * {"m": 60, "h": 3600, "d": 86400}[unit]


def _completed_epoch(checkpoint):
    completed = checkpoint.get("completed_at")
    if not completed:
        return None
    raw = completed[:-1] + "+00:00" if completed.endswith("Z") else completed
    return datetime.datetime.fromisoformat(raw).timestamp()


def _census_window_hours(checkpoint):
    """Return a completed checkpoint's fixed snapshot width, if known."""
    reported = checkpoint.get("window_hours")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        return float(reported)
    try:
        cutoff = float(checkpoint["cutoff_epoch"])
        until = float(checkpoint["until_epoch"])
    except (KeyError, TypeError, ValueError):
        return None
    return (until - cutoff) / (60 * 60)


def _active_conversation_data(source):
    """Load or refresh the fixed-window census feeding a broad Slack sweep."""
    cfg = source.get("active_conversations")
    if not cfg:
        return None
    gap = source.get("_active_conversation_gap")
    if gap:
        raise RuntimeError(str(gap))
    checkpoint_path = _census_path(cfg["checkpoint"])
    checkpoint = slack_census.load_checkpoint(checkpoint_path)
    now = _rfc3339_epoch(utc_now_iso())
    completed = _completed_epoch(checkpoint) if checkpoint else None
    snapshot_end = (
        float(checkpoint["until_epoch"])
        if checkpoint and checkpoint.get("until_epoch") is not None
        else None
    )
    requested_hours = float(cfg.get("hours", 48))
    cached_hours = _census_window_hours(checkpoint) if checkpoint else None
    window_matches = (
        cached_hours is not None
        and abs(cached_hours - requested_hours) < (1 / 3600)
    )
    refresh_seconds = _duration_seconds(cfg.get("refresh_every", "1d"))
    fresh = (
        checkpoint is not None
        and completed is not None
        and snapshot_end is not None
        and completed <= now
        and snapshot_end <= now
        and now - snapshot_end < refresh_seconds
        and window_matches
    )
    if fresh:
        data = checkpoint
        log(
            "slack census cache fresh: "
            f"active={len(data.get('active') or [])} "
            f"completed_at={data.get('completed_at')}"
        )
    else:
        args = [
            "census",
            "--hours", str(cfg.get("hours", 48)),
            "--requests-per-minute",
            str(cfg.get("requests_per_minute", 40)),
        ]
        if not source.get("_dry_run"):
            args.extend(["--checkpoint", str(checkpoint_path)])
        mode = "previewing" if source.get("_dry_run") else "refreshing"
        log(f"slack census cache stale or absent; {mode} fixed-window census")
        data = _cli(args, timeout=DEFAULT_CENSUS_TIMEOUT)
    returned_hours = _census_window_hours(data)
    if (
        returned_hours is None
        or abs(returned_hours - requested_hours) >= (1 / 3600)
    ):
        observed = "unknown" if returned_hours is None else f"{returned_hours:g}h"
        raise RuntimeError(
            "Slack census returned an incompatible window: "
            f"requested {requested_hours:g}h, received {observed}"
        )
    errors = data.get("errors") or []
    fatal = slack_census.fatal_errors(errors)
    if fatal:
        raise RuntimeError(
            "Slack census has fatal coverage errors: "
            + ", ".join(
                f"{row.get('id', '?')}={row.get('error', 'unknown')}"
                for row in fatal[:10]
            )
        )
    if errors:
        log(
            "slack census WARN ignored stale/unreadable conversations: "
            + ", ".join(
                f"{row.get('id', '?')}={row.get('error', 'unknown')}"
                for row in errors[:10]
            )
        )
    return data


def _with_active_conversations(source):
    """Materialize census-selected IDs as direct-history channels."""
    data = _active_conversation_data(source)
    if data is None:
        return source
    excluded = set(source.get("_exclude_channels") or ())
    active = [
        row["id"]
        for row in data.get("active") or []
        if row.get("id") and row["id"] not in excluded
    ]
    merged = list(dict.fromkeys([
        *source.get("direct_channels", []),
        *source.get("private_channels", []),
        *active,
    ]))
    effective = dict(source)
    effective["direct_channels"] = merged
    effective.pop("private_channels", None)

    # A conversation can first enter the census after the global catch-up
    # cursor has moved beyond its recent messages. Re-read from the census
    # floor; stable daily source IDs and content versions absorb the overlap.
    cutoff = data.get("cutoff_at")
    if cutoff and source.get("_since"):
        effective["_since"] = min(
            (source["_since"], cutoff),
            key=_rfc3339_epoch,
        )
    log(
        f"slack active-conversation scope: selected={len(active)} "
        f"excluded_owned={len(excluded)} total_direct={len(merged)}"
    )
    return effective


def _mention_excluded_channels(source):
    """Channels covered by a declared ingestion path anywhere in the run."""
    return configured_channels(source) | set(
        source.get("_exclude_mention_channels", [])
    )


def _history_args(source, channel, since=None):
    """Build one direct history command, including an explicit zero cap."""
    args = ["history", channel]
    since = since or source.get("_since")
    if since:
        args.extend(["--since", str(since)])
    else:
        args.extend(["--hours", str(source.get("hours", 26))])
    args.extend(["--limit", str(source.get("max_results", 30))])
    return args


def _activity_ts(message):
    """Latest activity Slack exposes on a root or reply."""
    return max(
        (
            value
            for value in (
                message.get("ts"),
                message.get("latest_reply"),
            )
            if value
        ),
        default="",
        key=float,
    )


def _legacy_candidates(source):
    """Backwards-compatible one-candidate-per-thread channel ingestion."""
    seen = {}
    latest = {}
    for channel in source.get("channels", []):
        data = _cli(_history_args(source, channel))
        for message in data.get("messages", []):
            sid = message["source_id"]
            activity = _activity_ts(message)
            if activity:
                latest[sid] = max(
                    filter(None, (latest.get(sid), activity)), key=float
                )
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


def _direct_digest_channels(source):
    """Configured raw-history channels and their capture-mode labels."""
    for channel in source.get("direct_channels", []):
        yield channel, "direct-daily-digest"
    for channel in source.get("private_channels", []):
        yield channel, "private-daily-digest"


def _rfc3339_epoch(value):
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    return datetime.datetime.fromisoformat(raw).timestamp()


def _daily_version(messages):
    """Content-sensitive version for a rebuilt daily digest."""
    content = [
        {
            "ts": message.get("ts"),
            "thread_ts": message.get("thread_ts"),
            "user": message.get("user"),
            "text": message.get("text"),
        }
        for message in messages
    ]
    return hashlib.sha256(
        json.dumps(
            # Schema 2 adds verified Slack participant identities to fetched
            # items. Bump the version once so existing recurring entries are
            # safely revisited and updated with people links.
            {"schema": CATCH_UP_SCHEMA, "messages": content},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]


def _normalize_direct_message(message, channel, day):
    """Retain unsupported non-text activity instead of silently dropping it."""
    if (
        (message.get("text") or "").strip()
        or int(message.get("reply_count") or 0) > 0
    ):
        return message
    normalized = dict(message)
    subtype = message.get("subtype")
    suffix = f"; subtype={subtype}" if subtype else ""
    normalized["text"] = (
        "[Slack message contained no extractable text"
        f"{suffix}]"
    )
    log(
        f"slack direct WARN channel={channel} day={day}: "
        "retaining activity with a no-text placeholder"
    )
    return normalized


def _catch_up_direct_candidates(source):
    """Lossless activity-day digests, including replies to older roots.

    Slack history is ordered and filtered by each root's original timestamp.
    It does not surface an old root merely because the thread received a new
    reply. Scan roots from the routine's explicit coverage floor, then expand
    threads whose latest activity can affect a newly active UTC day.
    """
    channel_modes = list(_direct_digest_channels(source))
    if not channel_modes:
        return []

    since_epoch = _rfc3339_epoch(source["_since"])
    boundary_epoch = _rfc3339_epoch(source["_catch_up_boundary"])
    root_floor = source["reply_roots_after"]
    out = []

    for channel, _capture_mode in channel_modes:
        roots = _cli([
            "history", channel,
            "--since", root_floor,
            "--limit", "0",
        ]).get("messages", [])
        active_days = set()
        expanded = {}

        for root in roots:
            root_ts = root.get("ts")
            if root_ts and float(root_ts) > since_epoch:
                active_days.add(_message_day(root_ts))

            latest_reply = root.get("latest_reply")
            if root_ts and latest_reply and float(latest_reply) > since_epoch:
                thread = _cli(
                    ["replies", channel, root_ts]
                ).get("messages", [])
                if not thread:
                    raise RuntimeError(
                        f"Slack thread {channel}:{root_ts} reports new replies "
                        "but conversations.replies returned no messages"
                    )
                expanded[root_ts] = thread
                active_days.update(
                    _message_day(message["ts"])
                    for message in thread
                    if message.get("ts")
                    and float(message["ts"]) > since_epoch
                )

        if not active_days:
            continue

        messages = {}
        for root in roots:
            root_ts = root.get("ts")
            if not root_ts:
                continue
            root_day = _message_day(root_ts)
            latest_reply = root.get("latest_reply")
            latest_day = (
                _message_day(latest_reply) if latest_reply else None
            )
            should_expand = int(root.get("reply_count") or 0) > 0 and (
                root_ts in expanded
                or root_day in active_days
                or latest_day in active_days
            )
            if root_ts in expanded:
                thread = expanded[root_ts]
            elif should_expand:
                thread = _cli(
                    ["replies", channel, root_ts]
                ).get("messages", [])
                if not thread:
                    raise RuntimeError(
                        f"Slack thread {channel}:{root_ts} could not be rebuilt"
                    )
            else:
                thread = [root]
            for message in thread:
                ts = message.get("ts")
                if (
                    ts
                    and float(ts) > boundary_epoch
                    and _message_day(ts) in active_days
                ):
                    day = _message_day(ts)
                    messages[ts] = _normalize_direct_message(
                        message, channel, day
                    )

        by_day = {}
        for message in messages.values():
            by_day.setdefault(_message_day(message["ts"]), []).append(message)
        for day, day_messages in by_day.items():
            day_messages.sort(key=lambda message: float(message["ts"]))
            sid = f"slack:{channel}:daily:{day}"
            latest = day_messages[-1]["ts"]
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
                # Latest activity keeps the version legible; the content hash
                # also changes when a wider root floor reveals older context
                # without changing the day's latest timestamp.
                "id": f"{sid}@{latest}:{_daily_version(day_messages)}",
                "title": first_text[:90] or f"Slack catch-up for {day}",
                "raw": {
                    "channel": channel,
                    "source_id": sid,
                    "mode": "catch_up_digest",
                    "capture_mode": "direct-catch-up-daily-digest",
                    "digest_day": day,
                    "messages": day_messages,
                    "messages_expanded": True,
                },
            })
    return out


def _direct_digest_candidates(source):
    """One direct-reader candidate per configured channel and UTC activity day."""
    per_channel = int(source.get("max_results", 30))
    out = []
    for channel, capture_mode in _direct_digest_channels(source):
        data = _cli(_history_args(source, channel))
        messages = data.get("messages", [])
        if per_channel and len(messages) >= per_channel:
            log(
                f"slack direct WARN channel={channel}: history reached "
                f"max_results={per_channel}; older activity may be omitted"
            )

        by_day = {}
        for message in messages:
            day = _message_day(message.get("ts"))
            by_day.setdefault(day, []).append(message)
        for day, day_messages in by_day.items():
            # The bundled CLI renders blocks, attachments, and files into text.
            # Retain a visible placeholder if an older external CLI still
            # returns a truly blank, unthreaded message: silently dropping an
            # unsupported payload would turn a recoverable limitation into
            # permanent data loss.
            day_messages = [
                _normalize_direct_message(message, channel, day)
                for message in day_messages
            ]
            day_messages.sort(key=lambda message: message.get("ts") or "")
            sid = f"slack:{channel}:digest:{day}"
            latest = max(
                (_activity_ts(message) for message in day_messages),
                default="",
                key=float,
            )
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
                "title": first_text[:90] or f"Slack digest for {day}",
                "raw": {
                    "channel": channel,
                    "source_id": sid,
                    "mode": "direct_digest",
                    "capture_mode": capture_mode,
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
    source = _with_active_conversations(source)
    out = _ada_digest_candidates(source)
    if source.get("catch_up"):
        out.extend(_catch_up_direct_candidates(source))
    else:
        out.extend(_direct_digest_candidates(source))
    seen, latest = _legacy_candidates(source)

    if source.get("include_mentions"):
        since = source.get("_since")
        cutoff = None
        if since:
            raw = since[:-1] + "+00:00" if since.endswith("Z") else since
            cutoff = datetime.datetime.fromisoformat(raw).timestamp()
            now = _rfc3339_epoch(utc_now_iso())
            days = max(1, math.ceil((now - cutoff) / (24 * 60 * 60)))
            if days > 30:
                raise RuntimeError(
                    "Slack mention catch-up exceeds Ada's 30-day search window; "
                    "run a manual backfill before advancing the cursor"
                )
        else:
            days = max(1, math.ceil(int(source.get("hours", 26)) / 24))
        data = _cli(["mentions", "--days", str(days)])
        if data.get("limit_reached"):
            raise RuntimeError(
                "Slack mention scan reached Ada's result limit; "
                "run a manual backfill before advancing coverage"
            )
        for message in data.get("mentions", []):
            sid = message.get("source_id")
            if not sid:
                continue
            if cutoff is not None:
                try:
                    if float(message.get("ts") or 0) <= cutoff:
                        continue
                except (TypeError, ValueError):
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


def _resolve_people(messages):
    user_ids = sorted({
        message["user"]
        for message in messages
        if message.get("user", "").startswith("U")
    })
    names = {}
    source_people = []
    if user_ids:
        try:
            users = _cli(["whois", *user_ids])["users"]
            names = {
                uid: (
                    user.get("real_name")
                    or user.get("display_name")
                    or uid
                )
                for uid, user in users.items()
            }
            source_people = [
                {
                    "name": names[uid],
                    "email": user.get("email"),
                }
                for uid, user in users.items()
                if user.get("email")
            ]
        except Exception as exc:
            log(f"slack whois failed ({exc}); keeping raw user ids")
    return names, user_ids, source_people


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


def _fetch_direct_digest(candidate):
    raw = candidate["raw"]
    channel = raw["channel"]
    messages = list(raw["messages"]) if raw.get("messages_expanded") else []
    if not raw.get("messages_expanded"):
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
        raise RuntimeError("direct channel digest has no text content")
    names, user_ids, source_people = _resolve_people(messages)
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
            "slack_capture_mode": raw["capture_mode"],
            "slack_participants": sorted(set(names.values())) or user_ids,
            "source_people": source_people,
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
    if mode in {"direct_digest", "catch_up_digest"}:
        return _fetch_direct_digest(candidate)

    channel = candidate["raw"]["channel"]
    anchor = candidate["raw"]["anchor"]
    data = _cli(["replies", channel, anchor])
    messages = data.get("messages", [])
    if not messages:
        raise RuntimeError("empty thread")

    names, user_ids, source_people = _resolve_people(messages)
    lines = [
        timestamped_line(
            slack_timestamp_iso(message.get("ts")),
            names.get(message.get("user"), message.get("user", "?")),
            message.get("text", ""),
        )
        for message in messages
    ]
    timestamps = [
        message.get("ts")
        for message in messages
        if message.get("ts")
    ]
    first_ts = min(timestamps, key=float) if timestamps else anchor
    latest_ts = max(timestamps, key=float) if timestamps else anchor
    # Thread candidates are versioned by their latest reply. File the memory
    # on that same activity date so a decision made weeks after the root
    # message is not incorrectly dated as the start of the thread.
    date = _message_day(latest_ts)

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
            "source_people": source_people,
            "via_mention": bool(candidate["raw"].get("via_mention")),
            "first_message_at": slack_timestamp_iso(first_ts),
            "latest_message_at": slack_timestamp_iso(latest_ts),
        },
    }
