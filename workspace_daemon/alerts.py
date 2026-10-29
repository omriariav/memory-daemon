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
- `budget-invalid`: state/budget.yaml cannot be read and routines are held
  because of it.
- `usage-unreadable`: the usage log cannot be read. It alerts even when
  nothing is held, since spend and connector health are then unknown.
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


# Keys that cannot be evaluated while the usage log or budget file is
# unreadable: their saved state is kept rather than treated as cleared.
USAGE_KEYS = ("budget-hit:", "budget-80:", "hold:", "connector:")
BUDGET_KEYS = ("budget-hit:", "budget-80:", "hold:")


def budget_conditions(routines, entries, budget, now):
    """Active budget conditions as ({key: (message, details)}, quiet keys).

    Who is held comes from claude_bridge.hold_reason, the gate the runner
    applies, evaluated on the same snapshot of the budget file and usage log
    as the spend figures. `entries` is the usage log (or a BridgeError
    reading it) and `budget` is load_budget's (cap, tz) (or a BridgeError).
    Quiet keys are active but covered by the cap-hit alert: they are neither
    sent nor recorded, so one still active after the cap hit clears alerts
    then.
    """
    found, quiet = {}, set()
    held = {}
    for routine in routines:
        if not routine.get("enabled", True):
            continue
        reason = claude_bridge.hold_reason(
            budget, entries, routine["id"], config.budget_cap(routine),
            now=now, include_global=config.uses_claude_bridge(routine),
        )
        if reason:
            held[routine["id"]] = reason
    # One key per broken file, so fixing one cannot re-announce the other.
    for key, problem in (("budget-invalid", budget),
                         ("usage-unreadable", entries)):
        if not isinstance(problem, Exception):
            continue
        blocked = sorted(rid for rid, (kind, _) in held.items() if kind == key)
        if key == "budget-invalid" and not blocked:
            continue
        found[key] = (
            f"{problem}; holding {', '.join(blocked) or 'nothing'}",
            {"held": blocked},
        )
    if isinstance(entries, Exception):
        return found, quiet
    cap, tz = (None, None) if isinstance(budget, Exception) else budget
    day_start = claude_bridge.day_start_epoch(now, tz)
    day = datetime.datetime.fromtimestamp(day_start, tz).date().isoformat()
    spent = status.spend_by_routine(entries, day_start)
    # Summed per entry in log order, exactly as hold_reason does, so float
    # rounding at the cap cannot split the alert from the gate.
    total = sum(claude_bridge.entry_cost(entry)
                for epoch, entry in entries if epoch >= day_start)
    hit = cap is not None and total >= cap
    for routine in routines:
        own = config.budget_cap(routine)
        rid = routine["id"]
        used = spent.get(rid, 0.0)
        if not routine.get("enabled", True) or own is None or used < own:
            continue
        # Keyed on the routine's own cap, not on which cap the gate reports
        # first, so a later global hit (or its lifting) cannot re-alert it.
        # Hidden behind a cap hit that already lists it.
        key = f"hold:{rid}:{day}"
        found[key] = (
            f"{rid} held: its own cap ${own:g} reached (${used:.2f} today)",
            {"routine": rid, "spent_usd": round(used, 2), "cap_usd": own},
        )
        if hit and held.get(rid, ("",))[0] == "global":
            quiet.add(key)
    if cap is None:
        return found, quiet
    spend = {tag: round(value, 2) for tag, value in spent.items()}
    if hit:
        by_global = sorted(rid for rid, (kind, _) in held.items()
                           if kind == "global")
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
    return found, quiet


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
        return found  # reported by budget_conditions as usage-unreadable
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
    """Saved alert state: key -> sent time, or {"pending": time}.

    Anything else in the file is dropped, so its key counts as new.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value for key, value in data.items()
        if (isinstance(value, str) and value)
        or (isinstance(value, dict) and isinstance(value.get("pending"), str)
            and value["pending"])
    }


def check(base_dir, routines, now=None):
    """Alert on newly active conditions; forget cleared ones.

    Returns the keys sent this tick (new ones and retried pending ones).

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
    active, quiet = budget_conditions(routines, entries, budget, now)
    active.update(health_conditions(base_dir, routines, entries, now))
    seen = _load(path)
    stamp = datetime.datetime.fromtimestamp(
        now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # alerts.json maps key -> time first sent, or {"pending": time} while a
    # send is unconfirmed. Pending keys are saved before sending, so a save
    # that keeps failing sends nothing (no flood), and an unconfirmed send is
    # retried rather than lost.
    due = sorted(
        key for key in active
        if key not in quiet and (key not in seen or isinstance(seen[key], dict))
    )
    unknown = (USAGE_KEYS if isinstance(entries, Exception) else ()) + (
        BUDGET_KEYS if isinstance(budget, Exception) else ())
    remembered = {
        key: value for key, value in seen.items()
        if unknown and key.startswith(unknown)
    }
    remembered.update({
        key: seen.get(key) or {"pending": stamp}
        for key in active if key in seen or key not in quiet
    })
    if due or remembered != seen:
        # Saved on every tick with something to send: if saving fails, nothing
        # is sent, so a broken alerts.json cannot cause a resend per tick.
        _save(path, remembered)
    sent = []
    for key in due:
        message, details = active[key]
        if key not in seen:
            log(f"alert {key}: {message}")
        # emit logs its own failure; the key stays pending and is retried.
        if events.emit(base_dir, events.build(
            "daemon", "alert", None, f"daemon:{key}", message, [],
            ts=stamp, details={"key": key, **details},
        )):
            remembered[key] = stamp
            sent.append(key)
            # Saved per send, so a later failure cannot resend this one.
            _save(path, remembered)
    return sent


def _save(path, remembered):
    state.write_atomic(path, json.dumps(remembered, sort_keys=True), mode=0o600)
