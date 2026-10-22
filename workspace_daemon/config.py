"""Routine discovery, loading, and validation."""
import datetime
import os
import re
from pathlib import Path
from string import Formatter
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .actions import VALID_ACTIONS  # single source of truth for action names
from .notes import FILENAME_FIELDS
from . import comms_inbox, slack_watch
from .time_utils import is_rfc3339_instant, rfc3339_key

REQUIRED_TOP_LEVEL = ["id", "analyze"]
VALID_SOURCE_KINDS = {
    "gmail", "gmail_mcp", "drive_docs", "slack", "slack_mcp", "gchat", "mila",
    "whisper",
}
VALID_ROUTINE_ROLES = {
    "general", "domain", "specialized", "partial", "maintenance",
}
VALID_MAINTENANCE_KINDS = {
    "google_tasks_sync", "slack_conversation_census", "calendar_cache",
    "fibery_tasks_inbound", "fibery_tasks_outbound",
    "slack_discovery",
}
MAINTENANCE_ALLOWED_KEYS = {
    "slack_conversation_census": {
        "kind", "checkpoint", "hours", "requests_per_minute",
        "thread_root_days",
    },
    "google_tasks_sync": {
        "kind", "checkpoint", "store", "tasklists",
        "outbound_tasklist", "outbound_since", "exclude_tags",
        "identity_exclude_people", "max_tasks", "reclassify_non_todo",
    },
    "slack_discovery": {
        "kind", "discovered", "watch", "search_tool", "self_user_id", "model",
        "active_days", "idle_days", "max_search_pages",
        "min_seconds_between_searches", "max_channels", "min_mentions",
    },
    "calendar_cache": {
        "kind", "calendar_id", "window_hours", "timezone", "owner_email",
        "internal_domains", "output", "skip_titles", "max_attendee_names",
        "model", "list_tool", "get_tool", "max_events", "max_details",
        "stale_after", "conference_hosts",
    },
    "fibery_tasks_inbound": {
        "kind", "document_secret", "read_tool", "model", "owner_slug",
        "tags", "checkpoint", "store", "import_done", "max_new_per_run",
        "max_changes_per_run", "outbound_checkpoint",
    },
    "fibery_tasks_outbound": {
        "kind", "document_secret", "read_tool", "write_tool", "write_mode",
        "model", "owner_slug", "checkpoint", "store", "outbound_since",
        "from_sources", "inbox_heading", "max_per_run", "exclude_tags",
    },
}
# Kinds that keep no checkpoint file; every other kind requires one.
MAINTENANCE_NO_CHECKPOINT = {"calendar_cache", "slack_discovery"}
# Kinds whose checkpoint path has a built-in default.
MAINTENANCE_DEFAULT_CHECKPOINT = {
    "fibery_tasks_inbound", "fibery_tasks_outbound",
}
# Before per-routine cadence existed, the launchd job ran hourly. Keep omitted
# schedules at that legacy frequency; new routines write their intended cadence
# explicitly, so installing `tick` never silently slows an existing routine.
DEFAULT_SCHEDULE = "1h"
_DURATION = re.compile(r"^(?P<count>[1-9]\d*)(?P<unit>[mhd])$")
_DURATION_SECONDS = {"m": 60, "h": 3600, "d": 86400}
_CLOCK = re.compile(r"^(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)$")
_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}
_ROUTINE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_GMAIL_TEMPORAL_OPERATOR = re.compile(
    r"(?<![A-Za-z0-9_])(?:newer_than|older_than|after|before)\s*:",
    flags=re.I,
)
HANDLER_OVERRIDE_KEYS = {"analyze", "output", "memory", "label", "streams"}
TOP_LEVEL_KEYS = {
    "id", "enabled", "description", "role", "schedule", "source", "sources",
    "handlers", "analyze", "output", "memory", "actions", "routing", "label",
    "streams", "maintenance", "budget", "_source_file",
}
ANALYZE_KEYS = {
    "provider", "model", "instruction", "instruction_from_connector",
    "instruction_extra", "connector_sweep", "max_output_tokens",
    "focus_domains", "pick_label", "skip_trivial", "trivial_max_chars",
    "trivial_extra_words", "local_triage",
}
OUTPUT_KEYS = {"vault_dir", "slug_prefix", "filename_template", "kind", "tags"}
SOURCE_KEYS = {
    "kind", "query", "queue_query", "exclude_query", "max_results", "actions",
    "read_thread", "handler",
    "self_forwarded_chat_followups", "catch_up", "catch_up_overlap",
    "catch_up_after", "channels", "ada_channels", "direct_channels",
    "private_channels", "include_mentions", "ada_days", "active_conversations",
    "owner_action_channels",
    "reply_roots_after", "spaces", "all_spaces", "hours", "batch_unthreaded",
    "batch_messages", "batch_messages_after", "max_per_space", "expand", "tabs",
    "session_gap_minutes", "watch", "people", "ignore_people",
    "mime_type", "name_contains", "recordings_file", "exclude_recording_ids",
    "manual_recordings", "calendar_timezone", "calendar_window_days",
    "max_calendar_candidates", "calendar_max_results", "match_max_output_tokens",
    "calendar_match_hours", "calendar", "match", "max_transcript_chars", "app_summary",
    "transcriptions_dir", "min_quiet_seconds", "exclude_base_names",
}


def _unsafe_filename_fragment(value):
    """Whether config-controlled filename text can change path location."""
    return ".." in value or "/" in value or "\\" in value


def analyze_cfg(routine):
    cfg = routine.get("analyze")
    return cfg if isinstance(cfg, dict) else {}


def has_sink(routine):
    """Whether an execution path has a non-empty configured sink block."""
    output = routine.get("output")
    memory = routine.get("memory")
    return bool(
        (isinstance(output, dict) and output)
        or (isinstance(memory, dict) and memory)
    )


def configured_labels(routine):
    """Every statically-configured Gmail label in a routine.

    The single source of truth for "does this routine apply labels from config".
    Validation and the runner both ask this; when they each computed it their own
    way they drifted, and a routine whose labels came only from `streams` ran
    with an empty catalog and rejected every one of its own valid labels.
    """
    names = []
    if routine.get("label"):
        names.append(routine["label"])
    stream_map = routine.get("streams")
    for cfg in stream_map.values() if isinstance(stream_map, dict) else []:
        if isinstance(cfg, dict) and cfg.get("label"):
            names.append(cfg["label"])
    return names


def routine_for_source(routine, source):
    """Return the execution profile selected by one deterministic source.

    A medium routine may list narrow source queries before its general source
    and attach a named ``handler`` to each narrow query. Candidate ownership is
    still resolved from source metadata before content reaches an LLM; this
    helper only overlays the prompt and sinks used after that deterministic
    choice. The routine id deliberately remains unchanged so scheduling,
    connector health, and the processed ledger stay medium-scoped.

    Mapping overrides are shallow. ``analyze`` has one important convenience:
    selecting an inline instruction replaces an inherited connector prompt (and
    vice versa), so a specialized handler cannot accidentally receive both.
    """
    handler_id = source.get("handler") if isinstance(source, dict) else None
    if not isinstance(handler_id, str) or not handler_id:
        return routine
    handlers = routine.get("handlers")
    if not isinstance(handlers, dict):
        return routine
    profile = handlers.get(handler_id)
    if not isinstance(profile, dict):
        return routine

    effective = dict(routine)
    effective["_handler_id"] = handler_id
    for key in ("analyze", "output", "memory"):
        if key not in profile:
            continue
        base = routine.get(key)
        override = profile[key]
        if not isinstance(override, dict):
            effective[key] = override
            continue
        merged = dict(base) if isinstance(base, dict) else {}
        if key == "analyze":
            if "instruction" in override:
                merged.pop("instruction_from_connector", None)
                merged.pop("instruction_extra", None)
                # Connector health belongs to the medium routine, not to the
                # specialized extraction profile selected for one item.
                merged.pop("connector_sweep", None)
            elif "instruction_from_connector" in override:
                merged.pop("instruction", None)
        merged.update(override)
        effective[key] = merged
    for key in ("label", "streams"):
        if key in profile:
            effective[key] = profile[key]
    return effective


def execution_routines(routine):
    """Effective per-source profiles used by one scheduled routine."""
    return [routine_for_source(routine, source) for source in sources(routine)]


def sources(routine):
    """Return the routine's source blocks in declared order.

    `source:` remains the compact, backwards-compatible form. New domain
    routines use `sources:` to combine transports under one prompt and sink.
    Validation rejects configurations that set both.
    """
    if isinstance(routine.get("sources"), list):
        return routine["sources"]
    source = routine.get("source")
    return [source] if isinstance(source, dict) else []


def is_maintenance(routine):
    """Whether this routine performs scheduled source maintenance only."""
    return "maintenance" in routine


def source_actions(routine, source):
    """Actions for one source block, with legacy routine-level fallback."""
    if "actions" in source:
        value = source.get("actions")
        return value if isinstance(value, list) else []
    value = routine.get("actions")
    return value if isinstance(value, list) else []


def _schedule_duration(routine, field, value):
    """Parse one schedule duration with a field-specific validation error."""
    rid = routine.get("id", "<missing id>")
    match = _DURATION.fullmatch(str(value))
    if not match:
        raise RoutineError(
            f"{rid}: {field} must look like '15m', '4h', or '1d' "
            f"(got {value!r})"
        )
    return int(match.group("count")) * _DURATION_SECONDS[match.group("unit")]


def _work_hours_settings(routine):
    """Return validated timezone-aware work-hours settings, or ``None``."""
    schedule = routine.get("schedule") or {}
    work_hours = schedule.get("work_hours")
    if work_hours is None:
        return None
    rid = routine.get("id", "<missing id>")
    if not isinstance(work_hours, dict):
        raise RoutineError(f"{rid}: schedule.work_hours must be a mapping")
    unknown = set(work_hours) - {
        "every", "days", "start", "end", "timezone",
    }
    if unknown:
        raise RoutineError(
            f"{rid}: schedule.work_hours has unknown key(s) "
            f"{', '.join(sorted(unknown))} "
            f"(valid: days, end, every, start, timezone)"
        )

    missing = [
        field
        for field in ("every", "days", "start", "end", "timezone")
        if field not in work_hours
    ]
    if missing:
        raise RoutineError(
            f"{rid}: schedule.work_hours missing field(s): "
            f"{', '.join(missing)}"
        )
    interval = _schedule_duration(
        routine, "schedule.work_hours.every", work_hours["every"]
    )
    days = work_hours["days"]
    if (
        not isinstance(days, list)
        or not days
        or any(not isinstance(day, str) or day not in _WEEKDAYS for day in days)
        or len(set(days)) != len(days)
    ):
        raise RoutineError(
            f"{rid}: schedule.work_hours.days must be a non-empty unique list "
            f"using {', '.join(_WEEKDAYS)}"
        )
    clocks = {}
    for field in ("start", "end"):
        value = work_hours[field]
        match = _CLOCK.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise RoutineError(
                f"{rid}: schedule.work_hours.{field} must be HH:MM "
                f"in 24-hour time (got {value!r})"
            )
        clocks[field] = datetime.time(
            int(match.group("hour")), int(match.group("minute"))
        )
    if clocks["start"] >= clocks["end"]:
        raise RoutineError(
            f"{rid}: schedule.work_hours.start must be earlier than end"
        )
    timezone = work_hours["timezone"]
    if not isinstance(timezone, str) or not timezone:
        raise RoutineError(
            f"{rid}: schedule.work_hours.timezone must be an IANA timezone"
        )
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RoutineError(
            f"{rid}: unknown schedule.work_hours.timezone {timezone!r}"
        ) from exc

    base_interval = _schedule_duration(
        routine,
        "schedule.every",
        schedule.get("every", DEFAULT_SCHEDULE),
    )
    if interval > base_interval:
        raise RoutineError(
            f"{rid}: schedule.work_hours.every must not be slower than "
            f"schedule.every"
        )
    return {
        "seconds": interval,
        "days": {_WEEKDAYS[day] for day in days},
        "start": clocks["start"],
        "end": clocks["end"],
        "zone": zone,
    }


def schedule_seconds(routine, now=None):
    """Configured cadence in seconds, optionally at a specific epoch."""
    every = (routine.get("schedule") or {}).get("every", DEFAULT_SCHEDULE)
    base_interval = _schedule_duration(routine, "schedule.every", every)
    settings = _work_hours_settings(routine)
    if settings is not None and now is not None:
        local = datetime.datetime.fromtimestamp(float(now), settings["zone"])
        local_time = local.time().replace(tzinfo=None)
        if (
            local.weekday() in settings["days"]
            and settings["start"] <= local_time < settings["end"]
        ):
            return settings["seconds"]
    return base_interval


def schedule_label(routine):
    """Compact human-readable base/work-hours cadence."""
    schedule = routine.get("schedule") or {}
    base = schedule.get("every", DEFAULT_SCHEDULE)
    work_hours = schedule.get("work_hours")
    if isinstance(work_hours, dict) and work_hours.get("every"):
        return f"{work_hours['every']} work / {base} off"
    return base


def next_due_epoch(routine, last_attempted_epoch, now):
    """Earliest due instant across base cadence and upcoming work windows."""
    now = float(now)
    last = float(last_attempted_epoch)
    if last > now:
        return now
    base_interval = schedule_seconds(routine)
    settings = _work_hours_settings(routine)
    if settings is None:
        return last + base_interval
    if now - last >= schedule_seconds(routine, now=now):
        return now

    candidates = [last + base_interval]
    local_now = datetime.datetime.fromtimestamp(now, settings["zone"])
    for offset in range(8):
        day = local_now.date() + datetime.timedelta(days=offset)
        if day.weekday() not in settings["days"]:
            continue
        start = datetime.datetime.combine(
            day, settings["start"], tzinfo=settings["zone"]
        ).timestamp()
        end = datetime.datetime.combine(
            day, settings["end"], tzinfo=settings["zone"]
        ).timestamp()
        candidate = max(now, start, last + settings["seconds"])
        if candidate < end:
            candidates.append(candidate)
    return min(candidate for candidate in candidates if candidate >= now)


def duration_seconds(value):
    """Parse a compact positive duration such as ``15m``, ``4h``, or ``1d``."""
    match = _DURATION.fullmatch(str(value))
    if not match:
        raise RoutineError(
            f"duration must look like '15m', '4h', or '1d' (got {value!r})"
        )
    return int(match.group("count")) * _DURATION_SECONDS[match.group("unit")]


def routing_rank(routine):
    """Specific routines beat fallbacks; lower explicit priority wins."""
    routing = routine.get("routing") or {}
    return (1 if routing.get("fallback", False) else 0, routing.get("priority", 100))


class RoutineError(Exception):
    pass


def routines_dir(base_dir):
    return Path(base_dir) / "routines"


def secure_routine_files(base_dir):
    """Make private runtime routines owner-only without touching examples."""
    directory = routines_dir(base_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        for path in directory.glob("*.yaml"):
            if not path.name.startswith("_"):
                path.chmod(0o600)
    except OSError as exc:
        raise RoutineError(
            f"cannot secure private routine configuration under {directory}: {exc}"
        ) from exc


def discover(base_dir, routine_id=None):
    """Load every routines/*.yaml (files starting with _ are templates, skipped).

    A malformed file raises RoutineError naming the file, never a bare YAML traceback.
    """
    found = []
    for path in sorted(routines_dir(base_dir).glob("*.yaml")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            raise RoutineError(f"{path.name}: invalid YAML — {exc}") from exc
        if not data:
            continue
        if not isinstance(data, dict):
            raise RoutineError(f"{path.name}: top level must be a mapping, got {type(data).__name__}")
        data["_source_file"] = str(path)
        data.setdefault("id", path.stem)
        expand_watch_files(data)
        found.append(data)

    seen = {}
    for routine in found:
        rid = routine["id"]
        if rid in seen:
            raise RoutineError(
                f"duplicate routine id '{rid}' in {Path(seen[rid]).name} "
                f"and {Path(routine['_source_file']).name}"
            )
        seen[rid] = routine["_source_file"]

    if routine_id:
        found = [r for r in found if r["id"] == routine_id]
        if not found:
            raise RoutineError(f"no routine with id '{routine_id}'")
    return found


def expand_watch_files(routine, missing_ok=False):
    """Apply each source's `watch:` file in place; idempotent.

    A failure is kept on the source as `_watch_error` for validate() to report.
    `missing_ok` (documentation examples) tolerates an absent file: the block
    is marked `_watch_unread` and validated without its watched ids.
    """
    if isinstance(routine.get("sources"), list):
        blocks = routine["sources"]
        for index, block in enumerate(blocks):
            if isinstance(block, dict):
                blocks[index] = _expanded(block, missing_ok)
    elif isinstance(routine.get("source"), dict):
        routine["source"] = _expanded(routine["source"], missing_ok)
    return routine


def _expanded(source, missing_ok=False):
    if "watch" not in source or source.get("_watch_done"):
        return source
    if missing_ok and not Path(str(source["watch"])).expanduser().exists():
        return dict(source, _watch_unread=True)
    merged, error = slack_watch.expand(source)
    merged = dict(merged, _watch_done=True)
    if error:
        merged["_watch_error"] = error
    return merged


def discovery_warnings(routines):
    """Cross-check watch `discover_from` paths against discovery routines.

    Warnings, not errors: the writer and the reader may live in another
    config set. Returns a list of messages.
    """
    reads, writes = {}, {}
    for routine in routines:
        for source in sources(routine):
            path = source.get("_watch_discover_from")
            if path and source.get("kind") == "slack_mcp":
                reads.setdefault(path, routine.get("id", "?"))
        maintenance = routine.get("maintenance")
        if (
            isinstance(maintenance, dict)
            and maintenance.get("kind") == "slack_discovery"
            and isinstance(maintenance.get("discovered"), str)
        ):
            writes.setdefault(
                str(Path(maintenance["discovered"]).expanduser()),
                routine.get("id", "?"),
            )
    out = [
        f"{rid}: watch discover_from is not written by any slack_discovery "
        "routine in this config set"
        for path, rid in sorted(reads.items()) if path not in writes
    ]
    out.extend(
        f"{rid}: slack_discovery writes a file no slack_mcp watch reads "
        "with discover_from"
        for path, rid in sorted(writes.items()) if path not in reads
    )
    return out


def budget_cap(routine):
    """The routine's budget.daily_usd, or None when unset or malformed."""
    budget = routine.get("budget")
    cap = budget.get("daily_usd") if isinstance(budget, dict) else None
    ok = isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0
    return cap if ok else None


def uses_claude_bridge(routine):
    """Whether any of the routine's work can spend through the Claude bridge.

    Global caps (and a broken budget file) only matter to such routines; a
    routine that never calls Claude has nothing to hold.
    """
    profiles = [routine]
    handlers = routine.get("handlers")
    if isinstance(handlers, dict):
        profiles.extend(h for h in handlers.values() if isinstance(h, dict))
    for profile in profiles:
        analyze = profile.get("analyze")
        if isinstance(analyze, dict) and analyze.get("provider") == "claude":
            return True
        memory = profile.get("memory")
        identity = memory.get("identity") if isinstance(memory, dict) else None
        if isinstance(identity, dict) and identity.get("provider") == "slack_mcp":
            return True
    maintenance = routine.get("maintenance")
    if isinstance(maintenance, dict) and maintenance.get("kind") in (
        "calendar_cache", "fibery_tasks_inbound", "fibery_tasks_outbound",
        "slack_discovery",
    ):
        return True
    return any(
        isinstance(source, dict) and source.get("kind") == "slack_mcp"
        for source in sources(routine)
    )


def _budget_problems(rid, routine):
    if "budget" not in routine:
        return []
    budget = routine["budget"]
    if not isinstance(budget, dict):
        return [f"{rid}: budget must be a mapping"]
    problems = []
    unknown = set(budget) - {"daily_usd"}
    if unknown:
        problems.append(
            f"{rid}: budget has unknown key(s) {', '.join(sorted(map(str, unknown)))}"
        )
    cap = budget.get("daily_usd")
    if (
        isinstance(cap, bool) or not isinstance(cap, (int, float))
        or not 0 < cap < float("inf")
    ):
        problems.append(f"{rid}: budget.daily_usd must be a positive number")
    return problems


# Relative state paths resolve against the repository root (the daemon's base
# directory), so `state/x.json` and its absolute spelling compare equal.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _norm_path(value):
    path = os.path.expanduser(str(value).strip())
    if not os.path.isabs(path):
        path = os.path.join(_REPO_ROOT, path)
    return os.path.normpath(path)


def cross_problems(routines):
    """Problems that need several routines: {routine id: [problem, ...]}.

    The inbound Fibery routine links `mem:` markers using the outbound
    routine's checkpoint, so its `outbound_checkpoint` must be that file.
    """
    outbound = {
        _norm_path(r["maintenance"].get("checkpoint", "state/fibery-outbound.json"))
        for r in routines
        if isinstance(r.get("maintenance"), dict)
        and r["maintenance"].get("kind") == "fibery_tasks_outbound"
    }
    found = {}
    if not outbound:
        return found
    for r in routines:
        m = r.get("maintenance")
        if not (isinstance(m, dict) and m.get("kind") == "fibery_tasks_inbound"):
            continue
        path = _norm_path(m.get("outbound_checkpoint", "state/fibery-outbound.json"))
        if path not in outbound:
            found.setdefault(r.get("id", "<missing id>"), []).append(
                f"{r.get('id', '<missing id>')}: maintenance.outbound_checkpoint "
                "must equal the fibery_tasks_outbound routine's checkpoint"
            )
    return found


def validate(routine):
    """Return a list of human-readable problems; empty means valid."""
    problems = []
    rid = routine.get("id", "<missing id>")
    unknown_top = set(routine) - TOP_LEVEL_KEYS
    if unknown_top:
        problems.append(
            f"{rid}: unknown top-level key(s) {', '.join(sorted(unknown_top))}"
        )
    if "_handler_id" in routine:
        problems.append(
            f"{rid}: `_handler_id` is reserved runtime metadata and cannot "
            "be configured"
        )
    maintenance_routine = is_maintenance(routine)
    problems.extend(_budget_problems(rid, routine))

    required = ["id"] if maintenance_routine else REQUIRED_TOP_LEVEL
    for key in required:
        if key not in routine:
            problems.append(f"{rid}: missing required top-level key '{key}'")
    if "id" in routine and (
        not isinstance(routine["id"], str)
        or not _ROUTINE_ID.fullmatch(routine["id"])
    ):
        problems.append(
            f"{rid}: id must use lowercase letters, digits, and hyphens "
            f"(got {routine['id']!r})"
        )
    role = routine.get("role")
    if role is not None and (
        not isinstance(role, str) or role not in VALID_ROUTINE_ROLES
    ):
        problems.append(
            f"{rid}: role must be one of "
            f"{', '.join(sorted(VALID_ROUTINE_ROLES))}"
        )
    elif role == "maintenance" and not maintenance_routine:
        problems.append(
            f"{rid}: role: maintenance requires a `maintenance` block"
        )

    if maintenance_routine:
        if role != "maintenance":
            problems.append(
                f"{rid}: maintenance routines require role: maintenance"
            )
        forbidden = [
            key for key in (
                "source", "sources", "analyze", "output", "memory",
                "actions", "routing",
            )
            if key in routine
        ]
        if forbidden:
            problems.append(
                f"{rid}: maintenance routines cannot set "
                f"{', '.join(sorted(forbidden))}"
            )
        maintenance = routine.get("maintenance")
        if not isinstance(maintenance, dict):
            problems.append(f"{rid}: `maintenance` must be a mapping")
        else:
            kind = maintenance.get("kind")
            # An unknown kind is reported below; fall back to the original
            # Google Tasks key set so that report stays unchanged.
            allowed = MAINTENANCE_ALLOWED_KEYS.get(
                kind, MAINTENANCE_ALLOWED_KEYS["google_tasks_sync"]
            )
            unknown = set(maintenance) - allowed
            if unknown:
                problems.append(
                    f"{rid}: maintenance has unknown key(s) "
                    f"{', '.join(sorted(unknown))}"
                )
            if kind not in VALID_MAINTENANCE_KINDS:
                problems.append(
                    f"{rid}: maintenance.kind must be one of "
                    f"{', '.join(sorted(VALID_MAINTENANCE_KINDS))}"
                )
            checkpoint = maintenance.get("checkpoint")
            if kind not in MAINTENANCE_NO_CHECKPOINT and (
                not isinstance(checkpoint, str) or not checkpoint
            ) and not (
                kind in MAINTENANCE_DEFAULT_CHECKPOINT
                and "checkpoint" not in maintenance
            ):
                problems.append(f"{rid}: maintenance.checkpoint is required")
            if kind == "fibery_tasks_inbound":
                problems.extend(_validate_fibery_tasks_inbound(rid, maintenance))
            elif kind == "fibery_tasks_outbound":
                problems.extend(_validate_fibery_tasks_outbound(rid, maintenance))
            elif kind == "calendar_cache":
                problems.extend(_validate_calendar_cache(rid, maintenance))
            elif kind == "slack_discovery":
                problems.extend(_validate_slack_discovery(rid, maintenance))
            elif kind == "slack_conversation_census":
                hours = maintenance.get("hours", 48)
                if (
                    not isinstance(hours, (int, float))
                    or isinstance(hours, bool)
                    or hours <= 0
                ):
                    problems.append(f"{rid}: maintenance.hours must be positive")
                rpm = maintenance.get("requests_per_minute", 40)
                if (
                    not isinstance(rpm, int)
                    or isinstance(rpm, bool)
                    or not 1 <= rpm <= 50
                ):
                    problems.append(
                        f"{rid}: maintenance.requests_per_minute must be an "
                        "integer from 1 to 50"
                    )
                root_days = maintenance.get("thread_root_days", 30)
                if (
                    not isinstance(root_days, (int, float))
                    or isinstance(root_days, bool)
                    or root_days <= 0
                ):
                    problems.append(
                        f"{rid}: maintenance.thread_root_days must be positive"
                    )
            elif kind == "google_tasks_sync":
                store = maintenance.get("store")
                if not isinstance(store, str) or not store.startswith("/"):
                    problems.append(
                        f"{rid}: maintenance.store must be an absolute path"
                    )
                tasklists = maintenance.get("tasklists", "all")
                if tasklists != "all" and (
                    not isinstance(tasklists, list)
                    or not tasklists
                    or any(not isinstance(value, str) or not value for value in tasklists)
                ):
                    problems.append(
                        f"{rid}: maintenance.tasklists must be 'all' or a "
                        "non-empty list of task-list ids"
                    )
                outbound = maintenance.get("outbound_tasklist")
                if not isinstance(outbound, str) or not outbound:
                    problems.append(
                        f"{rid}: maintenance.outbound_tasklist is required"
                    )
                elif isinstance(tasklists, list) and outbound not in tasklists:
                    problems.append(
                        f"{rid}: maintenance.outbound_tasklist must be included "
                        "in maintenance.tasklists"
                    )
                outbound_since = maintenance.get("outbound_since")
                if outbound_since is None:
                    problems.append(
                        f"{rid}: maintenance.outbound_since is required"
                    )
                else:
                    try:
                        parsed_since = datetime.date.fromisoformat(outbound_since)
                    except (TypeError, ValueError):
                        parsed_since = None
                    if parsed_since is None or parsed_since.isoformat() != outbound_since:
                        problems.append(
                            f"{rid}: maintenance.outbound_since must be YYYY-MM-DD"
                        )
                exclude_tags = maintenance.get("exclude_tags", [])
                if (
                    not isinstance(exclude_tags, list)
                    or any(
                        not isinstance(value, str)
                        or not _ROUTINE_ID.fullmatch(value)
                        for value in exclude_tags
                    )
                ):
                    problems.append(
                        f"{rid}: maintenance.exclude_tags must be kebab-case tags"
                    )
                excluded_people = maintenance.get("identity_exclude_people", [])
                if (
                    not isinstance(excluded_people, list)
                    or any(
                        not isinstance(value, str)
                        or not _ROUTINE_ID.fullmatch(value)
                        for value in excluded_people
                    )
                ):
                    problems.append(
                        f"{rid}: maintenance.identity_exclude_people must be "
                        "kebab-case person slugs"
                    )
                reclassify = maintenance.get("reclassify_non_todo", False)
                if not isinstance(reclassify, bool):
                    problems.append(
                        f"{rid}: maintenance.reclassify_non_todo must be a boolean"
                    )
                max_tasks = maintenance.get("max_tasks", 10000)
                if (
                    not isinstance(max_tasks, int)
                    or isinstance(max_tasks, bool)
                    or max_tasks < 1
                ):
                    problems.append(
                        f"{rid}: maintenance.max_tasks must be a positive integer"
                    )
        problems.extend(_validate_schedule(routine))
        return problems

    has_source = "source" in routine
    has_sources = "sources" in routine
    if has_source and has_sources:
        problems.append(f"{rid}: set `source` or `sources`, not both")
    source_list = sources(routine)
    if not has_source and not has_sources:
        problems.append(f"{rid}: missing required top-level key 'source' or 'sources'")
    if has_sources and (not isinstance(routine.get("sources"), list) or not source_list):
        problems.append(f"{rid}: `sources` must be a non-empty list")
    if has_source and not isinstance(routine.get("source"), dict):
        problems.append(f"{rid}: `source` must be a mapping")
    if len(source_list) > 1 and routine.get("actions"):
        problems.append(
            f"{rid}: multi-source routines put `actions` on each Gmail source block"
        )
    if "actions" in routine and not isinstance(routine.get("actions"), list):
        problems.append(f"{rid}: `actions` must be a list")

    source_dicts = []
    for index, source in enumerate(source_list):
        if not isinstance(source, dict):
            problems.append(f"{rid}: sources[{index}] must be a mapping")
            continue
        source_dicts.append(source)
        prefix = f"{rid}: source" if has_source else f"{rid}: sources[{index}]"
        problems.extend(
            _validate_source(routine_for_source(routine, source), source, prefix)
        )
    problems.extend(_validate_handlers(routine, source_dicts))
    if sum(
        source.get("self_forwarded_chat_followups") is True
        for source in source_dicts
    ) > 1:
        problems.append(
            f"{rid}: only one self_forwarded_chat_followups source is supported"
        )

    analyze = routine.get("analyze", {})
    if not isinstance(analyze, dict):
        problems.append(f"{rid}: `analyze` must be a mapping")
        analyze = {}
    unknown_analyze = set(analyze) - ANALYZE_KEYS
    if unknown_analyze:
        problems.append(
            f"{rid}: analyze has unknown key(s) "
            f"{', '.join(sorted(unknown_analyze))}"
        )
    for key in ("provider", "model"):
        if not analyze.get(key):
            problems.append(f"{rid}: analyze.{key} is required")
    # Whisper calendar matching still calls yoetz with analyze.provider; Mila
    # matches through source.match (rules + local model) instead.
    claude_blocked = sorted({
        s.get("kind") for s in sources(routine)
        if s.get("kind") == "whisper"
        or (s.get("kind") == "mila" and not s.get("match"))
    })
    if analyze.get("provider") == "claude" and claude_blocked:
        problems.append(
            f"{rid}: analyze.provider claude is not supported for "
            f"{', '.join(claude_blocked)} sources without a source.match "
            f"block (calendar matching would use yoetz)"
        )
    # The instruction may be inline or sourced from a store connector body.
    from . import connector_prompts
    problems.extend(connector_prompts.validate(routine))
    tokens = analyze.get("max_output_tokens", 4096)
    if not isinstance(tokens, int) or tokens < 1:
        problems.append(f"{rid}: analyze.max_output_tokens must be a positive integer")
    elif tokens < 2048:
        problems.append(
            f"{rid}: analyze.max_output_tokens={tokens} is too low — reasoning models "
            f"truncate mid-sentence below ~2048; use 4096"
        )
    skip_trivial = analyze.get("skip_trivial")
    if skip_trivial is not None and not isinstance(skip_trivial, bool):
        problems.append(f"{rid}: analyze.skip_trivial must be true or false")
    trivial_max = analyze.get("trivial_max_chars")
    if trivial_max is not None and (
        type(trivial_max) is not int or trivial_max < 1
    ):
        problems.append(
            f"{rid}: analyze.trivial_max_chars must be a positive integer"
        )
    extra_words = analyze.get("trivial_extra_words")
    if extra_words is not None and (
        not isinstance(extra_words, list)
        or any(not isinstance(w, str) or not w.strip() for w in extra_words)
    ):
        problems.append(
            f"{rid}: analyze.trivial_extra_words must be a list of strings"
        )
    from . import local_triage
    problems.extend(local_triage.validate(rid, analyze))
    if (
        isinstance(analyze.get("local_triage"), dict)
        and analyze["local_triage"].get("enabled") is True
        and any(s.get("kind") not in ("slack", "slack_mcp", "gchat")
                for s in source_dicts)
    ):
        problems.append(
            f"{rid}: analyze.local_triage applies to chat sources only "
            "(slack, slack_mcp, gchat)"
        )
    domains = analyze.get("focus_domains")
    if domains is not None and not isinstance(domains, list):
        problems.append(f"{rid}: analyze.focus_domains must be a list")
    if analyze.get("pick_label") and any(s.get("kind") != "gmail" for s in source_dicts):
        problems.append(
            f"{rid}: analyze.pick_label requires every source in the routine to be Gmail"
        )
    connector_sweep = analyze.get("connector_sweep")
    if connector_sweep is not None and not isinstance(connector_sweep, bool):
        problems.append(f"{rid}: analyze.connector_sweep must be true or false")
    if connector_sweep is True:
        connector = analyze.get("instruction_from_connector")
        if not connector:
            problems.append(
                f"{rid}: analyze.connector_sweep requires "
                f"analyze.instruction_from_connector"
            )
        elif connector not in VALID_SOURCE_KINDS:
            problems.append(
                f"{rid}: analyze.connector_sweep connector {connector!r} "
                f"does not match a supported source kind"
            )
        elif (
            not source_dicts
            or any(source.get("kind") != connector for source in source_dicts)
        ):
            problems.append(
                f"{rid}: analyze.connector_sweep requires every source block "
                f"to use {connector!r}"
            )
        elif (
            connector == "gchat"
            and (
                len(source_dicts) != 1
                or source_dicts[0].get("all_spaces") is not True
            )
        ):
            problems.append(
                f"{rid}: a gchat connector sweep requires exactly one source "
                "with all_spaces: true"
            )
        elif (
            connector == "gchat"
            and source_dicts[0].get("max_per_space") != 0
        ):
            problems.append(
                f"{rid}: a gchat connector sweep requires "
                f"source.max_per_space: 0"
            )
        elif (
            connector in {"gchat", "slack"}
            and any(source.get("catch_up") is not True for source in source_dicts)
        ):
            problems.append(
                f"{rid}: a {connector} connector sweep requires catch_up: true "
                "on every source"
            )
        elif (
            connector == "gmail"
            and not any(source.get("catch_up") is True for source in source_dicts)
        ):
            problems.append(
                f"{rid}: a gmail connector sweep requires at least one "
                "catch_up source for incremental incoming/outgoing coverage"
            )
        if source_dicts and any(
            source.get("max_results") != 0 for source in source_dicts
        ):
            problems.append(
                f"{rid}: analyze.connector_sweep requires max_results: 0 on "
                "every source so the declared sweep is not capped"
            )

    output = routine.get("output") or {}
    if not isinstance(output, dict):
        problems.append(f"{rid}: `output` must be a mapping")
        output = {}
    unknown_output = set(output) - OUTPUT_KEYS
    if unknown_output:
        problems.append(
            f"{rid}: output has unknown key(s) "
            f"{', '.join(sorted(unknown_output))}"
        )
    effective_routines = execution_routines(routine)
    missing_sinks = [
        (index, source.get("handler"))
        for index, (source, effective) in enumerate(
            zip(source_dicts, effective_routines)
        )
        if not has_sink(effective)
    ]
    if missing_sinks:
        multiple = len(source_dicts) > 1
        for index, handler_id in missing_sinks:
            owner = f"sources[{index}]" if multiple else "source"
            if handler_id:
                owner += f" handler {handler_id!r}"
            problems.append(
                f"{rid}: {owner} needs an `output:` block, a `memory:` "
                "block, or both"
            )
    elif not source_dicts and not has_sink(routine):
        problems.append(f"{rid}: needs an `output:` block, a `memory:` block, or both")
    if output:
        if not output.get("vault_dir"):
            problems.append(f"{rid}: output.vault_dir is required")
        elif not str(output["vault_dir"]).startswith("/"):
            problems.append(f"{rid}: output.vault_dir must be an absolute path")
        slug_prefix = output.get("slug_prefix")
        if not slug_prefix:
            problems.append(f"{rid}: output.slug_prefix is required")
        elif not isinstance(slug_prefix, str):
            problems.append(f"{rid}: output.slug_prefix must be a string")
        elif _unsafe_filename_fragment(slug_prefix):
            problems.append(
                f"{rid}: output.slug_prefix must be a filename component "
                "(no path separators or '..')"
            )
        if "tags" in output and (
            not isinstance(output["tags"], list)
            or any(not isinstance(tag, str) or not tag for tag in output["tags"])
        ):
            problems.append(f"{rid}: output.tags must be a list of strings")

    from . import memory_sink
    problems.extend(memory_sink.validate(routine))

    template = output.get("filename_template")
    if template is not None:
        allowed = FILENAME_FIELDS
        if not isinstance(template, str):
            problems.append(f"{rid}: output.filename_template must be a string")
        else:
            try:
                parsed = list(Formatter().parse(template))
            except ValueError as exc:
                problems.append(
                    f"{rid}: output.filename_template is malformed — {exc}"
                )
            else:
                replacement_fields = [
                    field for _, field, _, _ in parsed if field is not None
                ]
                if any(field == "" or field.isdecimal()
                       for field in replacement_fields):
                    problems.append(
                        f"{rid}: output.filename_template does not support "
                        "automatic or positional placeholders"
                    )
                fields = {field for field in replacement_fields if field}
                if any(
                    _unsafe_filename_fragment(literal)
                    for literal, _, _, _ in parsed
                ):
                    problems.append(
                        f"{rid}: output.filename_template literal text must not "
                        "contain path separators or '..'"
                    )
                if any(
                    format_spec or conversion
                    for _, field, format_spec, conversion in parsed
                    if field is not None
                ):
                    problems.append(
                        f"{rid}: output.filename_template does not support "
                        "format specifications or conversions"
                    )
                unknown = fields - allowed
                if unknown:
                    problems.append(
                        f"{rid}: output.filename_template has unknown placeholder(s) "
                        f"{', '.join('{%s}' % u for u in sorted(unknown))} "
                        f"(valid: {', '.join('{%s}' % a for a in sorted(allowed))})"
                    )
                if not fields:
                    problems.append(
                        f"{rid}: output.filename_template has no placeholders — "
                        f"every note would overwrite the same filename"
                    )

    streams = routine.get("streams")
    configured_label = bool(configured_labels(routine))
    for source in source_dicts:
        effective = routine_for_source(routine, source)
        action_list = source_actions(effective, source)
        effective_analyze = analyze_cfg(effective)
        effective_label = bool(configured_labels(effective))
        if "apply_label" in action_list and not (
            effective_analyze.get("pick_label") or effective_label
        ):
            handler = source.get("handler")
            suffix = f" handler {handler!r}" if handler else ""
            problems.append(
                f"{rid}:{suffix} action 'apply_label' needs `label:`, a "
                f"`streams:` entry with a label, or analyze.pick_label: true"
            )
    if configured_label and analyze.get("pick_label"):
        problems.append(
            f"{rid}: set a configured label OR analyze.pick_label, not both"
        )
    if routine.get("label") is not None and not isinstance(routine["label"], str):
        problems.append(f"{rid}: `label` must be a string")
    if streams is not None:
        if not isinstance(streams, dict) or not streams:
            problems.append(
                f"{rid}: `streams` must be a non-empty mapping keyed by subject "
                f"(or `from:<sender>`)"
            )
        else:
            allowed = {"title", "label", "message_updates"}
            for sender, cfg in streams.items():
                if not isinstance(cfg, dict):
                    problems.append(f"{rid}: streams[{sender}] must be a mapping")
                    continue
                unknown = set(cfg) - allowed
                if unknown:
                    problems.append(
                        f"{rid}: streams[{sender}] has unknown key(s) "
                        f"{', '.join(sorted(unknown))} "
                        f"(valid: title, label, message_updates)"
                    )
                if (
                    "message_updates" in cfg
                    and not isinstance(cfg["message_updates"], bool)
                ):
                    problems.append(
                        f"{rid}: streams[{sender}].message_updates must be a boolean"
                    )
            uses_message_updates = any(
                isinstance(cfg, dict) and cfg.get("message_updates") is True
                for cfg in streams.values()
            )
            if uses_message_updates and not any(
                source.get("kind") == "gmail" for source in source_dicts
            ):
                problems.append(
                    f"{rid}: streams.*.message_updates requires a Gmail source"
                )
            if uses_message_updates and any(
                source.get("kind") == "gmail"
                and source.get("read_thread") is True
                for source in source_dicts
            ):
                problems.append(
                    f"{rid}: Gmail source.read_thread cannot be combined with "
                    "streams.*.message_updates"
                )

    problems.extend(_validate_schedule(routine))

    routing = routine.get("routing")
    if routing is not None:
        if not isinstance(routing, dict):
            problems.append(f"{rid}: `routing` must be a mapping")
        else:
            unknown = set(routing) - {"fallback", "priority"}
            if unknown:
                problems.append(
                    f"{rid}: routing has unknown key(s) {', '.join(sorted(unknown))} "
                    f"(valid: fallback, priority)"
                )
            if "fallback" in routing and not isinstance(routing["fallback"], bool):
                problems.append(f"{rid}: routing.fallback must be true or false")
            priority = routing.get("priority", 100)
            if not isinstance(priority, int) or isinstance(priority, bool):
                problems.append(f"{rid}: routing.priority must be an integer")

    return problems


def _validate_handlers(routine, source_dicts):
    """Validate named execution profiles and their deterministic references."""
    rid = routine.get("id", "<missing id>")
    handlers = routine.get("handlers")
    references = [
        (index, source.get("handler"), source)
        for index, source in enumerate(source_dicts)
        if source.get("handler") is not None
    ]
    if handlers is None:
        return [
            f"{rid}: sources[{index}].handler references {handler!r}, but "
            "the routine has no `handlers` mapping"
            for index, handler, _ in references
        ]
    if not isinstance(handlers, dict) or not handlers:
        return [f"{rid}: `handlers` must be a non-empty mapping"]

    problems = []
    valid_profiles = {}
    for handler_id, profile in handlers.items():
        if not isinstance(handler_id, str) or not _ROUTINE_ID.fullmatch(handler_id):
            problems.append(
                f"{rid}: handler id must use lowercase letters, digits, and "
                f"hyphens (got {handler_id!r})"
            )
            continue
        if not isinstance(profile, dict):
            problems.append(f"{rid}: handlers[{handler_id}] must be a mapping")
            continue
        unknown = set(profile) - HANDLER_OVERRIDE_KEYS
        if unknown:
            problems.append(
                f"{rid}: handlers[{handler_id}] has unknown key(s) "
                f"{', '.join(sorted(unknown))} (valid: "
                f"{', '.join(sorted(HANDLER_OVERRIDE_KEYS))})"
            )
            continue
        for key in ("analyze", "output", "memory"):
            if key in profile and not isinstance(profile[key], dict):
                problems.append(
                    f"{rid}: handlers[{handler_id}].{key} must be a mapping"
                )
        memory = profile.get("memory")
        if (
            isinstance(memory, dict)
            and "operator_confirmed_source_ids" in memory
        ):
            problems.append(
                f"{rid}: handlers[{handler_id}].memory cannot set "
                "operator_confirmed_source_ids; put exact replay overrides "
                "in the routine-level memory block"
            )
        valid_profiles[handler_id] = profile

    used = set()
    multiple = len(source_dicts) > 1
    for index, handler_id, source in references:
        prefix = f"sources[{index}]" if multiple else "source"
        if not isinstance(handler_id, str) or not handler_id:
            problems.append(f"{rid}: {prefix}.handler must be a handler id string")
            continue
        if handler_id not in handlers:
            problems.append(
                f"{rid}: {prefix}.handler references unknown handler "
                f"{handler_id!r}"
            )
            continue
        used.add(handler_id)
        if handler_id not in valid_profiles:
            continue
        if source.get("max_results") != 0:
            problems.append(
                f"{rid}: {prefix}.handler requires max_results: 0 so a "
                "capped deterministic query cannot leak overflow items into "
                "the general source"
            )
        if source.get("self_forwarded_chat_followups") is True:
            problems.append(
                f"{rid}: {prefix}.self_forwarded_chat_followups must use the "
                "routine-level default handler because its lifecycle is "
                "reconciled after the source leaves Gmail"
            )

        # Reuse the complete routine validator on the materialized profile.
        # Removing `handlers` and the selector prevents recursion, while a
        # single source makes every error describe the exact executable shape.
        effective = dict(routine_for_source(routine, source))
        effective.pop("handlers", None)
        effective.pop("_handler_id", None)
        effective.pop("sources", None)
        effective_source = dict(source)
        effective_source.pop("handler", None)
        effective["source"] = effective_source
        for problem in validate(effective):
            problems.append(f"{rid}: handler {handler_id!r}: {problem}")

    unused = sorted(set(valid_profiles) - used)
    if unused:
        problems.append(
            f"{rid}: unused handler(s): {', '.join(unused)}"
        )
    return problems


def _non_empty_str(value):
    return isinstance(value, str) and bool(value.strip())


def _str_list(value):
    return isinstance(value, list) and all(_non_empty_str(v) for v in value)


def _validate_fibery_tasks_inbound(rid, cfg):
    problems = []
    secret = cfg.get("document_secret")
    if not isinstance(secret, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,128}", secret
    ):
        problems.append(
            f"{rid}: maintenance.document_secret is required "
            "(letters, digits, - and _ only)"
        )
    tool = cfg.get("read_tool")
    if not (
        isinstance(tool, str)
        and re.fullmatch(r"mcp__[A-Za-z0-9_-]+__read_document", tool)
        and tool.rsplit("__", 1)[-1] == "read_document"
    ):
        problems.append(
            f"{rid}: maintenance.read_tool must be an exact MCP tool name "
            "ending in __read_document"
        )
    store = cfg.get("store")
    if not isinstance(store, str) or not store.startswith("/"):
        problems.append(f"{rid}: maintenance.store must be an absolute path")
    owner = cfg.get("owner_slug")
    if owner is not None and not (
        isinstance(owner, str) and _ROUTINE_ID.fullmatch(owner)
    ):
        problems.append(f"{rid}: maintenance.owner_slug must be a kebab-case slug")
    tags = cfg.get("tags", [])
    if not isinstance(tags, list) or any(
        not isinstance(t, str) or not _ROUTINE_ID.fullmatch(t) for t in tags
    ):
        problems.append(f"{rid}: maintenance.tags must be kebab-case tags")
    if "model" in cfg and not _non_empty_str(cfg["model"]):
        problems.append(f"{rid}: maintenance.model must be a non-empty string")
    if "import_done" in cfg and not isinstance(cfg["import_done"], bool):
        problems.append(f"{rid}: maintenance.import_done must be a boolean")
    for key in ("max_new_per_run", "max_changes_per_run"):
        value = cfg.get(key, 1)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.append(f"{rid}: maintenance.{key} must be an integer >= 1")
    return problems


def _validate_fibery_tasks_outbound(rid, cfg):
    problems = []
    secret = cfg.get("document_secret")
    if not isinstance(secret, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,128}", secret
    ):
        problems.append(
            f"{rid}: maintenance.document_secret is required "
            "(letters, digits, - and _ only)"
        )
    read_tool, write_tool = cfg.get("read_tool"), cfg.get("write_tool")
    if not (
        isinstance(read_tool, str)
        and re.fullmatch(r"mcp__[A-Za-z0-9_-]+__read_document", read_tool)
    ):
        problems.append(
            f"{rid}: maintenance.read_tool must be an exact MCP tool name "
            "ending in __read_document"
        )
    if not (
        isinstance(write_tool, str)
        and re.fullmatch(r"mcp__[A-Za-z0-9_-]+__insert_document_blocks", write_tool)
    ):
        problems.append(
            f"{rid}: maintenance.write_tool must be an exact MCP tool name "
            "ending in __insert_document_blocks"
        )
    if (
        isinstance(read_tool, str) and isinstance(write_tool, str)
        and read_tool.rsplit("__", 1)[0] != write_tool.rsplit("__", 1)[0]
    ):
        problems.append(
            f"{rid}: maintenance.read_tool and write_tool must belong to the "
            "same connector"
        )
    if cfg.get("write_mode", "suggest") not in ("suggest", "apply"):
        problems.append(f"{rid}: maintenance.write_mode must be suggest or apply")
    store = cfg.get("store")
    if not isinstance(store, str) or not store.startswith("/"):
        problems.append(f"{rid}: maintenance.store must be an absolute path")
    owner = cfg.get("owner_slug")
    if not (isinstance(owner, str) and _ROUTINE_ID.fullmatch(owner)):
        problems.append(
            f"{rid}: maintenance.owner_slug is required (kebab-case slug)")
    since = cfg.get("outbound_since")
    valid_since = False
    if isinstance(since, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        try:
            datetime.date.fromisoformat(since)
            valid_since = True
        except ValueError:
            pass
    if not valid_since:
        problems.append(
            f"{rid}: maintenance.outbound_since is required (YYYY-MM-DD, "
            "quoted)"
        )
    if "from_sources" in cfg and (
        not _str_list(cfg["from_sources"]) or not cfg["from_sources"]
    ):
        problems.append(
            f"{rid}: maintenance.from_sources must be a non-empty list of "
            "source id prefixes"
        )
    heading = cfg.get("inbox_heading", "Inbox")
    if not _non_empty_str(heading) or len(heading) > 80 or "\n" in heading:
        problems.append(
            f"{rid}: maintenance.inbox_heading must be a short single-line string"
        )
    if "exclude_tags" in cfg and not _str_list(cfg["exclude_tags"]):
        problems.append(
            f"{rid}: maintenance.exclude_tags must be a list of strings")
    cap = cfg.get("max_per_run", 5)
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 50:
        problems.append(
            f"{rid}: maintenance.max_per_run must be an integer from 1 to 50")
    if "model" in cfg and not _non_empty_str(cfg["model"]):
        problems.append(f"{rid}: maintenance.model must be a non-empty string")
    return problems


def _validate_slack_discovery(rid, cfg):
    problems = []
    for key in ("discovered", "watch"):
        value = cfg.get(key)
        if value is None and key == "watch":
            continue
        if not _non_empty_str(value) or not (
            value.startswith("~") or value.startswith("/")
        ):
            problems.append(
                f"{rid}: maintenance.{key} must be an absolute or ~ path"
                + ("" if key == "watch" else " (a relative path would land "
                   "inside the repository)")
            )
    if not _read_only_tool(cfg.get("search_tool"), "slack_search_"):
        problems.append(
            f"{rid}: maintenance.search_tool must be an exact read-only MCP "
            "tool name (mcp__<server>__slack_search_...)"
        )
    owner = cfg.get("self_user_id")
    if not isinstance(owner, str) or not slack_watch.USER_ID.match(owner):
        problems.append(
            f"{rid}: maintenance.self_user_id must be the owner's Slack user id"
        )
    for key, lower, upper in (
        ("active_days", 1, 90), ("idle_days", 1, 365),
        ("max_search_pages", 1, 10), ("max_channels", 1, 200),
        ("min_mentions", 1, 20), ("min_seconds_between_searches", 0, 120),
    ):
        if key in cfg and not _bounded_int(cfg[key], lower, upper):
            problems.append(
                f"{rid}: maintenance.{key} must be an integer from "
                f"{lower} to {upper}"
            )
    active, idle = cfg.get("active_days", 14), cfg.get("idle_days", 30)
    if (
        _bounded_int(active, 1, 90) and _bounded_int(idle, 1, 365)
        and idle < active
    ):
        problems.append(
            f"{rid}: maintenance.idle_days must be at least active_days"
        )
    if "model" in cfg and not _non_empty_str(cfg["model"]):
        problems.append(f"{rid}: maintenance.model must be a non-empty string")
    return problems


def _validate_calendar_cache(rid, cfg):
    problems = []
    for key in ("owner_email", "list_tool", "get_tool", "output"):
        if not _non_empty_str(cfg.get(key)):
            problems.append(f"{rid}: maintenance.{key} is required")
    output = cfg.get("output")
    if _non_empty_str(output) and output.lower().endswith(".json"):
        problems.append(
            f"{rid}: maintenance.output must not end in .json "
            "(the JSON twin uses that name)"
        )
    if _non_empty_str(output) and not (
        output.startswith("~") or output.startswith("/")
    ):
        problems.append(
            f"{rid}: maintenance.output must be an absolute or ~ path "
            "(a relative path would land inside the repository)"
        )
    owner = cfg.get("owner_email")
    if _non_empty_str(owner) and not re.fullmatch(r"[^@\s]+@[^@\s]+", owner):
        problems.append(f"{rid}: maintenance.owner_email must be an email address")
    for key in ("list_tool", "get_tool"):
        value = cfg.get(key)
        if _non_empty_str(value) and not re.fullmatch(r"mcp__[A-Za-z0-9_-]+", value):
            problems.append(
                f"{rid}: maintenance.{key} must be an exact MCP tool name "
                "(mcp__<server>__<tool>)"
            )
    if (
        _non_empty_str(cfg.get("list_tool"))
        and cfg.get("list_tool") == cfg.get("get_tool")
    ):
        problems.append(
            f"{rid}: maintenance.list_tool and get_tool must differ"
        )
    if "calendar_id" in cfg and not _non_empty_str(cfg["calendar_id"]):
        problems.append(f"{rid}: maintenance.calendar_id must be a non-empty string")
    window = cfg.get("window_hours", 48)
    if (
        not isinstance(window, (int, float)) or isinstance(window, bool)
        or not 0 < window <= 24 * 31
    ):
        problems.append(
            f"{rid}: maintenance.window_hours must be a positive number "
            "of at most 744"
        )
    if "timezone" in cfg:
        try:
            ZoneInfo(cfg["timezone"])
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            problems.append(
                f"{rid}: maintenance.timezone must be a valid IANA timezone"
            )
    domains = cfg.get("internal_domains", [])
    if not _str_list(domains) or any(
        "@" in d or d != d.strip() or d != d.lower() for d in domains
    ):
        problems.append(
            f"{rid}: maintenance.internal_domains must be a list of "
            "lowercase domain names"
        )
    if not _str_list(cfg.get("skip_titles", [])):
        problems.append(
            f"{rid}: maintenance.skip_titles must be a list of strings"
        )
    names = cfg.get("max_attendee_names", 0)
    if not isinstance(names, int) or isinstance(names, bool) or names < 0:
        problems.append(
            f"{rid}: maintenance.max_attendee_names must be an integer >= 0"
        )
    if "model" in cfg and not _non_empty_str(cfg["model"]):
        problems.append(f"{rid}: maintenance.model must be a non-empty string")
    max_events = cfg.get("max_events", 50)
    if (
        not isinstance(max_events, int) or isinstance(max_events, bool)
        or not 1 <= max_events <= 250
    ):
        problems.append(
            f"{rid}: maintenance.max_events must be an integer from 1 to 250"
        )
    details = cfg.get("max_details", 20)
    if (
        not isinstance(details, int) or isinstance(details, bool)
        or not 0 <= details <= 100
    ):
        problems.append(
            f"{rid}: maintenance.max_details must be an integer from 0 to 100"
        )
    if "stale_after" in cfg:
        try:
            duration_seconds(cfg["stale_after"])
        except RoutineError:
            problems.append(
                f"{rid}: maintenance.stale_after must look like '30m', '2h', "
                "or '1d'"
            )
    hosts = cfg.get("conference_hosts", [])
    if not _str_list(hosts) or any(
        not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", h) or "." not in h
        for h in hosts
    ):
        problems.append(
            f"{rid}: maintenance.conference_hosts must be a list of "
            "lowercase host names such as zoom.us"
        )
    return problems


_FORBIDDEN_TOOL_WORDS = (
    "send", "schedule", "create", "update", "add", "delete", "upload",
    "complete",
)
_GMAIL_FORBIDDEN_TOOL_WORDS = (
    "send", "draft", "label", "archive", "mark", "delete", "trash", "create",
    "update", "modify",
)
_MCP_TOOL = re.compile(r"^mcp__[A-Za-z0-9_-]+__([A-Za-z0-9_-]+)$")


def _read_only_tool(value, family):
    """Whether `value` is an exact connector tool of a read-only family."""
    match = _MCP_TOOL.match(value) if isinstance(value, str) else None
    if not match:
        return False
    name = match.group(1)
    return name.startswith(family) and not any(
        word in name for word in _FORBIDDEN_TOOL_WORDS
    )


def _bounded_int(value, lower, upper):
    return (
        isinstance(value, int) and not isinstance(value, bool)
        and lower <= value <= upper
    )


def _validate_slack_mcp(prefix, source):
    """Connector-backed Slack source: read-only tools and bounded budgets."""
    problems = []
    for key, family in (
        ("search_tool", "slack_search_"), ("read_thread_tool", "slack_read_"),
    ):
        if not _read_only_tool(source.get(key), family):
            problems.append(
                f"{prefix}.{key} must be an exact read-only MCP tool name "
                f"(mcp__<server>__{family}...)"
            )
    if source.get("search_tool") == source.get("read_thread_tool"):
        problems.append(f"{prefix}: search_tool and read_thread_tool must differ")
    sessions = source.get("dm_sessions")
    if sessions is not None:
        why = slack_watch.dm_sessions_problem(sessions)
        if why:
            problems.append(f"{prefix}.dm_sessions {why}")
    if (
        isinstance(sessions, dict) and sessions.get("enabled") is True
        and source.get("all_dms") is not True
    ):
        problems.append(
            f"{prefix}.dm_sessions needs all_dms: true (owner-side messages "
            "are otherwise unverified and gaps one-sided)"
        )
    channel_tool = source.get("read_channel_tool")
    if (
        isinstance(sessions, dict) and sessions.get("enabled") is True
        and not _exact_tool(channel_tool, "slack_read_channel")
    ):
        problems.append(
            f"{prefix}.read_channel_tool must be the exact read-only MCP tool "
            "mcp__<server>__slack_read_channel when dm_sessions is enabled"
        )
    elif channel_tool is not None and not _exact_tool(
        channel_tool, "slack_read_channel"
    ):
        problems.append(
            f"{prefix}.read_channel_tool must be the exact read-only MCP tool "
            "mcp__<server>__slack_read_channel"
        )
    inbox = source.get("comms_inbox")
    inbox_problem = None if inbox is None else comms_inbox.problem(inbox)
    if inbox_problem:
        problems.append(f"{prefix}.comms_inbox {inbox_problem}")
    elif (
        inbox is not None and inbox.get("all_dms") is True
        and source.get("all_dms") is not True
    ):
        problems.append(
            f"{prefix}.comms_inbox.all_dms needs all_dms: true (no other "
            "query lists every DM)"
        )
    reactions_tool = source.get("reactions_tool")
    if reactions_tool is not None and not (
        _read_only_tool(reactions_tool, "slack_get_reactions")
        and _MCP_TOOL.match(reactions_tool).group(1) == "slack_get_reactions"
    ):
        problems.append(
            f"{prefix}.reactions_tool must be the exact read-only MCP tool "
            "mcp__<server>__slack_get_reactions"
        )
    owner = source.get("self_user_id")
    if not isinstance(owner, str) or not slack_watch.USER_ID.match(owner):
        problems.append(f"{prefix}.self_user_id must be the owner's Slack user id")
    for key, pattern in (
        ("channels", slack_watch.CHANNEL_ID),
        ("owner_action_channels", slack_watch.CHANNEL_ID),
        ("people", slack_watch.USER_ID),
    ):
        values = source.get(key)
        if values is None:
            continue
        if not isinstance(values, list) or not all(
            isinstance(v, str) and pattern.match(v) for v in values
        ):
            problems.append(f"{prefix}.{key} must be a list of Slack ids")
    if not (
        source.get("channels") or source.get("people")
        or source.get("include_mentions") is True
        or source.get("all_dms") is True
        or source.get("save_reactions")
        or source.get("_watch_unread")
    ):
        problems.append(
            f"{prefix}: source.kind 'slack_mcp' needs channels, people "
            "and/or include_mentions: true (inline or via watch)"
        )
    reactions = source.get("save_reactions")
    reactions_ok = reactions is None
    if reactions is not None:
        reactions_ok = (
            isinstance(reactions, list)
            and len(reactions) <= slack_watch.MAX_SAVE_REACTIONS
            and all(
                isinstance(n, str) and slack_watch.SAVE_REACTION.match(n)
                for n in reactions
            )
            and len(set(reactions)) == len(reactions)
        )
        if not reactions_ok:
            problems.append(
                f"{prefix}.save_reactions must be a list of at most "
                f"{slack_watch.MAX_SAVE_REACTIONS} distinct emoji names "
                "(no colons)"
            )
    tag = source.get("save_reaction_tag")
    if tag is not None and not (
        isinstance(tag, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", tag)
    ):
        problems.append(
            f"{prefix}.save_reaction_tag must be a lowercase tag "
            "(letters, digits, hyphens)"
        )
    if "include_mentions" in source and not isinstance(
        source["include_mentions"], bool
    ):
        problems.append(f"{prefix}.include_mentions must be true or false")
    if "all_dms" in source and not isinstance(source["all_dms"], bool):
        problems.append(f"{prefix}.all_dms must be true or false")
    for key, lower, upper in (
        ("max_threads_per_run", 1, 100), ("max_thread_replies", 1, 500),
        ("max_thread_chars", 500, 100000), ("max_search_pages", 1, 20),
        ("settle_minutes", 0, 1440), ("max_queries_per_run", 1, 60),
        ("calls_per_session", 1, 20),
        ("min_seconds_between_searches", 0, 120),
        ("save_reaction_lookback_days", 1, 90), ("max_session_pages", 1, 10),
    ):
        if key in source and not _bounded_int(source[key], lower, upper):
            problems.append(
                f"{prefix}.{key} must be an integer from {lower} to {upper}"
            )
    for key in ("bootstrap", "overlap"):
        if key in source:
            try:
                duration_seconds(source[key])
            except RoutineError:
                problems.append(
                    f"{prefix}.{key} must look like '15m', '4h', or '1d'"
                )
    estimate = source.get("estimate_analyze_usd")
    if estimate is not None and (
        not isinstance(estimate, (int, float)) or isinstance(estimate, bool)
        or not 0 <= estimate <= 10
    ):
        problems.append(
            f"{prefix}.estimate_analyze_usd must be a number from 0 to 10"
        )
    if "include_bots" in source and not isinstance(source["include_bots"], bool):
        problems.append(f"{prefix}.include_bots must be true or false")
    if "model" in source and not _non_empty_str(source["model"]):
        problems.append(f"{prefix}.model must be a non-empty string")
    keywords = source.get("mention_keywords")
    if keywords is not None:
        try:
            ok = (
                isinstance(keywords, list) and keywords
                and all(isinstance(w, str) and w for w in keywords)
                and all(
                    {f for _, f, _, _ in Formatter().parse(w) if f}
                    <= {"self_user_id"}
                    for w in keywords
                )
            )
        except ValueError:
            ok = False
        if not ok:
            problems.append(
                f"{prefix}.mention_keywords must be a non-empty list of "
                "strings using only the {self_user_id} placeholder"
            )
    extra = source.get("search_args")
    if extra is not None:
        from . import slack_mcp
        if not isinstance(extra, dict) or not all(
            isinstance(k, str) and isinstance(v, (str, int, float, bool))
            for k, v in extra.items()
        ):
            problems.append(
                f"{prefix}.search_args must map extra scalar arguments"
            )
        elif set(extra) & slack_mcp.RESERVED_SEARCH_ARGS:
            problems.append(
                f"{prefix}.search_args cannot set "
                f"{', '.join(sorted(set(extra) & slack_mcp.RESERVED_SEARCH_ARGS))} "
                "(fixed by the daemon)"
            )
    planned = (
        -(-len(source.get("channels") or []) // 15)
        + len(source.get("people") or [])
        + (1 if source.get("include_mentions") is True else 0)
        + (1 if source.get("all_dms") is True else 0)
        + (len(reactions) if reactions_ok and reactions else 0)
        + (
            len(set(inbox.get("reactions") or [])
                - set(reactions if reactions_ok and reactions else []))
            + len(inbox.get("track_reactions") or [])
            + (1 if inbox.get("follow_threads") is True else 0)
            if inbox is not None and not inbox_problem else 0
        )
    )
    cap = source.get("max_queries_per_run", 20)
    if _bounded_int(cap, 1, 100) and planned > cap:
        problems.append(
            f"{prefix}: {planned} planned searches exceed "
            f"max_queries_per_run ({cap})"
        )
    if "max_results" in source:
        problems.append(
            f"{prefix}.max_results is not used by slack_mcp; use "
            "max_threads_per_run"
        )
    return problems


def _exact_tool(value, name):
    """Whether `value` is the connector tool `mcp__<server>__<name>`, read-only."""
    match = _MCP_TOOL.match(value) if isinstance(value, str) else None
    return bool(match) and match.group(1) == name and not any(
        word in match.group(1) for word in _GMAIL_FORBIDDEN_TOOL_WORDS
    )


def _validate_gmail_mcp(prefix, source):
    """Connector-backed Gmail source: read-only tools and bounded budgets."""
    from . import gmail_mcp
    problems = []
    for key, name in (
        ("search_tool", gmail_mcp.SEARCH_TOOL_NAME),
        ("fetch_tool", gmail_mcp.FETCH_TOOL_NAME),
    ):
        if not _exact_tool(source.get(key), name):
            problems.append(
                f"{prefix}.{key} must be the exact read-only MCP tool "
                f"mcp__<server>__{name}"
            )
    if source.get("search_tool") == source.get("fetch_tool"):
        problems.append(f"{prefix}: search_tool and fetch_tool must differ")
    query = source.get("query")
    if isinstance(query, str) and _GMAIL_TEMPORAL_OPERATOR.search(query):
        problems.append(
            f"{prefix}.query must not contain newer_than/older_than/after/"
            "before; the daemon adds its own after: cursor"
        )
    elif query is not None and not isinstance(query, str):
        problems.append(f"{prefix}.query must be a string")
    for key, lower, upper in (
        ("max_threads_per_run", 1, 100), ("max_messages_per_thread", 1, 30),
        ("max_message_chars", 500, 100000), ("max_search_pages", 1, 20),
        ("settle_minutes", 0, 1440), ("calls_per_session", 1, 20),
        ("min_seconds_between_sessions", 0, 120),
    ):
        if key in source and not _bounded_int(source[key], lower, upper):
            problems.append(
                f"{prefix}.{key} must be an integer from {lower} to {upper}"
            )
    for key in ("bootstrap", "overlap"):
        if key in source:
            try:
                duration_seconds(source[key])
            except RoutineError:
                problems.append(
                    f"{prefix}.{key} must look like '15m', '4h', or '1d'"
                )
    estimate = source.get("estimate_analyze_usd")
    if estimate is not None and (
        not isinstance(estimate, (int, float)) or isinstance(estimate, bool)
        or not 0 <= estimate <= 10
    ):
        problems.append(
            f"{prefix}.estimate_analyze_usd must be a number from 0 to 10"
        )
    if "exclude_self_only" in source and not isinstance(
        source["exclude_self_only"], bool
    ):
        problems.append(f"{prefix}.exclude_self_only must be true or false")
    owner = source.get("owner_email")
    if owner is not None and not (
        isinstance(owner, str) and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", owner)
    ):
        problems.append(f"{prefix}.owner_email must be an email address")
    if source.get("exclude_self_only") is True and owner is None:
        problems.append(f"{prefix}.exclude_self_only requires owner_email")
    if "model" in source and not _non_empty_str(source["model"]):
        problems.append(f"{prefix}.model must be a non-empty string")
    for key in ("queue_query", "exclude_query", "max_results"):
        if key in source:
            problems.append(
                f"{prefix}.{key} is not supported by gmail_mcp"
            )
    return problems


def _validate_schedule(routine):
    """Validate the cadence shared by capture and maintenance routines."""
    problems = []
    rid = routine.get("id", "<missing id>")
    schedule = routine.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            problems.append(f"{rid}: `schedule` must be a mapping")
        else:
            unknown = set(schedule) - {"every", "work_hours"}
            if unknown:
                problems.append(
                    f"{rid}: schedule has unknown key(s) "
                    f"{', '.join(sorted(unknown))} "
                    f"(valid: every, work_hours)"
                )
            try:
                schedule_seconds(routine)
            except RoutineError as exc:
                problems.append(str(exc))
    return problems


def _validate_mila_match(source, prefix):
    from . import local_llm, mila_source
    problems = []
    calendar = source.get("calendar")
    if calendar is not None:
        if not isinstance(calendar, dict):
            problems.append(f"{prefix}.calendar must be a mapping")
        else:
            unknown = set(calendar) - mila_source.CALENDAR_KEYS
            if unknown:
                problems.append(
                    f"{prefix}.calendar has unknown key(s) "
                    f"{', '.join(sorted(unknown))}"
                )
            provider = calendar.get("provider", "gws")
            if provider not in mila_source.CALENDAR_PROVIDERS:
                problems.append(
                    f"{prefix}.calendar.provider must be one of "
                    f"{', '.join(mila_source.CALENDAR_PROVIDERS)}"
                )
            elif provider == "connector":
                if not mila_source.valid_list_tool(calendar.get("list_tool")):
                    problems.append(
                        f"{prefix}.calendar.list_tool must be an exact "
                        "read-only MCP list tool name "
                        "(mcp__<server>__list_events)"
                    )
                if "calendar_id" in calendar and not _non_empty_str(
                    calendar["calendar_id"]
                ):
                    problems.append(
                        f"{prefix}.calendar.calendar_id must be a non-empty string"
                    )
                owner = calendar.get("owner_email")
                if owner is not None and not (
                    _non_empty_str(owner) and re.fullmatch(r"[^@\s]+@[^@\s]+", owner)
                ):
                    problems.append(
                        f"{prefix}.calendar.owner_email must be an email address"
                    )
                if "model" in calendar and not _non_empty_str(calendar["model"]):
                    problems.append(
                        f"{prefix}.calendar.model must be a non-empty string"
                    )
            elif set(calendar) - {"provider"}:
                problems.append(
                    f"{prefix}.calendar: list_tool, calendar_id, owner_email and "
                    "model apply only to provider connector"
                )
    match = source.get("match")
    if match is not None:
        if not isinstance(match, dict):
            problems.append(f"{prefix}.match must be a mapping")
        else:
            unknown = set(match) - {"provider", "model", "base_url", "timeout"}
            if unknown:
                problems.append(
                    f"{prefix}.match has unknown key(s) "
                    f"{', '.join(sorted(unknown))}"
                )
            if match.get("provider") not in mila_source.MATCH_PROVIDERS:
                problems.append(
                    f"{prefix}.match.provider must be one of "
                    f"{', '.join(mila_source.MATCH_PROVIDERS)}"
                )
            if not _non_empty_str(match.get("model")):
                problems.append(f"{prefix}.match.model is required")
            timeout = match.get("timeout")
            if timeout is not None and (
                not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or not 1 <= timeout <= 600
            ):
                problems.append(
                    f"{prefix}.match.timeout must be a number from 1 to 600"
                )
            if match.get("provider") == "ollama":
                base_url = match.get("base_url", local_llm.DEFAULT_BASE_URL)
                host = urlparse(base_url).hostname if isinstance(base_url, str) else None
                scheme = urlparse(base_url).scheme if isinstance(base_url, str) else None
                if scheme not in ("http", "https") or host not in (
                    "localhost", "127.0.0.1", "::1"
                ):
                    problems.append(
                        f"{prefix}.match.base_url must be an http(s) URL on "
                        "localhost (transcripts must stay on this machine)"
                    )
            elif "base_url" in match:
                problems.append(
                    f"{prefix}.match.base_url applies only to provider ollama"
                )
    app = source.get("app_summary")
    if app is not None:
        if not isinstance(app, dict):
            problems.append(f"{prefix}.app_summary must be a mapping")
        else:
            unknown = set(app) - mila_source.APP_SUMMARY_KEYS
            if unknown:
                problems.append(
                    f"{prefix}.app_summary has unknown key(s) "
                    f"{', '.join(sorted(unknown))}"
                )
            if app.get("mode", "off") not in mila_source.APP_SUMMARY_MODES:
                problems.append(
                    f"{prefix}.app_summary.mode must be one of "
                    f"{', '.join(mila_source.APP_SUMMARY_MODES)}"
                )
            for key, low, high in (("wait_minutes", 0, 1440),
                                   ("min_chars", 1, 100000),
                                   ("min_overlap", 0, 1),
                                   ("item_min_overlap", 0, 1)):
                value = app.get(key)
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool) or not low <= value <= high
                ):
                    problems.append(
                        f"{prefix}.app_summary.{key} must be a number from "
                        f"{low} to {high}"
                    )
    if isinstance(app, dict) and isinstance(
        app.get("wait_minutes"), (int, float)
    ) and not isinstance(app.get("wait_minutes"), bool):
        hours = source.get("calendar_match_hours", 6)
        if isinstance(hours, (int, float)) and app["wait_minutes"] >= hours * 60:
            problems.append(
                f"{prefix}.app_summary.wait_minutes must be less than "
                "calendar_match_hours * 60"
            )
    limit = source.get("max_transcript_chars")
    if limit is not None and (
        not isinstance(limit, int) or isinstance(limit, bool)
        or not 1000 <= limit <= 1000000
    ):
        problems.append(
            f"{prefix}.max_transcript_chars must be an integer from 1000 to 1000000"
        )
    return problems


def _validate_source(routine, source, prefix):
    """Validate one source block. Messages carry the source's config path."""
    problems = []
    from . import gmail_mcp, slack_mcp
    allowed = SOURCE_KEYS | (
        slack_mcp.SOURCE_KEYS if source.get("kind") == "slack_mcp" else set()
    ) | (
        gmail_mcp.SOURCE_KEYS if source.get("kind") == "gmail_mcp" else set()
    )
    unknown_source = {
        key for key in set(source) - allowed
        if not str(key).startswith("_watch")
    }
    if unknown_source:
        problems.append(
            f"{prefix} has unknown key(s) {', '.join(sorted(unknown_source))}"
        )
    kind = source.get("kind")
    if kind not in VALID_SOURCE_KINDS:
        problems.append(
            f"{prefix}.kind must be one of {', '.join(sorted(VALID_SOURCE_KINDS))} "
            f"(got {kind!r})"
        )
    if kind in ("gmail", "gmail_mcp", "drive_docs") and not source.get("query"):
        problems.append(f"{prefix}.query is required")
    if "watch" in source:
        if kind not in {"slack", "slack_mcp"}:
            problems.append(f"{prefix}.watch is supported only for slack sources")
        if source.get("_watch_error"):
            problems.append(f"{prefix}.watch: {source['_watch_error']}")
    ignore_people = source.get("ignore_people")
    if ignore_people is not None and not isinstance(ignore_people, bool):
        problems.append(f"{prefix}.ignore_people must be true or false")
    if kind == "slack" and source.get("people") and ignore_people is not True:
        problems.append(
            f"{prefix}.people is not read by source.kind 'slack'; set "
            "ignore_people: true to share a watch file with a slack_mcp source"
        )
    action_list = source_actions(routine, source)
    if "actions" in source and not isinstance(source.get("actions"), list):
        problems.append(f"{prefix}.actions must be a list")
        action_list = []
    for action in action_list:
        if action not in VALID_ACTIONS:
            problems.append(
                f"{prefix}: unknown action '{action}' "
                f"(valid: {', '.join(sorted(VALID_ACTIONS))})"
            )
    if kind == "gmail_mcp" and action_list:
        problems.append(
            f"{prefix}: actions are not supported for source.kind 'gmail_mcp' "
            "(it reads only; use actions: [])"
        )
    elif kind != "gmail" and action_list:
        problems.append(f"{prefix}: source.kind {kind!r} does not support Gmail actions")
    read_thread = source.get("read_thread")
    if read_thread is not None and not isinstance(read_thread, bool):
        problems.append(f"{prefix}.read_thread must be true or false")
    if kind not in ("gmail", "gmail_mcp") and read_thread is not None:
        problems.append(f"{prefix}.read_thread is supported only for gmail")

    chat_followups = source.get("self_forwarded_chat_followups")
    if chat_followups is not None and not isinstance(chat_followups, bool):
        problems.append(
            f"{prefix}.self_forwarded_chat_followups must be true or false"
        )
    if kind != "gmail" and chat_followups is not None:
        problems.append(
            f"{prefix}.self_forwarded_chat_followups is supported only for gmail"
        )
    if chat_followups is True and not isinstance(routine.get("memory"), dict):
        problems.append(
            f"{prefix}.self_forwarded_chat_followups requires a memory block"
        )
    if chat_followups is True and action_list:
        problems.append(
            f"{prefix}.self_forwarded_chat_followups requires actions: [] "
            "so only the user can complete the Inbox queue"
        )

    catch_up = source.get("catch_up", False)
    if not isinstance(catch_up, bool):
        problems.append(f"{prefix}.catch_up must be true or false")
    catch_up_overlap = source.get("catch_up_overlap")
    if catch_up_overlap is not None and catch_up is not True:
        problems.append(
            f"{prefix}.catch_up_overlap requires catch_up: true"
        )
    catch_up_after = source.get("catch_up_after")
    if catch_up_after is not None:
        if catch_up is not True:
            problems.append(f"{prefix}.catch_up_after requires catch_up: true")
        if not is_rfc3339_instant(catch_up_after):
            problems.append(
                f"{prefix}.catch_up_after must be a quoted RFC3339 timestamp"
            )
    if catch_up is True:
        if kind not in {"gmail", "gchat", "slack"}:
            problems.append(
                f"{prefix}.catch_up is supported only for gmail, gchat, and slack"
            )
        try:
            duration_seconds(catch_up_overlap or "1h")
        except RoutineError:
            problems.append(
                f"{prefix}.catch_up_overlap must look like '15m', '4h', or '1d'"
            )

    if kind == "gmail" and catch_up is True:
        if source.get("max_results") != 0:
            problems.append(f"{prefix}.catch_up requires max_results: 0")
        if catch_up_after is None:
            problems.append(f"{prefix}.catch_up requires catch_up_after")
        if _GMAIL_TEMPORAL_OPERATOR.search(str(source.get("query", ""))):
            problems.append(
                f"{prefix}.catch_up query must not contain a fixed temporal "
                "operator; the daemon appends its durable after: cursor"
            )
        queue_query = source.get("queue_query")
        if queue_query is not None and (
            not isinstance(queue_query, str) or not queue_query.strip()
        ):
            problems.append(f"{prefix}.queue_query must be a non-empty string")
        if queue_query and _GMAIL_TEMPORAL_OPERATOR.search(queue_query):
            problems.append(
                f"{prefix}.queue_query must be timeless so unread/starred "
                "items survive sleep and outages"
            )
        exclude_query = source.get("exclude_query")
        if exclude_query is not None and (
            not isinstance(exclude_query, str) or not exclude_query.strip()
        ):
            problems.append(f"{prefix}.exclude_query must be a non-empty string")
        if exclude_query and _GMAIL_TEMPORAL_OPERATOR.search(exclude_query):
            problems.append(f"{prefix}.exclude_query must be timeless")
    elif source.get("queue_query") is not None:
        problems.append(f"{prefix}.queue_query requires catch_up: true")

    if kind == "slack":
        channel_keys = (
            "channels", "ada_channels", "direct_channels", "private_channels"
        )
        configured = []
        for key in channel_keys:
            values = source.get(key)
            if values is None:
                continue
            if not isinstance(values, list) or not values:
                problems.append(f"{prefix}.{key} must be a non-empty list")
                continue
            if not all(isinstance(value, str) and value for value in values):
                problems.append(f"{prefix}.{key} entries must be channel ID strings")
                continue
            configured.extend((key, value) for value in values)
        owner_channels = source.get("owner_action_channels")
        if owner_channels is not None:
            if not isinstance(owner_channels, list) or not owner_channels:
                problems.append(
                    f"{prefix}.owner_action_channels must be a non-empty list"
                )
            elif not all(
                isinstance(value, str) and value for value in owner_channels
            ):
                problems.append(
                    f"{prefix}.owner_action_channels entries must be "
                    "channel ID strings"
                )
        active_conversations = source.get("active_conversations")
        if active_conversations is not None:
            if not isinstance(active_conversations, dict):
                problems.append(
                    f"{prefix}.active_conversations must be a mapping"
                )
            else:
                unknown = set(active_conversations) - {
                    "checkpoint", "hours", "refresh_every",
                    "requests_per_minute", "refresh_if_stale",
                }
                if unknown:
                    problems.append(
                        f"{prefix}.active_conversations has unknown key(s): "
                        f"{', '.join(sorted(unknown))}"
                    )
                checkpoint = active_conversations.get("checkpoint")
                if not isinstance(checkpoint, str) or not checkpoint:
                    problems.append(
                        f"{prefix}.active_conversations.checkpoint is required"
                    )
                hours = active_conversations.get("hours", 48)
                valid_hours = (
                    isinstance(hours, (int, float))
                    and not isinstance(hours, bool)
                    and hours > 0
                )
                if (
                    not valid_hours
                ):
                    problems.append(
                        f"{prefix}.active_conversations.hours must be positive"
                    )
                refresh = active_conversations.get("refresh_every", "1d")
                try:
                    refresh_seconds = duration_seconds(refresh)
                except RoutineError:
                    refresh_seconds = None
                    problems.append(
                        f"{prefix}.active_conversations.refresh_every must "
                        "look like '15m', '4h', or '1d'"
                    )
                if (
                    valid_hours
                    and refresh_seconds is not None
                    and refresh_seconds > float(hours) * 60 * 60
                ):
                    problems.append(
                        f"{prefix}.active_conversations.refresh_every must "
                        "not exceed its hours window"
                    )
                if source.get("catch_up") is not True:
                    problems.append(
                        f"{prefix}.active_conversations requires catch_up: true"
                    )
                rpm = active_conversations.get("requests_per_minute", 40)
                if (
                    not isinstance(rpm, int)
                    or isinstance(rpm, bool)
                    or not 1 <= rpm <= 50
                ):
                    problems.append(
                        f"{prefix}.active_conversations.requests_per_minute "
                        "must be an integer from 1 to 50"
                    )
                refresh_if_stale = active_conversations.get(
                    "refresh_if_stale", True
                )
                if not isinstance(refresh_if_stale, bool):
                    problems.append(
                        f"{prefix}.active_conversations.refresh_if_stale "
                        "must be true or false"
                    )
        if (
            not configured
            and not source.get("include_mentions")
            and not active_conversations
            and not source.get("_watch_unread")
        ):
            problems.append(
                f"{prefix}: source.kind 'slack' needs channels, ada_channels, "
                "direct_channels, private_channels, active_conversations, "
                "and/or include_mentions: true"
            )
        owners = {}
        for key, channel in configured:
            previous = owners.setdefault(channel, key)
            if previous != key:
                problems.append(
                    f"{prefix}: Slack channel {channel} appears in both "
                    f"{previous} and {key}"
                )
        ada_days = source.get("ada_days")
        if ada_days is not None and (
            not isinstance(ada_days, int)
            or isinstance(ada_days, bool)
            or not 1 <= ada_days <= 90
        ):
            problems.append(f"{prefix}.ada_days must be an integer from 1 to 90")
        if catch_up is True:
            if source.get("ada_channels"):
                problems.append(
                    f"{prefix}.catch_up requires direct Slack reads; "
                    "move ada_channels to direct_channels"
                )
            if source.get("channels"):
                problems.append(
                    f"{prefix}.catch_up requires daily direct reads; "
                    "move channels to direct_channels"
                )
            if source.get("max_results") != 0:
                problems.append(
                    f"{prefix}.catch_up requires max_results: 0"
                )
            if catch_up_after is None:
                problems.append(
                    f"{prefix}.catch_up requires catch_up_after"
                )
            direct = (
                source.get("direct_channels")
                or source.get("private_channels")
                or source.get("active_conversations")
            )
            reply_roots_after = source.get("reply_roots_after")
            if direct and reply_roots_after is None:
                problems.append(
                    f"{prefix}.catch_up with direct channels requires "
                    "reply_roots_after"
                )
            elif reply_roots_after is not None:
                if not is_rfc3339_instant(reply_roots_after):
                    problems.append(
                        f"{prefix}.reply_roots_after must be a quoted "
                        "RFC3339 timestamp"
                    )
                elif (
                    is_rfc3339_instant(catch_up_after)
                    and rfc3339_key(reply_roots_after)
                    > rfc3339_key(catch_up_after)
                ):
                    problems.append(
                        f"{prefix}.reply_roots_after must not be later than "
                        "catch_up_after"
                    )
    if kind == "slack_mcp":
        problems.extend(_validate_slack_mcp(prefix, source))
    if kind == "gmail_mcp":
        problems.extend(_validate_gmail_mcp(prefix, source))
    if kind == "gchat":
        spaces = source.get("spaces")
        all_spaces = source.get("all_spaces", False)
        if not isinstance(all_spaces, bool):
            problems.append(f"{prefix}.all_spaces must be true or false")
        if bool(spaces) == (all_spaces is True):
            problems.append(
                f"{prefix}: source.kind 'gchat' needs exactly one of a non-empty "
                "`spaces` list or `all_spaces: true`"
            )
        if spaces is not None and (
            not isinstance(spaces, list) or not spaces
        ):
            problems.append(f"{prefix}.spaces must be a non-empty list")
        elif spaces and not all(str(s).startswith("spaces/") for s in spaces):
            problems.append(f"{prefix}: gchat spaces must be full resource names ('spaces/AAAA...')")
        batch = source.get("batch_unthreaded")
        if batch is not None and batch != "daily":
            problems.append(f"{prefix}.batch_unthreaded must be 'daily' when set")
        batch_messages = source.get("batch_messages")
        if batch_messages is not None and batch_messages != "daily":
            problems.append(f"{prefix}.batch_messages must be 'daily' when set")
        if batch is not None and batch_messages is not None:
            problems.append(
                f"{prefix}: set batch_unthreaded or batch_messages, not both"
            )
        batch_messages_after = source.get("batch_messages_after")
        if batch_messages_after is not None:
            if batch_messages != "daily":
                problems.append(
                    f"{prefix}.batch_messages_after requires batch_messages: daily"
                )
            if not is_rfc3339_instant(batch_messages_after):
                problems.append(
                    f"{prefix}.batch_messages_after must be a quoted RFC3339 timestamp"
                )
        if catch_up is True:
            if all_spaces is not True:
                problems.append(
                    f"{prefix}.catch_up currently requires all_spaces: true"
                )
            if batch_messages != "daily":
                problems.append(
                    f"{prefix}.catch_up requires batch_messages: daily"
                )
            if source.get("max_results") != 0:
                problems.append(
                    f"{prefix}.catch_up requires max_results: 0"
                )
            if source.get("max_per_space") != 0:
                problems.append(
                    f"{prefix}.catch_up requires max_per_space: 0"
                )
        max_per_space = source.get("max_per_space")
        if max_per_space is not None and (
            not isinstance(max_per_space, int)
            or isinstance(max_per_space, bool)
            or max_per_space < 0
        ):
            problems.append(f"{prefix}.max_per_space must be a non-negative integer")
        if max_per_space is not None and all_spaces is not True:
            problems.append(f"{prefix}.max_per_space requires `all_spaces: true`")

    session_gap = source.get("session_gap_minutes")
    if session_gap is not None:
        if kind not in {"gchat", "slack"}:
            problems.append(
                f"{prefix}.session_gap_minutes is supported only for gchat and slack"
            )
        if (
            not isinstance(session_gap, int)
            or isinstance(session_gap, bool)
            or not 15 <= session_gap <= 1440
        ):
            problems.append(
                f"{prefix}.session_gap_minutes must be an integer from 15 to 1440"
            )

    expand = source.get("expand")
    if expand is not None:
        if not isinstance(expand, dict):
            problems.append(f"{prefix}.expand must be a mapping")
            return problems
        if kind != "gmail":
            problems.append(f"{prefix}.expand is only supported for source.kind 'gmail'")
        unknown_expand = set(expand) - {
            "kind", "title_from_subject", "tabs", "on_missing", "name_contains",
        }
        if unknown_expand:
            problems.append(
                f"{prefix}.expand has unknown key(s) "
                f"{', '.join(sorted(unknown_expand))}"
            )
        if expand.get("kind") != "drive_doc":
            problems.append(
                f"{prefix}.expand.kind must be 'drive_doc' (got {expand.get('kind')!r})"
            )
        pattern = expand.get("title_from_subject")
        if not pattern:
            problems.append(f"{prefix}.expand.title_from_subject is required")
        else:
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                problems.append(f"{prefix}.expand.title_from_subject is not valid regex — {exc}")
            else:
                if "title" not in compiled.groupindex:
                    problems.append(
                        f"{prefix}.expand.title_from_subject must contain a named "
                        f"group (?P<title>...) — that is what gets matched against Drive"
                    )
        tabs = expand.get("tabs")
        if tabs is not None and (not isinstance(tabs, list) or not tabs):
            problems.append(f"{prefix}.expand.tabs must be a non-empty list of tab titles")
        on_missing = expand.get("on_missing", "body")
        if on_missing not in ("body", "error"):
            problems.append(
                f"{prefix}.expand.on_missing must be 'body' or 'error' (got {on_missing!r})"
            )

    if kind == "drive_docs":
        tabs = source.get("tabs")
        if tabs is not None and (not isinstance(tabs, list) or not tabs):
            problems.append(f"{prefix}.tabs must be a non-empty list of tab titles")
        # Gmail triage has no meaning for a Drive file, and the label catalog
        # the model would pick from is the mailbox's.
        if analyze_cfg(routine).get("pick_label"):
            problems.append(
                f"{prefix}: analyze.pick_label requires source.kind 'gmail' — "
                f"there is no Gmail message to label"
            )

    if kind == "mila":
        recordings_file = source.get("recordings_file")
        if (
            not isinstance(recordings_file, str)
            or not recordings_file.startswith("/")
        ):
            problems.append(
                f"{prefix}.recordings_file must be an absolute path"
            )
        excluded = source.get("exclude_recording_ids")
        if excluded is not None and (
            not isinstance(excluded, list)
            or any(not isinstance(value, str) or not value for value in excluded)
        ):
            problems.append(
                f"{prefix}.exclude_recording_ids must be a list of recording IDs"
            )
        manual = source.get("manual_recordings")
        if manual is not None:
            if not isinstance(manual, list) or not manual:
                problems.append(
                    f"{prefix}.manual_recordings must be a non-empty list"
                )
            else:
                for index, entry in enumerate(manual):
                    entry_prefix = f"{prefix}.manual_recordings[{index}]"
                    if not isinstance(entry, dict):
                        problems.append(f"{entry_prefix} must be a mapping")
                        continue
                    if (
                        not isinstance(entry.get("recording_id"), str)
                        or not entry["recording_id"]
                    ):
                        problems.append(f"{entry_prefix}.recording_id is required")
                    for field in (
                        "recordings_file", "transcript_file", "fallback_file"
                    ):
                        value = entry.get(field)
                        if value is not None and (
                            not isinstance(value, str)
                            or not value.startswith("/")
                        ):
                            problems.append(
                                f"{entry_prefix}.{field} must be an absolute path"
                            )
                    if not entry.get("transcript_file"):
                        problems.append(
                            f"{entry_prefix}.transcript_file is required"
                        )

    if kind == "mila":
        problems.extend(_validate_mila_match(source, prefix))
    elif any(key in source for key in ("calendar", "match", "max_transcript_chars", "app_summary")):
        problems.append(
            f"{prefix}: calendar, match, max_transcript_chars and "
            f"app_summary are only "
            f"supported for kind mila"
        )

    if kind == "whisper":
        transcriptions_dir = source.get("transcriptions_dir")
        if (
            not isinstance(transcriptions_dir, str)
            or not transcriptions_dir.startswith("/")
        ):
            problems.append(
                f"{prefix}.transcriptions_dir must be an absolute path"
            )
        quiet = source.get("min_quiet_seconds")
        if quiet is not None and (
            not isinstance(quiet, (int, float))
            or isinstance(quiet, bool)
            or quiet < 0
            or quiet > 3600
        ):
            problems.append(
                f"{prefix}.min_quiet_seconds must be a number from 0 to 3600"
            )
        excluded = source.get("exclude_base_names")
        if excluded is not None and (
            not isinstance(excluded, list)
            or any(not isinstance(value, str) or not value for value in excluded)
        ):
            problems.append(
                f"{prefix}.exclude_base_names must be a list of output base names"
            )

    if kind in {"mila", "whisper"}:
        timezone = source.get("calendar_timezone", "UTC")
        try:
            ZoneInfo(timezone)
        except (TypeError, ValueError, ZoneInfoNotFoundError):
            problems.append(
                f"{prefix}.calendar_timezone must be an IANA timezone"
            )
        for field, lower, upper in (
            ("calendar_window_days", 1, 14),
            ("max_calendar_candidates", 1, 20),
            ("calendar_max_results", 1, 2500),
            ("match_max_output_tokens", 256, 8192),
        ):
            value = source.get(field)
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not lower <= value <= upper
            ):
                problems.append(
                    f"{prefix}.{field} must be an integer from {lower} to {upper}"
                )
        match_hours = source.get("calendar_match_hours")
        if match_hours is not None and (
            not isinstance(match_hours, (int, float))
            or isinstance(match_hours, bool)
            or match_hours <= 0
            or match_hours > 24
        ):
            problems.append(
                f"{prefix}.calendar_match_hours must be greater than 0 and at most 24"
            )

    max_results = source.get("max_results", 20)
    unlimited_source = kind in {"gmail", "gchat", "slack", "mila", "whisper"}
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or max_results < (0 if unlimited_source else 1)
    ):
        qualifier = "non-negative" if unlimited_source else "positive"
        problems.append(f"{prefix}.max_results must be a {qualifier} integer")
    return problems
