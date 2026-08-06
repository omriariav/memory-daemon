"""Shared machinery for transcript sources (Mila, Whisper).

Both connectors ingest meeting transcripts whose memory-worthiness is gated by
an unambiguous Google Calendar match.  The identity-and-versioning models
differ per source, but the Calendar match contract, its validation, the
accepted-event bookkeeping, and the receipt files are one shared protocol so a
gate fix cannot land in one connector and silently miss the other.
"""
import datetime
import json
import re
from pathlib import Path

from . import state
from .shell import log, utc_now_iso


MATCH_CONFIDENCE = {"high", "medium", "low"}

_SAFE_RECEIPT = re.compile(r"[^A-Za-z0-9._-]+")

RECEIPT_STATUSES = {"processed", "failed"}


def parse_instant(value):
    raw = str(value or "")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid transcript timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def event_instant(value):
    """A datetime for timed events, None for date-only (all-day) values."""
    if not value or len(str(value)) == 10:
        return None
    return parse_instant(value)


def title_tokens(value):
    return {
        token
        for token in re.findall(r"[A-Za-z0-9\u0590-\u05ff]+", str(value).casefold())
        if len(token) > 1
    }


def extract_json(raw):
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"calendar matcher returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("calendar matcher must return one JSON object")
    return value


def match_prompt(metadata, events, excerpt):
    return (
        "Match this recording to exactly one supplied Google Calendar event. "
        "Use time overlap, duration, title, attendees, and the transcript excerpt. "
        "Do not invent an event and do not choose merely because an event is nearby. "
        "If the evidence is ambiguous, return matched=false. Confidence may be "
        "high, medium, or low; high means the identity is unambiguous enough for "
        "unattended memory capture.\n\n"
        "Return only JSON with this exact shape:\n"
        '{"matched":true|false,"event_id":"id or null",'
        '"confidence":"high|medium|low","reason":"short explanation"}\n\n'
        f"Recording metadata:\n{json.dumps(metadata, ensure_ascii=False)}\n\n"
        f"Calendar candidates:\n{json.dumps(events, ensure_ascii=False)}\n\n"
        "Transcript excerpt:\n"
        f"{excerpt}"
    )


def validate_match(match, event_ids):
    """Return ``(accepted, normalized_match)``; raise on contract violations."""
    confidence = str(match.get("confidence") or "").casefold()
    if confidence not in MATCH_CONFIDENCE:
        raise RuntimeError(
            f"calendar matcher returned invalid confidence {confidence!r}"
        )
    event_id = match.get("event_id")
    if match.get("matched") is True and (
        not isinstance(event_id, str) or not event_id.strip()
    ):
        raise RuntimeError(
            "calendar matcher returned matched=true without a non-empty event_id"
        )
    if event_id is not None and event_id not in event_ids:
        raise RuntimeError(
            f"calendar matcher selected an event outside the supplied candidates: "
            f"{event_id}"
        )
    accepted = (
        match.get("matched") is True
        and confidence == "high"
        and isinstance(event_id, str)
        and bool(event_id.strip())
        and event_id in event_ids
    )
    return accepted, {
        "matched": bool(match.get("matched")),
        "event_id": event_id,
        "confidence": confidence,
        "reason": str(match.get("reason") or "")[:500],
    }


def source_people(event):
    """Deduplicated organizer + attendees for memory person attribution."""
    people = []
    seen = set()
    organizer = event.get("organizer")
    if isinstance(organizer, str):
        organizer = {"email": organizer}
    for role, person in [
        ("calendar-organizer", organizer),
        *[("calendar-attendee", attendee) for attendee in event.get("attendees") or []],
    ]:
        if not isinstance(person, dict):
            continue
        email = str(person.get("email") or "").strip().casefold()
        if not email or email in seen:
            continue
        seen.add(email)
        people.append({
            "email": email,
            "name": str(person.get("display_name") or ""),
            "role": role,
        })
    return people


def accept_event(item, event, confidence, reason):
    """Adopt the matched event's identity onto the fetched item."""
    item["title"] = event.get("summary") or item["title"]
    item["date"] = str(event.get("start") or item["date"])[:10]
    item["frontmatter"].update({
        "calendar_event_id": event.get("id"),
        "calendar_event_title": event.get("summary") or "",
        "calendar_event_start": event.get("start"),
        "calendar_event_end": event.get("end"),
        "calendar_match_confidence": confidence,
        "calendar_match_reason": reason,
        "source_people": source_people(event),
    })


def receipt_path(base_dir, status_name, identity):
    safe = _SAFE_RECEIPT.sub("-", identity).strip("-")
    return (
        Path(base_dir)
        / "state"
        / "transcriptions"
        / status_name
        / f"{safe}.json"
    )


def write_receipt_payload(base_dir, status_name, identity, payload, label):
    """Write the current-state receipt and drop the opposite-status one."""
    if status_name not in RECEIPT_STATUSES:
        raise ValueError(f"unknown transcription receipt status {status_name!r}")
    payload = {
        "status": status_name,
        "updated_at": utc_now_iso(),
        **payload,
    }
    target = receipt_path(base_dir, status_name, identity)
    state.write_atomic(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    opposite = receipt_path(
        base_dir,
        "failed" if status_name == "processed" else "processed",
        identity,
    )
    try:
        opposite.unlink()
    except FileNotFoundError:
        pass
    log(f"routine receipt={status_name} {label}")
    return target
