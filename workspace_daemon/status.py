"""Read-only health reporting for the scheduler and its routines."""
import datetime
import os
import re
import subprocess
import time
from pathlib import Path

from . import config, state


DEFAULT_LAUNCHD_LABEL = "com.memory-daemon"
MAINTENANCE_LAUNCHD_LABEL = "com.memory-daemon-maintenance"
LEGACY_LAUNCHD_LABEL = "com.workspace-daemon"
TICK_STALE_INTERVALS = 2
_LOG_LINE = re.compile(r"^(?P<at>\S+)\s+(?P<message>.*)$")
_TICK_PREFIX = (
    r"^tick(?:\[(?P<id>[^\]]+)\])?"
    r"(?:\((?P<group>capture|maintenance|all)\))?"
)
_TICK_DUE = re.compile(
    _TICK_PREFIX + r": due=(?P<ids>.*?)"
    r"(?P<dry> \(dry-run\))?$"
)
_TICK_DONE = re.compile(_TICK_PREFIX + r" done:")
_TICK_SKIPPED = re.compile(_TICK_PREFIX + r" skipped\b")
_TICK_NOOP = re.compile(
    _TICK_PREFIX + r": no routines due"
    r"(?P<dry> \(dry-run\))?$"
)
_TICK_TOTALS = re.compile(r"(?P<errors>\d+) error\(s\)")
_ROUTINE_ERROR = re.compile(
    r"^routine=(?P<id>\S+)\b.*\b(?:ERROR|FATAL)\b"
)
_LEGACY_TICK_KEY = "<legacy>"


def _routine_role(routine):
    """Explicit semantic purpose label; never infer intent from transport."""
    role = routine.get("role")
    return (
        role
        if isinstance(role, str) and role in config.VALID_ROUTINE_ROLES
        else "-"
    )


def _routine_sources(routine):
    """Declared connector kinds, in first-seen order."""
    kinds = list(dict.fromkeys(
        source.get("kind")
        for source in config.sources(routine)
        if source.get("kind")
    ))
    return "+".join(kinds) or "-"


def probe_launchd(label=DEFAULT_LAUNCHD_LABEL, uid=None):
    """Return selected launchd state without exposing the job environment."""
    uid = os.getuid() if uid is None else uid
    target = f"gui/{uid}/{label}"
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "loaded": False,
            "label": label,
            "detail": (
                "launchctl unavailable"
                if isinstance(exc, OSError)
                else "launchctl timed out"
            ),
        }
    if result.returncode != 0:
        return {"loaded": False, "label": label, "detail": "not loaded"}

    output = result.stdout

    def text_field(name):
        match = re.search(
            rf"^\s*{re.escape(name)} = (?P<value>.+?)\s*$",
            output,
            re.MULTILINE,
        )
        return match.group("value") if match else None

    def int_field(name):
        value = text_field(name)
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    interval = re.search(r"^\s*run interval = (\d+) seconds\s*$", output, re.MULTILINE)
    return {
        "loaded": True,
        "label": label,
        "state": text_field("state") or "loaded",
        "pid": int_field("pid"),
        "runs": int_field("runs"),
        "last_exit": int_field("last exit code"),
        "interval_seconds": int(interval.group(1)) if interval else None,
    }


def read_tick_history(path):
    """Summarize coordinator ticks and the last result for every routine."""
    path = Path(path)
    if not path.exists():
        return {"latest": None, "latest_by_group": {}, "routines": {}}
    try:
        # Status is interactive and must stay bounded even after years of
        # unattended operation. Rotation normally keeps this small; tailing is
        # a second line of defence for legacy/unrotated logs.
        max_bytes = 32 * 1024 * 1024
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size > max_bytes:
                handle.seek(-max_bytes, 2)
                handle.readline()  # discard a partial first line
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return {"latest": None, "latest_by_group": {}, "routines": {}}

    routine_results = {}
    active = {}
    latest = None
    latest_by_group = {}

    def tick_group(match):
        return match.group("group") or "all"

    def tick_key(match):
        return (tick_group(match), match.group("id") or _LEGACY_TICK_KEY)

    def record_latest(group, result):
        nonlocal latest
        latest = result
        latest_by_group[group] = result

    def close_incomplete(block):
        if not block:
            return
        if block["dry_run"]:
            return
        for routine_id in block["due_ids"]:
            routine_results[routine_id] = {
                "state": "incomplete",
                "at": block["at"],
                "group": block["group"],
            }

    for line in lines:
        match = _LOG_LINE.match(line)
        if not match:
            continue
        at = match.group("at")
        message = match.group("message")

        due = _TICK_DUE.match(message)
        if due:
            group = tick_group(due)
            key = tick_key(due)
            close_incomplete(active.pop(key, None))
            dry_run = bool(due.group("dry"))
            due_ids = {
                value.strip()
                for value in due.group("ids").split(",")
                if value.strip()
            }
            active[key] = {
                "at": at,
                "due_ids": due_ids,
                "dry_run": dry_run,
                "group": group,
                "routine_errors": {},
            }
            if not dry_run:
                record_latest(group, {
                    "state": "incomplete",
                    "at": at,
                    "message": message,
                    "group": group,
                })
            continue

        done = _TICK_DONE.match(message)
        if done:
            group = tick_group(done)
            key = tick_key(done)
            block = active.pop(key, None)
            dry_run = message.endswith(" (dry-run)") or bool(
                block and block["dry_run"]
            )
            totals = _TICK_TOTALS.search(message)
            error_count = int(totals.group("errors")) if totals else None
            if not dry_run:
                if block:
                    routine_errors = block.get("routine_errors", {})
                    attributed_count = sum(routine_errors.values())
                    attribution_complete = (
                        error_count is not None
                        and error_count > 0
                        and attributed_count >= error_count
                    )
                    for routine_id in block["due_ids"]:
                        routine_error_count = routine_errors.get(
                            routine_id, 0
                        )
                        failed = bool(error_count) and (
                            not attribution_complete
                            or routine_error_count > 0
                        )
                        routine_results[routine_id] = {
                            "state": "error" if failed else "ok",
                            "at": at,
                            "group": block["group"],
                        }
                        if failed and attribution_complete:
                            routine_results[routine_id]["errors"] = (
                                routine_error_count
                            )
                record_latest(group, {
                    "state": "error" if error_count else "done",
                    "at": at,
                    "message": message,
                    "group": group,
                })
            continue

        routine_error = _ROUTINE_ERROR.match(message)
        if routine_error:
            routine_id = routine_error.group("id")
            for block in active.values():
                if block["dry_run"] or routine_id not in block["due_ids"]:
                    continue
                counts = block["routine_errors"]
                counts[routine_id] = counts.get(routine_id, 0) + 1

        skipped = _TICK_SKIPPED.match(message)
        if skipped:
            group = tick_group(skipped)
            key = tick_key(skipped)
            block = active.pop(key, None)
            dry_run = message.endswith(" (dry-run)") or bool(
                block and block["dry_run"]
            )
            if not dry_run:
                if block:
                    for routine_id in block["due_ids"]:
                        routine_results[routine_id] = {
                            "state": "skipped",
                            "at": at,
                            "group": block["group"],
                        }
                record_latest(group, {
                    "state": "skipped",
                    "at": at,
                    "message": message,
                    "group": group,
                })
            continue

        noop = _TICK_NOOP.match(message)
        if noop:
            group = tick_group(noop)
            key = tick_key(noop)
            close_incomplete(active.pop(key, None))
            if not noop.group("dry"):
                record_latest(group, {
                    "state": "idle", "at": at, "message": message,
                    "group": group,
                })

    for block in active.values():
        close_incomplete(block)
    return {
        "latest": latest,
        "latest_by_group": latest_by_group,
        "routines": routine_results,
    }


def routine_rows(
    base_dir, routines, tick_history, now=None, scheduler_running=False,
    scheduler_running_groups=None,
):
    """Build privacy-safe status rows from durable state and operational logs."""
    now = time.time() if now is None else float(now)
    schedule = state.ScheduleStore(base_dir)
    ledger = state.load(base_dir)
    tick_results = tick_history.get("routines", {})
    latest_tick = tick_history.get("latest") or {}
    latest_by_group = tick_history.get("latest_by_group", {})
    if scheduler_running_groups is None:
        scheduler_running_groups = {"all"} if scheduler_running else set()
    else:
        scheduler_running_groups = set(scheduler_running_groups)
    rows = []

    for routine in routines:
        routine_id = routine["id"]
        enabled = routine.get("enabled", True)
        schedule_record = schedule.entries.get(routine_id) or {}
        last_epoch = schedule_record.get("last_attempted_epoch")
        if not isinstance(last_epoch, (int, float)):
            last_epoch = None

        entries = [
            entry for entry in ledger.values()
            if entry.get("rule_id") == routine_id
        ]
        captures = [
            entry["processed_at"]
            for entry in entries
            if entry.get("processed_at")
        ]
        memory_errors = sum(bool(entry.get("memory_error")) for entry in entries)
        expansion_fallbacks = sum(
            bool(entry.get("expand_fallback")) for entry in entries
        )
        pending_actions = sum(bool(entry.get("actions_pending")) for entry in entries)
        calendar_reviews = sum(
            bool(entry.get("calendar_match_rejected")) for entry in entries
        )
        issues = []
        if memory_errors:
            issues.append(f"{memory_errors} memory sink")
        if expansion_fallbacks:
            issues.append(f"{expansion_fallbacks} source expansion")
        if pending_actions:
            issues.append(f"{pending_actions} Gmail triage")
        if calendar_reviews:
            issues.append(f"{calendar_reviews} meeting match")

        tick_result = tick_results.get(routine_id) or {}
        tick_state = tick_result.get("state")
        tick_group = tick_result.get("group", "all")
        latest_for_group = latest_by_group.get(tick_group) or (
            latest_tick if tick_group == "all" else {}
        )
        matching_scheduler_running = (
            tick_group in scheduler_running_groups
            or (
                tick_group == "all"
                and bool(scheduler_running_groups)
            )
        )
        current_tick = (
            matching_scheduler_running
            and tick_state == "incomplete"
            and latest_for_group.get("state") == "incomplete"
            and tick_result.get("at") == latest_for_group.get("at")
        )
        if tick_state == "error":
            error_count = tick_result.get("errors")
            if isinstance(error_count, int) and error_count > 0:
                suffix = "error" if error_count == 1 else "errors"
                issues.append(f"{error_count} last-run {suffix}")
            else:
                issues.append("last run")
        elif tick_state == "skipped":
            issues.append("overlap")
        elif tick_state == "incomplete" and not current_tick:
            issues.append("incomplete")

        if not enabled:
            status_name = "disabled"
        elif current_tick:
            # The tick selects a batch. Its routines may be fetching,
            # processing, already finished, or still waiting their turn, so
            # membership alone is not proof of active execution.
            status_name = "in-tick"
        elif issues:
            status_name = "attention"
        elif schedule.due(routine, now=now):
            status_name = "due"
        else:
            status_name = "waiting"

        if enabled and last_epoch is not None:
            remaining = (
                config.next_due_epoch(routine, last_epoch, now) - now
            )
            next_run = "due" if remaining <= 0 else f"in {_duration(remaining)}"
        elif enabled:
            next_run = "due"
        else:
            next_run = "-"

        every = config.schedule_label(routine)
        rows.append({
            "routine": routine_id,
            "role": _routine_role(routine),
            "sources": _routine_sources(routine),
            "armed": "yes" if enabled else "no",
            "status": status_name,
            "every": every,
            "last_attempt": _age(last_epoch, now),
            "next": next_run,
            "last_capture": _iso_age(max(captures) if captures else None, now),
            "issues": ", ".join(issues) or "-",
        })
    return rows


def render(base_dir, routines, label=DEFAULT_LAUNCHD_LABEL, now=None):
    """Return ``(text, healthy)`` for the complete local daemon."""
    now = time.time() if now is None else float(now)
    launchd = probe_launchd(label)
    maintenance = (
        probe_launchd(MAINTENANCE_LAUNCHD_LABEL)
        if label == DEFAULT_LAUNCHD_LABEL else None
    )
    legacy = None
    if label == DEFAULT_LAUNCHD_LABEL:
        legacy = probe_launchd(LEGACY_LAUNCHD_LABEL)
    history = read_tick_history(Path(base_dir) / "logs" / "run.log")
    scheduler_running_groups = set()
    if launchd.get("state") == "running":
        scheduler_running_groups.add("capture")
    if maintenance and maintenance.get("state") == "running":
        scheduler_running_groups.add("maintenance")
    rows = routine_rows(
        base_dir,
        routines,
        history,
        now=now,
        scheduler_running_groups=scheduler_running_groups,
    )

    if launchd["loaded"]:
        state_name = launchd.get("state") or "loaded"
        if state_name == "not running":
            # StartInterval jobs exit between checks; that is healthy idle
            # behavior, not a stopped persistent service.
            state_name = "idle"
        # Arming (loaded in launchd) and active execution are separate facts.
        # StartInterval jobs are normally idle between their short-lived ticks.
        scheduler_bits = ["armed"]
        scheduler_bits.append(
            "tick running" if state_name == "running" else state_name
        )
        if launchd.get("pid") is not None:
            scheduler_bits.append(f"pid {launchd['pid']}")
        if launchd.get("interval_seconds") is not None:
            scheduler_bits.append(
                f"checks every {_duration(launchd['interval_seconds'])}"
            )
        if launchd.get("runs") is not None:
            scheduler_bits.append(f"{launchd['runs']} launches")
        if launchd.get("last_exit") is not None:
            scheduler_bits.append(f"last exit {launchd['last_exit']}")
        if legacy and legacy["loaded"]:
            scheduler_bits.append(
                f"legacy {LEGACY_LAUNCHD_LABEL} is also loaded; migrate it"
            )
    else:
        scheduler_bits = [f"not armed ({launchd['detail']})", label]
        if legacy and legacy["loaded"]:
            scheduler_bits.append(
                f"legacy {LEGACY_LAUNCHD_LABEL} is still loaded; migrate it"
            )

    latest_by_group = history.get("latest_by_group", {})
    grouped_history = any(
        group in latest_by_group for group in ("capture", "maintenance")
    )
    legacy_latest = latest_by_group.get("all") or (
        history.get("latest") if not grouped_history else None
    )
    capture_latest = latest_by_group.get("capture") or (
        legacy_latest if not grouped_history else None
    )
    maintenance_latest = (
        latest_by_group.get("maintenance")
        or (legacy_latest if not grouped_history else None)
        if maintenance is not None else None
    )
    capture_tick_issue = _tick_issue(launchd, capture_latest, now)
    maintenance_tick_issue = (
        _tick_issue(maintenance, maintenance_latest, now)
        if maintenance is not None else None
    )

    lines = [
        "Memory Daemon",
        f"Scheduler: {' · '.join(scheduler_bits)}",
        f"Next coordinator run: {_next_coordinator_run(launchd, legacy)}",
    ]
    if maintenance is not None:
        maintenance_bits = _scheduler_bits(maintenance)
        lines.extend([
            f"Maintenance scheduler: {' · '.join(maintenance_bits)}",
            f"Next maintenance run: {_next_coordinator_run(maintenance)}",
        ])
    lines.append(
        "Last capture tick: "
        f"{_tick_text(capture_latest, capture_tick_issue, now)}"
    )
    if maintenance is not None:
        lines.append(
            "Last maintenance tick: "
            f"{_tick_text(maintenance_latest, maintenance_tick_issue, now)}"
        )
    lines.append("")
    headers = (
        "ROUTINE", "ROLE", "SOURCES", "ARMED", "STATUS", "EVERY",
        "LAST ATTEMPT", "NEXT", "LAST CAPTURE", "ISSUES",
    )
    values = [
        (
            row["routine"], row["role"], row["sources"], row["armed"],
            row["status"], row["every"], row["last_attempt"],
            row["next"], row["last_capture"], row["issues"],
        )
        for row in rows
    ]
    lines.extend(_table(headers, values))
    lines.extend([
        "",
        "Logs: logs/run.log (rotated, owner-only)",
    ])

    scheduler_healthy = (
        launchd["loaded"]
        and launchd.get("last_exit") in (None, 0)
        and (
            maintenance is None
            or (
                maintenance.get("loaded")
                and maintenance.get("last_exit") in (None, 0)
            )
        )
        and capture_tick_issue is None
        and maintenance_tick_issue is None
        and not (legacy and legacy["loaded"])
    )
    routines_healthy = all(row["issues"] == "-" for row in rows)
    latest_healthy = all(
        _latest_tick_healthy(
            result,
            group in scheduler_running_groups,
        )
        for group, result in (
            ("capture", capture_latest),
            ("maintenance", maintenance_latest),
        )
        if result is not None
    )
    return "\n".join(lines), scheduler_healthy and routines_healthy and latest_healthy


def _tick_text(latest, issue, now):
    if latest:
        text = f"{_iso_age(latest['at'], now)} — {latest['message']}"
    else:
        text = "never recorded"
    if issue:
        text += f" · ATTENTION: {issue}"
    return text


def _latest_tick_healthy(latest, scheduler_running):
    if not latest or latest["state"] not in {"error", "incomplete"}:
        return True
    return latest["state"] == "incomplete" and scheduler_running


def _scheduler_bits(job):
    """Compact state for the independent maintenance LaunchAgent."""
    if not job.get("loaded"):
        return [f"not armed ({job.get('detail', 'not loaded')})", job.get("label", "?")]
    state_name = job.get("state") or "loaded"
    if state_name == "not running":
        state_name = "idle"
    bits = ["armed", "tick running" if state_name == "running" else state_name]
    if job.get("pid") is not None:
        bits.append(f"pid {job['pid']}")
    if job.get("interval_seconds") is not None:
        bits.append(f"checks every {_duration(job['interval_seconds'])}")
    if job.get("runs") is not None:
        bits.append(f"{job['runs']} launches")
    if job.get("last_exit") is not None:
        bits.append(f"last exit {job['last_exit']}")
    return bits


def _next_coordinator_run(launchd, legacy=None):
    """Describe launchd's next opportunity without inventing an exact time."""
    if launchd.get("loaded") and legacy and legacy.get("loaded"):
        return "multiple schedulers loaded; schedule ambiguous"
    if launchd.get("loaded"):
        return _loaded_coordinator_run(launchd)
    if legacy and legacy.get("loaded"):
        return f"legacy scheduler: {_loaded_coordinator_run(legacy)}"

    details = [launchd.get("detail")]
    if legacy is not None:
        details.append(legacy.get("detail"))
    if any(detail != "not loaded" for detail in details):
        return "schedule unavailable"
    return "not scheduled"


def _loaded_coordinator_run(launchd):
    """Describe the next opportunity for one known-loaded launchd job."""

    interval = launchd.get("interval_seconds")
    interval_text = (
        _exact_duration(interval)
        if isinstance(interval, int) and interval > 0
        else None
    )
    if launchd.get("state") == "running":
        if interval_text:
            return (
                "after current tick "
                f"(then within {interval_text})"
            )
        return "current tick running; future schedule unavailable"
    if interval_text:
        return f"within {interval_text}"
    return "schedule unavailable"


def _table(headers, rows):
    all_rows = [headers, *rows]
    widths = [
        max(len(str(row[index])) for row in all_rows)
        for index in range(len(headers))
    ]
    rendered = []
    for row_index, row in enumerate(all_rows):
        rendered.append(
            "  ".join(
                str(value).ljust(widths[index])
                for index, value in enumerate(row)
            ).rstrip()
        )
        if row_index == 0:
            rendered.append(
                "  ".join("-" * width for width in widths).rstrip()
            )
    return rendered


def _iso_age(value, now):
    if not value:
        return "never"
    try:
        parsed = datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return str(value)
    return _age(parsed, now)


def _tick_issue(launchd, latest, now):
    if not launchd["loaded"]:
        return None
    launches = launchd.get("runs")
    if latest is None:
        if isinstance(launches, int) and launches > 0:
            return f"no tick log after {launches} launch(es)"
        return None

    interval = launchd.get("interval_seconds")
    if not isinstance(interval, int) or interval <= 0:
        return None
    tick_epoch = _iso_epoch(latest.get("at"))
    if tick_epoch is None:
        return "last tick timestamp is invalid"
    stale_after = interval * TICK_STALE_INTERVALS
    if now - tick_epoch > stale_after:
        return (
            f"last tick is stale (expected within {_duration(stale_after)})"
        )
    return None


def _iso_epoch(value):
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return None


def _age(value, now):
    if value is None:
        return "never"
    delta = now - float(value)
    if delta < -1:
        return f"in {_duration(-delta)}"
    return f"{_duration(max(0, delta))} ago"


def _duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, remainder = divmod(seconds, 86400)
    hours = remainder // 3600
    return f"{days}d{hours}h" if hours else f"{days}d"


def _exact_duration(seconds):
    """Format an interval without rounding down an asserted upper bound."""
    remaining = max(0, int(seconds))
    parts = []
    for suffix, unit in (("d", 86400), ("h", 3600), ("m", 60)):
        value, remaining = divmod(remaining, unit)
        if value:
            parts.append(f"{value}{suffix}")
    if remaining or not parts:
        parts.append(f"{remaining}s")
    return "".join(parts)
