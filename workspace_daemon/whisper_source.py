"""Read-only Whisper pipeline transcript ingestion with Calendar matching.

The speech-to-text pipeline (speech-to-text-tools) drops finished transcripts
into one output directory as ``{base}-{lang}[-diarized].txt`` files, where
``base`` is frozen at enqueue time as ``{queued-at}-{sanitized-name}-{hash6}``.
Google Meet recordings additionally carry the meeting's own local start time
inside the sanitized name (``{title}---{YYYY_MM_DD}-{HH_MM}-{TZ}--Recording``);
that embedded instant — never the transcription time — anchors Calendar
matching, because a recording may be transcribed days after the meeting.

This adapter never moves or edits pipeline outputs.  Runtime outcomes are
written under the daemon's own ``state/transcriptions/{processed,failed}``
directories, sharing the Mila receipt protocol.

Candidate identity has two layers, mirroring the Mila source:

* candidate id: ``whisper:<identity>@<capture-hash>`` — a re-transcription or
  late-arriving diarized variant is reconsidered;
* memory source id: ``whisper:<identity>`` — corrections update the same
  memory instead of creating duplicates.  For Meet recordings the identity is
  the embedded meeting instant plus title, so re-running the same recording
  through the pipeline (new queue timestamp, new path hash) still lands on the
  same memory.
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import transcripts
from .chat_text import redact_secrets
from .shell import gws_bin, run, run_json, yoetz_bin


_VARIANT = re.compile(
    r"^(?P<base>.+)-(?P<lang>he|en)(?P<diarized>-diarized)?\.txt$"
)
# The queue stamps seconds since 2026-08; older archives used minute precision.
_PROC_STAMP = re.compile(
    r"^(?P<stamp>20\d{2}-\d{2}-\d{2}-\d{2}-\d{2}(?:-\d{2})?)-(?P<name>.+)$"
)
_PATH_HASH = re.compile(r"^(?P<name>.+)-(?P<hash>[0-9a-f]{6})$")
# Sanitized Google Meet recording names keep the meeting's local start time:
# "Value-reflection---2026_02_18-13_03-IST---Recording".  Observed archive
# variants: dashes or underscores inside the date and time, two or three
# dashes before "Recording", a surviving en/em dash from the original
# " – Recording" suffix, and a numeric copy suffix from a duplicate Drive
# download ("Recording (2)") — the same meeting, so the same identity.
_MEET_NAME = re.compile(
    r"^(?P<title>.+?)---"
    r"(?P<year>20\d{2})[-_](?P<month>\d{2})[-_](?P<day>\d{2})-"
    r"(?P<hour>\d{2})[-_](?P<minute>\d{2})-(?P<tz>[A-Z]{2,5})"
    r"[-–—]{1,4}Recording(?:-\d{1,2})?$"
)
_SLUG = re.compile(r"[^a-z0-9\u0590-\u05ff]+")

# Preferred transcript variant, best first: diarized keeps speaker turns, and
# Hebrew is the original language of most meetings this pipeline handles.
_VARIANT_PREFERENCE = [
    ("he", True), ("he", False), ("en", True), ("en", False),
]


def ffprobe_bin():
    """Best-effort ffprobe; duration matching degrades gracefully without it."""
    override = os.environ.get("WORKSPACE_DAEMON_FFPROBE_BIN")
    if override:
        return override
    return shutil.which("ffprobe")


def _probe_duration(audio_path):
    binary = ffprobe_bin()
    if not binary or not Path(audio_path).is_file():
        return None
    try:
        result = run([
            binary, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(audio_path),
        ], timeout=60)
        duration = float(
            json.loads(result.stdout)["format"]["duration"]
        )
    except Exception:
        return None
    return duration if duration > 0 else None


def _slug(value):
    return _SLUG.sub("-", str(value).casefold()).strip("-")


def _humanize(value):
    """Best-effort display title from a sanitized filename fragment."""
    return re.sub(r"[-_]+", " ", str(value)).strip()


def _parse_base(base, zone):
    """Split one frozen output base name into its provenance fields."""
    stamped = _PROC_STAMP.match(base)
    if not stamped:
        raise RuntimeError(
            f"whisper output base {base!r} has no queue timestamp"
        )
    stamp = stamped.group("stamp")
    stamp_format = (
        "%Y-%m-%d-%H-%M-%S" if stamp.count("-") == 5 else "%Y-%m-%d-%H-%M"
    )
    queued_at = datetime.datetime.strptime(stamp, stamp_format).replace(
        tzinfo=zone
    )
    name = stamped.group("name")
    hashed = _PATH_HASH.match(name)
    if hashed:
        name = hashed.group("name")
    meet = _MEET_NAME.match(name)
    if meet:
        start = datetime.datetime(
            int(meet.group("year")), int(meet.group("month")),
            int(meet.group("day")),
            int(meet.group("hour")), int(meet.group("minute")),
            tzinfo=zone,
        )
        title = _humanize(meet.group("title"))
        identity = (
            f"whisper:meet:{start.strftime('%Y-%m-%dT%H-%M')}:"
            f"{_slug(meet.group('title'))}"
        )
        origin = "meet"
    else:
        start = queued_at
        title = _humanize(name)
        identity = f"whisper:file:{_slug(name)}"
        origin = "file"
    return {
        "queued_at": queued_at,
        "name": name,
        "title": title,
        "identity": identity,
        "origin": origin,
        "recording_start": start.astimezone(datetime.timezone.utc),
    }


def _pick_variant(variants):
    by_key = {
        (match.group("lang"), bool(match.group("diarized"))): path
        for match, path in variants
    }
    for key in _VARIANT_PREFERENCE:
        if key in by_key:
            return by_key[key]
    raise RuntimeError("no readable transcript variant")


def _utc_label(instant):
    return instant.isoformat().replace("+00:00", "Z") if instant else None


def _candidate(source, base, variants, zone):
    parsed = _parse_base(base, zone)
    transcript_path = _pick_variant(variants)
    body = transcript_path.read_text(encoding="utf-8").strip()
    if not body:
        raise RuntimeError(
            f"whisper transcript {transcript_path.name} is empty"
        )
    duration = _probe_duration(transcript_path.parent / f"{base}.m4a")
    end = (
        parsed["recording_start"] + datetime.timedelta(seconds=duration)
        if duration else None
    )
    transcript_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    capture = {
        "transcript_hash": transcript_hash,
        "transcript_name": transcript_path.name,
        "title": parsed["title"],
        "recording_start": _utc_label(parsed["recording_start"]),
        "duration": duration,
        "origin": parsed["origin"],
    }
    digest = hashlib.sha256(
        json.dumps(
            capture, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "id": f"{parsed['identity']}@{digest}",
        "title": parsed["title"],
        "raw": {
            "source_id": parsed["identity"],
            "base_name": base,
            "origin": parsed["origin"],
            "transcript": body,
            "transcript_path": str(transcript_path),
            "content_hash": digest,
            "transcript_hash": transcript_hash,
            "recording_start": _utc_label(parsed["recording_start"]),
            "recording_end": _utc_label(end),
            "duration": duration,
            "queued_at": _utc_label(
                parsed["queued_at"].astimezone(datetime.timezone.utc)
            ),
        },
    }


def candidates(source):
    """One candidate per finished recording, newest first.

    Enumeration is pattern-scoped: only ``{queue-stamp}-…-{lang}.txt`` files
    are pipeline outputs, so the connector stays safe even when the output
    directory is shared with other applications' files.
    """
    directory = Path(source["transcriptions_dir"])
    if not directory.is_dir():
        raise RuntimeError(
            f"whisper transcriptions directory does not exist: {directory}"
        )
    zone = ZoneInfo(source.get("calendar_timezone", "UTC"))
    quiet = float(source.get("min_quiet_seconds", 300))
    now = time.time()
    excluded = {str(value) for value in source.get("exclude_base_names") or []}

    groups = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        match = _VARIANT.match(path.name)
        if not match or not _PROC_STAMP.match(match.group("base")):
            continue
        groups.setdefault(match.group("base"), []).append((match, path))

    found = {}
    for base, variants in sorted(groups.items()):
        if base in excluded:
            continue
        try:
            if any(
                now - path.stat().st_mtime < quiet for _match, path in variants
            ):
                # The worker may still be writing sibling variants; a settled
                # group is picked up unchanged on the next scheduled run.
                continue
            candidate = _candidate(source, base, variants, zone)
        except Exception as exc:
            # Keep one item-level failure visible without letting a single
            # malformed output hide every other recording.
            digest = hashlib.sha256(
                f"{base}:{exc}".encode("utf-8")
            ).hexdigest()[:16]
            try:
                parsed = _parse_base(base, zone)
                source_id = parsed["identity"]
                title = parsed["title"]
                start = _utc_label(parsed["recording_start"])
            except Exception:
                source_id = f"whisper:file:{_slug(base)}"
                title = base
                start = None
            candidate = {
                "id": f"{source_id}@error-{digest}",
                "title": title,
                "raw": {
                    "source_id": source_id,
                    "base_name": base,
                    "transcript_error": str(exc),
                    "content_hash": digest,
                    "recording_start": start,
                    "recording_end": None,
                },
            }
        source_id = candidate["raw"]["source_id"]
        previous = found.get(source_id)
        # The same recording re-run through the pipeline gets a fresh queue
        # stamp and path hash; keep only the newest output base per identity.
        if previous is None or (
            previous["raw"].get("base_name") or ""
        ) < (candidate["raw"].get("base_name") or ""):
            found[source_id] = candidate

    ordered = sorted(
        found.values(),
        key=lambda item: item["raw"].get("recording_start") or "",
        reverse=True,
    )
    limit = int(source.get("max_results", 0))
    return ordered[:limit] if limit else ordered


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


def _calendar_candidates(source, title, recording_start, recording_end):
    horizon = float(source.get("calendar_match_hours", 6)) * 3600
    recording_tokens = transcripts.title_tokens(title)
    ranked = []
    for event in _raw_calendar_events(source, recording_start):
        event_id = event.get("id")
        if (
            not isinstance(event_id, str)
            or not event_id.strip()
            or event.get("status") == "cancelled"
            or event.get("all_day")
            or event.get("response_status") == "declined"
            or event.get("event_type", "default") != "default"
        ):
            continue
        event_start = transcripts.event_instant(event.get("start"))
        event_end = transcripts.event_instant(event.get("end"))
        if not event_start or not event_end:
            continue
        start_gap = abs((event_start - recording_start).total_seconds())
        if recording_end:
            overlap = max(
                0.0,
                (
                    min(recording_end, event_end)
                    - max(recording_start, event_start)
                ).total_seconds(),
            )
            duration_gap = abs(
                (event_end - event_start).total_seconds()
                - (recording_end - recording_start).total_seconds()
            )
        else:
            # Unknown duration: treat "the recording starts inside the event"
            # as the overlap signal and stay neutral on duration.
            overlap = 1.0 if event_start <= recording_start <= event_end else 0.0
            duration_gap = 0.0
        if not overlap and start_gap > horizon:
            continue
        title_overlap = len(
            recording_tokens & transcripts.title_tokens(event.get("summary"))
        )
        rank = (
            0 if overlap else 1,
            -overlap,
            start_gap,
            duration_gap,
            -title_overlap,
        )
        ranked.append((rank, {
            "id": event_id,
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
    start = transcripts.parse_instant(raw["recording_start"])
    end = (
        transcripts.parse_instant(raw["recording_end"])
        if raw.get("recording_end") else None
    )
    events = _calendar_candidates(source, candidate["title"], start, end)
    timezone = source.get("calendar_timezone", "UTC")
    local_date = start.astimezone(ZoneInfo(timezone)).date().isoformat()
    return {
        "id": candidate["id"],
        "source_id": raw["source_id"],
        "source_kind": "whisper",
        "title": candidate["title"],
        "date": local_date,
        # Transcripts can contain dictated credentials or pasted tokens.
        # Redact before either the Calendar matcher or the summarizer sees
        # the text; the pipeline's own files stay untouched.
        "body": redact_secrets(raw["transcript"]),
        "_whisper_calendar_candidates": events,
        "frontmatter": {
            "whisper_base_name": raw["base_name"],
            "whisper_origin": raw["origin"],
            "whisper_transcript_file": Path(raw["transcript_path"]).name,
            "whisper_recording_start": raw["recording_start"],
            "whisper_recording_end": raw.get("recording_end"),
            "whisper_duration_seconds": raw.get("duration"),
            "whisper_queued_at": raw.get("queued_at"),
            "whisper_content_hash": raw["content_hash"],
            "calendar_candidate_count": len(events),
        },
    }


def _match_prompt(item, events):
    meta = item["frontmatter"]
    metadata = {
        "recording_title": item.get("title"),
        "recording_start": meta["whisper_recording_start"],
        "recording_end": meta["whisper_recording_end"],
        "duration_seconds": meta["whisper_duration_seconds"],
        # A locally dropped file carries only its transcription-queue time,
        # which may trail the meeting by hours or days.  Tell the matcher so
        # it demands stronger title/content evidence for those.
        "start_is_exact_meeting_time": meta["whisper_origin"] == "meet",
    }
    return transcripts.match_prompt(metadata, events, item["body"][:5000])


def match_calendar(routine, source, item):
    """Return ``(accepted, match)``; only a validated high match is accepted."""
    events = [
        event
        for event in item.get("_whisper_calendar_candidates") or []
        if (
            isinstance(event.get("id"), str)
            and bool(event["id"].strip())
        )
    ]
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
    match = transcripts.extract_json(result.get("content"))
    accepted, match = transcripts.validate_match(
        match, {event["id"] for event in events}
    )
    if not accepted:
        return False, match
    event = next(
        event for event in events if event.get("id") == match["event_id"]
    )
    transcripts.accept_event(
        item, event, match["confidence"], match["reason"]
    )
    return True, match


def dry_run_description(item):
    return (
        f"would ask Yoetz to select among "
        f"{len(item.get('_whisper_calendar_candidates') or [])} Calendar "
        "candidate(s); only a high-confidence match would be sent for "
        "memory analysis"
    )


def write_receipt(base_dir, status_name, item, details):
    # One receipt per stable source identity: a re-transcription resolves the
    # old failed receipt instead of leaving permanent stale attention behind.
    identity = item.get("source_id") or item["id"]
    payload = {
        "base_name": item.get("frontmatter", {}).get("whisper_base_name"),
        "candidate_id": item.get("id"),
        "source_id": item.get("source_id"),
        "content_hash": item.get("frontmatter", {}).get("whisper_content_hash"),
        "recording_start": item.get("frontmatter", {}).get(
            "whisper_recording_start"
        ),
        **details,
    }
    return transcripts.write_receipt_payload(
        base_dir, status_name, identity, payload,
        f"transcript={payload['base_name']}",
    )


def error_receipt_item(candidate):
    """Receipt-shaped stand-in for a candidate that failed before fetch."""
    raw = candidate.get("raw") or {}
    return {
        "id": candidate["id"],
        "source_id": raw.get("source_id"),
        "frontmatter": {
            "whisper_base_name": raw.get("base_name"),
            "whisper_content_hash": raw.get("content_hash"),
            "whisper_recording_start": raw.get("recording_start"),
        },
    }
