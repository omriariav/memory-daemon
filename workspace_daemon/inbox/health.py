"""The inbox's Health data: a read-only snapshot of the daemon (no Textual).

`HealthReader.read()` gathers, from the same code `daemon.py status` uses
(`workspace_daemon.status`), one dict that the health strip (one line under
the tab bar) and the Health view both draw:

    {"at": epoch, "error": None | text,
     "scheduler": {"armed": bool | None, "last_tick": epoch | None,
                   "paused": text | None},
     "routines": [{"id", "group", "state", "status", "every", "last_run",
                   "last_capture", "note", "good_at"}],
     "groups": [{"name", "state", "good_at", "note", "count"}],
     "alerts": [{"key", "routine", "since"}],
     "connectors": [{"name", "state", "at"}],
     "spend": {"total": float | None, "cap": float | None},
     "heartbeats": [{"name", "state", "age_s", "note"}]}

States are `ok`, `warn`, `fail`, `busy` and `off` (a disabled routine). A
routine's state comes from its last tick result and the issues
`status.routine_rows` finds; a routine group is the worst of its routines
(`slack-direct` and `slack-mcp` are the group `slack`: the text before the
first `-`, `_` or `.` of the id).

Everything is read, nothing is written. The read is meant to run in a worker
thread: it is cached by the mtimes of `logs/run.log`, `state/alerts.json`,
`state/claude-usage.jsonl` and the schedule state, so an unchanged daemon costs
a few `stat` calls (and a full read at most every `MAX_AGE_S`, so launchd state
still moves). Heartbeat files are only `stat`ed, every time.
"""
import os
import re
import time
from datetime import datetime

OK, WARN, FAIL, BUSY, OFF = "ok", "warn", "fail", "busy", "off"
GLYPHS = {OK: "✓", WARN: "⚠", FAIL: "✗", BUSY: "◌", OFF: "·"}
_RANK = {FAIL: 4, WARN: 3, BUSY: 2, OK: 1, OFF: 0}
REFRESH_S = 30.0
MAX_AGE_S = 300.0
MAX_HEARTBEATS = 10
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HOURS = re.compile(
    r"^\s*(?:(?P<days>[a-z]{3}(?:\s*[-,]\s*[a-z]{3})*)\s+)?"
    r"(?P<a>\d{1,2}):(?P<am>\d{2})\s*-\s*(?P<b>\d{1,2}):(?P<bm>\d{2})\s*$",
    re.IGNORECASE)


# --- heartbeats ------------------------------------------------------------

def parse_hours(text):
    """`sun-thu 09:00-19:00` (days optional: every day) as
    `(days, start_min, end_min)` with `days` a set of weekday numbers
    (Monday is 0), or None when the text is not understood. A range may wrap
    (`fri-mon`), a list is `mon,wed,fri`, and an end before the start crosses
    midnight."""
    if not isinstance(text, str):
        return None
    m = _HOURS.match(text)
    if not m:
        return None
    start = int(m.group("a")) * 60 + int(m.group("am"))
    end = int(m.group("b")) * 60 + int(m.group("bm"))
    if (int(m.group("a")) > 23 or int(m.group("b")) > 24
            or int(m.group("am")) > 59 or int(m.group("bm")) > 59):
        return None
    days = set(range(7))
    if m.group("days"):
        raw = m.group("days").lower()
        names = re.split(r"\s*([-,])\s*", raw)
        try:
            if "-" in names:
                parts = [n for n in names if n not in ("-", ",")]
                if len(parts) != 2 or "," in names:
                    return None
                first, last = (_DAYS.index(p) for p in parts)
                days = set()
                day = first
                while True:
                    days.add(day)
                    if day == last:
                        break
                    day = (day + 1) % 7
            else:
                days = {_DAYS.index(n) for n in names if n != ","}
        except ValueError:
            return None
    return days, start, end


def in_hours(window, epoch):
    """True when the local time of `epoch` is inside the parsed window (no
    window: always)."""
    if window is None:
        return True
    days, start, end = window
    local = datetime.fromtimestamp(epoch)
    minute = local.hour * 60 + local.minute
    if start <= end:
        return local.weekday() in days and start <= minute < end
    # Crosses midnight: the part after midnight belongs to the day before.
    if minute >= start:
        return local.weekday() in days
    return (local.weekday() - 1) % 7 in days and minute < end


def heartbeat_state(hb, base_dir, now):
    """One heartbeat cell from `{name, path, stale_min, hours?}`: `ok` while
    the file's mtime is newer than `stale_min`, `warn` when it is older or
    missing, and only inside `hours` (outside it the cell is `ok`, noted as
    off hours)."""
    path = os.path.expanduser(hb["path"])
    if not os.path.isabs(path):
        path = os.path.join(str(base_dir), path)
    try:
        age = max(0.0, now - os.stat(path).st_mtime)
    except OSError:
        age = None
    window = parse_hours(hb["hours"]) if hb.get("hours") else None
    inside = in_hours(window, now)
    fresh = age is not None and age <= float(hb["stale_min"]) * 60
    if fresh:
        state, note = OK, ""
    elif not inside:
        state, note = OK, "off hours"
    else:
        state = WARN
        note = "no heartbeat file" if age is None else "stale"
    return {"name": hb["name"], "state": state, "age_s": age, "note": note}


def check_heartbeats(heartbeats, base_dir, now):
    return [heartbeat_state(hb, base_dir, now)
            for hb in (heartbeats or [])[:MAX_HEARTBEATS]]


# --- routines ---------------------------------------------------------------

def routine_group(routine_id):
    """The strip group of a routine: the text before its first `-`, `_` or
    `.`."""
    return re.split(r"[-_.]", routine_id, maxsplit=1)[0] or routine_id


def _issue_parts(issues):
    if not issues or issues == "-":
        return []
    return [p.strip() for p in issues.split(",") if p.strip()]


def classify_routine(row, tick, alert_keys):
    """`(state, note)` of one routine from its `status.routine_rows` row, its
    last tick result (`{"state", "at"}` or None) and the active alert keys
    that name it."""
    status = row.get("status") or ""
    if status == "disabled":
        return OFF, "disabled"
    issues = _issue_parts(row.get("issues"))
    failing = [i for i in issues if i == "last run" or "last-run" in i]
    if any(k.startswith("attention:") for k in alert_keys):
        return FAIL, issues[0] if issues else "needs attention"
    if (tick or {}).get("state") == "error" or failing:
        return FAIL, (failing or ["last run failed"])[0]
    if "paused" in status or "budget held" in status:
        return WARN, status
    other = [i for i in issues if i not in ("overlap",)]
    if other:
        return WARN, ", ".join(other)
    if status == "in-tick":
        return BUSY, "running now"
    if "overlap" in issues:
        return BUSY, "previous run still going"
    if any(k.startswith("hold:") for k in alert_keys):
        return WARN, "held by the budget"
    return OK, ""


def _routine_of_key(key):
    kind, _, rest = key.partition(":")
    if kind in ("attention", "interrupted"):
        return rest
    if kind == "hold":
        return rest.rsplit(":", 1)[0]
    return ""


def build_routines(rows, tick_results, alerts, tick_epoch=None):
    """The `routines` and `groups` lists from status rows (see
    `status.routine_rows`), the per-routine tick results
    (`read_tick_history()["routines"]`), the active alerts and a function
    from an ISO time to epoch seconds."""
    by_routine = {}
    for a in alerts:
        by_routine.setdefault(a.get("routine") or "", []).append(a["key"])
    out = []
    for row in rows:
        rid = row["routine"]
        tick = tick_results.get(rid) or {}
        state, note = classify_routine(row, tick, by_routine.get(rid, []))
        good = None
        if tick.get("state") == "ok" and tick_epoch is not None:
            good = tick_epoch(tick.get("at"))
        out.append({
            "id": rid, "group": routine_group(rid), "state": state,
            "status": row.get("status") or "", "every": row.get("every") or "-",
            "last_run": _bare(row.get("last_attempt")),
            "last_capture": _bare(row.get("last_capture")),
            "note": note, "good_at": good})
    return out, group_routines(out)


def _bare(text):
    """`8m ago` as `8m` (the tables say `last run`, not `ago`)."""
    text = text or "-"
    return text[:-4] if text.endswith(" ago") else text


def group_routines(routines):
    """One entry per routine group: the worst state, its note, and the newest
    good run among the routines that are ok."""
    groups = {}
    for r in routines:
        if r["state"] == OFF:
            continue
        g = groups.setdefault(r["group"], {
            "name": r["group"], "state": OK, "good_at": None, "note": "",
            "count": 0})
        g["count"] += 1
        if _RANK[r["state"]] > _RANK[g["state"]]:
            g["state"], g["note"] = r["state"], r["note"]
        if r["state"] == OK and r["good_at"]:
            g["good_at"] = max(g["good_at"] or 0.0, r["good_at"])
    return sorted(groups.values(), key=lambda g: g["name"])


def needs_you(alerts, benign_keys=()):
    """The alerts that need attention: every active key but the ones whose
    only issues are benign run states (they never need you)."""
    return [a for a in alerts if a["key"] not in set(benign_keys)]


# --- the reader -------------------------------------------------------------

def _mtime(path):
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


class HealthReader:
    """Reads the snapshot. `read()` blocks (stat calls, file reads, one
    `launchctl` probe): call it from a worker thread."""

    def __init__(self, base_dir, heartbeats=(), launchd=True):
        self.base_dir = str(base_dir)
        self.heartbeats = list(heartbeats or [])
        self.launchd = launchd
        self._sig = None
        self._core = None
        self._stamp = 0.0

    def signature(self):
        b = self.base_dir
        return tuple(_mtime(os.path.join(b, *p)) for p in (
            ("logs", "run.log"), ("state", "alerts.json"),
            ("state", "claude-usage.jsonl"), ("state", "schedule.json"),
            ("state", "PAUSED"), ("state", "budget.yaml"), ("routines",)))

    def read(self, now=None):
        now = time.time() if now is None else now
        sig = self.signature()
        if (self._core is None or sig != self._sig
                or now - self._stamp > MAX_AGE_S):
            self._core = self._collect(now)
            self._sig, self._stamp = sig, now
        snap = dict(self._core, at=now)
        snap["heartbeats"] = check_heartbeats(self.heartbeats, self.base_dir,
                                              now)
        return snap

    def _collect(self, now):
        core = {"error": None,
                "scheduler": {"armed": None, "last_tick": None,
                              "paused": None},
                "routines": [], "groups": [], "alerts": [],
                "connectors": [], "spend": {"total": None, "cap": None}}
        errors = []
        try:
            self._scheduler_and_routines(core, now)
        except Exception as exc:  # noqa: BLE001 - a health read never raises
            errors.append(f"routines: {type(exc).__name__}: {exc}")
        for part in (self._alerts, self._spend):
            try:
                part(core, now)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{part.__name__.strip('_')}: "
                              f"{type(exc).__name__}: {exc}")
        # Alerts are known now: classify the routines with them.
        if core["routines"] and core["alerts"]:
            self._apply_alerts(core)
        core["error"] = "; ".join(errors) or None
        return core

    def _scheduler_and_routines(self, core, now):
        from .. import config, state, status
        base = self.base_dir
        core["scheduler"]["paused"] = state.paused(base)
        routines = config.discover(base)
        history = status.read_tick_history(
            os.path.join(base, "logs", "run.log"))
        latest = history.get("latest_by_group") or {}
        epochs = [status._iso_epoch(v.get("at")) for v in latest.values()]
        epochs = [e for e in epochs if e is not None]
        core["scheduler"]["last_tick"] = max(epochs) if epochs else None
        running = set()
        if self.launchd:
            main = status.probe_launchd(status.DEFAULT_LAUNCHD_LABEL)
            core["scheduler"]["armed"] = bool(main.get("loaded"))
            for label, group in (
                    (status.DEFAULT_LAUNCHD_LABEL, "capture"),
                    (status.MAINTENANCE_LAUNCHD_LABEL, "maintenance"),
                    (status.CALENDAR_LAUNCHD_LABEL, "calendar")):
                probe = main if label == status.DEFAULT_LAUNCHD_LABEL \
                    else status.probe_launchd(label)
                if probe.get("loaded") and probe.get("state") == "running":
                    running.add(group)
        schedule = state.schedule_file(base)
        if schedule.exists():
            rows = status.routine_rows(
                base, routines, history, now=now,
                scheduler_running_groups=running)
        else:  # no schedule yet: do not create anything just to look
            rows = [{"routine": r["id"],
                     "status": "waiting" if r.get("enabled", True)
                     else "disabled", "every": config.schedule_label(r),
                     "last_attempt": "never", "last_capture": "never",
                     "issues": "-"} for r in routines]
        by_id = {r["id"]: r for r in routines}
        pause = core["scheduler"]["paused"]
        from .. import claude_bridge
        for row in rows:
            if row["status"] in ("disabled", "in-tick"):
                continue
            routine = by_id.get(row["routine"]) or {}
            if pause:
                row["status"] = "paused"
            else:
                try:
                    held = claude_bridge.budget_exhausted(
                        base, row["routine"], config.budget_cap(routine),
                        now=now,
                        include_global=config.uses_claude_bridge(routine))
                except Exception:  # noqa: BLE001
                    held = False
                if held:
                    row["status"] = "budget held"
        core["routines"], core["groups"] = build_routines(
            rows, history.get("routines") or {}, [], status._iso_epoch)
        core["_rows"] = rows
        core["_ticks"] = history.get("routines") or {}

    def _apply_alerts(self, core):
        from .. import status
        core["routines"], core["groups"] = build_routines(
            core["_rows"], core["_ticks"], core["alerts"], status._iso_epoch)

    def _alerts(self, core, now):
        from .alertstate import Recovery
        active = Recovery(self.base_dir).active_since()
        core["alerts"] = [
            {"key": key, "routine": _routine_of_key(key), "since": since}
            for key, since in sorted(active.items(), key=lambda kv: kv[1])]

    def _spend(self, core, now):
        from .. import claude_bridge, status
        entries = claude_bridge.recent_usage(self.base_dir, now=now)
        try:
            cap, tz = claude_bridge.load_budget(self.base_dir)
        except claude_bridge.BridgeError:
            cap, tz = None, None
        if entries or cap is not None:
            spent = status.spend_by_routine(
                entries, claude_bridge.day_start_epoch(now, tz))
            core["spend"] = {"total": float(sum(spent.values())),
                             "cap": float(cap) if cap is not None else None}
        times = status.connector_times(entries)
        servers = sorted(set(times["ok"]) | set(times["error"])
                         | set(times["in_failed"]))
        out = []
        for name in servers:
            failing = name in times["first_error"]
            out.append({
                "name": name, "state": FAIL if failing else OK,
                "at": times["error" if failing else "ok"].get(name)
                or times["ok"].get(name)})
        core["connectors"] = out


# --- drawing helpers (plain text; the widgets add colour) ------------------

def short_age(seconds):
    """`now`, `3m`, `2h`, `4d`: the age style of the strip and the tables."""
    from .models import fmt_age
    return fmt_age(seconds)


def connector_label(name):
    """A connector server name for display: Claude's own `claude_ai_` prefix
    dropped and underscores as spaces."""
    text = name[len("claude_ai_"):] if name.startswith("claude_ai_") else name
    return text.replace("_", " ").strip() or name


def alert_info(items):
    """`{routine: {...}}` for the active alert rows (`models.Item`s): the key,
    the newest row's `cause` (the daemon's enum), `connector`, `last_error`,
    `remedy`, `since` and its title, whichever the row carries. A resolved or
    benign alert is left out. Used to word the strip cells and NEEDS YOU."""
    out = {}
    for it in items:
        if it.source != "alert" or it.resolved or it.meta.get("benign"):
            continue
        key = it.meta.get("key") or ""
        routine = it.meta.get("routine") or _routine_of_key(key)
        info = {"key": key, "title": it.gist, "ts": it.ts}
        for name in ("cause", "connector", "last_error", "remedy", "since"):
            if it.meta.get(name):
                info[name] = it.meta[name]
        prev = out.get(routine or key)
        if prev is None or it.ts >= prev["ts"]:
            out[routine or key] = info
    return out


def cause_text(info):
    """The cause of an alert as a short phrase, or ''."""
    from .sources import cause_label
    return cause_label((info or {}).get("cause"))


def strip_cells(snap, now, infos=None):
    """The strip as `[(state, text)]`: a cell per routine group, `$x/$cap`
    for the spend, and a cell per heartbeat. `text` carries no colour. With
    `infos` (`alert_info`'s result), a group that is failing or needs
    attention says the cause its alert names instead of a bare `failing`."""
    infos = infos or {}
    cells = []
    members = {}
    for r in snap.get("routines") or []:
        members.setdefault(r["group"], []).append(r["id"])
    for g in snap.get("groups") or []:
        text = g["name"]
        why = next((cause_text(infos[rid]) for rid in members.get(g["name"], [])
                    if rid in infos and cause_text(infos[rid])), "")
        if g["state"] == OK:
            text += f" {GLYPHS[OK]}"
            if g.get("good_at"):
                text += short_age(now - g["good_at"])
        elif g["state"] == FAIL:
            text += f" {GLYPHS[FAIL]} {why or 'failing'}"
        elif g["state"] == BUSY:
            text += f" {GLYPHS[BUSY]} busy"
        else:
            note = why or g.get("note")
            text += f" {GLYPHS[WARN]}" + (f" {note}" if note else "")
        cells.append((g["state"], text))
    spend = snap.get("spend") or {}
    if spend.get("total") is not None:
        text = f"${spend['total']:.0f}"
        if spend.get("cap") is not None:
            text += f"/${spend['cap']:.0f}"
        over = (spend.get("cap") is not None
                and spend["total"] >= spend["cap"])
        cells.append((WARN if over else OK, text))
    for hb in snap.get("heartbeats") or []:
        text = f"{hb['name']} {GLYPHS[hb['state']]}"
        if hb["state"] == OK and hb.get("age_s") is not None \
                and not hb.get("note"):
            text += short_age(hb["age_s"])
        elif hb.get("note"):
            text += f" {hb['note']}"
        cells.append((hb["state"], text))
    return cells
