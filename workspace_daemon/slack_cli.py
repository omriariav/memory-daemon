"""Minimal read-only Slack CLI used by the daemon's Slack source.

Run directly with:

    python3 -m workspace_daemon.slack_cli auth-test

The user token is read from ``$MEMORY_DAEMON_SLACK_CONFIG`` when set, otherwise
from ``$XDG_CONFIG_HOME/memory-daemon/slack.json`` (falling back to
``~/.config/memory-daemon/slack.json``).
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional


API = "https://slack.com/api"
PERMALINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")
MENTION_MAX_RESULTS = 100


class SlackAPIError(RuntimeError):
    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


def config_path() -> Path:
    override = os.environ.get("MEMORY_DAEMON_SLACK_CONFIG")
    if override:
        return Path(override).expanduser()
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    ).expanduser()
    return config_home / "memory-daemon" / "slack.json"


def die(msg: str, code: int = 1) -> None:
    json.dump({"ok": False, "error": msg}, sys.stdout, indent=2)
    print()
    raise SystemExit(code)


def config_data() -> Dict:
    path = config_path()
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read Slack config from {path}: {exc}")
    if not isinstance(data, dict):
        die(f"Slack config at {path} must contain a JSON object")
    return data


def token() -> str:
    path = config_path()
    try:
        value = config_data()["user_token"]
    except KeyError:
        die(f"cannot read user_token from {path}: key is missing")
    if not value or "PASTE" in value:
        die(f"user_token is not set in {path}")
    return value


def slack_request(method: str, params: Optional[Dict] = None) -> Dict:
    """Call one Slack Web API method via curl.

    The system Python on managed Macs may not have a usable certificate bundle;
    curl uses the operating system's trusted configuration.
    """
    url = f"{API}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(
            {key: value for key, value in params.items() if value is not None}
        )
    result = subprocess.run(
        [
            "curl", "-sS", "-X", "POST", url,
            "-H", "@-",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        input=f"Authorization: Bearer {token()}\n",
    )
    if result.returncode != 0:
        raise SlackAPIError(
            method, f"curl failed: {result.stderr.strip()[:200]}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise SlackAPIError(
            method, f"non-JSON response: {result.stdout[:200]}"
        )
    if not data.get("ok"):
        raise SlackAPIError(method, data.get("error", "unknown_error"))
    return data


def slack(method: str, params: Optional[Dict] = None) -> Dict:
    """CLI-facing API call that renders failures as the standard JSON error."""
    try:
        return slack_request(method, params)
    except SlackAPIError as exc:
        die(str(exc))


def out(payload) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    print()


def parse_since(args: List[str]) -> Optional[str]:
    """Translate ``--since`` or ``--hours`` to Slack's oldest timestamp."""
    if "--since" in args:
        raw = args[args.index("--since") + 1]
        if raw.endswith("Z"):
            # Python 3.9's fromisoformat does not accept the standard UTC
            # suffix; +00:00 has the same meaning and works on every supported
            # Python version.
            raw = raw[:-1] + "+00:00"
        value = datetime.fromisoformat(raw)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return f"{value.timestamp():.6f}"
    if "--hours" in args:
        hours = float(args[args.index("--hours") + 1])
        value = datetime.now(timezone.utc) - timedelta(hours=hours)
        return f"{value.timestamp():.6f}"
    return None


def opt(args: List[str], flag: str, default=None):
    return args[args.index(flag) + 1] if flag in args else default


def _structured_text(value) -> List[str]:
    """Extract human-visible text from Slack blocks and attachments."""
    parts = []
    if isinstance(value, list):
        for child in value:
            parts.extend(_structured_text(child))
        return parts
    if not isinstance(value, dict):
        return parts
    for key, child in value.items():
        if key in {
            "text", "fallback", "pretext", "title", "alt_text"
        } and isinstance(child, str):
            if child.strip():
                parts.append(child.strip())
        elif isinstance(child, (dict, list)):
            parts.extend(_structured_text(child))
    return parts


def _attachment_text(value) -> List[str]:
    """Extract visible attachment text without internal action values."""
    parts = _structured_text(value)
    attachments = value if isinstance(value, list) else [value]
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        for field in attachment.get("fields") or []:
            if not isinstance(field, dict):
                continue
            field_value = field.get("value")
            if isinstance(field_value, str) and field_value.strip():
                parts.append(field_value.strip())
    return parts


def _message_text(message: Dict) -> str:
    """Render text plus safe fallbacks for structured Slack messages."""
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text

    parts = [
        *_structured_text(message.get("blocks")),
        *_attachment_text(message.get("attachments")),
    ]
    for slack_file in message.get("files") or []:
        if not isinstance(slack_file, dict):
            continue
        name = (
            slack_file.get("title")
            or slack_file.get("name")
            or slack_file.get("id")
            or "unnamed file"
        )
        kind = slack_file.get("mimetype") or slack_file.get("pretty_type")
        label = f"{name} ({kind})" if kind else str(name)
        permalink = slack_file.get("permalink")
        marker = f"[Shared file: {label}]"
        if permalink:
            marker = f"{marker} {permalink}"
        parts.append(marker)

    unique = []
    for part in parts:
        if part not in unique:
            unique.append(part)
    if unique:
        return "\n".join(unique)

    subtype = message.get("subtype")
    suffix = f"; subtype={subtype}" if subtype else ""
    return f"[Slack message contained no extractable text{suffix}]"


def simplify_message(message: Dict, channel: str) -> Dict:
    raw_text = message.get("text")
    return {
        "ts": message.get("ts"),
        "thread_ts": message.get("thread_ts"),
        "latest_reply": message.get("latest_reply"),
        "user": message.get("user") or message.get("bot_id", "?"),
        "text": _message_text(message),
        "subtype": message.get("subtype"),
        "non_text_fallback": not (
            isinstance(raw_text, str) and raw_text.strip()
        ),
        "reply_count": message.get("reply_count", 0),
        "source_id": (
            f"slack:{channel}:"
            f"{message.get('thread_ts') or message.get('ts')}"
        ),
    }


def paged_messages(
    method: str,
    params: Dict,
    limit: Optional[int] = None,
) -> List[Dict]:
    """Read cursor-paginated Slack messages up to an optional total cap."""
    if limit is not None and limit < 1:
        die("message limit must be a positive integer")

    messages = []
    cursor = None
    seen_cursors = set()
    while limit is None or len(messages) < limit:
        request = dict(params)
        request["limit"] = min(200, limit - len(messages)) if limit else 200
        if cursor:
            request["cursor"] = cursor
        data = slack(method, request)
        page = data.get("messages") or []
        messages.extend(page)

        cursor = (
            data.get("response_metadata", {}).get("next_cursor") or None
        )
        if not cursor:
            break
        if cursor in seen_cursors:
            die(f"{method} returned a repeated pagination cursor")
        seen_cursors.add(cursor)

    return messages[:limit] if limit is not None else messages


def cmd_auth_test(_args: List[str]) -> None:
    data = slack("auth.test")
    out({
        key: data.get(key)
        for key in ("ok", "team", "user", "user_id", "url")
    })


def list_conversations(
    method: str,
    types: str,
    limit: Optional[int],
    api=None,
) -> List[Dict]:
    """Read cursor-paginated conversation metadata."""
    api = api or slack
    channels, cursor = [], None
    while limit is None or len(channels) < limit:
        request_limit = (
            min(limit - len(channels), 200) if limit is not None else 200
        )
        data = api(
            method,
            {
                "types": types,
                "limit": request_limit,
                "exclude_archived": "true",
                "cursor": cursor,
            },
        )
        channels += [
            {
                "id": channel["id"],
                "name": channel.get("name"),
                "user": channel.get("user"),
                "is_private": channel.get("is_private"),
                "is_member": channel.get("is_member"),
                "is_im": channel.get("is_im"),
                "is_mpim": channel.get("is_mpim"),
                "num_members": channel.get("num_members"),
            }
            for channel in data.get("channels", [])
        ]
        cursor = (
            data.get("response_metadata", {}).get("next_cursor") or None
        )
        if not cursor:
            break
    return channels[:limit] if limit is not None else channels


def _cmd_conversations(
    args: List[str],
    method: str,
    membership_scoped: bool,
) -> None:
    types = opt(args, "--types", "public_channel,private_channel")
    raw_limit = int(opt(args, "--limit", 200))
    if raw_limit < 0:
        die("channel limit must be a non-negative integer")
    # Match the history command: an explicit zero means exhaustive cursor
    # pagination. Dynamic sweeps must not silently cover only the first page.
    limit = None if raw_limit == 0 else raw_limit
    selected = list_conversations(method, types, limit)
    out({
        "ok": True,
        "count": len(selected),
        "membership_scoped": membership_scoped,
        "channels": selected,
    })


def cmd_channels(args: List[str]) -> None:
    """List workspace channels; useful for explicit channel lookup."""
    _cmd_conversations(args, "conversations.list", membership_scoped=False)


def cmd_joined(args: List[str]) -> None:
    """List only conversations joined by the authenticated user."""
    _cmd_conversations(args, "users.conversations", membership_scoped=True)


def cmd_census(args: List[str]) -> None:
    """Find joined conversations with top-level activity in a recent window."""
    from . import slack_census

    hours = float(opt(args, "--hours", 48))
    rpm = int(opt(args, "--requests-per-minute", 40))
    if hours <= 0:
        die("census hours must be greater than zero")
    if rpm < 1 or rpm > 50:
        die("census requests per minute must be between 1 and 50")
    checkpoint_value = opt(args, "--checkpoint")
    checkpoint = (
        Path(checkpoint_value).expanduser()
        if checkpoint_value
        else None
    )
    existing = slack_census.load_checkpoint(checkpoint)
    if existing:
        conversations = existing["inventory"]
        cutoff_epoch = float(existing["cutoff_epoch"])
    else:
        conversations = list_conversations(
            "users.conversations",
            "public_channel,private_channel,im,mpim",
            None,
            api=slack_request,
        )
        cutoff_epoch = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).timestamp()

    result = slack_census.run(
        conversations,
        slack_request,
        cutoff_epoch,
        requests_per_minute=rpm,
        checkpoint=checkpoint,
        progress=lambda message: print(message, file=sys.stderr, flush=True),
    )
    payload = {
        "ok": not result["errors"],
        "window_hours": hours,
        "cutoff_at": result["cutoff_at"],
        "considered": len(result["inventory"]),
        "active_count": len(result["active"]),
        "error_count": len(result["errors"]),
        "active": result["active"],
        "errors": result["errors"],
        "checkpoint": str(checkpoint) if checkpoint else None,
    }
    out(payload)
    if result["errors"]:
        raise SystemExit(1)


def cmd_history(args: List[str]) -> None:
    channel = args[0]
    oldest = parse_since(args)
    raw_limit = int(opt(args, "--limit", 50))
    # A catch-up source must exhaust every page after its durable cursor.
    # Keep the historical default cap, while making an explicit zero mean
    # "unbounded" like the other daemon source limits.
    limit = None if raw_limit == 0 else raw_limit
    messages = paged_messages(
        "conversations.history",
        {"channel": channel, "oldest": oldest},
        limit=limit,
    )
    simplified = [
        simplify_message(message, channel)
        for message in messages
    ]
    out({
        "ok": True,
        "channel": channel,
        "count": len(simplified),
        "messages": simplified,
    })


def cmd_replies(args: List[str]) -> None:
    channel, thread_ts = args[0], args[1]
    messages = paged_messages(
        "conversations.replies",
        {"channel": channel, "ts": thread_ts},
    )
    simplified = [
        simplify_message(message, channel)
        for message in messages
    ]
    out({
        "ok": True,
        "channel": channel,
        "thread_ts": thread_ts,
        "count": len(simplified),
        "source_id": f"slack:{channel}:{thread_ts}",
        "messages": simplified,
    })


def cmd_whois(args: List[str]) -> None:
    users = {}
    for user_id in args:
        data = slack("users.info", {"user": user_id})
        user = data.get("user", {})
        users[user_id] = {
            "real_name": user.get("real_name"),
            "display_name": user.get("profile", {}).get("display_name"),
            "email": user.get("profile", {}).get("email"),
        }
    out({"ok": True, "users": users})


def mention_user() -> str:
    """Identity passed to the optional ada mentions helper.

    A private config override avoids two API calls. Otherwise derive the email
    from the authenticated Slack user so no person's or company's identity is
    compiled into the public client.
    """
    configured = (
        os.environ.get("MEMORY_DAEMON_SLACK_MENTION_USER")
        or config_data().get("mention_user")
    )
    if configured:
        return str(configured)
    auth = slack("auth.test")
    user_id = auth.get("user_id")
    if not user_id:
        die("auth.test returned no user_id for mention lookup")
    user = slack("users.info", {"user": user_id}).get("user", {})
    email = user.get("profile", {}).get("email")
    if not email:
        die(
            "could not resolve the authenticated user's email for mentions; "
            "grant users:read.email or set mention_user in the Slack config"
        )
    return email


def cmd_mentions(args: List[str]) -> None:
    """Get workspace-wide mentions via ada, which owns search access."""
    days = opt(args, "--days", "1")
    result = subprocess.run(
        [
            "ada", "slack", "mentions",
            "--user", mention_user(),
            "--days", str(days),
            "--max-results", str(MENTION_MAX_RESULTS),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        die(
            "ada slack mentions failed: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        die(f"non-JSON from ada: {result.stdout[:200]}")

    mentions = []
    for mention in data.get("mentions", []):
        link = mention.get("permalink", "")
        permalink = PERMALINK_RE.search(link)
        channel_id, timestamp = (
            (
                permalink.group(1),
                f"{permalink.group(2)}.{permalink.group(3)}",
            )
            if permalink
            else (None, None)
        )
        thread_ts = None
        if "thread_ts=" in link:
            thread_ts = link.split("thread_ts=")[1].split("&")[0]
        anchor = thread_ts or timestamp
        mentions.append({
            "channel": mention.get("channel"),
            "channel_id": channel_id,
            "sender": mention.get("sender"),
            "text": mention.get("text"),
            "timestamp": mention.get("timestamp"),
            "ts": timestamp,
            "thread_ts": thread_ts,
            "permalink": link,
            "source_id": (
                f"slack:{channel_id}:{anchor}"
                if channel_id and anchor
                else None
            ),
        })
    out({
        "ok": True,
        "days": int(days),
        "count": len(mentions),
        "max_results": MENTION_MAX_RESULTS,
        # Ada exposes no pagination cursor. Exactly the requested maximum is
        # therefore ambiguous and must fail closed in a durable sweep.
        "limit_reached": (
            len(mentions) >= MENTION_MAX_RESULTS
            or bool(data.get("has_more"))
            or int(data.get("total_count") or 0) > len(mentions)
        ),
        "via": "ada slack mentions",
        "mentions": mentions,
    })


COMMANDS = {
    "auth-test": cmd_auth_test,
    "channels": cmd_channels,
    "joined": cmd_joined,
    "census": cmd_census,
    "history": cmd_history,
    "replies": cmd_replies,
    "whois": cmd_whois,
    "mentions": cmd_mentions,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        raise SystemExit(0)
    command, args = sys.argv[1], sys.argv[2:]
    handler = COMMANDS.get(command)
    if not handler:
        die(f"unknown command: {command} (see --help)")
    try:
        handler(args)
    except SlackAPIError as exc:
        die(str(exc))
    except (IndexError, ValueError):
        die(f"invalid or missing argument for '{command}' (see --help)")


if __name__ == "__main__":
    main()
