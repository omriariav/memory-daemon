"""Terminal UI: data model, actions, pause switch, launchd discovery, rendering."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import yaml

from workspace_daemon import state, tui

REPO = Path(__file__).resolve().parent.parent

ROUTINE = {
    "enabled": True,
    "description": "synthetic",
    "schedule": {"every": "15m"},
    "source": {"kind": "gmail", "query": "is:unread"},
    "analyze": {"provider": "p", "model": "m", "instruction": "x"},
}


def _make_base(tmp, routines=None):
    base = Path(tmp)
    (base / "routines").mkdir()
    for rid, extra in (routines or {"alpha": {}, "beta": {"enabled": False}}).items():
        data = dict(ROUTINE, id=rid, **extra)
        (base / "routines" / f"{rid}.yaml").write_text(yaml.safe_dump(data))
    return base


def _fake_launchctl(directory, listing):
    """A launchctl on PATH: `list` prints `listing`, `print` succeeds for them."""
    script = Path(directory) / "launchctl"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  list) printf '%s' '{listing}' ;;\n"
        "  print) printf '    state = running\\n    runs = 3\\n    last exit code = 0\\n' ;;\n"
        "  *) echo \"$@\" >> \"$(dirname \"$0\")/calls.txt\" ;;\n"
        "esac\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class PauseFileTest(unittest.TestCase):
    def test_pause_resume_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self.assertIsNone(state.paused(base))
            self.assertTrue(state.pause(base, "maintenance window"))
            self.assertEqual(state.paused(base), "maintenance window")
            self.assertFalse(state.pause(base, "again"))  # idempotent
            self.assertEqual(state.paused(base), "maintenance window")
            self.assertEqual(
                stat.S_IMODE(state.pause_file(base).stat().st_mode), 0o600)
            self.assertTrue(state.resume(base))
            self.assertIsNone(state.paused(base))
            self.assertFalse(state.resume(base))


class LabelDiscoveryTest(unittest.TestCase):
    def test_prefix_filter_with_fake_launchctl(self):
        listing = (
            "PID\tStatus\tLabel\n"
            "-\t0\tcom.memory-daemon\n"
            "123\t0\tcom.memory-daemon-maintenance\n"
            "-\t0\tcom.other.job\n"
            "-\t0\tcom.workspace-daemon\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _fake_launchctl(tmp, listing)
            with mock.patch.dict(
                    os.environ, {"PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"}):
                self.assertEqual(
                    tui.discover_labels("com.memory-daemon"),
                    ["com.memory-daemon", "com.memory-daemon-maintenance"])
                self.assertEqual(
                    tui.discover_labels("com.workspace"), ["com.workspace-daemon"])
                self.assertEqual(tui.discover_labels("nope"), [])

    def test_job_names_strip_common_prefix(self):
        jobs = [{"label": "com.x"}, {"label": "com.x-maintenance"}]
        self.assertEqual(tui.job_names(jobs), ["main", "maintenance"])
        self.assertEqual(tui.job_names(jobs[:1]), ["com.x"])

    def test_missing_launchctl_is_graceful(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertEqual(tui.discover_labels("com.memory-daemon"), [])
            self.assertFalse(tui.launchctl_available())

    def test_reload_waits_for_bootout_then_bootstraps(self):
        calls, loaded = [], [True]

        def run(args):
            calls.append(args)
            if args[0] == "bootout":
                return 0, ""
            if args[0] == "print":
                loaded[0] = False  # gone after the first poll
                return (0 if loaded[0] else 113), ""
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "a.label.plist"
            plist.write_text("x")
            lines, ok = tui.reload_jobs(
                ["a.label", "b.label"], run, uid=501, plist_dir=tmp,
                sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual([c[0] for c in calls], ["bootout", "print", "bootstrap"])
        self.assertEqual(calls[-1], ["bootstrap", "gui/501", str(plist)])
        self.assertEqual(lines[0], "a.label: reloaded")
        self.assertIn("left loaded", lines[1])

    def test_reload_retries_then_reports_restore_command(self):
        attempts = []

        def run(args):
            if args[0] == "bootstrap":
                attempts.append(args)
                return 5, ""
            return 113, ""

        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / "a.label.plist"
            plist.write_text("x")
            lines, ok = tui.reload_jobs(
                ["a.label"], run, uid=501, plist_dir=tmp, sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertEqual(len(attempts), 4)
        self.assertIn(f"launchctl bootstrap gui/501 {plist}", lines[0])

    def test_reload_succeeds_after_a_transient_bootstrap_failure(self):
        rcs = iter([5, 0])

        def run(args):
            return (next(rcs), "") if args[0] == "bootstrap" else (113, "")

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "a.label.plist").write_text("x")
            lines, ok = tui.reload_jobs(
                ["a.label"], run, uid=1, plist_dir=tmp, sleep=lambda s: None)
        self.assertTrue(ok)


class TargetTest(unittest.TestCase):
    def test_program_from_launchctl_print(self):
        out = ("gui/1/x = {\n\targuments = {\n\t\t/opt/venv/bin/python\n"
               "\t\t/srv/live/daemon.py\n\t\ttick\n\t}\n}\n")
        self.assertEqual(
            tui.job_program("x", lambda a: (0, out), uid=1),
            ("/opt/venv/bin/python", "/srv/live/daemon.py"))

    def test_program_falls_back_to_plist(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            with open(Path(tmp) / "x.plist", "wb") as fh:
                plistlib.dump({"ProgramArguments": ["/p/python", "/d/daemon.py", "tick"]}, fh)
            self.assertEqual(
                tui.job_program("x", lambda a: (1, ""), uid=1, plist_dir=tmp),
                ("/p/python", "/d/daemon.py"))
            self.assertIsNone(
                tui.job_program("missing", lambda a: (1, ""), uid=1, plist_dir=tmp))

    def test_resolve_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            live, dev = Path(tmp) / "live", Path(tmp) / "dev"
            live.mkdir()
            dev.mkdir()
            progs = [("/py", str(live / "daemon.py")), ("/py2", str(live / "daemon.py"))]
            t = tui.resolve_target(progs, dev, explicit=False)
            self.assertEqual((t["dir"], t["python"], t["conflict"]),
                             (str(live.resolve()), "/py", None))
            t = tui.resolve_target(progs, dev, explicit=True)
            self.assertIsNone(t["dir"])
            self.assertIn("launchd runs", t["conflict"])
            self.assertIsNone(
                tui.resolve_target(progs, live, explicit=True)["conflict"])
            t = tui.resolve_target(
                progs + [("/py", str(dev / "daemon.py"))], live)
            self.assertIn("different checkouts", t["conflict"])
            self.assertEqual(tui.resolve_target([], dev)["dir"], None)


class CollectorTest(unittest.TestCase):
    def test_snapshot_rows_spend_and_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _make_base(tmp, {
                "alpha": {"budget": {"daily_usd": 1.0}},
                "beta": {"enabled": False},
            })
            (base / "state").mkdir()
            now = time.time()
            usage = base / "state" / "claude-usage.jsonl"
            usage.write_text(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "routine": "alpha", "total_cost_usd": 1.25,
            }) + "\n")
            (base / "state" / "budget.yaml").write_text("daily_usd: 5\n")
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                collector = tui.Collector(base)
                snap = collector.snapshot(now)
                self.assertFalse(snap["have_launchctl"])
                self.assertEqual([r["id"] for r in snap["rows"]], ["alpha", "beta"])
                alpha, beta = snap["rows"]
                self.assertAlmostEqual(alpha["spend"], 1.25)
                self.assertEqual(alpha["cap"], 1.0)
                self.assertEqual(alpha["status"], "budget held")
                self.assertEqual(alpha["group"], "capture")
                self.assertFalse(beta["enabled"])
                self.assertEqual(snap["spend_cap"], 5)
                self.assertIsNone(snap["paused"])
                state.pause(base, "stop")
                snap = collector.snapshot(now)
                self.assertEqual(snap["paused"], "stop")
                self.assertEqual(snap["rows"][0]["status"], "paused")
                line = tui.summary_line(snap)
                self.assertIn("PAUSED (stop)", line)
                self.assertIn("$1.25/$5", line)
                self.assertNotIn("\n", line)

    def test_broken_state_files_do_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = _make_base(tmp)
            (base / "routines" / "bad.yaml").write_text("a: [unclosed")
            (base / "state").mkdir()
            (base / "state" / "budget.yaml").write_text("nonsense: 1\n")
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                snap = tui.Collector(base).snapshot()
        self.assertFalse(snap["healthy"])
        self.assertTrue(snap["problems"])
        self.assertEqual(snap["rows"], [])

    def test_all_matching_jobs_are_probed(self):
        listing = "-\t0\tcom.x.one\n-\t0\tcom.x.two\n"
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as bin_dir:
            base = _make_base(tmp)
            _fake_launchctl(bin_dir, listing)
            with mock.patch.dict(
                    os.environ,
                    {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
                snap = tui.Collector(base, "com.x").snapshot()
        self.assertEqual([j["label"] for j in snap["jobs"]],
                         ["com.x.one", "com.x.two"])
        self.assertTrue(all(j["loaded"] for j in snap["jobs"]))


class ActionTest(unittest.TestCase):
    def test_command_construction(self):
        self.assertEqual(
            tui.build_run_command("py", "d.py", "alpha", "run"),
            ["py", "d.py", "run", "--routine=alpha"])
        self.assertEqual(
            tui.build_run_command("py", "d.py", "alpha", "dry")[-1], "--dry-run")
        self.assertEqual(
            tui.build_run_command("py", "d.py", "alpha", "count")[-1],
            "--count-only")
        self.assertEqual(
            tui.build_tick_command("py", "d.py", "capture"),
            ["py", "d.py", "tick", "--group", "capture"])

    def test_concurrency_guard_and_log_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake = base / "fake_daemon.py"
            fake.write_text(
                "import sys, time\nprint('args', sys.argv[1:])\n"
                "time.sleep(float(sys.argv[-1]) if sys.argv[-1].replace('.','').isdigit() else 0)\n")
            runner = tui.ActionRunner(base, python=sys.executable, script=fake)
            ok, _ = runner.start("alpha", [sys.executable, str(fake), "2"], "run alpha")
            self.assertTrue(ok)
            self.assertTrue(runner.busy("alpha"))
            ok2, msg = runner.start("alpha", [sys.executable, str(fake), "0"], "run alpha")
            self.assertFalse(ok2)
            self.assertIn("already running", msg)
            # A different routine is independent.
            ok3, _ = runner.start("beta", [sys.executable, str(fake), "0"], "run beta")
            self.assertTrue(ok3)
            runner.procs["beta"][0].wait(10)
            self.assertEqual(runner.running_keys(), {"alpha"})
            self.assertEqual(runner.reap(), ["run beta: finished"])
            runner.procs["alpha"][0].kill()
            runner.procs["alpha"][0].wait(10)
            self.assertTrue(runner.reap()[0].startswith("run alpha: finished (exit"))
            ok4, _ = runner.start("alpha", [sys.executable, str(fake), "0"], "run alpha")
            self.assertTrue(ok4)
            runner.procs["alpha"][0].wait(10)
            text = (base / "logs" / "tui.log").read_text()
            self.assertIn("--- ", text)
            self.assertIn("args", text)
            self.assertEqual(
                stat.S_IMODE((base / "logs" / "tui.log").stat().st_mode), 0o600)

    def test_start_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = tui.ActionRunner(tmp, popen=mock.Mock(side_effect=OSError("boom")))
            ok, msg = runner.start("a", ["x"], "run a")
            self.assertFalse(ok)
            self.assertIn("boom", msg)
            self.assertFalse(runner.busy("a"))


class FakeRunner:
    python, script = "py", "d.py"

    def __init__(self):
        self.started = []
        self.busy_keys = set()

    def busy(self, key):
        return key in self.busy_keys

    def running_keys(self):
        return set(self.busy_keys)

    def start(self, key, argv, label):
        self.started.append((key, argv))
        return True, f"{label}: started"

    def reap(self):
        return []

    def retarget(self, base_dir, python=None):
        self.python = python or self.python
        self.target_dir = base_dir


class AppKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = _make_base(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"PATH": "/nonexistent"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = FakeRunner()
        self.app = tui.App(self.base, runner=self.runner)

    def press(self, *keys):
        for k in keys:
            self.assertTrue(self.app.handle_key(ord(k) if isinstance(k, str) else k))

    def test_navigation_and_quit(self):
        self.press("j")
        self.assertEqual(self.app.ui.selected, 1)
        self.press("j")
        self.assertEqual(self.app.ui.selected, 1)
        self.press("k", tui.KEY_UP)
        self.assertEqual(self.app.ui.selected, 0)
        self.assertFalse(self.app.handle_key(ord("q")))

    def test_run_requires_confirmation(self):
        self.press("r")
        self.assertIsNotNone(self.app.ui.prompt)
        self.assertEqual(self.runner.started, [])
        self.press("n")
        self.assertEqual(self.runner.started, [])
        self.press("r", "y")
        self.assertEqual(self.runner.started, [
            ("alpha", ["py", "d.py", "run", "--routine=alpha"])])

    def test_dry_and_count_modes(self):
        self.press("d", "y", "c", "y")
        self.assertEqual([a[-1] for _, a in self.runner.started],
                         ["--dry-run", "--count-only"])

    def test_disabled_routine_is_refused(self):
        self.press("j", "r")
        self.assertIsNone(self.app.ui.prompt)
        self.assertIn("disabled", self.app.ui.message)

    def test_busy_routine_is_refused(self):
        self.runner.busy_keys.add("alpha")
        self.press("r")
        self.assertIsNone(self.app.ui.prompt)
        self.assertIn("already running", self.app.ui.message)

    def test_pause_toggle_with_confirm(self):
        self.press("p")
        self.assertIsNone(state.paused(self.base))  # not yet confirmed
        self.press("y")
        self.assertIsNotNone(state.paused(self.base))
        self.assertTrue(self.app.snap["paused"])
        self.press("p", "y")
        self.assertIsNone(state.paused(self.base))

    def test_tick_asks_group_then_confirms(self):
        self.press("t", "m", "y")
        self.assertEqual(self.runner.started, [
            ("tick:maintenance", ["py", "d.py", "tick", "--group", "maintenance"])])

    def test_reload_skips_without_launchctl(self):
        self.press("L")
        self.assertIsNone(self.app.ui.prompt)
        self.assertIn("skipped", self.app.ui.message)

    def test_modes_help_log_filter(self):
        self.press("?")
        self.assertEqual(self.app.ui.mode, "text")
        self.press("q")
        self.assertEqual(self.app.ui.mode, "main")
        self.press("l")
        self.assertEqual(self.app.ui.mode, "log")
        self.press("f")
        self.assertTrue(self.app.ui.filter)
        self.press("l")
        self.assertEqual(self.app.ui.mode, "main")


class AppSafetyTest(unittest.TestCase):
    """Retargeting, wrong-target refusal, reload guards, other fixes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dev, self.live = root / "dev", root / "live"
        self.dev.mkdir()
        self.live.mkdir()
        _make_base_at(self.dev)
        _make_base_at(self.live)
        (self.live / "daemon.py").write_text("# live\n")
        p = mock.patch.dict(os.environ, {"PATH": "/nonexistent"})
        p.start()
        self.addCleanup(p.stop)

    def _app(self, programs, explicit=False, **kw):
        probe = {"have_launchctl": True, "programs": programs, "jobs": [{
            "label": "com.x", "loaded": True, "state": "not running",
            "interval_seconds": 900, "last_exit": 0}]}
        patcher = mock.patch.object(tui.Collector, "probe", return_value=probe)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tui.App(self.dev, runner=FakeRunner(), explicit=explicit, **kw), probe

    def test_retargets_to_the_checkout_launchd_runs(self):
        app, _ = self._app([(sys.executable, str(self.live / "daemon.py"))])
        self.assertEqual(app.base_dir.resolve(), self.live.resolve())
        self.assertEqual(app.collector.base_dir.resolve(), self.live.resolve())
        self.assertEqual(app.runner.python, sys.executable)
        app.handle_key(ord("p"))
        app.handle_key(ord("y"))
        self.assertTrue((self.live / "state" / "PAUSED").exists())
        self.assertFalse((self.dev / "state" / "PAUSED").exists())
        header = [t for t, _ in tui.render_lines(
            app.snap, app.ui, 100, 30)]
        self.assertTrue(any(t.startswith("Target:") for t in header))

    def test_explicit_mismatch_disables_controls(self):
        app, _ = self._app(
            [(sys.executable, str(self.live / "daemon.py"))], explicit=True)
        self.assertEqual(app.base_dir.resolve(), self.dev.resolve())
        for key in "prtL":
            app.handle_key(ord(key))
            self.assertIsNone(app.ui.prompt, key)
            self.assertIn("controls disabled", app.ui.message)
            app.ui.message = ""
        self.assertFalse((self.dev / "state" / "PAUSED").exists())
        self.assertEqual(app.runner.started, [])
        lines = [t for t, _ in tui.render_lines(app.snap, app.ui, 100, 30)]
        self.assertTrue(any(t.startswith("WARNING:") for t in lines))

    def test_disagreeing_jobs_disable_controls(self):
        app, _ = self._app([(sys.executable, str(self.live / "daemon.py")),
                            (sys.executable, str(self.dev / "daemon.py"))])
        app.handle_key(ord("p"))
        self.assertIn("different checkouts", app.ui.message)

    def test_reload_refused_while_a_tick_runs(self):
        app, probe = self._app([])
        probe["jobs"][0]["state"] = "running"
        app.collector._probe_result = probe
        app.refresh()
        with mock.patch.object(tui.sys, "platform", "darwin"):
            app.handle_key(ord("L"))
        self.assertIsNone(app.ui.prompt)
        self.assertIn("reload refused", app.ui.message)

    def test_reload_runs_off_thread_and_reports_failure(self):
        spawned = []
        app, _ = self._app([], spawn=spawned.append)
        results = [(["com.x: NOT LOADED. Restore: launchctl bootstrap gui/1 /p"], False)]
        with mock.patch.object(tui.sys, "platform", "darwin"), \
                mock.patch.object(tui, "reload_jobs", side_effect=results):
            app.handle_key(ord("L"))
            app.handle_key(ord("y"))
            self.assertTrue(app.reloading)
            app.handle_key(ord("p"))  # blocked while reloading
            self.assertIn("in progress", app.ui.message)
            spawned[0]()  # what the worker thread would do
        app.refresh()
        self.assertFalse(app.reloading)
        self.assertEqual(app.ui.mode, "text")
        self.assertIn("launchctl bootstrap gui/1 /p", app.ui.text_lines[0])

    def test_count_only_refused_for_non_capture_routine(self):
        app, _ = self._app([])
        app.snap["rows"][0]["group"] = "maintenance"
        app.handle_key(ord("c"))
        self.assertIsNone(app.ui.prompt)
        self.assertIn("capture routines", app.ui.message)

    def test_probe_is_cached(self):
        collector = tui.Collector(self.dev)
        with mock.patch.object(
                tui.Collector, "probe",
                return_value={"have_launchctl": False, "jobs": [], "programs": []}
        ) as probe:
            collector.snapshot()
            collector.snapshot()
        self.assertEqual(probe.call_count, 1)

    def test_reap_reports_lock_skip(self):
        class Proc:
            def poll(self):
                return state.EXIT_LOCKED
        runner = tui.ActionRunner(self.dev)
        runner.procs["a"] = (Proc(), "run a")
        self.assertEqual(runner.reap(), ["run a: skipped, another run holds the lock"])

    def test_integer_routine_ids_render(self):
        snap = RenderTest()._snap(2)
        snap["rows"][0]["id"] = 7
        ui = tui.UIState()
        ui.filter = True
        ui.log_lines = ["x routine=7 ok"]
        lines = tui.render_lines(snap, ui, 100, 30)
        self.assertTrue(any(" 7 " in t for t, _ in lines))
        self.assertEqual(tui.filter_log(["a routine=7"], 7), ["a routine=7"])


class RenderTest(unittest.TestCase):
    def _snap(self, n=3):
        rows = [{
            "id": f"routine-{i}", "role": "domain", "group": "capture",
            "enabled": i != 2, "every": "15m", "status": "waiting",
            "last_attempt": "5m ago", "result": "ok", "next": "in 10m",
            "spend": 0.5 * i, "cap": 2.0 if i else None,
            "flags": ["1 memory sink"] if i == 1 else [],
        } for i in range(n)]
        return {
            "now": 0, "paused": None, "have_launchctl": True,
            "jobs": [{"label": "com.memory-daemon", "loaded": True,
                      "state": "not running", "interval_seconds": 900,
                      "last_exit": 0}],
            "last_tick": "3m ago (done)", "spend_total": 1.5, "spend_cap": 10,
            "rows": rows, "problems": [], "healthy": False,
        }

    def test_exact_geometry_at_many_sizes(self):
        for w, h in ((100, 30), (80, 24), (40, 8), (60, 12), (200, 60)):
            ui = tui.UIState()
            ui.log_lines = ["2024-01-01T00:00:00Z tick: x"] * 50
            lines = tui.render_lines(self._snap(30), ui, w, h)
            self.assertEqual(len(lines), h, (w, h))
            self.assertTrue(all(len(t) <= w for t, _ in lines), (w, h))

    def test_control_characters_are_neutralized(self):
        self.assertEqual(tui._clip("a\x00b\x1b[31m\x85c\td", 40), "a?b?[31m?c d")
        ui = tui.UIState()
        ui.mode = "log"
        ui.log_lines = ["before\x00after", "tab\there"]
        for t, _ in tui.render_lines(self._snap(), ui, 80, 12):
            self.assertNotIn("\x00", t)
            self.assertNotIn("\t", t)

    def test_wide_characters_are_measured_in_cells(self):
        text = tui._clip("\u65e5\u672c\u8a9e" * 10, 21)
        self.assertLessEqual(tui._dwidth(text), 21)
        self.assertTrue(text.endswith("..."))
        self.assertEqual(tui._dwidth(tui._pad("\u65e5", 6)), 6)

    def test_paint_survives_nul_and_value_error(self):
        class Screen:
            def getmaxyx(self):
                return 30, 100

            def erase(self):
                pass

            def refresh(self):
                pass

            def addnstr(self, *a):
                raise ValueError("embedded null character")

        app = mock.Mock()
        app.snap, app.ui = self._snap(), tui.UIState()
        app.runner.running_keys.return_value = set()
        tui._paint(Screen(), app, {})

    def test_tiny_terminal_message(self):
        lines = tui.render_lines(self._snap(), tui.UIState(), 20, 5)
        self.assertEqual(len(lines), 5)
        self.assertIn("too small", lines[0][0])
        self.assertEqual(tui.render_lines(self._snap(), tui.UIState(), 0, 0), [])

    def test_selection_marker_scroll_and_flags(self):
        ui = tui.UIState()
        ui.selected = 25
        lines = tui.render_lines(self._snap(30), ui, 100, 30, running={"routine-25"})
        text = [t for t, _ in lines]
        marked = [t for t in text if t.startswith(">")]
        self.assertEqual(len(marked), 1)
        self.assertIn("routine-25", marked[0])
        self.assertIn("running...", marked[0])
        self.assertTrue(any("1 memory sink" in t for t in text) or ui.top > 1)

    def test_paused_header_and_prompt(self):
        snap = self._snap()
        snap["paused"] = "stop"
        ui = tui.UIState()
        ui.prompt = tui.Prompt("Resume? [y/N]", "yn", lambda k: None)
        lines = tui.render_lines(snap, ui, 100, 20)
        self.assertIn("PAUSED: stop", lines[0][0])
        self.assertEqual(lines[-1][0], "Resume? [y/N]")

    def test_log_filter_and_missing_log(self):
        ui = tui.UIState()
        ui.mode, ui.filter = "log", True
        ui.log_lines = ["a routine=routine-0 ok", "b routine=routine-1 ok",
                        "c due=routine-0, other", "d routine=routine-00 ok"]
        lines = [t for t, _ in tui.render_lines(self._snap(), ui, 80, 12)]
        body = "\n".join(lines)
        self.assertIn("routine=routine-0 ok", body)
        self.assertIn("due=routine-0", body)
        self.assertNotIn("routine-1 ok", body)
        self.assertNotIn("routine-00", body)
        ui.log_lines = None
        ui.filter = False
        self.assertIn("no logs/run.log yet",
                      "\n".join(t for t, _ in tui.render_lines(self._snap(), ui, 80, 12)))

    def test_no_launchctl_and_empty_routines(self):
        snap = self._snap(0)
        snap["have_launchctl"] = False
        lines = [t for t, _ in tui.render_lines(snap, tui.UIState(), 80, 20)]
        self.assertTrue(any("no launchctl" in t for t in lines))
        self.assertTrue(any("no routines defined" in t for t in lines))

    def test_read_log_tail_missing_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tui.read_log_tail(Path(tmp) / "none.log"))
            path = Path(tmp) / "run.log"
            path.write_text("".join(f"line {i}\n" for i in range(2000)))
            tail = tui.read_log_tail(path, 5)
            self.assertEqual(tail[-1], "line 1999")
            self.assertEqual(len(tail), 5)


def _make_base_at(base):
    (base / "routines").mkdir(exist_ok=True)
    data = dict(ROUTINE, id="alpha")
    (base / "routines" / "alpha.yaml").write_text(yaml.safe_dump(data))


class CliTest(unittest.TestCase):
    def test_resume_on_a_directory_is_a_clean_error(self):
        import daemon as daemon_cli
        with tempfile.TemporaryDirectory() as tmp:
            state.pause_file(tmp).parent.mkdir()
            state.pause_file(tmp).mkdir()
            args = mock.Mock(action="resume")
            with mock.patch.object(daemon_cli, "BASE_DIR", Path(tmp)), \
                    mock.patch("sys.stderr") as err:
                self.assertEqual(daemon_cli.cmd_ctl(args), 1)
            self.assertTrue(err.write.called)

    def test_script_derives_dir_and_python_from_the_plist(self):
        import plistlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live = root / "live"
            live.mkdir()
            (live / "daemon.py").write_text(
                "import sys\nprint('LIVE', __file__.split('/')[-2], sys.argv[1:])\n")
            agents = root / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            with open(agents / "com.t.x.plist", "wb") as fh:
                plistlib.dump({"ProgramArguments": [
                    sys.executable, str(live / "daemon.py"), "tick"]}, fh)
            env = {"PATH": "/usr/bin:/bin", "HOME": str(root),
                   "MEMORY_DAEMON_LAUNCHD_LABEL": "com.t.x"}
            result = subprocess.run(
                ["bash", str(REPO / "memory-daemon-status.sh"), "state"],
                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("LIVE live ['ctl', 'state']", result.stdout)

    def test_script_logs_n_and_missing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "daemon.py").write_text("")
            env = {"PATH": "/usr/bin:/bin", "HOME": tmp,
                   "MEMORY_DAEMON_DIR": tmp}
            script = str(REPO / "memory-daemon-status.sh")
            missing = subprocess.run(["bash", script, "logs"],
                                     capture_output=True, text=True, env=env)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("no log yet", missing.stderr)
            (repo / "logs").mkdir()
            (repo / "logs" / "run.log").write_text(
                "".join(f"l{i}\n" for i in range(100)))
            out = subprocess.run(["bash", script, "logs", "-n", "3"],
                                 capture_output=True, text=True, env=env)
            self.assertEqual(out.stdout.split(), ["l97", "l98", "l99"], out.stderr)


    def _run(self, base_args, env_extra=None):
        env = dict(os.environ, PATH="/usr/bin:/bin")
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(REPO / "daemon.py")] + base_args,
            capture_output=True, text=True, env=env, cwd=str(REPO),
            stdin=subprocess.DEVNULL)

    def test_tui_without_tty_refuses(self):
        result = self._run(["tui"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("terminal", result.stderr)

    def test_script_default_without_tty_is_status(self):
        result = subprocess.run(
            ["bash", str(REPO / "memory-daemon-status.sh"), "--label", "zz.none"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            env=dict(os.environ, MEMORY_DAEMON_PYTHON=sys.executable))
        self.assertIn("Memory Daemon", result.stdout)

    def test_script_missing_dir_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "memory-daemon-status.sh"
            script.write_text((REPO / "memory-daemon-status.sh").read_text())
            result = subprocess.run(
                ["bash", str(script)], capture_output=True, text=True,
                env={"PATH": "/usr/bin:/bin", "HOME": tmp})
        self.assertEqual(result.returncode, 1)
        self.assertIn("MEMORY_DAEMON_DIR", result.stderr)


if __name__ == "__main__":
    unittest.main()
