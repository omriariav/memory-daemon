"""Resumable, read-only discovery of recently active Slack conversations."""
import datetime
import json
import time
from pathlib import Path

from . import state


CENSUS_VERSION = 1


def conversation_type(conversation):
    if conversation.get("is_im") is True:
        return "im"
    if conversation.get("is_mpim") is True:
        return "mpim"
    if conversation.get("is_private") is True:
        return "private_channel"
    return "public_channel"


def load_checkpoint(path):
    if not path or not Path(path).exists():
        return None
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot resume Slack census from {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != CENSUS_VERSION:
        raise RuntimeError(
            f"Slack census checkpoint {path} has an unsupported format"
        )
    return data


def _save_checkpoint(path, data):
    if path:
        state.write_atomic(
            Path(path),
            json.dumps(data, indent=2, sort_keys=True) + "\n",
        )


def _iso_from_epoch(value):
    return datetime.datetime.fromtimestamp(
        float(value), datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    conversations,
    api,
    cutoff_epoch,
    requests_per_minute=40,
    checkpoint=None,
    progress=None,
    sleep=time.sleep,
):
    """Probe one recent top-level message per conversation.

    The checkpoint contains only IDs, names, type metadata, timestamps, and API
    errors—never message text. It makes a long inventory safe across laptop
    sleep or interruption without turning the census into a content store.
    """
    if requests_per_minute < 1 or requests_per_minute > 50:
        raise ValueError("requests_per_minute must be between 1 and 50")
    progress = progress or (lambda _message: None)
    checkpoint = Path(checkpoint) if checkpoint else None
    existing = load_checkpoint(checkpoint)

    inventory = [
        {
            key: conversation.get(key)
            for key in (
                "id", "name", "user", "is_private", "is_im", "is_mpim"
            )
        }
        for conversation in conversations
        if conversation.get("id")
    ]
    if existing:
        data = existing
        cutoff_epoch = float(data["cutoff_epoch"])
        if [row["id"] for row in data.get("inventory", [])] != [
            row["id"] for row in inventory
        ]:
            raise RuntimeError(
                "Slack conversation inventory changed since the checkpoint; "
                "choose a new checkpoint path"
            )
    else:
        data = {
            "version": CENSUS_VERSION,
            "cutoff_epoch": float(cutoff_epoch),
            "cutoff_at": _iso_from_epoch(cutoff_epoch),
            "inventory": inventory,
            "next_index": 0,
            "active": [],
            "errors": [],
        }
        _save_checkpoint(checkpoint, data)

    delay = 60.0 / requests_per_minute
    total = len(inventory)
    index = int(data.get("next_index") or 0)
    while index < total:
        conversation = inventory[index]
        channel = conversation["id"]
        try:
            response = api(
                "conversations.history",
                {
                    "channel": channel,
                    "oldest": f"{float(cutoff_epoch):.6f}",
                    "inclusive": "false",
                    "limit": 1,
                },
            )
        except Exception as exc:
            error = getattr(exc, "error", None) or str(exc)
            if error == "ratelimited":
                progress(
                    f"Slack census rate-limited at {index}/{total}; "
                    "waiting 60s and retrying"
                )
                sleep(60)
                continue
            data["errors"].append({"id": channel, "error": error})
        else:
            messages = response.get("messages") or []
            if messages:
                data["active"].append({
                    "id": channel,
                    "name": conversation.get("name"),
                    "user": conversation.get("user"),
                    "type": conversation_type(conversation),
                    "is_private": conversation.get("is_private"),
                    "latest_ts": messages[0].get("ts"),
                })

        index += 1
        data["next_index"] = index
        if index % 10 == 0 or index == total:
            _save_checkpoint(checkpoint, data)
        if index % 25 == 0 or index == total:
            progress(
                f"Slack census {index}/{total}: "
                f"{len(data['active'])} active, {len(data['errors'])} errors"
            )
        if index < total:
            sleep(delay)

    data["completed_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    _save_checkpoint(checkpoint, data)
    return data
