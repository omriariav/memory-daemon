"""Read-only Mila transcription ingestion with guarded Calendar matching.

Mila's recordings directory is a live application store.  This adapter never
moves or edits audio, transcript sidecars, ``recordings.json``, or
``folders.json``.  Runtime outcomes are written under the daemon's own
``state/transcriptions/{processed,failed}`` directories instead.

Candidate identity has two layers:

* candidate id: ``mila:<recording-uuid>@<content-hash>`` — a corrected
  transcript is reconsidered;
* memory source id: ``mila:<recording-uuid>`` — corrections update the same
  memory instead of creating duplicates.
"""
import datetime
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from . import state
from .shell import gws_bin, log, run_json, utc_now_iso, yoetz_bin


_CAPTURE_STAMP = re.compile(
    r"(?P<stamp>20\d{2}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)"
)
_SAFE_RECEIPT = re.compile(r"[^A-Za-z0-9._-]+")
_SRT_TIMING = re.compile(
    r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s+-->\s+"
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}"
)
_MATCH_CONFIDENCE = {"high", "medium", "low"}


def _read_json(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise RuntimeError(f"Mila metadata file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Mila metadata {path}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"Mila metadata {path} must contain a JSON array")
    return data


def _record_by_id(path, recording_id):
    matches = [
        record
        for record in _read_json(path)
        if str(record.get("id")) == str(recording_id)
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Mila metadata {path} contains {len(matches)} records for "
            f"{recording_id}"
        )
    return matches[0]


def _parse_instant(value):
    raw = str(value or "")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid Mila timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _recording_interval(record):
    """Return the actual capture interval, not the file-import time.

    Native Mila meeting filenames contain their start instant.  Mila's
    ``createdAt`` for those records is the completion time, so using it
    directly shifts Calendar matching by the full meeting duration.  Imported
    Voice Memos use their original creation time as the start.
    """
    duration = max(0.0, float(record.get("duration") or 0))
    source = str(record.get("source") or "")
    audio_name = str(record.get("audioFileName") or "")
    stamp = _CAPTURE_STAMP.search(audio_name)
    if stamp and source != "voiceMemo":
        start = datetime.datetime.strptime(
            stamp.group("stamp"), "%Y-%m-%dT%H-%M-%SZ"
        ).replace(tzinfo=datetime.timezone.utc)
    else:
        created = _parse_instant(record.get("createdAt"))
        start = created if source == "voiceMemo" else created - datetime.timedelta(
            seconds=duration
        )
    return start, start + datetime.timedelta(seconds=duration)


def _timestamp_label(seconds):
    seconds = max(0, int(float(seconds or 0)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _segments_text(record):
    names = record.get("speakerNames") or {}
    lines = []
    for segment in record.get("segments") or []:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        speaker_id = segment.get("speaker")
        speaker = names.get(speaker_id, speaker_id) if speaker_id else None
        prefix = f"{speaker}: " if speaker else ""
        lines.append(
            f"[{_timestamp_label(segment.get('start'))}] {prefix}{text}"
        )
    return "\n".join(lines)


def _srt_text(path):
    raw = Path(path).read_text(encoding="utf-8")
    lines = []
    for block in re.split(r"\r?\n\r?\n", raw.strip()):
        block_lines = [line.strip() for line in block.splitlines()]
        if block_lines and block_lines[0].isdigit():
            block_lines = block_lines[1:]
        if block_lines and _SRT_TIMING.match(block_lines[0]):
            timing = block_lines.pop(0).split("-->", 1)[0].strip()
        else:
            timing = ""
        text = " ".join(line for line in block_lines if line).strip()
        if text:
            lines.append(f"[{timing}] {text}" if timing else text)
    return "\n".join(lines)


def _transcript(record, directory, manual=None):
    manual = manual or {}
    preferred = manual.get("transcript_file")
    fallback = manual.get("fallback_file")
    if preferred:
        path = Path(preferred)
        if path.suffix.casefold() == ".srt":
            try:
                return _srt_text(path), str(path)
            except FileNotFoundError:
                pass
        elif path.exists():
            return path.read_text(encoding="utf-8").strip(), str(path)

    structured = _segments_text(record)
    if structured:
        return structured, str(Path(manual.get("recordings_file") or directory))

    candidates = []
    if fallback:
        candidates.append(Path(fallback))
    audio_name = str(record.get("audioFileName") or "")
    if audio_name:
        candidates.append(Path(directory) / Path(audio_name).with_suffix(".txt"))
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip(), str(path)
    raise RuntimeError(
        f"completed Mila recording {record.get('id')} has no readable transcript"
    )


def _candidate(record, directory, manual=None):
    body, transcript_path = _transcript(record, directory, manual)
    if not body.strip():
        raise RuntimeError(
            f"completed Mila recording {record.get('id')} has an empty transcript"
        )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    recording_id = str(record["id"])
    source_id = f"mila:{recording_id}"
    start, end = _recording_interval(record)
    return {
        "id": f"{source_id}@{digest}",
        "title": str(record.get("title") or Path(transcript_path).stem),
        "raw": {
            "source_id": source_id,
            "recording": record,
            "transcript": body,
            "transcript_path": transcript_path,
            "content_hash": digest,
            "recording_start": start.isoformat().replace("+00:00", "Z"),
            "recording_end": end.isoformat().replace("+00:00", "Z"),
        },
    }


def candidates(source):
    """List manual recordings first, then completed indexed Mila recordings."""
    found = {}
    for manual in source.get("manual_recordings") or []:
        metadata_file = manual.get("recordings_file") or source["recordings_file"]
        record = _record_by_id(metadata_file, manual["recording_id"])
        candidate = _candidate(record, Path(metadata_file).parent, manual)
        found[candidate["raw"]["source_id"]] = candidate

    excluded = {str(value) for value in source.get("exclude_recording_ids") or []}
    metadata_file = Path(source["recordings_file"])
    for record in _read_json(metadata_file):
        recording_id = str(record.get("id") or "")
        if (
            not recording_id
            or recording_id in excluded
            or record.get("status") != "completed"
            or record.get("deletedAt")
        ):
            continue
        source_id = f"mila:{recording_id}"
        if source_id in found:
            continue
        try:
            found[source_id] = _candidate(
                record, metadata_file.parent
            )
        except Exception as exc:
            # Keep one item-level failure visible without making a single
            # malformed sidecar prevent every other recording from running.
            start, end = _recording_interval(record)
            digest = hashlib.sha256(
                f"{recording_id}:{exc}".encode("utf-8")
            ).hexdigest()[:16]
            found[source_id] = {
                "id": f"{source_id}@error-{digest}",
                "title": str(record.get("title") or recording_id),
                "raw": {
                    "source_id": source_id,
                    "recording": record,
                    "transcript_error": str(exc),
                    "content_hash": digest,
                    "recording_start": start.isoformat().replace("+00:00", "Z"),
                    "recording_end": end.isoformat().replace("+00:00", "Z"),
                },
            }

    ordered = sorted(
        found.values(),
        key=lambda item: item["raw"].get("recording_start", ""),
        reverse=True,
    )
    limit = int(source.get("max_results", 0))
    return ordered[:limit] if limit else ordered


def _event_instant(value):
    if not value or len(str(value)) == 10:
        return None
    return _parse_instant(value)


def _raw_calendar_events(source, recording_start):
    timezone = source.get("calendar_timezone", "UTC")
    zone = ZoneInfo(timezone)
    local_day = recording_start.astimezone(zone).date()
    days = int(source.get("calendar_window_days", 3))
    from_day = local_day - datetime.timedelta(days=days // 2)
    result = run_json([
        gws_bin(), "calendar", "events",
        "--from", from_day.isoformat(),
        "--days", str(days),
        "--max", str(source.get("calendar_max_results", 250)),
        "--timezone", timezone,
        "--format", "json",
    ], timeout=120)
    return result.get("events") or []


def _title_tokens(value):
    return {
        token
        for token in re.findall(r"[A-Za-z0-9\u0590-\u05ff]+", str(value).casefold())
        if len(token) > 1
    }


def _calendar_candidates(source, record, recording_start, recording_end):
    horizon = float(source.get("calendar_match_hours", 6)) * 3600
    recording_duration = max(
        1.0, (recording_end - recording_start).total_seconds()
    )
    recording_tokens = _title_tokens(record.get("title"))
    ranked = []
    for event in _raw_calendar_events(source, recording_start):
        if (
            event.get("status") == "cancelled"
            or event.get("all_day")
            or event.get("response_status") == "declined"
            or event.get("event_type", "default") != "default"
        ):
            continue
        event_start = _event_instant(event.get("start"))
        event_end = _event_instant(event.get("end"))
        if not event_start or not event_end:
            continue
        overlap = max(
            0.0,
            (
                min(recording_end, event_end)
                - max(recording_start, event_start)
            ).total_seconds(),
        )
        start_gap = abs((event_start - recording_start).total_seconds())
        if not overlap and start_gap > horizon:
            continue
        event_duration = max(1.0, (event_end - event_start).total_seconds())
        title_overlap = len(
            recording_tokens & _title_tokens(event.get("summary"))
        )
        rank = (
            0 if overlap else 1,
            -overlap,
            start_gap,
            abs(event_duration - recording_duration),
            -title_overlap,
        )
        ranked.append((rank, {
            "id": event.get("id"),
            "summary": event.get("summary") or "",
            "start": event.get("start"),
            "end": event.get("end"),
            "organizer": event.get("organizer"),
            "creator": event.get("creator"),
            "attendees": event.get("attendees") or [],
            "response_status": event.get("response_status"),
        }))
    ranked.sort(key=lambda pair: pair[0])
    limit = int(source.get("max_calendar_candidates", 8))
    return [event for _rank, event in ranked[:limit]]


def fetch(_routine, source, candidate):
    raw = candidate["raw"]
    if raw.get("transcript_error"):
        raise RuntimeError(raw["transcript_error"])
    record = raw["recording"]
    start = _parse_instant(raw["recording_start"])
    end = _parse_instant(raw["recording_end"])
    events = _calendar_candidates(source, record, start, end)
    timezone = source.get("calendar_timezone", "UTC")
    local_date = start.astimezone(ZoneInfo(timezone)).date().isoformat()
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],
        "source_kind": "mila",
        "title": candidate["title"],
        "date": local_date,
        "body": raw["transcript"],
        "_mila_calendar_candidates": events,
        "frontmatter": {
            "mila_recording_id": str(record["id"]),
            "mila_recording_source": record.get("source"),
            "mila_recording_start": raw["recording_start"],
            "mila_recording_end": raw["recording_end"],
            "mila_duration_seconds": float(record.get("duration") or 0),
            "mila_content_hash": raw["content_hash"],
            "calendar_candidate_count": len(events),
        },
    }


def _extract_json(raw):
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"calendar matcher returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("calendar matcher must return one JSON object")
    return value


def _match_prompt(item, events):
    metadata = {
        "recording_title": item.get("title"),
        "recording_start": item["frontmatter"]["mila_recording_start"],
        "recording_end": item["frontmatter"]["mila_recording_end"],
        "duration_seconds": item["frontmatter"]["mila_duration_seconds"],
    }
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
        f"{item['body'][:5000]}"
    )


def match_calendar(routine, source, item):
    """Return ``(accepted, match)``; only a validated high match is accepted."""
    events = item.get("_mila_calendar_candidates") or []
    if not events:
        return False, {
            "matched": False,
            "event_id": None,
            "confidence": "low",
            "reason": "no plausible calendar candidates",
        }
    cfg = routine["analyze"]
    result = run_json([
        yoetz_bin(), "ask",
        "-p", _match_prompt(item, events),
        "--provider", cfg["provider"],
        "--model", cfg["model"],
        "--max-output-tokens", str(source.get("match_max_output_tokens", 2048)),
        "--format", "json",
    ], timeout=300)
    match = _extract_json(result.get("content"))
    confidence = str(match.get("confidence") or "").casefold()
    if confidence not in _MATCH_CONFIDENCE:
        raise RuntimeError(
            f"calendar matcher returned invalid confidence {confidence!r}"
        )
    event_ids = {event.get("id") for event in events}
    event_id = match.get("event_id")
    if event_id is not None and event_id not in event_ids:
        raise RuntimeError(
            f"calendar matcher selected an event outside the supplied candidates: "
            f"{event_id}"
        )
    accepted = (
        match.get("matched") is True
        and confidence == "high"
        and event_id in event_ids
    )
    match = {
        "matched": bool(match.get("matched")),
        "event_id": event_id,
        "confidence": confidence,
        "reason": str(match.get("reason") or "")[:500],
    }
    if not accepted:
        return False, match

    event = next(event for event in events if event.get("id") == event_id)
    source_people = []
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
        source_people.append({
            "email": email,
            "name": str(person.get("display_name") or ""),
            "role": role,
        })

    item["title"] = event.get("summary") or item["title"]
    item["date"] = str(event.get("start") or item["date"])[:10]
    item["frontmatter"].update({
        "calendar_event_id": event_id,
        "calendar_event_title": event.get("summary") or "",
        "calendar_event_start": event.get("start"),
        "calendar_event_end": event.get("end"),
        "calendar_match_confidence": confidence,
        "calendar_match_reason": match["reason"],
        "source_people": source_people,
    })
    return True, match


def dry_run_description(item):
    return (
        f"would ask Yoetz to select among "
        f"{len(item.get('_mila_calendar_candidates') or [])} Calendar candidate(s); "
        "only a high-confidence match would be sent for memory analysis"
    )


def _receipt_path(base_dir, status_name, item_id):
    safe = _SAFE_RECEIPT.sub("-", item_id).strip("-")
    return (
        Path(base_dir)
        / "state"
        / "transcriptions"
        / status_name
        / f"{safe}.json"
    )


def write_receipt(base_dir, status_name, item, details):
    if status_name not in {"processed", "failed"}:
        raise ValueError(f"unknown transcription receipt status {status_name!r}")
    payload = {
        "status": status_name,
        "recording_id": item.get("frontmatter", {}).get("mila_recording_id"),
        "candidate_id": item.get("id"),
        "source_id": item.get("source_id"),
        "content_hash": item.get("frontmatter", {}).get("mila_content_hash"),
        "recording_start": item.get("frontmatter", {}).get("mila_recording_start"),
        "updated_at": utc_now_iso(),
        **details,
    }
    target = _receipt_path(base_dir, status_name, item["id"])
    state.write_atomic(
        target,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )
    opposite = _receipt_path(
        base_dir,
        "failed" if status_name == "processed" else "processed",
        item["id"],
    )
    try:
        opposite.unlink()
    except FileNotFoundError:
        pass
    log(f"routine receipt={status_name} recording={payload['recording_id']}")
    return target
