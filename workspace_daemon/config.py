"""Routine discovery, loading, and validation."""
import datetime
import re
from pathlib import Path
from string import Formatter
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .actions import VALID_ACTIONS  # single source of truth for action names
from .notes import FILENAME_FIELDS
from .time_utils import is_rfc3339_instant, rfc3339_key

REQUIRED_TOP_LEVEL = ["id", "analyze"]
VALID_SOURCE_KINDS = {"gmail", "drive_docs", "slack", "gchat"}
VALID_ROUTINE_ROLES = {"general", "domain", "specialized", "partial"}
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


def analyze_cfg(routine):
    cfg = routine.get("analyze")
    return cfg if isinstance(cfg, dict) else {}


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
    except ZoneInfoNotFoundError as exc:
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


def validate(routine):
    """Return a list of human-readable problems; empty means valid."""
    problems = []
    rid = routine.get("id", "<missing id>")

    for key in REQUIRED_TOP_LEVEL:
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
        problems.extend(_validate_source(routine, source, prefix))
    if sum(source.get("catch_up") is True for source in source_dicts) > 1:
        problems.append(
            f"{rid}: only one catch_up source is currently supported per routine"
        )

    analyze = routine.get("analyze", {})
    if not isinstance(analyze, dict):
        problems.append(f"{rid}: `analyze` must be a mapping")
        analyze = {}
    for key in ("provider", "model"):
        if not analyze.get(key):
            problems.append(f"{rid}: analyze.{key} is required")
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
            len(source_dicts) != 1
            or source_dicts[0].get("kind") != connector
        ):
            problems.append(
                f"{rid}: analyze.connector_sweep requires exactly one "
                f"{connector!r} source block"
            )
        elif (
            connector == "gchat"
            and source_dicts[0].get("all_spaces") is not True
        ):
            problems.append(
                f"{rid}: a gchat connector sweep requires source.all_spaces: true"
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
            and source_dicts[0].get("catch_up") is not True
        ):
            problems.append(
                f"{rid}: a {connector} connector sweep requires "
                f"source.catch_up: true"
            )
        elif source_dicts[0].get("max_results") != 0:
            problems.append(
                f"{rid}: analyze.connector_sweep requires source.max_results: 0 "
                f"so the declared sweep is not capped"
            )

    output = routine.get("output") or {}
    if not isinstance(output, dict):
        problems.append(f"{rid}: `output` must be a mapping")
        output = {}
    has_memory = isinstance(routine.get("memory"), dict)
    if not output and not has_memory:
        problems.append(f"{rid}: needs an `output:` block, a `memory:` block, or both")
    if output:
        if not output.get("vault_dir"):
            problems.append(f"{rid}: output.vault_dir is required")
        elif not str(output["vault_dir"]).startswith("/"):
            problems.append(f"{rid}: output.vault_dir must be an absolute path")
        if not output.get("slug_prefix"):
            problems.append(f"{rid}: output.slug_prefix is required")

    from . import memory_sink
    problems.extend(memory_sink.validate(routine))

    template = output.get("filename_template")
    if template is not None:
        allowed = FILENAME_FIELDS
        try:
            fields = {f for _, f, _, _ in Formatter().parse(template) if f}
        except ValueError as exc:
            problems.append(f"{rid}: output.filename_template is malformed — {exc}")
        else:
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
    action_lists = [source_actions(routine, source) for source in source_dicts]

    if any("apply_label" in values for values in action_lists) and not (
        analyze.get("pick_label") or configured_label
    ):
        problems.append(
            f"{rid}: action 'apply_label' needs `label:`, a `streams:` entry "
            f"with a label, or analyze.pick_label: true"
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
                isinstance(cfg, dict) and "message_updates" in cfg
                for cfg in streams.values()
            )
            if uses_message_updates and not any(
                source.get("kind") == "gmail" for source in source_dicts
            ):
                problems.append(
                    f"{rid}: streams.*.message_updates requires a Gmail source"
                )

    schedule = routine.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            problems.append(f"{rid}: `schedule` must be a mapping")
        else:
            unknown = set(schedule) - {"every", "work_hours"}
            if unknown:
                problems.append(
                    f"{rid}: schedule has unknown key(s) {', '.join(sorted(unknown))} "
                    f"(valid: every, work_hours)"
                )
            try:
                schedule_seconds(routine)
            except RoutineError as exc:
                problems.append(str(exc))

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


def _validate_source(routine, source, prefix):
    """Validate one source block. Messages carry the source's config path."""
    problems = []
    kind = source.get("kind")
    if kind not in VALID_SOURCE_KINDS:
        problems.append(
            f"{prefix}.kind must be one of {', '.join(sorted(VALID_SOURCE_KINDS))} "
            f"(got {kind!r})"
        )
    if kind in ("gmail", "drive_docs") and not source.get("query"):
        problems.append(f"{prefix}.query is required")
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
    if kind != "gmail" and action_list:
        problems.append(f"{prefix}: source.kind {kind!r} does not support Gmail actions")

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
        if kind not in {"gchat", "slack"}:
            problems.append(
                f"{prefix}.catch_up is supported only for gchat and slack"
            )
        try:
            duration_seconds(catch_up_overlap or "1h")
        except RoutineError:
            problems.append(
                f"{prefix}.catch_up_overlap must look like '15m', '4h', or '1d'"
            )

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
        if not configured and not source.get("include_mentions"):
            problems.append(
                f"{prefix}: source.kind 'slack' needs channels, ada_channels, "
                "direct_channels, private_channels, and/or include_mentions: true"
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

    expand = source.get("expand")
    if expand is not None:
        if not isinstance(expand, dict):
            problems.append(f"{prefix}.expand must be a mapping")
            return problems
        if kind != "gmail":
            problems.append(f"{prefix}.expand is only supported for source.kind 'gmail'")
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

    max_results = source.get("max_results", 20)
    unlimited_source = kind in {"gmail", "gchat", "slack"}
    if (
        not isinstance(max_results, int)
        or isinstance(max_results, bool)
        or max_results < (0 if unlimited_source else 1)
    ):
        qualifier = "non-negative" if unlimited_source else "positive"
        problems.append(f"{prefix}.max_results must be a {qualifier} integer")
    return problems
