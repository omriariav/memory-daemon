"""Google Chat source: sweep Google Chat via the `gws` CLI, grouped by thread.

Routine config:

    source:
      kind: gchat
      spaces:
        - spaces/AAAA000000A     # #my-team-space
      hours: 26                  # lookback window; overlap the schedule slightly
      max_results: 50            # cap per space; 0 paginates exhaustively

For a broad fallback sweep, replace ``spaces`` with ``all_spaces: true``.
That uses ``gws chat recent``: it cheaply filters the user's entire space list
by activity time, then reads only spaces active inside the requested window.
In this mode ``max_results`` is a global message cap and may be 0 for no cap.
Set ``batch_messages: daily`` to give every space/UTC-day one stable digest source
id; hourly reruns then update that digest as messages and replies arrive. Each
discovered day is re-fetched completely before analysis. Existing routines can
set ``batch_messages_after`` to an exclusive RFC3339 cutover boundary so content
handled by the previous batching mode is not captured again. The runner may
inject an exact ``_since`` checkpoint for ``catch_up: true`` sources; this
replaces the fixed discovery window without changing daily candidate identity.

One `gws chat messages --after` call per space returns full message texts, so
candidates are grouped client-side by thread. Message lists are requested in
raw API shape (``--raw``): the ergonomic transform drops emoji reaction
summaries and quoted-message context, and both matter — a terse "done" reply
is only legible next to the request it quotes, and a reaction is the only
Chat-native acknowledgement signal. Raw messages are normalized back into the
flattened snake_case shape this module consumes. Fetch resolves the space name,
conversation type, and members once per space per process. `gws chat members`
uses gws's persistent user cache for display names; this module adds a small
in-process cache so several threads from one space do not repeat API calls.

Identity has two layers (this is what keeps re-sweeps of an active thread from
freezing at first capture — the ledger dedupes on the *candidate* id, while the
memory store dedupes on the stable *source* id):

- candidate id:  gchat:<space>:<thread>@<latest_message_ts>  — changes when a
  thread gains replies, so the runner reprocesses it
- source id:     gchat:<space>:<thread-or-day/daily>         — stable anchor;
  `memory add` updates the same entry in place
"""
import datetime
import hashlib
import json
import subprocess

from .chat_text import redact_secrets, timestamped_line
from .shell import gws_bin, log
from .time_utils import rfc3339_key

# Bump only when candidate construction changes in a way that needs a replay
# from the declared batching cutover rather than the ordinary live overlap.
CATCH_UP_SCHEMA = 1
MAX_CONTEXT_MEMBERS = 20
MAX_QUOTED_SNIPPET = 200
_member_cache = {}
_space_cache = {}


def _gws(args, timeout=60):
    r = subprocess.run([gws_bin(), *args, "--format", "json"],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"gws {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return json.loads(r.stdout)


def _after_iso(hours):
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=float(hours))
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _space_id(space):
    return space.split("/")[-1]


def _thread_id(message):
    return (message.get("thread") or message.get("name", "")).split("/")[-1]


def _normalize_raw_message(message):
    """Flatten a raw Chat API message into the shape this module consumes.

    Already-flattened messages (``gws chat recent``, ledgered fixtures) pass
    through unchanged. Beyond the ergonomic fields, raw mode contributes
    ``reactions`` (emoji + count summaries) and ``quoted_message`` (the quoted
    snippet a reply was anchored to).
    """
    if "createTime" not in message:
        return message
    sender = message.get("sender")
    thread = message.get("thread")
    space = message.get("space")
    flat = {
        "name": message.get("name"),
        "create_time": message.get("createTime"),
        "last_update_time": message.get("lastUpdateTime"),
        "text": message.get("text"),
        "sender": sender.get("name") if isinstance(sender, dict) else sender,
        "sender_type": sender.get("type") if isinstance(sender, dict) else None,
        "thread": thread.get("name") if isinstance(thread, dict) else thread,
        "space": space.get("name") if isinstance(space, dict) else space,
        "cards_v2": message.get("cardsV2"),
    }
    attachments = message.get("attachment") or message.get("attachments") or []
    if isinstance(attachments, dict):
        attachments = [attachments]
    normalized_attachments = [
        {
            "content_name": attachment.get("contentName")
            or attachment.get("content_name"),
            "content_type": attachment.get("contentType")
            or attachment.get("content_type"),
        }
        for attachment in attachments
        if isinstance(attachment, dict)
    ]
    if normalized_attachments:
        flat["attachments"] = normalized_attachments
    reactions = []
    for summary in message.get("emojiReactionSummaries") or []:
        if not isinstance(summary, dict):
            continue
        emoji = summary.get("emoji") or {}
        label = (
            emoji.get("unicode")
            or (emoji.get("customEmoji") or {}).get("name")
            or "custom emoji"
        )
        reactions.append({
            "emoji": label,
            "count": summary.get("reactionCount", 1),
        })
    if reactions:
        flat["reactions"] = reactions
    quoted = (
        (message.get("quotedMessageMetadata") or {})
        .get("quotedMessageSnapshot") or {}
    ).get("text")
    if quoted:
        flat["quoted_message"] = {"text": quoted}
    return {key: value for key, value in flat.items() if value is not None}


def _message_content(message):
    """Renderable message text plus safe attachment metadata.

    Attachment bodies and URLs are deliberately excluded; names and MIME types
    are enough for the model to understand that a file/image was shared.
    """
    parts = []
    text = (message.get("text") or "").strip()
    if text:
        parts.append(text)
    attachments = message.get("attachments") or message.get("attachment") or []
    if isinstance(attachments, dict):
        attachments = [attachments]
    for attachment in attachments if isinstance(attachments, list) else []:
        if not isinstance(attachment, dict):
            continue
        name = (
            attachment.get("content_name")
            or attachment.get("name")
            or "attachment"
        )
        mime = attachment.get("content_type") or attachment.get("mime_type") or ""
        detail = f"{name} ({mime})" if mime else str(name)
        parts.append(f"[Attachment: {detail}]")
    if not parts and (message.get("cards_v2") or message.get("cards")):
        parts.append("[Rich card attachment]")
    return redact_secrets("\n".join(parts))


def _rendered_reactions(message):
    """Human-readable reaction summary, or empty string."""
    parts = []
    for reaction in message.get("reactions") or []:
        emoji = reaction.get("emoji") or "?"
        count = reaction.get("count") or 1
        parts.append(f"{emoji} x{count}" if count > 1 else str(emoji))
    return ", ".join(parts)


def _rendered_text(message):
    """Message content plus quoted-reply context and reaction annotations.

    A reaction does not advance the message's update time, so it must also be
    part of the version payload below — otherwise a heart on yesterday's
    "done" would never reprocess the digest.
    """
    text = _message_content(message)
    quoted = (message.get("quoted_message") or {}).get("text") or ""
    if quoted:
        snippet = redact_secrets(quoted).replace("\n", " ")[:MAX_QUOTED_SNIPPET]
        prefix = f'[in reply to: "{snippet}"]'
        text = f"{prefix}\n{text}" if text else prefix
    reactions = _rendered_reactions(message)
    if reactions:
        suffix = f"[reactions: {reactions}]"
        text = f"{text}\n{suffix}" if text else suffix
    return text


def _message_version(message):
    return message.get("last_update_time") or message.get("create_time") or ""


def _batch_version(messages):
    latest = max((_message_version(message) for message in messages), default="")
    payload = [
        {
            "name": message.get("name"),
            "created": message.get("create_time"),
            "updated": message.get("last_update_time"),
            "sender": message.get("sender"),
            "content": _message_content(message),
            "reactions": message.get("reactions") or [],
            "quoted": (message.get("quoted_message") or {}).get("text") or "",
        }
        for message in messages
    ]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return f"{latest}:{digest}"


def _sessions(messages, gap_minutes):
    if not gap_minutes:
        return [messages]
    batches = []
    for message in messages:
        if not batches:
            batches.append([message])
            continue
        previous = batches[-1][-1]
        current_at, _ = rfc3339_key(message.get("create_time") or "")
        previous_at, _ = rfc3339_key(previous.get("create_time") or "")
        if (current_at - previous_at).total_seconds() >= gap_minutes * 60:
            batches.append([message])
        else:
            batches[-1].append(message)
    return batches


def _hours_duration(hours):
    value = float(hours)
    return f"{int(value) if value.is_integer() else value}h"


def _full_daily_batches(discovered, cutoff=None):
    """Re-fetch complete UTC days for every discovered ``(space, day)``.

    Discovery uses a sliding window and may be capped. Reusing a daily source
    id with only that slice would let the memory store replace a complete
    digest with a partial one. One exhaustive, paginated history call per
    discovered space starts just before its earliest relevant UTC day; only
    discovered days are retained.
    """
    days_by_space = {}
    for space, day in discovered:
        days_by_space.setdefault(space, set()).add(day)

    cutoff_instant = rfc3339_key(cutoff) if cutoff else None
    batches = {}
    for space, days in days_by_space.items():
        earliest = min(days)
        start = (
            datetime.datetime.strptime(earliest, "%Y-%m-%d")
            .replace(tzinfo=datetime.timezone.utc)
            - datetime.timedelta(microseconds=1)
        )
        after = start.isoformat(timespec="microseconds").replace("+00:00", "Z")
        data = _gws(
            ["chat", "messages", space, "--after", after, "--raw", "--all"],
            timeout=300,
        )
        for message in data.get("messages") or []:
            message = _normalize_raw_message(message)
            timestamp = message.get("create_time") or ""
            day = timestamp[:10]
            key = (space, day)
            if not timestamp or key not in discovered:
                continue
            if cutoff_instant and rfc3339_key(timestamp) <= cutoff_instant:
                continue
            batches.setdefault(key, []).append(message)
    return batches


def _windowed_messages(source):
    """Yield ``(space, message)`` pairs for explicit or all-space sweeps."""
    excluded_spaces = set(source.get("_exclude_spaces") or ())
    if source.get("all_spaces"):
        since = source.get("_since") or _hours_duration(source.get("hours", 26))
        args = [
            "chat", "recent",
            "--since", since,
            "--max", str(source.get("max_results", 0)),
            "--max-per-space", str(source.get("max_per_space", 0)),
        ]
        data = _gws(args, timeout=300)
        messages = data.get("messages") or []
        discovered_spaces = set()
        considered = []
        for message in messages:
            space = message.get("space")
            if not space:
                name = message.get("name", "")
                if "/messages/" in name:
                    space = name.split("/messages/", 1)[0]
            if not space:
                continue
            discovered_spaces.add(space)
            if space in excluded_spaces:
                continue
            considered.append((space, message))
            if space not in _space_cache:
                _space_cache[space] = {
                    "display_name": message.get("space_display_name") or "",
                    "type": message.get("space_type") or "",
                }
        excluded_active = discovered_spaces & excluded_spaces
        considered_spaces = {
            space for space, _message in considered
        }
        log(
            f"gchat recent coverage since={since} messages={len(messages)} "
            f"discovered_space_ids={sorted(discovered_spaces)} "
            f"excluded_active_space_ids={sorted(excluded_active)} "
            f"considered_space_ids={sorted(considered_spaces)}"
        )
        for space, message in considered:
            yield space, message
        return

    configured_max = int(source.get("max_results", 50))
    # In raw mode zero means "no cap": --all paginates exhaustively, while a
    # positive max is a single bounded page, matching the old cap semantics.
    limit_args = (
        ["--all"] if configured_max == 0 else ["--max", str(configured_max)]
    )
    after = source.get("_since") or _after_iso(source.get("hours", 26))
    for space in source.get("spaces", []):
        if space in excluded_spaces:
            continue
        data = _gws(
            ["chat", "messages", space, "--after", after, "--raw", *limit_args]
        )
        for message in data.get("messages") or []:
            yield space, _normalize_raw_message(message)


def candidates(source):
    """One candidate per thread that saw traffic inside the window.

    The candidate carries every windowed message of its thread in `raw`, so
    fetch never re-queries. Messages that arrived before the window (a thread's
    earlier history) are not included — the summary covers the active slice,
    and the stable source id folds successive slices into one memory entry.
    """
    threads = {}
    discovered_daily = set()
    batch_messages = source.get("batch_messages") == "daily"
    cutoff = source.get("batch_messages_after")
    cutoff_instant = rfc3339_key(cutoff) if cutoff else None

    for space, message in _windowed_messages(source):
        if batch_messages:
            timestamp = message.get("create_time") or ""
            if not timestamp:
                continue
            if cutoff_instant and rfc3339_key(timestamp) <= cutoff_instant:
                continue
            discovered_daily.add((space, timestamp[:10]))
            continue
        sid = f"gchat:{_space_id(space)}:{_thread_id(message)}"
        thread = threads.setdefault(sid, {"space": space, "messages": []})
        thread["messages"].append(message)

    all_daily = (
        _full_daily_batches(discovered_daily, cutoff)
        if discovered_daily else {}
    )
    out = []
    daily = {}
    for sid, t in threads.items():
        msgs = sorted(t["messages"], key=lambda m: m.get("create_time", ""))
        if not any(_message_content(m) for m in msgs):
            continue

        if source.get("batch_unthreaded") == "daily" and len(msgs) == 1:
            day = (msgs[0].get("create_time") or "")[:10] or "date-unknown"
            key = (t["space"], day)
            daily.setdefault(key, []).extend(msgs)
            continue

        version = _batch_version(msgs)
        first_text = _message_content(msgs[0]).replace("\n", " ")
        out.append({
            "id": f"{sid}@{version}",
            "title": first_text[:90],
            "raw": {"source_id": sid, "space": t["space"], "messages": msgs},
        })

    # Google Chat assigns every unthreaded room message its own thread id. A
    # conversational burst would therefore cost one LLM call per sentence.
    # Opt-in daily batching gives those messages a stable space/day identity;
    # rerunning later the same day updates one memory entry as the digest grows.
    for namespace, batches in (("day", daily), ("daily", all_daily)):
        for (space, day), msgs in batches.items():
            # Empty system events are not prompt content and must not advance
            # the candidate version on their own.
            msgs = sorted(
                (m for m in msgs if _message_content(m)),
                key=lambda m: m.get("create_time", ""),
            )
            if not msgs:
                continue
            sessions = _sessions(msgs, source.get("session_gap_minutes"))
            for session_index, session in enumerate(sessions):
                session_suffix = ""
                if session_index > 0:
                    first_at = session[0].get("create_time") or "unknown"
                    session_suffix = f":session:{first_at}"
                sid = (
                    f"gchat:{_space_id(space)}:{namespace}:{day}"
                    f"{session_suffix}"
                )
                version = _batch_version(session)
                first_text = _message_content(session[0]).replace("\n", " ")
                out.append({
                    "id": f"{sid}@{version}",
                    "title": first_text[:90] or f"Google Chat digest for {day}",
                    "raw": {
                        "source_id": sid,
                        "space": space,
                        "messages": session,
                        "digest_day": day,
                    },
                })
    return out


def _member_context(space):
    """Return verified member metadata plus display names, cached per run."""
    if space in _member_cache:
        return _member_cache[space]
    names = {}
    members = []
    people = {}
    try:
        d = _gws(["chat", "members", space])
        rows = d if isinstance(d, list) else d.get("memberships") or d.get("members") or []
        for r in rows:
            user = r.get("user")
            display = r.get("display_name") or r.get("email") or user
            if user and display:
                names[user] = display
                people[user] = {
                    "email": str(r.get("email") or "").strip().casefold(),
                    "name": display,
                    "role": "gchat-member",
                }
            if display:
                members.append(display)
    except Exception as exc:  # cosmetic — raw user ids still identify speakers
        log(f"gchat members lookup failed for {space} ({exc}); keeping raw ids")
    context = {
        "names": names,
        "members": sorted(set(members)),
        "people": people,
    }
    _member_cache[space] = context
    return context


def _space_context(space):
    """Return live space name/type metadata, cached once per process."""
    if space in _space_cache:
        return _space_cache[space]
    context = {}
    try:
        d = _gws(["chat", "get-space", space])
        if isinstance(d, dict):
            context = {
                "display_name": d.get("display_name") or "",
                "type": d.get("type") or "",
            }
    except Exception as exc:  # enrichment only; messages remain processable
        log(f"gchat space lookup failed for {space} ({exc}); keeping space id")
    _space_cache[space] = context
    return context


def fetch(routine, candidate):
    """Render the thread's windowed messages as a readable conversation."""
    raw = candidate["raw"]
    msgs = raw["messages"]
    member_context = _member_context(raw["space"])
    space_context = _space_context(raw["space"])
    names = member_context["names"]
    members = member_context["members"]
    member_people = member_context["people"]
    space_type = space_context.get("type", "")
    context_members = (
        members
        if space_type == "DIRECT_MESSAGE" or len(members) <= MAX_CONTEXT_MEMBERS
        else []
    )
    participant_ids = {
        m.get("sender") for m in msgs if m.get("sender")
    }
    # Small-room membership is useful for resolving people mentioned in a
    # message even when they did not speak in the supplied window.  In a large
    # room, constrain identity candidates to actual senders to avoid needless
    # directory calls and accidental broad attribution.
    identity_ids = (
        set(member_people)
        if len(member_people) <= MAX_CONTEXT_MEMBERS
        else participant_ids
    )
    source_people = [
        member_people[user]
        for user in sorted(identity_ids)
        if user in member_people and member_people[user].get("email")
    ]

    lines = [
        timestamped_line(
            m.get("create_time"),
            names.get(m.get("sender"), m.get("sender", "?")),
            _rendered_text(m),
        )
        for m in msgs
        if _message_content(m)
    ]
    if not lines:
        raise RuntimeError("thread has no text content in the window")

    date = (msgs[0].get("create_time") or "")[:10]
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],  # stable anchor for the memory store
        "title": candidate["title"] or f"chat thread in {raw['space']}",
        "date": date,
        "body": "\n".join(lines),
        "frontmatter": {
            "gchat_space": raw["space"],
            "gchat_space_display_name": space_context.get("display_name", ""),
            "gchat_space_type": space_type,
            "gchat_space_members": context_members,
            "gchat_space_member_count": len(members),
            "gchat_thread": raw["source_id"],
            "gchat_participants": sorted({names.get(m.get("sender"), m.get("sender", "?"))
                                          for m in msgs}),
            "source_people": source_people,
            "reaction_count": sum(
                reaction.get("count") or 1
                for m in msgs
                for reaction in m.get("reactions") or []
            ),
            "message_count": len(msgs),
            "first_message_at": msgs[0].get("create_time", ""),
            "latest_message_at": msgs[-1].get("create_time", ""),
            "digest_day": raw.get("digest_day"),
        },
    }
