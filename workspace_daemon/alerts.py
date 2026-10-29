"""Operational alerts: budget pressure, budget holds and unhealthy routines.

Every scheduler tick that passes the pause and preflight checks calls
`check` (dry runs do not). It evaluates a fixed set of conditions,
and each condition that becomes true is announced once: a line in the run log
and an event in `state/events.jsonl` with source "daemon" and kind "alert",
which the notifier (capture-events.py + notify.json) delivers. A condition
stays quiet while it holds and may alert again only after it clears.

Conditions (`key` in the event's details):

- `budget-80:<day>`: today's spend reached 80% of state/budget.yaml daily_usd.
- `budget-hit:<day>`: the global cap is reached, so every Claude routine is
  held. It replaces the 80% condition, so a jump past the cap alerts once.
- `budget-invalid`: state/budget.yaml cannot be read, which also holds them.
- `usage-unreadable`: the usage log cannot be read, so spend is unknown and
  Claude routines and routines with their own cap are held.
- `hold:<routine>:<day>`: a routine reached its own budget.daily_usd. Holds
  caused by the global cap are listed on `budget-hit` instead, so a cap hit
  sends one alert, not one per routine.
- `attention:<routine>`: `status` would show the routine as "attention".
  Tick states that a running tick produces on its own (incomplete, overlap)
  are ignored.
- `connector:<server>`: an MCP connector's last error has been newer than
  its last success for more than CONNECTOR_GRACE_SECONDS (counted from the
  first error since that success).

Budget conditions carry spend by routine. Deduplication state lives in
`state/alerts.json`; `state/alerts.lock` keeps concurrent ticks (capture,
maintenance, calendar) from alerting twice.
"""
import datetime
import fcntl
import json
import os
import time
from pathlib import Path

from . import claude_bridge, config, events, state, status
from .shell import log

ALERT_FILE = "alerts.json"
LOCK_FILE = "alerts.lock"
WARN_FRACTION = 0.8
CONNECTOR_GRACE_SECONDS = 3600
# Produced by ticks still in flight in another scheduler group, not by faults.
TRANSIENT_ISSUES = ("incomplete", "overlap")


def _spend_text(spent):
    return ", ".join(
        f"{tag} ${spent[tag]:.2f}"
        for tag in sorted(spent, key=lambda tag: (-spent[tag], tag))
    ) or "no spend"


def budget_conditions(routines, entries, budget, now):
    """Active budget conditions as {key: (message, details)}.

    `entries` is the usage log (or a BridgeError reading it) and `budget` is
    load_budget's (cap, tz) (or a BridgeError).
    """
    found = {}
    bridged = sorted(
        routine["id"] for routine in routines
        if routine.get("enabled", True) and config.uses_claude_bridge(routine)
    )
    capped = sorted(
        routine["id"] for routine in routines
        if routine.get("enabled", True) and config.budget_cap(routine)
    )
    if isinstance(budget, claude_bridge.BridgeError):
        if bridged:
            found["budget-invalid"] = (
                f"Budget unreadable ({budget}); holding {', '.join(bridged)}",
                {"held": bridged},
            )
        return found
    if isinstance(entries, claude_bridge.BridgeError):
        held = sorted(set(bridged) | set(capped))
        if held:
            found["usage-unreadable"] = (
                f"Spend unknown ({entries}); holding {', '.join(held)}",
                {"held": held},
            )
        return found
    cap, tz = budget
    day_start = claude_bridge.day_start_epoch(now, tz)
    day = datetime.datetime.fromtimestamp(day_start, tz).date().isoformat()
    spent = status.spend_by_routine(entries, day_start)
    total = sum(spent.values())
    spend = {tag: round(value, 2) for tag, value in spent.items()}
    hit = cap is not None and total >= cap
    if hit:
        found[f"budget-hit:{day}"] = (
            f"Global Claude cap reached: ${total:.2f} of ${cap:g}; "
            f"holding {', '.join(bridged) or 'nothing'}. "
            f"Spend: {_spend_text(spent)}",
            {"spent_usd": round(total, 2), "cap_usd": cap,
             "held": bridged, "by_routine": spend},
        )
    elif cap is not None and total >= WARN_FRACTION * cap:
        found[f"budget-80:{day}"] = (
            f"Claude spend at {total / cap:.0%} of the ${cap:g} daily cap "
            f"(${total:.2f}). Spend: {_spend_text(spent)}",
            {"spent_usd": round(total, 2), "cap_usd": cap,
             "by_routine": spend},
        )
    for routine in routines:
        own = config.budget_cap(routine)
        if not routine.get("enabled", True) or own is None:
            continue
        rid = routine["id"]
        if hit and rid in bridged:
            continue  # already listed as held on the cap hit
        used = spent.get(rid, 0.0)
        if used >= own:
            found[f"hold:{rid}:{day}"] = (
                f"{rid} held: its own cap ${own:g} reached (${used:.2f} today)",
                {"routine": rid, "spent_usd": round(used, 2), "cap_usd": own},
            )
    return found


def health_conditions(base_dir, routines, entries, now):
    """Routines `status` flags for attention, and connectors failing too long."""
    found = {}
    history = status.read_tick_history(Path(base_dir) / "logs" / "run.log")
    for row in status.routine_rows(base_dir, routines, history, now=now):
        if row["issues"] == "-" or row["armed"] != "yes":
            continue
        issues = [
            issue for issue in row["issues"].split(", ")
            if issue not in TRANSIENT_ISSUES
        ]
        if issues:
            found[f"attention:{row['routine']}"] = (
                f"{row['routine']} needs attention: {', '.join(issues)}",
                {"routine": row["routine"], "issues": issues},
            )
    if isinstance(entries, claude_bridge.BridgeError):
        return found  # reported as usage-unreadable
    # connector_times keeps first_error only while no ok has followed it.
    for server, first in status.connector_times(entries)["first_error"].items():
        if now - first >= CONNECTOR_GRACE_SECONDS:
            found[f"connector:{server}"] = (
                f"Connector {server} failing for "
                f"{status._duration(now - first)} with no successful call",
                {"connector": server},
            )
    return found


def _load(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def check(base_dir, routines, now=None):
    """Alert on newly active conditions; forget cleared ones. Returns new keys.

    Never raises: alerting must not break the tick that runs it.
    """
    now = time.time() if now is None else now
    lock_fd = None
    try:
        state_dir = state.ensure_private_dir(Path(base_dir) / "state")
        lock_fd = os.open(
            str(state_dir / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return []  # another tick is checking right now
        return _check_locked(base_dir, routines, now, state_dir / ALERT_FILE)
    except Exception as exc:  # noqa: BLE001 - see docstring
        log(f"alerts WARN check failed: {type(exc).__name__}: {exc}")
        return []
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _read(loader):
    try:
        return loader()
    except claude_bridge.BridgeError as exc:
        return exc


def _check_locked(base_dir, routines, now, path):
    entries = _read(lambda: claude_bridge.recent_usage(base_dir, now=now))
    budget = _read(lambda: claude_bridge.load_budget(base_dir))
    active = budget_conditions(routines, entries, budget, now)
    active.update(health_conditions(base_dir, routines, entries, now))
    seen = _load(path)
    new = [key for key in active if key not in seen]
    stamp = datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    remembered = {key: seen.get(key) or stamp for key in active}
    if remembered != seen:
        # Saved before delivery: a failed save sends nothing and retries next
        # tick, rather than repeating the alert on every tick.
        _save(path, remembered)
    undelivered = []
    for key in sorted(new):
        message, details = active[key]
        log(f"alert {key}: {message}")
        if not events.emit(base_dir, events.build(
            "daemon", "alert", None, f"daemon:{key}", message, [],
            ts=stamp, details={"key": key, **details},
        )):
            undelivered.append(key)
    if undelivered:
        # Forget them so the next tick tries again.
        for key in undelivered:
            remembered.pop(key)
        _save(path, remembered)
    return [key for key in sorted(new) if key not in undelivered]


def _save(path, remembered):
    state.write_atomic(path, json.dumps(remembered, sort_keys=True), mode=0o600)
