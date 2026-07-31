"""Resumable, read-only discovery of recently active Slack conversations."""
import datetime
import json
import math
import time
from pathlib import Path

from . import state


CENSUS_VERSION = 1
IGNORABLE_CONVERSATION_ERRORS = {
    # Slack can retain stale DM/channel rows in users.conversations after the
    # conversation is no longer readable. They are not evidence that the
    # remainder of the fixed-window census is incomplete.
    "channel_not_found",
    "is_archived",
    "not_in_channel",
}


def fatal_errors(errors):
    """Errors that make a completed census unsafe to consume."""
    return [
        row for row in errors
        if row.get("error") not in IGNORABLE_CONVERSATION_ERRORS
    ]


def _checkpoint_error(path, detail):
    raise RuntimeError(f"cannot resume Slack census from {path}: {detail}")


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_checkpoint(path, data):
    required = {
        "cutoff_epoch", "cutoff_at", "inventory",
        "next_index", "active", "errors",
    }
    missing = sorted(required - set(data))
    if missing:
        _checkpoint_error(path, f"missing field(s): {', '.join(missing)}")
    if not _finite_number(data["cutoff_epoch"]):
        _checkpoint_error(path, "cutoff_epoch must be a finite number")
    if not isinstance(data["cutoff_at"], str) or not data["cutoff_at"]:
        _checkpoint_error(path, "cutoff_at must be a non-empty string")
    has_until_epoch = "until_epoch" in data
    has_until_at = "until_at" in data
    if has_until_epoch != has_until_at:
        _checkpoint_error(
            path, "until_epoch and until_at must either both be present or absent"
        )
    if has_until_epoch:
        if not _finite_number(data["until_epoch"]):
            _checkpoint_error(path, "until_epoch must be a finite number")
        if float(data["until_epoch"]) < float(data["cutoff_epoch"]):
            _checkpoint_error(path, "until_epoch must not precede cutoff_epoch")
    if has_until_at and (
        not isinstance(data["until_at"], str) or not data["until_at"]
    ):
        _checkpoint_error(path, "until_at must be a non-empty string")

    inventory = data["inventory"]
    if not isinstance(inventory, list):
        _checkpoint_error(path, "inventory must be a list")
    inventory_ids = []
    for index, row in enumerate(inventory):
        if not isinstance(row, dict):
            _checkpoint_error(path, f"inventory[{index}] must be an object")
        item_id = row.get("id")
        if not isinstance(item_id, str) or not item_id:
            _checkpoint_error(
                path, f"inventory[{index}].id must be a non-empty string"
            )
        inventory_ids.append(item_id)
    if len(set(inventory_ids)) != len(inventory_ids):
        _checkpoint_error(path, "inventory contains duplicate conversation IDs")

    next_index = data["next_index"]
    if not isinstance(next_index, int) or isinstance(next_index, bool):
        _checkpoint_error(path, "next_index must be an integer")
    if not 0 <= next_index <= len(inventory):
        _checkpoint_error(
            path, f"next_index must be between 0 and {len(inventory)}"
        )
    processed_ids = set(inventory_ids[:next_index])
    result_ids = set()
    for field in ("active", "errors"):
        rows = data[field]
        if not isinstance(rows, list):
            _checkpoint_error(path, f"{field} must be a list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                _checkpoint_error(path, f"{field}[{index}] must be an object")
            item_id = row.get("id")
            if not isinstance(item_id, str) or not item_id:
                _checkpoint_error(
                    path, f"{field}[{index}].id must be a non-empty string"
                )
            if item_id not in processed_ids:
                _checkpoint_error(
                    path,
                    f"{field}[{index}].id is outside the completed prefix",
                )
            if item_id in result_ids:
                _checkpoint_error(
                    path, f"conversation {item_id} has duplicate results"
                )
            result_ids.add(item_id)
    if "completed_at" in data:
        if not isinstance(data["completed_at"], str) or not data["completed_at"]:
            _checkpoint_error(path, "completed_at must be a non-empty string")
        if next_index != len(inventory):
            _checkpoint_error(
                path, "completed_at requires every inventory item to be checked"
            )


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
    _validate_checkpoint(path, data)
    return data


def load_resumable_checkpoint(path):
    """Return only an interrupted census; completed runs are final artifacts."""
    data = load_checkpoint(path)
    if data and not data.get("completed_at"):
        return data
    return None


def _save_checkpoint(path, data):
    if path:
        state.write_atomic(
            Path(path),
            json.dumps(data, indent=2, sort_keys=True) + "\n",
            mode=0o600,
        )


def _iso_from_epoch(value):
    return datetime.datetime.fromtimestamp(
        float(value), datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    conversations,
    api,
    cutoff_epoch,
    until_epoch=None,
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
    existing = load_resumable_checkpoint(checkpoint)

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
        if "until_epoch" not in data:
            raise RuntimeError(
                "cannot resume Slack census: interrupted checkpoint predates "
                "fixed-window snapshots; choose a new checkpoint path"
            )
        if [row["id"] for row in data.get("inventory", [])] != [
            row["id"] for row in inventory
        ]:
            raise RuntimeError(
                "Slack conversation inventory changed since the checkpoint; "
                "choose a new checkpoint path"
            )
    else:
        until_epoch = (
            float(until_epoch)
            if until_epoch is not None
            else datetime.datetime.now(datetime.timezone.utc).timestamp()
        )
        if not _finite_number(cutoff_epoch) or not _finite_number(until_epoch):
            raise ValueError("census boundaries must be finite numbers")
        if until_epoch < float(cutoff_epoch):
            raise ValueError("census upper bound must not precede its cutoff")
        data = {
            "version": CENSUS_VERSION,
            "cutoff_epoch": float(cutoff_epoch),
            "cutoff_at": _iso_from_epoch(cutoff_epoch),
            "until_epoch": until_epoch,
            "until_at": _iso_from_epoch(until_epoch),
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
                    "latest": f"{float(data['until_epoch']):.6f}",
                    "inclusive": "false",
                    "limit": 1,
                },
            )
        except Exception as exc:
            error = getattr(exc, "error", None) or str(exc)
            if error == "ratelimited":
                retry_after = getattr(exc, "retry_after", None)
                wait_seconds = (
                    retry_after
                    if isinstance(retry_after, int) and retry_after > 0
                    else 60
                )
                progress(
                    f"Slack census rate-limited at {index}/{total}; "
                    f"waiting {wait_seconds}s and retrying"
                )
                sleep(wait_seconds)
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
