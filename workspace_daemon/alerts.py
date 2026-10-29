"""Operational alerts: budget pressure, budget holds and unhealthy routines.

Every scheduler tick that passes the pause and preflight checks and finds
every routine valid calls `check` (dry runs do not). It evaluates a fixed set of conditions,
and each condition that becomes true is announced once: a line in the run log
and an event in `state/events.jsonl` with source "daemon" and kind "alert",
which the notifier (capture-events.py + notify.json) delivers. A condition
stays quiet while it holds and may alert again only after it clears.

Conditions (`key` in the event's details):

- `budget-80:<day>`: today's spend reached 80% of state/budget.yaml daily_usd.
- `budget-hit:<day>`: the global cap is reached, so every Claude routine is
  held. It replaces the 80% condition, so a jump past the cap alerts once.
- `budget-invalid` / `usage-unreadable`: state/budget.yaml or the usage log
  cannot be read, and routines are held because of it.
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


def budget_conditions(base_dir, routines, entries, budget, now):
    """Active budget conditions as {key: (message, details)}.

    Who is held comes from claude_bridge.budget_exhausted, the same gate the
    runner applies, so the alert cannot drift from what actually stops.
    `entries` is the usage log (or a BridgeError reading it) and `budget` is
    load_budget's (cap, tz) (or a BridgeError).
    """
    found = {}
    held = {}
    for routine in routines:
        if not routine.get("enabled", True):
            continue
        reason = claude_bridge.budget_exhausted(
            base_dir, routine["id"], routine_cap=config.budget_cap(routine),
            now=now, include_global=config.uses_claude_bridge(routine),
        )
        if reason:
            held[routine["id"]] = reason
    by_global = sorted(rid for rid, why in held.items()
                       if why.startswith("global cap"))
    unknown = sorted(rid for rid, why in held.items()
                     if not why.startswith(("global cap", "routine cap")))
    if unknown:
        problems = sorted({held[rid] for rid in unknown})
        key = ("budget-invalid" if any("budget.yaml" in p for p in problems)
               else "usage-unreadable")
        found[key] = (
            f"Spend cannot be checked ({'; '.join(problems)}); "
            f"holding {', '.join(unknown)}",
            {"held": unknown},
        )
    tz = None if isinstance(budget, Exception) else budget[1]
    day_start = claude_bridge.day_start_epoch(now, tz)
    day = datetime.datetime.fromtimestamp(day_start, tz).date().isoformat()
    if isinstance(entries, Exception):
        return found
    spent = status.spend_by_routine(entries, day_start)
    for rid, why in sorted(held.items()):
        if why.startswith("routine cap"):
            found[f"hold:{rid}:{day}"] = (
                f"{rid} held: {why} (${spent.get(rid, 0.0):.2f} today)",
                {"routine": rid, "spent_usd": round(spent.get(rid, 0.0), 2)},
            )
    if isinstance(budget, Exception) or budget[0] is None:
        return found
    cap = budget[0]
    total = sum(spent.values())
    spend = {tag: round(value, 2) for tag, value in spent.items()}
    if total >= cap:
        found[f"budget-hit:{day}"] = (
            f"Global Claude cap reached: ${total:.2f} of ${cap:g}; "
            f"holding {', '.join(by_global) or 'nothing'}. "
            f"Spend: {_spend_text(spent)}",
            {"spent_usd": round(total, 2), "cap_usd": cap,
             "held": by_global, "by_routine": spend},
        )
    elif total >= WARN_FRACTION * cap:
        found[f"budget-80:{day}"] = (
            f"Claude spend at {total / cap:.0%} of the ${cap:g} daily cap "
            f"(${total:.2f}). Spend: {_spend_text(spent)}",
            {"spent_usd": round(total, 2), "cap_usd": cap,
             "by_routine": spend},
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
    active = budget_conditions(base_dir, routines, entries, budget, now)
    active.update(health_conditions(base_dir, routines, entries, now))
    seen = _load(path)
    stamp = datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sent = []
    for key in sorted(key for key in active if key not in seen):
        message, details = active[key]
        # emit logs its own failure; an unsent key stays out of alerts.json
        # so the next tick retries it.
        if events.emit(base_dir, events.build(
            "daemon", "alert", None, f"daemon:{key}", message, [],
            ts=stamp, details={"key": key, **details},
        )):
            log(f"alert {key}: {message}")
            sent.append(key)
    remembered = {
        key: seen.get(key) or stamp for key in active
        if key in seen or key in sent
    }
    if remembered != seen:
        # Saved after delivery: a failed save can repeat an alert next tick,
        # but never loses one.
        _save(path, remembered)
    return sent


def _save(path, remembered):
    state.write_atomic(path, json.dumps(remembered, sort_keys=True), mode=0o600)
