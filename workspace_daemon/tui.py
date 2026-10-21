"""Terminal UI: live daemon status plus pause/run/tick controls.

Layers, thinnest last:

* ``Collector`` builds a plain-dict snapshot from the same helpers `status`
  uses (probe_launchd, read_tick_history, routine_rows) plus today's spend.
* ``ActionRunner`` starts `daemon.py` subcommands as detached processes and
  refuses a second concurrent run of the same routine.
* ``App.handle_key`` is the whole key map; it needs no terminal.
* ``render_lines`` turns snapshot + UI state into ``(text, style)`` rows at a
  given width and height. ``run_curses`` only paints those rows.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from workspace_daemon import claude_bridge, config, state, status

try:  # pragma: no cover - curses is absent on some platforms
    import curses
except ImportError:  # pragma: no cover
    curses = None

MIN_WIDTH = 40
MIN_HEIGHT = 8
LOG_TAIL_BYTES = 128 * 1024
LOG_TAIL_LINES = 500
TUI_LOG_MAX_BYTES = 1024 * 1024
CALENDAR_KIND = "calendar_cache"
TICK_GROUPS = (("a", "all"), ("c", "capture"), ("m", "maintenance"),
               ("k", "calendar"))

KEY_UP = getattr(curses, "KEY_UP", 259)
KEY_DOWN = getattr(curses, "KEY_DOWN", 258)
KEY_PPAGE = getattr(curses, "KEY_PPAGE", 339)
KEY_NPAGE = getattr(curses, "KEY_NPAGE", 338)
KEY_HOME = getattr(curses, "KEY_HOME", 262)
KEY_END = getattr(curses, "KEY_END", 360)
KEY_RESIZE = getattr(curses, "KEY_RESIZE", 410)
KEY_ESC = 27
KEY_ENTER = (10, 13)


# --- launchd ----------------------------------------------------------------

def launchctl_run(args, timeout=10):
    """Run launchctl; return (returncode, stdout). (127, "") when unavailable."""
    try:
        result = subprocess.run(
            ["launchctl"] + list(args), capture_output=True, text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout


def launchctl_available():
    return shutil.which("launchctl") is not None


def discover_labels(prefix, run=launchctl_run):
    """Every loaded launchd job whose label starts with ``prefix`` (sorted)."""
    rc, out = run(["list"])
    if rc != 0:
        return []
    labels = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1].startswith(prefix):
            labels.add(parts[-1])
    return sorted(labels)


def reload_jobs(labels, run=launchctl_run, uid=None, plist_dir=None):
    """bootout + bootstrap each label from its LaunchAgents plist.

    A label with no plist on disk is left loaded (it could not be restored).
    Returns human-readable result lines.
    """
    uid = os.getuid() if uid is None else uid
    plist_dir = Path(plist_dir or Path.home() / "Library" / "LaunchAgents")
    domain = f"gui/{uid}"
    out = []
    for label in labels:
        plist = plist_dir / f"{label}.plist"
        if not plist.is_file():
            out.append(f"{label}: no plist at LaunchAgents, left loaded")
            continue
        rc_out, _ = run(["bootout", f"{domain}/{label}"])
        rc_in, _ = run(["bootstrap", domain, str(plist)])
        if rc_in == 0:
            out.append(f"{label}: reloaded")
        else:
            out.append(f"{label}: bootstrap failed (rc={rc_in}, bootout rc={rc_out})")
    return out


def _groups_for_label(label):
    if label == status.DEFAULT_LAUNCHD_LABEL or label.endswith("-capture"):
        return {"capture"}
    if label.endswith("-maintenance"):
        return {"maintenance"}
    if label.endswith("-calendar"):
        return {"calendar"}
    return {"capture"}


def routine_group(routine):
    """Tick group a routine belongs to."""
    if not config.is_maintenance(routine):
        return "capture"
    kind = (routine.get("maintenance") or {}).get("kind")
    return "calendar" if kind == CALENDAR_KIND else "maintenance"


# --- data model -------------------------------------------------------------

class Collector:
    """Builds snapshots; caches the YAML load and the (large) tick history."""

    def __init__(self, base_dir, label_prefix=status.DEFAULT_LAUNCHD_LABEL,
                 run=launchctl_run):
        self.base_dir = Path(base_dir)
        self.label_prefix = label_prefix
        self.run = run
        self._routines = (None, [], None)
        self._history = (None, None)

    def _load_routines(self):
        directory = config.routines_dir(self.base_dir)
        try:
            sig = tuple(
                (p.name, p.stat().st_mtime_ns)
                for p in sorted(directory.glob("*.yaml"))
            )
        except OSError:
            sig = ()
        if sig == self._routines[0] and self._routines[2] is None:
            return self._routines[1], None
        try:
            routines = config.discover(self.base_dir)
            self._routines = (sig, routines, None)
            return routines, None
        except Exception as exc:  # a bad YAML must not kill the UI
            self._routines = (sig, [], str(exc))
            return [], str(exc)

    def _load_history(self):
        path = self.base_dir / "logs" / "run.log"
        try:
            st = path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        if sig is not None and sig == self._history[0]:
            return self._history[1]
        history = status.read_tick_history(path)
        self._history = (sig, history)
        return history

    def snapshot(self, now=None):
        now = time.time() if now is None else float(now)
        problems = []
        routines, routine_error = self._load_routines()
        if routine_error:
            problems.append(f"routines: {routine_error}")
        have_launchctl = launchctl_available()
        labels = discover_labels(self.label_prefix, self.run) if have_launchctl else []
        probe_labels = labels or [self.label_prefix]
        jobs = [status.probe_launchd(label) for label in probe_labels]
        running_groups = set()
        for job in jobs:
            if job.get("loaded") and job.get("state") == "running":
                running_groups |= _groups_for_label(job["label"])
        history = self._load_history()
        rows = status.routine_rows(
            self.base_dir, routines, history, now=now,
            scheduler_running_groups=running_groups,
        )
        pause = state.paused(self.base_dir)

        try:
            cap, tz = claude_bridge.load_budget(self.base_dir)
        except claude_bridge.BridgeError as exc:
            cap, tz = None, None
            problems.append(str(exc))
        spent = {}
        try:
            day_start = claude_bridge.day_start_epoch(now, tz)
            for _, entry in claude_bridge.recent_usage(
                    self.base_dir, day_start, now):
                tag = entry.get("routine") or "(unattributed)"
                spent[tag] = spent.get(tag, 0.0) + claude_bridge.entry_cost(entry)
        except claude_bridge.BridgeError as exc:
            problems.append(str(exc))

        by_id = {r["id"]: r for r in routines}
        results = history.get("routines", {})
        out_rows = []
        for row in rows:
            routine = by_id.get(row["routine"]) or {}
            rid = row["routine"]
            routine_cap = config.budget_cap(routine)
            today = spent.get(rid, 0.0)
            result = (results.get(rid) or {}).get("state")
            flags = [] if row["issues"] == "-" else [row["issues"]]
            if row.get("extract_failed"):
                flags.append(f"{row['extract_failed']} extract-failed")
            status_name = row["status"]
            if status_name not in ("disabled", "in-tick"):
                if pause:
                    status_name = "paused"
                elif routine_cap is not None and today >= routine_cap:
                    status_name = "budget held"
            out_rows.append({
                "id": rid,
                "role": row["role"],
                "group": routine_group(routine),
                "enabled": row["armed"] == "yes",
                "every": row["every"],
                "status": status_name,
                "last_attempt": row["last_attempt"],
                "result": result or "-",
                "next": row["next"],
                "spend": today,
                "cap": routine_cap,
                "flags": flags,
            })

        latest = history.get("latest")
        last_tick = (
            f"{status._iso_age(latest['at'], now)} ({latest['state']})"
            if latest else "never recorded"
        )
        attention = bool(problems) or any(r["flags"] for r in out_rows)
        for job in jobs:
            if job.get("loaded") and job.get("last_exit") not in (None, 0):
                attention = True
            if have_launchctl and not job.get("loaded"):
                attention = True
        return {
            "now": now,
            "paused": pause,
            "jobs": jobs,
            "have_launchctl": have_launchctl,
            "last_tick": last_tick,
            "spend_total": sum(spent.values()),
            "spend_cap": cap,
            "rows": out_rows,
            "problems": problems,
            "healthy": not attention,
        }


def job_names(jobs):
    """Display names: labels minus their common prefix when there are several."""
    labels = [j["label"] for j in jobs]
    if len(labels) < 2:
        return labels
    common = os.path.commonprefix(labels).rstrip("-.")
    return [
        label[len(common):].lstrip("-.") or "main" if common else label
        for label in labels
    ]


def jobs_text(jobs, sep=" | "):
    return sep.join(
        job_text(j, name) for j, name in zip(jobs, job_names(jobs)))


def job_text(job, short=None):
    short = short or job["label"]
    if not job.get("loaded"):
        return f"{short}: {job.get('detail', 'not loaded')}"
    name = job.get("state") or "loaded"
    if name == "not running":
        name = "idle"
    bits = [name]
    if job.get("interval_seconds"):
        bits.append(f"every {status._duration(job['interval_seconds'])}")
    if job.get("last_exit") not in (None, 0):
        bits.append(f"last exit {job['last_exit']}")
    return f"{short}: {', '.join(bits)}"


def summary_line(snap):
    """One-line summary for `daemon.py ctl state`."""
    rows = snap["rows"]
    armed = sum(1 for r in rows if r["enabled"])
    attn = sum(1 for r in rows if r["flags"])
    cap = snap["spend_cap"]
    spend = f"${snap['spend_total']:.2f}" + (f"/${cap:g}" if cap is not None else "")
    if snap["paused"]:
        pause = f"PAUSED ({snap['paused']})"
    else:
        pause = "running"
    jobs = jobs_text(snap["jobs"], ", ")
    return (
        f"{'OK' if snap['healthy'] else 'ATTENTION'} | {pause} | "
        f"routines {len(rows)} ({armed} armed, {attn} flagged) | "
        f"spend today {spend} | last tick {snap['last_tick']} | {jobs}"
    )


# --- actions ----------------------------------------------------------------

RUN_MODES = {"run": [], "dry": ["--dry-run"], "count": ["--count-only"]}


def build_run_command(python, script, routine_id, mode="run"):
    return [str(python), str(script), "run", "--routine", routine_id] + RUN_MODES[mode]


def build_tick_command(python, script, group):
    return [str(python), str(script), "tick", "--group", group]


class ActionRunner:
    """Detached `daemon.py` subprocesses, one at a time per key."""

    def __init__(self, base_dir, python=None, script=None, popen=subprocess.Popen):
        self.base_dir = Path(base_dir)
        self.python = python or sys.executable
        self.script = script or self.base_dir / "daemon.py"
        self.popen = popen
        self.procs = {}  # key -> (proc, label)

    @property
    def log_path(self):
        return self.base_dir / "logs" / "tui.log"

    def _open_log(self):
        path = self.log_path
        state.ensure_private_dir(path.parent)
        try:
            if path.stat().st_size > TUI_LOG_MAX_BYTES:
                os.replace(path, path.with_name("tui.log.1"))
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        return os.fdopen(fd, "ab")

    def busy(self, key):
        entry = self.procs.get(key)
        return entry is not None and entry[0].poll() is None

    def running_keys(self):
        return {k for k in self.procs if self.busy(k)}

    def start(self, key, argv, label):
        """Start argv unless key is already running. Returns (ok, message)."""
        if self.busy(key):
            return False, f"{label}: already running, not starting a second copy"
        try:
            handle = self._open_log()
        except OSError as exc:
            return False, f"cannot open TUI log: {exc}"
        try:
            handle.write(
                f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} {label}: "
                f"{' '.join(argv[1:])}\n".encode()
            )
            handle.flush()
            proc = self.popen(
                argv, stdin=subprocess.DEVNULL, stdout=handle,
                stderr=subprocess.STDOUT, cwd=str(self.base_dir),
                start_new_session=True,
            )
        except OSError as exc:
            return False, f"{label}: failed to start ({exc})"
        finally:
            handle.close()
        self.procs[key] = (proc, label)
        return True, f"{label}: started (output in logs/tui.log)"

    def reap(self):
        """Messages for processes that finished since the last call."""
        done = []
        for key in list(self.procs):
            proc, label = self.procs[key]
            rc = proc.poll()
            if rc is not None:
                del self.procs[key]
                done.append(f"{label}: finished" + ("" if rc == 0 else f" (exit {rc})"))
        return done


# --- log pane ---------------------------------------------------------------

def read_log_tail(path, max_lines=LOG_TAIL_LINES):
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if size > LOG_TAIL_BYTES:
                handle.seek(-LOG_TAIL_BYTES, 2)
                handle.readline()
            data = handle.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def filter_log(lines, routine_id):
    pattern = re.compile(r"(?<![\w-])" + re.escape(routine_id) + r"(?![\w-])")
    return [line for line in lines if pattern.search(line)]


# --- UI state + key handling -------------------------------------------------

class Prompt:
    def __init__(self, text, choices, callback):
        self.text, self.choices, self.callback = text, choices, callback


class UIState:
    def __init__(self):
        self.selected = 0
        self.top = 0
        self.mode = "main"  # main | log | text
        self.filter = False
        self.log_lines = None
        self.log_scroll = 0
        self.message = ""
        self.prompt = None
        self.text_title = ""
        self.text_lines = []
        self.text_scroll = 0


HELP_LINES = [
    "up/down, j/k    select routine       PgUp/PgDn  page",
    "r               run selected routine now",
    "d               dry-run it (no LLM call, no writes)",
    "c               count-only it (list + cost estimate)",
    "t               tick a group (all/capture/maintenance/calendar)",
    "p               pause / resume (creates/removes state/PAUSED)",
    "l               full-screen log        f  filter log to routine",
    "L               reload launchd jobs (macOS), asks first",
    "v               validate routines",
    "?               this help              q  quit",
    "",
    "Costly or destructive actions ask y/n first. Output of started",
    "actions goes to logs/tui.log. Never two runs of one routine at once.",
]


class App:
    def __init__(self, base_dir, collector=None, runner=None,
                 label_prefix=status.DEFAULT_LAUNCHD_LABEL, run=launchctl_run):
        self.base_dir = Path(base_dir)
        self.run = run
        self.collector = collector or Collector(base_dir, label_prefix, run)
        self.runner = runner or ActionRunner(base_dir)
        self.ui = UIState()
        self.snap = None
        self.refresh()

    # data
    def refresh(self):
        self.snap = self.collector.snapshot()
        self.ui.log_lines = read_log_tail(self.base_dir / "logs" / "run.log")
        for msg in self.runner.reap():
            self.ui.message = msg
        rows = self.snap["rows"]
        self.ui.selected = max(0, min(self.ui.selected, len(rows) - 1))

    def selected_row(self):
        rows = self.snap["rows"]
        return rows[self.ui.selected] if rows else None

    # key map
    def handle_key(self, ch):
        """Handle one key code; returns False when the app should quit."""
        ui = self.ui
        if isinstance(ch, str):
            ch = ord(ch)
        if ui.prompt is not None:
            prompt, ui.prompt = ui.prompt, None
            key = chr(ch) if 0 <= ch < 256 else ""
            if key in prompt.choices:
                prompt.callback(key)
            else:
                ui.message = "cancelled"
            return True
        if ui.mode == "text":
            return self._text_key(ch)
        if ui.mode == "log":
            return self._log_key(ch)
        return self._main_key(ch)

    def _text_key(self, ch):
        ui = self.ui
        if ch in (KEY_DOWN, ord("j")):
            ui.text_scroll += 1
        elif ch in (KEY_UP, ord("k")):
            ui.text_scroll = max(0, ui.text_scroll - 1)
        elif ch in (KEY_NPAGE, ord(" ")):
            ui.text_scroll += 10
        elif ch == KEY_PPAGE:
            ui.text_scroll = max(0, ui.text_scroll - 10)
        elif ch in (ord("q"), KEY_ESC) + KEY_ENTER:
            ui.mode = "main"
        return True

    def _log_key(self, ch):
        ui = self.ui
        if ch in (ord("q"), ord("l"), KEY_ESC):
            ui.mode = "main"
        elif ch in (KEY_UP, ord("k")):
            ui.log_scroll += 1
        elif ch in (KEY_DOWN, ord("j")):
            ui.log_scroll = max(0, ui.log_scroll - 1)
        elif ch == KEY_PPAGE:
            ui.log_scroll += 10
        elif ch == KEY_NPAGE:
            ui.log_scroll = max(0, ui.log_scroll - 10)
        elif ch in (ord("g"), KEY_HOME):
            ui.log_scroll = 10 ** 9
        elif ch in (ord("G"), KEY_END):
            ui.log_scroll = 0
        elif ch == ord("f"):
            ui.filter = not ui.filter
            ui.log_scroll = 0
        return True

    def _main_key(self, ch):
        ui = self.ui
        rows = self.snap["rows"]
        if ch == ord("q"):
            return False
        if ch in (KEY_DOWN, ord("j")):
            ui.selected = min(len(rows) - 1, ui.selected + 1) if rows else 0
        elif ch in (KEY_UP, ord("k")):
            ui.selected = max(0, ui.selected - 1)
        elif ch == KEY_NPAGE:
            ui.selected = min(len(rows) - 1, ui.selected + 10) if rows else 0
        elif ch == KEY_PPAGE:
            ui.selected = max(0, ui.selected - 10)
        elif ch == KEY_HOME:
            ui.selected = 0
        elif ch == KEY_END:
            ui.selected = max(0, len(rows) - 1)
        elif ch == ord("l"):
            ui.mode, ui.log_scroll = "log", 0
        elif ch == ord("f"):
            ui.filter = not ui.filter
            ui.message = "log filter: " + (
                "selected routine" if ui.filter else "off")
        elif ch == ord("?"):
            self._show_text("Help", HELP_LINES)
        elif ch == ord("p"):
            self._toggle_pause()
        elif ch in (ord("r"), ord("d"), ord("c")):
            self._start_run({"r": "run", "d": "dry", "c": "count"}[chr(ch)])
        elif ch == ord("t"):
            self._ask_tick()
        elif ch == ord("L"):
            self._ask_reload()
        elif ch == ord("v"):
            self._validate()
        elif ch == KEY_RESIZE:
            pass
        return True

    # actions
    def _show_text(self, title, lines):
        ui = self.ui
        ui.mode, ui.text_title, ui.text_lines, ui.text_scroll = "text", title, lines, 0

    def confirm(self, text, fn):
        self.ui.prompt = Prompt(
            text + " [y/N]", "yn", lambda k: fn() if k == "y" else None)

    def _toggle_pause(self):
        if self.snap["paused"]:
            self.confirm("Resume the daemon (remove state/PAUSED)?", self._resume)
        else:
            self.confirm("Pause the daemon (create state/PAUSED)?", self._pause)

    def _pause(self):
        try:
            state.pause(self.base_dir)
            self.ui.message = "paused (state/PAUSED created)"
        except OSError as exc:
            self.ui.message = f"pause failed: {exc}"
        self.refresh()

    def _resume(self):
        try:
            removed = state.resume(self.base_dir)
            self.ui.message = "resumed" if removed else "was not paused"
        except OSError as exc:
            self.ui.message = f"resume failed: {exc}"
        self.refresh()

    def _start_run(self, mode):
        row = self.selected_row()
        if row is None:
            self.ui.message = "no routine selected"
            return
        rid = row["id"]
        if not row["enabled"]:
            self.ui.message = f"{rid} is disabled; enable it in its YAML first"
            return
        if self.runner.busy(rid):
            self.ui.message = f"{rid}: already running, not starting a second copy"
            return
        verb = {"run": "Run", "dry": "Dry-run", "count": "Count-only"}[mode]
        extra = " (daemon is PAUSED: this will be a no-op)" if self.snap["paused"] else ""
        if mode == "run":
            extra += " This may call the LLM and write data."
        argv = build_run_command(
            self.runner.python, self.runner.script, rid, mode)

        def go():
            ok, msg = self.runner.start(rid, argv, f"{verb.lower()} {rid}")
            self.ui.message = msg
            self.refresh()

        self.confirm(f"{verb} {rid}?{extra}", go)

    def _ask_tick(self):
        legend = " ".join(f"{k}={g}" for k, g in TICK_GROUPS)
        groups = dict(TICK_GROUPS)

        def chosen(key):
            group = groups[key]
            argv = build_tick_command(
                self.runner.python, self.runner.script, group)

            def go():
                ok, msg = self.runner.start(
                    f"tick:{group}", argv, f"tick {group}")
                self.ui.message = msg
                self.refresh()

            self.confirm(f"Run a {group} tick now? Due routines will really run.", go)

        self.ui.prompt = Prompt(f"Tick group: {legend}", "".join(groups), chosen)

    def _ask_reload(self):
        if sys.platform != "darwin" or not self.snap["have_launchctl"]:
            self.ui.message = "reload: needs macOS launchctl; skipped"
            return
        labels = [j["label"] for j in self.snap["jobs"] if j.get("loaded")]
        if not labels:
            self.ui.message = "reload: no loaded launchd jobs match the prefix"
            return

        def go():
            self.ui.message = "; ".join(reload_jobs(labels, self.run))
            self.refresh()

        self.confirm(f"Reload {len(labels)} launchd job(s) (bootout+bootstrap)?", go)

    def _validate(self):
        argv = [str(self.runner.python), str(self.runner.script), "validate"]
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=60,
                cwd=str(self.base_dir), stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.ui.message = f"validate failed to run: {exc}"
            return
        lines = (result.stdout + result.stderr).splitlines() or ["(no output)"]
        self._show_text(f"Validate (exit {result.returncode})", lines)


# --- rendering --------------------------------------------------------------

def _clip(text, width):
    text = text.replace("\t", " ")
    if len(text) <= width:
        return text
    return text[:max(0, width - 3)] + "..." if width > 3 else text[:width]


def _money(value):
    return f"${value:.2f}"


COLUMNS = [
    # key, header, priority (lower dropped last)
    ("id", "ROUTINE", 0),
    ("status", "STATUS", 1),
    ("last", "LAST RUN", 3),
    ("next", "NEXT", 4),
    ("spend", "SPEND/DAY", 2),
    ("every", "EVERY", 5),
    ("en", "ON", 6),
    ("role", "ROLE/GROUP", 7),
    ("flags", "FLAGS", 1),
]


def _row_cells(row, running):
    last = row["last_attempt"]
    if row["result"] not in ("-", "ok"):
        last += f" {row['result']}"
    elif row["result"] == "ok":
        last += " ok"
    spend = _money(row["spend"])
    if row["cap"] is not None:
        spend += f"/{_money(row['cap'])}"
    flags = list(row["flags"])
    if running:
        flags.insert(0, "running...")
    return {
        "id": row["id"],
        "status": row["status"],
        "last": last,
        "next": row["next"],
        "spend": spend,
        "every": row["every"],
        "en": "yes" if row["enabled"] else "no",
        "role": f"{row['role']}/{row['group']}",
        "flags": ", ".join(flags) or "-",
    }


def _table_lines(snap, ui, running, width, height):
    """Header + scrolled rows, each (text, style). Exactly ``height`` lines."""
    cells = [_row_cells(r, r["id"] in running) for r in snap["rows"]]
    cols = list(COLUMNS)
    caps = {"id": 30, "flags": 30}

    def widths(selected):
        return {
            key: min(caps.get(key, 24), max(
                [len(header)] + [len(c[key]) for c in cells]))
            for key, header, _ in selected
        }

    def total(selected):
        w = widths(selected)
        return 2 + sum(w.values()) + 2 * (len(selected) - 1)

    # Drop the least important columns until the table fits.
    while total(cols) > width and len(cols) > 1:
        drop = max((c for c in cols if c[0] not in ("id",)),
                   key=lambda c: c[2], default=None)
        if drop is None:
            break
        cols.remove(drop)
    w = widths(cols)
    over = total(cols) - width
    if over > 0:
        w["id"] = max(8, w["id"] - over)
    elif cols[-1][0] == "flags":
        w["flags"] += -over  # leftover width to the flags column

    def fmt(values):
        return "  " + "  ".join(
            _clip(values[key], w[key]).ljust(w[key]) for key, _, _ in cols
        )

    lines = [(_clip(fmt({k: h for k, h, _ in COLUMNS}), width), "title")]
    body_h = max(0, height - 1)
    if not cells:
        lines.append(("  (no routines defined)", "dim"))
    else:
        if ui.selected < ui.top:
            ui.top = ui.selected
        elif body_h and ui.selected >= ui.top + body_h:
            ui.top = ui.selected - body_h + 1
        ui.top = max(0, min(ui.top, max(0, len(cells) - body_h)))
        for i in range(ui.top, min(len(cells), ui.top + body_h)):
            text = fmt(cells[i])
            if i == ui.selected:
                text = ">" + text[1:]
            row = snap["rows"][i]
            style = "selected" if i == ui.selected else (
                "dim" if not row["enabled"] else
                "bad" if row["flags"] else
                "warn" if row["status"] in ("paused", "budget held") else
                "normal")
            lines.append((_clip(text, width), style))
    lines = lines[:height]
    while len(lines) < height:
        lines.append(("", "normal"))
    return lines


def _log_lines(snap, ui, width, height, title_extra=""):
    """Title + tail of the run log, exactly ``height`` lines."""
    raw = ui.log_lines
    row = snap["rows"][ui.selected] if snap["rows"] else None
    title = "logs/run.log"
    if ui.filter and row is not None:
        title += f" [filter: {row['id']}]"
        if raw is not None:
            raw = filter_log(raw, row["id"])
    title += title_extra
    lines = [(_clip(title, width).ljust(width), "title")]
    avail = max(0, height - 1)
    if raw is None:
        body = ["(no logs/run.log yet)"]
    elif not raw:
        body = ["(no matching log lines)" if ui.filter else "(log is empty)"]
    else:
        body = raw
    ui.log_scroll = max(0, min(ui.log_scroll, max(0, len(body) - avail)))
    end = len(body) - ui.log_scroll
    shown = body[max(0, end - avail):end]
    for line in shown:
        style = "bad" if re.search(r"\b(ERROR|error|failed)\b", line) else "normal"
        lines.append((_clip(line, width), style))
    lines = lines[:height]
    while len(lines) < height:
        lines.append(("", "normal"))
    return lines


def _header_lines(snap, width):
    now_text = time.strftime("%H:%M:%S", time.localtime(snap["now"]))
    health = "OK" if snap["healthy"] else "ATTENTION"
    pause = f"PAUSED: {snap['paused']}" if snap["paused"] else "running"
    cap = snap["spend_cap"]
    spend = _money(snap["spend_total"]) + (
        f" / {_money(cap)}" if cap is not None else " (no global cap)")
    sched = jobs_text(snap["jobs"])
    if not snap["have_launchctl"]:
        sched = "no launchctl (not macOS); scheduler state unknown"
    lines = [
        (_clip(f"Memory Daemon  [{health}]  {pause}  updated {now_text}", width),
         "bad" if snap["paused"] else ("good" if snap["healthy"] else "warn")),
        (_clip(f"Scheduler: {sched}", width), "normal"),
        (_clip(f"Last tick: {snap['last_tick']}   Spend today: {spend}", width),
         "normal"),
    ]
    for problem in snap["problems"][:2]:
        lines.append((_clip(f"ATTENTION: {problem}", width), "bad"))
    return lines


KEY_HINT = ("j/k select  r run  d dry  c count  t tick  p pause  l log  "
            "f filter  L reload  v validate  ? help  q quit")


def render_lines(snap, ui, width, height, running=()):
    """Return exactly ``height`` ``(text, style)`` rows, each <= ``width`` wide.

    Styles: normal, title, selected, good, warn, bad, dim. Pure function of
    its inputs (it only normalizes scroll offsets on ``ui``).
    """
    running = set(running)
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        msg = f"too small ({width}x{height}), need {MIN_WIDTH}x{MIN_HEIGHT}"
        out = [(_clip(msg, max(1, width)), "warn")] if height > 0 else []
        return (out + [("", "normal")] * height)[:max(0, height)]

    if ui.prompt is not None:
        bottom = (_clip(ui.prompt.text, width), "warn")
    elif ui.message:
        bottom = (_clip(ui.message, width), "title")
    else:
        bottom = (_clip(KEY_HINT, width), "dim")

    if ui.mode == "text":
        body = [(_clip(ui.text_title, width), "title")]
        avail = height - 2
        ui.text_scroll = max(0, min(ui.text_scroll, max(0, len(ui.text_lines) - avail)))
        for line in ui.text_lines[ui.text_scroll:ui.text_scroll + avail]:
            body.append((_clip(line, width), "normal"))
        body += [("", "normal")] * (height - 1 - len(body))
        return (body + [bottom])[:height]

    if ui.mode == "log":
        body = _log_lines(snap, ui, width, height - 1)
        return body + [bottom]

    head = _header_lines(snap, width)
    avail = height - len(head) - 1
    log_total = 0
    table_h = avail
    if avail >= 10:
        # The table takes what it needs; the log gets the rest (at least 4).
        table_h = min(len(snap["rows"]) + 1, avail - 4)
        table_h = max(table_h, min(6, avail - 4))
        log_total = avail - table_h
    out = list(head)
    out += _table_lines(snap, ui, running, width, table_h)
    if log_total:
        out += _log_lines(snap, ui, width, log_total)
    out.append(bottom)
    return out[:height]


# --- curses shell -----------------------------------------------------------

def _styles():
    """Map style names to curses attributes; monochrome when colors are absent."""
    bold, rev, dim = curses.A_BOLD, curses.A_REVERSE, getattr(curses, "A_DIM", 0)
    attrs = {"normal": 0, "title": bold, "selected": rev, "dim": dim,
             "good": bold, "warn": bold, "bad": bold}
    try:
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            for pair, fg in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW),
                             (3, curses.COLOR_RED), (4, curses.COLOR_CYAN)):
                curses.init_pair(pair, fg, bg)
            attrs["good"] = curses.color_pair(1) | bold
            attrs["warn"] = curses.color_pair(2) | bold
            attrs["bad"] = curses.color_pair(3)
            attrs["title"] = curses.color_pair(4) | bold
    except curses.error:
        pass
    return attrs


def _paint(stdscr, app, attrs):
    height, width = stdscr.getmaxyx()
    lines = render_lines(
        app.snap, app.ui, width, height, running=app.runner.running_keys())
    stdscr.erase()
    for y, (text, style) in enumerate(lines[:height]):
        try:
            stdscr.addnstr(y, 0, text, max(0, width - 1), attrs.get(style, 0))
        except curses.error:
            pass  # writing the bottom-right cell raises; harmless
    stdscr.refresh()


def run_curses(base_dir, interval=2.0, label_prefix=status.DEFAULT_LAUNCHD_LABEL):
    if curses is None:
        print("curses is not available on this platform", file=sys.stderr)
        return 2
    app = App(base_dir, label_prefix=label_prefix)

    def loop(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        attrs = _styles()
        stdscr.keypad(True)
        stdscr.timeout(int(max(0.2, interval) * 1000))
        while True:
            _paint(stdscr, app, attrs)
            ch = stdscr.getch()
            if ch == -1:
                app.refresh()
                continue
            if not app.handle_key(ch):
                return 0

    try:
        return curses.wrapper(loop)
    except KeyboardInterrupt:
        return 130
