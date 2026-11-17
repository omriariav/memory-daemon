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
  are ignored, and so is "interrupted": a failure whose log line names a
  sleep or offline outage is the laptop's, not the routine's. Last-run
  errors alert only on a streak: the last ERROR_STREAK ticks in a row ended
  in error (interrupted ticks neither count nor break the streak, a clean
  tick ends it), or the routine has had no clean tick for AWAKE_WINDOW_SECONDS
  of awake time since the streak's first error. Awake time sums the gaps
  between consecutive daemon ticks that are at most status.AWAKE_GAP_CAP_SECONDS
  apart; a longer gap is sleep and adds nothing. The second rule keeps
  rarely scheduled routines alerting. Only a measured sleep or a dead network
  counts as an outage; timeouts and refused connections stay errors. As a
  safety net, `interrupted:<routine>` alerts (separately from `attention:`,
  so a change of cause alerts again) when at least INTERRUPTED_STREAK ticks in
  a row were interrupted, with no clean or error tick between, and
  AWAKE_WINDOW_SECONDS of awake time passed since the first. A tick the budget held
  neither counts nor resets anything.
- `interrupted:<routine>`: the safety net for outages (see `attention:`).
- `connector:<server>`: an MCP connector's last error has been newer than
  its last success for more than CONNECTOR_GRACE_SECONDS (counted from the
  first error since that success).

Every alert's details may also say why (all optional; see `cause_of`):

- `cause`: connector_auth, connector_down, tool_error, memory_duplicate,
  meeting_match, budget, run_state or unknown.
- `last_error`: the newest ERROR/FATAL line of the routine in logs/run.log
  since its streak began (only the last LOG_TAIL_BYTES are read), cut to
  LAST_ERROR_CHARS with ids and addresses removed.
- `since`: when the streak began (UTC, ISO 8601).
- `remedy`: short generic advice. `state/daemon.yaml` may add or replace the
  text per alert-key prefix (`alert_remedies`, see `load_remedies`).
- `connector`: the connector's server name, when the cause names one.

"meeting match" (a recording not matched to a calendar meeting) and a memory
sink error that is only a near-duplicate are review items, not failures: they
never raise `attention:` by themselves.

Budget conditions carry spend by routine. Deduplication state lives in
`state/alerts.json`; `state/alerts.lock` keeps concurrent ticks (capture,
maintenance, calendar) from alerting twice.
"""
import datetime
import fcntl
import json
import os
import re
import time
from pathlib import Path

import yaml

from . import claude_bridge, config, events, sleepclock, state, status
from .shell import log

ALERT_FILE = "alerts.json"
LOCK_FILE = "alerts.lock"
WARN_FRACTION = 0.8
CONNECTOR_GRACE_SECONDS = 3600
# Failed ticks in a row (interrupted ones do not count) that raise attention.
ERROR_STREAK = 3
# Interrupted ticks in a row (no clean or error tick between) that, with
# AWAKE_WINDOW_SECONDS of awake time since the first, raise `interrupted:`.
INTERRUPTED_STREAK = 3
# Awake time with no clean tick, counted from the first error of the streak,
# that raises attention even when fewer than ERROR_STREAK ticks ran.
AWAKE_WINDOW_SECONDS = 2 * 3600
# Produced by ticks still in flight in another scheduler group, or by an
# outage of the machine, not by faults.
TRANSIENT_ISSUES = ("incomplete", "overlap", "interrupted")
# status's wording for a failed last tick; the streak rules replace it.
_LAST_RUN_ISSUE = re.compile(r"^(?:\d+ last-run errors?|last run)$")

# Review items, not failures: a recording that matched no calendar meeting,
# and (when the log says so) a memory sink error that is a near-duplicate.
_REVIEW_ISSUE = re.compile(r"^\d+ meeting match$")
_MEMORY_SINK_LINE = re.compile(r"^routine=\S+ memory ERROR\b")
_MEMORY_SINK_ISSUE = re.compile(r"^\d+ memory sink$")

LOG_TAIL_BYTES = 256 * 1024
LAST_ERROR_CHARS = 160
MAX_REMEDY_CHARS = 300
CAUSES = ("connector_auth", "connector_down", "tool_error", "memory_duplicate",
          "meeting_match", "budget", "run_state", "unknown")
# Generic advice; `{server}` is the connector name taken from the data.
REMEDIES = {
    "connector_auth": ("Sign in to the {server} connector again (in Claude "
                       "Code: /mcp), then the routine retries on its next run"),
    "connector_down": ("The {server} connector is not answering; check it in "
                       "Claude Code (/mcp). The routine retries on its next "
                       "run"),
    "tool_error": ("A connector tool call failed; the routine retries on its "
                   "next run. If it keeps failing, check the connector "
                   "(/mcp) and its access"),
    "memory_duplicate": ("The memory store skipped a near-duplicate entry; "
                         "nothing to fix"),
    "meeting_match": ("A recording matched no calendar meeting; review it, "
                      "nothing to fix in the daemon"),
    "budget": ("Raise the daily cap in state/budget.yaml (or the routine's "
               "budget.daily_usd), or wait for the day to roll over"),
    "run_state": ("The machine was asleep or offline; the routine retries on "
                  "its next awake run"),
    "unknown": "Run `daemon.py status` and read logs/run.log for the routine",
}
_UNNAMED_SERVER = "affected"
_AUTH_MARKERS = (
    "unauthenticated", "unauthorized", "invalid api key", "authentication",
    "invalid_auth", "not_authed", "token_expired", "token_revoked",
    "invalid_grant", "auth failed", "sign in again", "re-authenticate",
    "reauthenticate",
)
_DOWN_MARKERS = (
    "timed out", "connection refused", "connection reset", "not connected",
    "failed to connect", "could not resolve host", "name or service not known",
    "network is unreachable", "service unavailable", "mcp server",
    "had no init event",
)
_TOOL_MARKERS = ("tool returned", "tool results unclear", "tool call",
                 "no usable result")
_BUDGET_MARKERS = ("budget exhausted", "daily budget", "budget cap")
_MCP_SERVER = re.compile(r"\bmcp__([A-Za-z0-9][A-Za-z0-9_-]*?)__")
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"[^\s<>@]+@[^\s<>@]+\.[A-Za-z]{2,}")
# Digit runs, and long tokens that mix letters and digits (ids, hashes).
_ID_LIKE = re.compile(
    r"\b(?:\d{5,}|(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{12,})\b")


def read_log_tail(path, cap=LOG_TAIL_BYTES):
    """The last `cap` bytes of the run log as lines; [] when unreadable."""
    try:
        with Path(path).open("rb") as handle:
            if Path(path).stat().st_size > cap:
                handle.seek(-cap, 2)
                handle.readline()  # discard a partial first line
            return handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def clean_error(message, routine_id=None):
    """One log message as a short, id-free `last_error` text."""
    text = str(message)
    if routine_id:
        text = text.replace(f"routine={routine_id} ", "", 1)
    text = _URL.sub("<url>", text)
    text = _EMAIL.sub("<email>", text)
    text = _ID_LIKE.sub("<id>", text)
    return events.one_line(text, LAST_ERROR_CHARS)


def _stamp(epoch):
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def routine_errors(lines, routine_id, floor):
    """(at, message) of the routine's ERROR/FATAL lines stamped at or after
    `floor`, newest first. Sleep and offline lines are skipped: they are
    the machine's, not an error of the routine."""
    found = []
    for line in lines:
        match = status._LOG_LINE.match(line)
        if not match or match.group("at") < floor:
            continue
        message = match.group("message")
        hit = status._ROUTINE_ERROR.match(message)
        if (hit and hit.group("id") == routine_id
                and not sleepclock.text_is_outage(message)):
            found.append((match.group("at"), message))
    found.reverse()  # the log is chronological
    return found


def _is_near_duplicate(message):
    """The memory store's own refusal of an entry it already holds."""
    text = message.casefold()
    return "near-duplicate" in text or "near duplicate" in text


def cause_of(message):
    """The cause tag for one error text (a log message or a usage outcome)."""
    text = str(message).casefold()
    if sleepclock.text_is_outage(text):
        return "run_state"
    if any(m in text for m in _AUTH_MARKERS):
        return "connector_auth"
    if any(m in text for m in _BUDGET_MARKERS):
        return "budget"
    if _is_near_duplicate(text):
        return "memory_duplicate"
    if "calendar match" in text or "meeting match" in text:
        return "meeting_match"
    if any(m in text for m in _DOWN_MARKERS):
        return "connector_down"
    if any(m in text for m in _TOOL_MARKERS):
        return "tool_error"
    return "unknown"


def server_of(message):
    """The connector server named by an `mcp__<server>__<tool>` name, or None."""
    match = _MCP_SERVER.search(str(message))
    return match.group(1) if match else None


def remedy_for(cause, server=None):
    return REMEDIES.get(cause, REMEDIES["unknown"]).replace(
        "{server}", server or _UNNAMED_SERVER)


def load_remedies(base_dir):
    """Remedy overrides from `alert_remedies` in the optional state/daemon.yaml.

    A mapping of alert-key prefix to text (`attention:`, `connector:zoom`,
    `budget-`...); the longest matching prefix wins and replaces the generic
    remedy. `{server}` and `{routine}` in the text are filled from the alert.
    A missing or malformed file, and any entry that is not a non-empty
    string, add nothing.
    """
    try:
        data = yaml.safe_load((Path(base_dir) / "state" / "daemon.yaml").read_text())
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    table = data.get("alert_remedies") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return {}
    return {
        str(prefix): events.one_line(text, MAX_REMEDY_CHARS)
        for prefix, text in table.items()
        if isinstance(text, str) and events.one_line(text, MAX_REMEDY_CHARS)
    }


def override_remedy(key, details, remedies):
    """Set details["remedy"] from the longest configured prefix of `key`."""
    prefix = max((p for p in remedies if key.startswith(p)), key=len,
                 default=None)
    if prefix is None:
        return
    details["remedy"] = remedies[prefix].replace(
        "{server}", str(details.get("connector") or _UNNAMED_SERVER)
    ).replace("{routine}", str(details.get("routine") or ""))


_CAUSE_TEXT = {
    "connector_auth": "connector sign-in expired",
    "connector_down": "connector not answering",
    "tool_error": "connector tool error",
    "budget": "budget cap reached",
}


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
    Quiet keys are active but covered by the cap-hit or budget-invalid
    alert: they are neither sent nor recorded, so one still active after that
    alert clears alerts then.
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
            {"held": blocked, "cause": "budget" if key == "budget-invalid"
             else "unknown", "remedy": (
                 "Fix state/budget.yaml (daily_usd and day_timezone only)"
                 if key == "budget-invalid" else
                 "Make state/claude-usage.jsonl readable again")},
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
            {"routine": rid, "spent_usd": round(used, 2), "cap_usd": own,
             "cause": "budget", "remedy": remedy_for("budget")},
        )
        if held.get(rid, ("",))[0] in ("global", "budget-invalid"):
            quiet.add(key)  # listed on budget-hit or budget-invalid
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
             "held": by_global, "by_routine": spend, "cause": "budget",
             "remedy": remedy_for("budget")},
        )
    elif total >= WARN_FRACTION * cap:
        found[f"budget-80:{day}"] = (
            f"Claude spend at {total / cap:.0%} of the ${cap:g} daily cap "
            f"(${total:.2f}). Spend: {_spend_text(spent)}",
            {"spent_usd": round(total, 2), "cap_usd": cap,
             "by_routine": spend, "cause": "budget",
             "remedy": remedy_for("budget")},
        )
    return found, quiet


def _failing_streak(history, routine_id):
    """Issue text when the routine's run of failed ticks is long enough."""
    streak = history.get("streaks", {}).get(routine_id)
    if not streak:
        return None
    if streak["errors"] >= ERROR_STREAK:
        return f"{streak['errors']} failed runs in a row"
    if streak["awake"] >= AWAKE_WINDOW_SECONDS:
        return (f"failing for {status._duration(streak['awake'])} "
                "of awake time")
    return None


def _cut_short(history, routine_id):
    """Text when a routine only gets interrupted, for long enough."""
    run = history.get("interrupted", {}).get(routine_id)
    if (run and run["ticks"] >= INTERRUPTED_STREAK
            and run["awake"] >= AWAKE_WINDOW_SECONDS):
        return (f"{run['ticks']} interrupted runs in a row, "
                f"{status._duration(run['awake'])} of awake time")
    return None


def _connector_failures(entries):
    """{server: (first, last, outcome)} of failed calls that name the server.

    `first` is the first failure after the server's last success (as in
    status.connector_times); `outcome` is the newest failure's usage outcome.
    """
    times = status.connector_times(entries)["first_error"]
    newest = {}
    for epoch, entry in entries:
        outcome = str(entry.get("outcome", ""))
        if not (outcome == "missing_results" or outcome.startswith("error")):
            continue
        for server in times:
            if (f"mcp__{server}__" in outcome
                    and epoch >= newest.get(server, (0, ""))[0]):
                newest[server] = (epoch, outcome)
    return {
        server: (first, *newest.get(server, (first, "")))
        for server, first in times.items()
    }


def _outcome_cause(outcome):
    """Cause of a connector's failed usage outcome."""
    cause = cause_of(outcome)
    if cause != "unknown":
        return cause
    return "connector_down"


def _diagnose(lines, rid, streak, issues, failing, now, failures):
    """(issues without review items, details) for one routine's attention.

    Reads the routine's ERROR/FATAL lines since its streak began and says
    what they mean. Near-duplicate memory errors are review items: the memory
    sink issue is dropped when the newest memory error of the last day is
    one, and so is the streak when every error of it is one.
    """
    since = (streak or {}).get("started_at") or (streak or {}).get(
        "first_error_at")
    day_ago = _stamp(now - status.MEMORY_ERROR_WINDOW_SECONDS)
    recent = routine_errors(lines, rid, day_ago)
    errors = routine_errors(lines, rid, since) if since else []

    def duplicate(message):
        return bool(_MEMORY_SINK_LINE.match(message)
                    and _is_near_duplicate(message))

    memory = [m for _, m in recent if _MEMORY_SINK_LINE.match(m)]
    dropped = set()
    if memory and duplicate(memory[0]):
        dropped.update(i for i in issues if _MEMORY_SINK_ISSUE.match(i))
    if failing and errors and all(duplicate(m) for _, m in errors):
        dropped.add(failing)
    issues = [i for i in issues if i not in dropped]
    if not issues:
        return issues, {}
    details = {}
    if since and failing in issues:
        details["since"] = since
    # With a streak its own errors tell the cause; without one only the
    # memory sink issue has a line to look at.
    pool = errors if failing in issues else (
        [(at, m) for at, m in recent if _MEMORY_SINK_LINE.match(m)]
        if any(_MEMORY_SINK_ISSUE.match(i) for i in issues) else [])
    pool = [(at, m) for at, m in pool if not duplicate(m)] or pool
    cause, server = "unknown", None
    if pool:
        message = pool[0][1]
        cause, server = cause_of(message), server_of(message)
        details["last_error"] = clean_error(message, rid)
        if cause == "tool_error" and server in failures:
            # "no usable result" hides why; the usage log may know.
            if _outcome_cause(failures[server][2]) == "connector_auth":
                cause = "connector_auth"
    details["cause"] = cause
    if server and cause in ("connector_auth", "connector_down"):
        details["connector"] = server
    details["remedy"] = remedy_for(cause, server)
    return issues, details


def health_conditions(base_dir, routines, entries, now):
    """Routines `status` flags for attention, and connectors failing too long."""
    found = {}
    log_path = Path(base_dir) / "logs" / "run.log"
    history = status.read_tick_history(log_path)
    lines = read_log_tail(log_path)
    failures = ({} if isinstance(entries, claude_bridge.BridgeError)
                else _connector_failures(entries))
    for row in status.routine_rows(base_dir, routines, history, now=now):
        if row["armed"] != "yes":
            continue
        rid = row["routine"]
        issues = [
            issue for issue in row["issues"].split(", ")
            if issue != "-" and issue not in TRANSIENT_ISSUES
            and not _LAST_RUN_ISSUE.match(issue)
            and not _REVIEW_ISSUE.match(issue)
        ]
        failing = _failing_streak(history, rid)
        if failing:
            issues.append(failing)
        cut_short = _cut_short(history, rid)
        if cut_short:
            run = history["interrupted"][rid]
            found[f"interrupted:{rid}"] = (
                f"{rid} keeps being interrupted: {cut_short}",
                {"routine": rid, "issues": [cut_short],
                 "cause": "run_state", "since": run["first_at"],
                 "remedy": remedy_for("run_state")},
            )
        issues, details = _diagnose(
            lines, rid, history.get("streaks", {}).get(rid), issues, failing,
            now, failures)
        if issues:
            cause = details.get("cause")
            text = ", ".join(
                _CAUSE_TEXT.get(cause, i) if i == failing and cause in
                _CAUSE_TEXT else i for i in issues)
            found[f"attention:{rid}"] = (
                f"{rid}: {text}" if cause in _CAUSE_TEXT
                else f"{rid} needs attention: {text}",
                {"routine": rid, "issues": issues, **details},
            )
    if isinstance(entries, claude_bridge.BridgeError):
        return found  # reported by budget_conditions as usage-unreadable
    # connector_times keeps first_error only while no ok has followed it.
    for server, (first, _last, outcome) in failures.items():
        if now - first >= CONNECTOR_GRACE_SECONDS:
            cause = _outcome_cause(outcome)
            duration = status._duration(now - first)
            details = {"connector": server, "cause": cause,
                       "since": _stamp(first)}
            last_error = clean_error(
                re.sub(r"^(?:error|outage): ", "", outcome))
            if last_error:
                details["last_error"] = last_error
            details["remedy"] = remedy_for(cause, server)
            found[f"connector:{server}"] = (
                f"Connector {server} sign-in expired, failing for {duration}"
                if cause == "connector_auth" else
                f"Connector {server} failing for {duration} "
                "with no successful call", details)
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
    remedies = load_remedies(base_dir)
    for key, (_message, details) in active.items():
        override_remedy(key, details, remedies)
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
