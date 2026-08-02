"""Read-only health reporting for the scheduler and its routines."""
import datetime
import os
import re
import subprocess
import time
from pathlib import Path

from . import config, state


DEFAULT_LAUNCHD_LABEL = "com.memory-daemon"
LEGACY_LAUNCHD_LABEL = "com.workspace-daemon"
TICK_STALE_INTERVALS = 2
_LOG_LINE = re.compile(r"^(?P<at>\S+)\s+(?P<message>.*)$")
_TICK_DUE = re.compile(
    r"^tick(?:\[(?P<id>[^\]]+)\])?: due=(?P<ids>.*?)"
    r"(?P<dry> \(dry-run\))?$"
)
_TICK_DONE = re.compile(r"^tick(?:\[(?P<id>[^\]]+)\])? done:")
_TICK_SKIPPED = re.compile(r"^tick(?:\[(?P<id>[^\]]+)\])? skipped\b")
_TICK_NOOP = re.compile(
    r"^tick(?:\[(?P<id>[^\]]+)\])?: no routines due"
    r"(?P<dry> \(dry-run\))?$"
)
_TICK_TOTALS = re.compile(r"(?P<errors>\d+) error\(s\)")
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
        return {"latest": None, "routines": {}}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return {"latest": None, "routines": {}}

    routine_results = {}
    active = {}
    latest = None

    def close_incomplete(block):
        if not block:
            return
        if block["dry_run"]:
            return
        for routine_id in block["due_ids"]:
            routine_results[routine_id] = {
                "state": "incomplete",
                "at": block["at"],
            }

    for line in lines:
        match = _LOG_LINE.match(line)
        if not match:
            continue
        at = match.group("at")
        message = match.group("message")

        due = _TICK_DUE.match(message)
        if due:
            key = due.group("id") or _LEGACY_TICK_KEY
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
            }
            if not dry_run:
                latest = {
                    "state": "incomplete",
                    "at": at,
                    "message": message,
                }
            continue

        done = _TICK_DONE.match(message)
        if done:
            key = done.group("id") or _LEGACY_TICK_KEY
            block = active.pop(key, None)
            dry_run = message.endswith(" (dry-run)") or bool(
                block and block["dry_run"]
            )
            totals = _TICK_TOTALS.search(message)
            error_count = int(totals.group("errors")) if totals else None
            if not dry_run:
                if block:
                    for routine_id in block["due_ids"]:
                        routine_results[routine_id] = {
                            "state": "error" if error_count else "ok",
                            "at": at,
                        }
                latest = {
                    "state": "error" if error_count else "done",
                    "at": at,
                    "message": message,
                }
            continue

        skipped = _TICK_SKIPPED.match(message)
        if skipped:
            key = skipped.group("id") or _LEGACY_TICK_KEY
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
                        }
                latest = {
                    "state": "skipped",
                    "at": at,
                    "message": message,
                }
            continue

        noop = _TICK_NOOP.match(message)
        if noop:
            key = noop.group("id") or _LEGACY_TICK_KEY
            close_incomplete(active.pop(key, None))
            if not noop.group("dry"):
                latest = {"state": "idle", "at": at, "message": message}

    for block in active.values():
        close_incomplete(block)
    return {"latest": latest, "routines": routine_results}


def routine_rows(
    base_dir, routines, tick_history, now=None, scheduler_running=False
):
    """Build privacy-safe status rows from durable state and operational logs."""
    now = time.time() if now is None else float(now)
    schedule = state.ScheduleStore(base_dir)
    ledger = state.load(base_dir)
    tick_results = tick_history.get("routines", {})
    latest_tick = tick_history.get("latest") or {}
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
        pending_actions = sum(bool(entry.get("actions_pending")) for entry in entries)
        calendar_reviews = sum(
            bool(entry.get("calendar_match_rejected")) for entry in entries
        )
        issues = []
        if memory_errors:
            issues.append(f"{memory_errors} memory sink")
        if pending_actions:
            issues.append(f"{pending_actions} Gmail triage")
        if calendar_reviews:
            issues.append(f"{calendar_reviews} meeting match")

        tick_result = tick_results.get(routine_id) or {}
        tick_state = tick_result.get("state")
        current_tick = (
            scheduler_running
            and tick_state == "incomplete"
            and latest_tick.get("state") == "incomplete"
            and tick_result.get("at") == latest_tick.get("at")
        )
        if tick_state == "error":
            issues.append("last run")
        elif tick_state == "skipped":
            issues.append("overlap")
        elif tick_state == "incomplete" and not current_tick:
            issues.append("incomplete")

        if not enabled:
            status_name = "disabled"
        elif current_tick:
            status_name = "running"
        elif issues:
            status_name = "attention"
        elif schedule.due(routine, now=now):
            status_name = "due"
        else:
            status_name = "ok"

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
    legacy = None
    if label == DEFAULT_LAUNCHD_LABEL:
        legacy = probe_launchd(LEGACY_LAUNCHD_LABEL)
    history = read_tick_history(Path(base_dir) / "logs" / "run.log")
    rows = routine_rows(
        base_dir,
        routines,
        history,
        now=now,
        scheduler_running=launchd.get("state") == "running",
    )

    if launchd["loaded"]:
        state_name = launchd.get("state") or "loaded"
        if state_name == "not running":
            # StartInterval jobs exit between checks; that is healthy idle
            # behavior, not a stopped persistent service.
            state_name = "idle"
        scheduler_bits = [f"loaded ({state_name})"]
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
        scheduler_bits = [launchd["detail"], label]
        if legacy and legacy["loaded"]:
            scheduler_bits.append(
                f"legacy {LEGACY_LAUNCHD_LABEL} is still loaded; migrate it"
            )

    latest = history.get("latest")
    tick_issue = _tick_issue(launchd, latest, now)
    if latest:
        tick_text = f"{_iso_age(latest['at'], now)} — {latest['message']}"
    else:
        tick_text = "never recorded"
    if tick_issue:
        tick_text += f" · ATTENTION: {tick_issue}"

    lines = [
        "Memory Daemon",
        f"Scheduler: {' · '.join(scheduler_bits)}",
        f"Next coordinator run: {_next_coordinator_run(launchd, legacy)}",
        f"Last tick: {tick_text}",
        "",
    ]
    headers = (
        "ROUTINE", "ROLE", "SOURCES", "STATUS", "EVERY", "LAST ATTEMPT",
        "NEXT", "LAST CAPTURE", "ISSUES",
    )
    values = [
        (
            row["routine"], row["role"], row["sources"], row["status"],
            row["every"], row["last_attempt"], row["next"],
            row["last_capture"], row["issues"],
        )
        for row in rows
    ]
    lines.extend(_table(headers, values))
    lines.extend([
        "",
        "Logs: logs/run.log · logs/launchd.err.log",
    ])

    scheduler_healthy = (
        launchd["loaded"]
        and launchd.get("last_exit") in (None, 0)
        and tick_issue is None
        and not (legacy and legacy["loaded"])
    )
    routines_healthy = all(row["issues"] == "-" for row in rows)
    latest_healthy = (
        not latest
        or latest["state"] not in {"error", "incomplete"}
        or (
            latest["state"] == "incomplete"
            and launchd.get("state") == "running"
        )
    )
    return "\n".join(lines), scheduler_healthy and routines_healthy and latest_healthy


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
