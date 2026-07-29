"""Read-only health reporting for the scheduler and its routines."""
import datetime
import os
import re
import subprocess
import time
from pathlib import Path

from . import config, state


DEFAULT_LAUNCHD_LABEL = "com.memory-daemon"
_LOG_LINE = re.compile(r"^(?P<at>\S+)\s+(?P<message>.*)$")
_ROUTINE_ERROR = re.compile(
    r"\broutine=(?P<routine>[a-z0-9][a-z0-9-]*)\b.*\b(?:ERROR|FATAL)\b"
)
_TICK_TOTALS = re.compile(r"(?P<errors>\d+) error\(s\)")


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
    active = None
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

        if message.startswith("tick: due="):
            close_incomplete(active)
            dry_run = message.endswith(" (dry-run)")
            raw_ids = message.removeprefix("tick: due=").split(" (dry-run)", 1)[0]
            due_ids = {
                value.strip() for value in raw_ids.split(",") if value.strip()
            }
            active = {
                "at": at,
                "due_ids": due_ids,
                "error_routines": set(),
                "dry_run": dry_run,
            }
            latest = {
                "state": "dry-run" if dry_run else "incomplete",
                "at": at,
                "message": message,
            }
            continue

        if active:
            error = _ROUTINE_ERROR.search(message)
            if error:
                active["error_routines"].add(error.group("routine"))

            if message.startswith("tick done:"):
                totals = _TICK_TOTALS.search(message)
                error_count = int(totals.group("errors")) if totals else None
                mapped = active["error_routines"]
                unknown_error = bool(error_count) and not mapped
                if not active["dry_run"]:
                    for routine_id in active["due_ids"]:
                        routine_results[routine_id] = {
                            "state": (
                                "error"
                                if routine_id in mapped or unknown_error
                                else "ok"
                            ),
                            "at": at,
                        }
                latest = {
                    "state": "dry-run" if active["dry_run"] else "done",
                    "at": at,
                    "message": (
                        f"{message} (dry-run)"
                        if active["dry_run"] else message
                    ),
                }
                active = None
                continue

            if message.startswith("tick skipped"):
                if not active["dry_run"]:
                    for routine_id in active["due_ids"]:
                        routine_results[routine_id] = {
                            "state": "skipped",
                            "at": at,
                        }
                latest = {"state": "skipped", "at": at, "message": message}
                active = None
                continue

        if message == "tick: no routines due":
            close_incomplete(active)
            active = None
            latest = {"state": "idle", "at": at, "message": message}

    if active and not active["dry_run"]:
        for routine_id in active["due_ids"]:
            routine_results[routine_id] = {
                "state": "incomplete",
                "at": active["at"],
            }
    return {"latest": latest, "routines": routine_results}


def routine_rows(
    base_dir, routines, tick_history, now=None, scheduler_running=False
):
    """Build privacy-safe status rows from durable state and operational logs."""
    now = time.time() if now is None else float(now)
    schedule = state.ScheduleStore(base_dir)
    ledger = state.load(base_dir)
    tick_results = tick_history.get("routines", {})
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
        issues = []
        if memory_errors:
            issues.append(f"{memory_errors} memory sink")
        if pending_actions:
            issues.append(f"{pending_actions} Gmail triage")

        tick_result = tick_results.get(routine_id) or {}
        tick_state = tick_result.get("state")
        if tick_state == "error":
            issues.append("last run")
        elif tick_state == "skipped":
            issues.append("overlap")
        elif tick_state == "incomplete" and not scheduler_running:
            issues.append("incomplete")

        if not enabled:
            status_name = "disabled"
        elif tick_state == "incomplete" and scheduler_running:
            status_name = "running"
        elif issues:
            status_name = "attention"
        elif schedule.due(routine, now=now):
            status_name = "due"
        else:
            status_name = "ok"

        if enabled and last_epoch is not None:
            remaining = last_epoch + config.schedule_seconds(routine) - now
            next_run = "due" if remaining <= 0 else f"in {_duration(remaining)}"
        elif enabled:
            next_run = "due"
        else:
            next_run = "-"

        every = (routine.get("schedule") or {}).get(
            "every", config.DEFAULT_SCHEDULE
        )
        rows.append({
            "routine": routine_id,
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
    else:
        scheduler_bits = [launchd["detail"], label]

    latest = history.get("latest")
    if latest:
        tick_text = f"{_iso_age(latest['at'], now)} — {latest['message']}"
    else:
        tick_text = "never recorded"

    lines = [
        "Memory Daemon",
        f"Scheduler: {' · '.join(scheduler_bits)}",
        f"Last tick: {tick_text}",
        "",
    ]
    headers = (
        "ROUTINE", "STATUS", "EVERY", "LAST ATTEMPT",
        "NEXT", "LAST CAPTURE", "ISSUES",
    )
    values = [
        (
            row["routine"], row["status"], row["every"], row["last_attempt"],
            row["next"], row["last_capture"], row["issues"],
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
    )
    routines_healthy = all(row["issues"] == "-" for row in rows)
    latest_healthy = (
        not latest
        or latest["state"] != "incomplete"
        or launchd.get("state") == "running"
    )
    return "\n".join(lines), scheduler_healthy and routines_healthy and latest_healthy


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
