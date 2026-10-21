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
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
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


def _agents_dir(plist_dir=None):
    return Path(plist_dir or Path.home() / "Library" / "LaunchAgents")


def _find_program(args):
    """(interpreter, daemon.py) from a job's argument vector, or None."""
    for i, arg in enumerate(args):
        if os.path.basename(str(arg)) == "daemon.py" and i >= 1:
            return str(args[i - 1]), str(arg)
    return None


def job_program(label, run=launchctl_run, uid=None, plist_dir=None):
    """The (interpreter, daemon.py) a launchd job really runs, or None.

    Reads `launchctl print`, then falls back to the plist's ProgramArguments.
    """
    uid = os.getuid() if uid is None else uid
    rc, out = run(["print", f"gui/{uid}/{label}"])
    if rc == 0:
        match = re.search(
            r"^\s*arguments = \{\n(.*?)^\s*\}", out, re.MULTILINE | re.DOTALL)
        if match:
            found = _find_program(
                [ln.strip() for ln in match.group(1).splitlines() if ln.strip()])
            if found:
                return found
    try:
        with open(_agents_dir(plist_dir) / f"{label}.plist", "rb") as handle:
            args = plistlib.load(handle).get("ProgramArguments") or []
    except Exception:  # missing, unreadable or malformed plist
        return None
    return _find_program(list(args))


def resolve_target(programs, current_dir, explicit=False):
    """Decide which checkout the UI controls from what launchd actually runs.

    ``programs`` are (interpreter, daemon.py) pairs of the loaded jobs. Returns
    {"dir", "python", "conflict"}; ``dir`` is None when no job reveals one.
    """
    dirs = {}
    for python, script in programs:
        dirs.setdefault(Path(script).resolve().parent, python)
    if not dirs:
        return {"dir": None, "python": None, "conflict": None}
    if len(dirs) > 1:
        names = ", ".join(sorted(_short(d) for d in dirs))
        return {"dir": None, "python": None,
                "conflict": f"launchd jobs run different checkouts: {names}"}
    (target, python), = dirs.items()
    if explicit and target != Path(current_dir).resolve():
        return {"dir": None, "python": None, "conflict": (
            f"requested {_short(current_dir)} but launchd runs {_short(target)}")}
    return {"dir": str(target), "python": python, "conflict": None}


def _short(path):
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def reload_jobs(labels, run=launchctl_run, uid=None, plist_dir=None,
                sleep=time.sleep, progress=None):
    """bootout, wait until gone, bootstrap (with retries) from each plist.

    A label with no plist is left loaded (it could not be restored). Returns
    ``(lines, ok)``; on a failed bootstrap the lines include the exact command
    that restores the job.
    """
    uid = os.getuid() if uid is None else uid
    agents = _agents_dir(plist_dir)
    domain = f"gui/{uid}"
    out, ok = [], True

    def note(text):
        if progress:
            progress(text)

    for label in labels:
        plist = agents / f"{label}.plist"
        if not plist.is_file():
            out.append(f"{label}: no plist in LaunchAgents, left loaded")
            continue
        target = f"{domain}/{label}"
        note(f"reload {label}: unloading...")
        run(["bootout", target])
        for _ in range(20):
            if run(["print", target])[0] != 0:
                break
            sleep(0.5)
        note(f"reload {label}: loading...")
        rc_in = 1
        for delay in (0, 0.5, 1, 2):
            if delay:
                sleep(delay)
            rc_in, _ = run(["bootstrap", domain, str(plist)])
            if rc_in == 0:
                break
        if rc_in == 0:
            out.append(f"{label}: reloaded")
        else:
            ok = False
            out.append(
                f"{label}: NOT LOADED. Restore: launchctl bootstrap {domain} {plist}")
    return out, ok


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

PROBE_INTERVAL = 10.0
HISTORY_INTERVAL = 5.0
HISTORY_MAX_BYTES = 4 * 1024 * 1024


class Collector:
    """Builds snapshots; caches the YAML load, launchd probe and tick history.

    The launchd probe runs at most every ``PROBE_INTERVAL`` seconds. With
    ``start_background()`` it runs on a thread, so a hung launchctl never
    freezes the UI. Tick history is parsed from the last ``HISTORY_MAX_BYTES``
    of run.log, at most every ``HISTORY_INTERVAL`` seconds and only when the
    file changed.
    """

    def __init__(self, base_dir, label_prefix=status.DEFAULT_LAUNCHD_LABEL,
                 run=launchctl_run, explicit=False, plist_dir=None):
        self.base_dir = Path(base_dir)
        self.label_prefix = label_prefix
        self.run = run
        self.explicit = explicit
        self.plist_dir = plist_dir
        self._probe_result = None
        self._probe_at = 0.0
        self._stop = threading.Event()
        self._thread = None
        self.retarget(self.base_dir)

    def retarget(self, base_dir):
        self.base_dir = Path(base_dir)
        self._routines = (None, [], None)
        self._history = (None, None, 0.0)

    # launchd probe
    def probe(self):
        """Query launchd now: jobs, and the programs they run."""
        have = launchctl_available()
        labels = discover_labels(self.label_prefix, self.run) if have else []
        jobs = [status.probe_launchd(label) for label in labels or [self.label_prefix]]
        programs = []
        for job in jobs:
            if job.get("loaded"):
                found = job_program(job["label"], self.run, plist_dir=self.plist_dir)
                if found:
                    programs.append(found)
        return {"have_launchctl": have, "jobs": jobs, "programs": programs}

    def _probe_cached(self):
        stale = self._probe_result is None or (
            self._thread is None
            and time.monotonic() - self._probe_at >= PROBE_INTERVAL)
        if stale:
            self._probe_result = self.probe()
            self._probe_at = time.monotonic()
        return self._probe_result

    def start_background(self):
        """Re-probe every PROBE_INTERVAL on a daemon thread."""
        self._probe_cached()  # first result synchronously, before the UI starts
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(PROBE_INTERVAL):
                try:
                    result = self.probe()
                except Exception:
                    continue
                self._probe_result, self._probe_at = result, time.monotonic()

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

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
        cached_sig, cached, at = self._history
        if cached is not None and (
                sig == cached_sig
                or time.monotonic() - at < HISTORY_INTERVAL):
            return cached
        history = status.read_tick_history(path, max_bytes=HISTORY_MAX_BYTES)
        self._history = (sig, history, time.monotonic())
        return history

    def snapshot(self, now=None):
        now = time.time() if now is None else float(now)
        problems = []
        routines, routine_error = self._load_routines()
        if routine_error:
            problems.append(f"routines: {routine_error}")
        probe = self._probe_cached()
        have_launchctl, jobs = probe["have_launchctl"], probe["jobs"]
        target = resolve_target(probe["programs"], self.base_dir, self.explicit)
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
        attention = (bool(problems) or bool(target["conflict"])
                     or any(r["flags"] for r in out_rows))
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
            "target": dict(target, current=str(self.base_dir)),
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
    return [str(python), str(script), "run", f"--routine={routine_id}"] + RUN_MODES[mode]


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

    def retarget(self, base_dir, python=None):
        """Point at another checkout (and the interpreter launchd uses)."""
        self.base_dir = Path(base_dir)
        self.script = self.base_dir / "daemon.py"
        if python and os.access(python, os.X_OK):
            self.python = python

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
                if rc == state.EXIT_LOCKED:
                    done.append(f"{label}: skipped, another run holds the lock")
                else:
                    done.append(
                        f"{label}: finished" + ("" if rc == 0 else f" (exit {rc})"))
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
    pattern = re.compile(r"(?<![\w-])" + re.escape(str(routine_id)) + r"(?![\w-])")
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
                 label_prefix=status.DEFAULT_LAUNCHD_LABEL, run=launchctl_run,
                 explicit=False, spawn=None):
        self.base_dir = Path(base_dir)
        self.run = run
        self.collector = collector or Collector(
            base_dir, label_prefix, run, explicit=explicit)
        self.runner = runner or ActionRunner(base_dir)
        self.spawn = spawn or self._spawn_thread
        self.reloading = False
        self.reload_result = None
        self.ui = UIState()
        self.snap = None
        self.refresh()

    @staticmethod
    def _spawn_thread(fn):
        threading.Thread(target=fn, daemon=True).start()

    def _retarget(self):
        """Follow launchd: control the checkout and interpreter it runs."""
        target = self.snap["target"]
        new = Path(target["dir"]) if target["dir"] else None
        if target["conflict"] or new is None or not (new / "daemon.py").is_file():
            return False
        changed = new.resolve() != self.base_dir.resolve()
        if changed:
            self.base_dir = new
            self.collector.retarget(new)
        self.runner.retarget(self.base_dir, target["python"])
        return changed

    def _blocked(self):
        """Why p/r/t/L must not act right now (and say so), or None."""
        conflict = self.snap["target"]["conflict"]
        if conflict:
            self.ui.message = f"controls disabled: {conflict}"
            return True
        if self.reloading:
            self.ui.message = "a launchd reload is in progress"
            return True
        return False

    # data
    def refresh(self):
        self.snap = self.collector.snapshot()
        if self._retarget():
            self.snap = self.collector.snapshot()
        if self.reload_result is not None:
            lines, ok = self.reload_result
            self.reload_result, self.reloading = None, False
            if ok:
                self.ui.message = "; ".join(lines)
            else:
                self._show_text("launchd reload FAILED: restore manually", lines)
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
        def answer(key):
            if key == "y" and not self._blocked():
                fn()

        self.ui.prompt = Prompt(text + " [y/N]", "yn", answer)

    def _toggle_pause(self):
        if self._blocked():
            return
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
        if self._blocked():
            return
        rid = row["id"]
        if mode == "count" and row["group"] != "capture":
            self.ui.message = "count-only applies to capture routines"
            return
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
        if self._blocked():
            return
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
        if self._blocked():
            return
        if sys.platform != "darwin" or not self.snap["have_launchctl"]:
            self.ui.message = "reload: needs macOS launchctl; skipped"
            return
        loaded = [j for j in self.snap["jobs"] if j.get("loaded")]
        labels = [j["label"] for j in loaded]
        if not labels:
            self.ui.message = "reload: no loaded launchd jobs match the prefix"
            return
        busy = [j["label"] for j in loaded if j.get("state") == "running"]
        if busy:
            self.ui.message = (
                "reload refused: a tick is running (" + ", ".join(busy)
                + "); unloading would kill it. Retry when idle")
            return

        def work():
            try:
                result = reload_jobs(
                    labels, self.run, plist_dir=self.collector.plist_dir,
                    progress=lambda text: setattr(self.ui, "message", text))
            except Exception as exc:
                result = ([f"reload crashed: {exc}"]
                          + [f"Check: launchctl print gui/{os.getuid()}/{x}"
                             for x in labels], False)
            self.reload_result = result

        def go():
            self.reloading = True
            self.ui.message = "reloading launchd jobs..."
            self.spawn(work)

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

def _sanitize(text):
    """Control characters (NUL, C1, escapes) would crash or garble curses."""
    return "".join(
        " " if ch == "\t" else
        "?" if ord(ch) < 32 or 127 <= ord(ch) < 160 else ch
        for ch in str(text))


def _cw(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _dwidth(text):
    return sum(_cw(ch) for ch in text)


def _cut(text, cols):
    out, used = [], 0
    for ch in text:
        w = _cw(ch)
        if used + w > cols:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _clip(text, width):
    text = _sanitize(text)
    if _dwidth(text) <= width:
        return text
    return _cut(text, width - 3) + "..." if width > 3 else _cut(text, width)


def _pad(text, width):
    return text + " " * max(0, width - _dwidth(text))


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
        "id": str(row["id"]),
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
            _pad(_clip(values[key], w[key]), w[key]) for key, _, _ in cols
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
    lines = [(_pad(_clip(title, width), width), "title")]
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
    target = snap.get("target") or {}
    if target.get("conflict"):
        lines.append((_clip(
            f"WARNING: {target['conflict']}. p/r/t/L are disabled.", width), "bad"))
    elif target.get("current"):
        lines.append((_clip(f"Target: {_short(target['current'])}", width), "dim"))
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
        except (curses.error, ValueError):
            pass  # bottom-right cell raises curses.error; NUL raises ValueError
    stdscr.refresh()


def run_curses(base_dir, interval=2.0, label_prefix=status.DEFAULT_LAUNCHD_LABEL,
               explicit=False):
    if curses is None:
        print("curses is not available on this platform", file=sys.stderr)
        return 2
    app = App(base_dir, label_prefix=label_prefix, explicit=explicit)
    app.collector.start_background()

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
    finally:
        app.collector.stop()
