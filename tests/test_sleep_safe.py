"""Sleep-safe ticks: preflight skips, sleep-during-call outages, status."""
import contextlib
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import daemon as daemon_cli
import test_fibery_tasks_outbound as tfo
from test_multi_source_routing import multi_routine
from workspace_daemon import (
    tui,
    claude_bridge, gmail_mcp, preflight, runner, sleepclock, slack_mcp,
    state, status,
)

CLAMSHELL_YES = (
    '  |   "AppleClamshellCausesSleep" = Yes\n'
    '  |   "AppleClamshellState" = Yes\n'
)
CLAMSHELL_DOCKED = (  # closed, but a display/power keeps the Mac awake
    '  |   "AppleClamshellCausesSleep" = No\n'
    '  |   "AppleClamshellState" = Yes\n'
)
CLAMSHELL_NO = (
    '  |   "AppleClamshellCausesSleep" = Yes\n'
    '  |   "AppleClamshellState" = No\n'
)
ROOT_FULL = '  | "System Capabilities" = 15\n'
ROOT_DARK = '  | "System Capabilities" = 9\n'


def fake_ioreg(clamshell=CLAMSHELL_NO, root=ROOT_FULL, returncode=0,
               error=None):
    def run(cmd, **kwargs):
        assert cmd[0] == "ioreg" and kwargs["timeout"] <= 2
        if error:
            raise error
        out = clamshell if "AppleClamshellState" in " ".join(cmd) else root
        return SimpleNamespace(returncode=returncode, stdout=out)
    return run


class LidGuardTest(unittest.TestCase):
    def reason(self, **kwargs):
        return preflight.lid_skip_reason("darwin", fake_ioreg(**kwargs))

    def test_lid_closed_and_closing_sleeps_the_mac_skips(self):
        self.assertEqual(
            self.reason(clamshell=CLAMSHELL_YES), preflight.REASON_LID)

    def test_clamshell_mode_with_display_runs(self):
        self.assertIsNone(self.reason(clamshell=CLAMSHELL_DOCKED))

    def test_lid_open_runs(self):
        self.assertIsNone(self.reason())

    def test_dark_wake_skips(self):
        self.assertEqual(
            self.reason(root=ROOT_DARK), preflight.REASON_DARK_WAKE)

    def test_detection_errors_fail_open(self):
        for kwargs in (
            {"error": OSError("no ioreg")},
            {"error": subprocess.TimeoutExpired("ioreg", 2)},
            {"returncode": 1, "clamshell": CLAMSHELL_YES},
            {"clamshell": "garbage", "root": "garbage"},
        ):
            self.assertIsNone(self.reason(**kwargs), kwargs)

    def test_missing_causes_sleep_key_fails_open(self):
        self.assertIsNone(self.reason(
            clamshell='  |   "AppleClamshellState" = Yes\n'))

    def test_other_platforms_never_skip(self):
        self.assertIsNone(preflight.lid_skip_reason(
            "linux", fake_ioreg(clamshell=CLAMSHELL_YES)))


class NetworkAndConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMORY_DAEMON_PREFLIGHT", None)

    def write_config(self, text):
        (self.base / "state").mkdir(exist_ok=True)
        (self.base / "state" / "daemon.yaml").write_text(text)

    def test_defaults_without_file(self):
        self.assertEqual(preflight.load_config(self.base), preflight.defaults())

    def test_broken_or_odd_config_falls_back_to_defaults(self):
        for text in ("{{{ not yaml", "- a list", "network_check: 7"):
            self.write_config(text)
            self.assertEqual(
                preflight.load_config(self.base), preflight.defaults(), text)

    def test_custom_config(self):
        self.write_config(
            "skip_when_lid_closed: false\n"
            "network_check:\n  enabled: true\n"
            "  hosts: ['a.example:443', 'bad host', 'b.example:80']\n"
            "  timeout_seconds: 1.5\nskip_warn_hours: 2\n"
        )
        cfg = preflight.load_config(self.base)
        self.assertFalse(cfg["skip_when_lid_closed"])
        self.assertEqual(
            cfg["network_check"]["hosts"], ["a.example:443", "b.example:80"])
        self.assertEqual(cfg["network_check"]["timeout_seconds"], 1.5)
        self.assertEqual(cfg["skip_warn_hours"], 2.0)
        self.assertTrue(cfg["network_check"]["hosts_explicit"])

    def test_offline_only_when_every_host_fails(self):
        seen = []

        def connect(target, timeout):
            seen.append((target, timeout))
            if target[0] == "down.example":
                raise OSError("unreachable")
            return mock.Mock()

        self.assertTrue(preflight.offline(
            ["down.example:443"], 3, connect))
        self.assertFalse(preflight.offline(
            ["down.example:443", "up.example:443"], 3, connect))
        self.assertEqual(seen[0], (("down.example", 443), 3))

    def test_check_reports_offline_then_lid_first(self):
        refuse = mock.Mock(side_effect=OSError("down"))
        self.assertEqual(
            preflight.check(self.base, "darwin", fake_ioreg(), refuse),
            preflight.REASON_OFFLINE)
        self.assertEqual(
            preflight.check(
                self.base, "darwin", fake_ioreg(clamshell=CLAMSHELL_YES),
                refuse),
            preflight.REASON_LID)
        self.assertIsNone(preflight.check(
            self.base, "darwin", fake_ioreg(), mock.Mock()))

    def test_disabled_checks_never_skip(self):
        self.write_config(
            "skip_when_lid_closed: false\nnetwork_check: {enabled: false}\n")
        refuse = mock.Mock(side_effect=OSError("down"))
        self.assertIsNone(preflight.check(
            self.base, "darwin", fake_ioreg(clamshell=CLAMSHELL_YES), refuse))
        refuse.assert_not_called()


class TickGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMORY_DAEMON_PREFLIGHT", None)
        self.routine = multi_routine(self.base / "vault")
        self.logs = []

    def tick(self, args=None, **patches):
        args = args or SimpleNamespace(
            dry_run=False, refresh_labels=False, group="all")
        totals = {"processed": 0, "skipped": 0, "errors": 0, "matched": 0,
                  "fallbacks": 0, "pending_actions": 0, "ambiguous": 0}
        self.run_mock = mock.Mock(return_value=totals)
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli.uuid, "uuid4",
                 return_value=SimpleNamespace(hex="abc123456789ffff")), \
             mock.patch.object(
                 daemon_cli.config, "discover", return_value=[self.routine]), \
             mock.patch.object(daemon_cli.runner, "run", self.run_mock), \
             mock.patch.object(
                 daemon_cli, "log", side_effect=self.logs.append):
            with contextlib.ExitStack() as stack:
                for target, value in patches.items():
                    stack.enter_context(
                        mock.patch.object(preflight, target, value))
                return daemon_cli.cmd_tick(args)

    def untouched(self):
        self.run_mock.assert_not_called()
        self.assertFalse(state.schedule_file(self.base).exists())
        self.assertFalse(state.cursor_file(self.base).exists())
        self.assertFalse(state.state_file(self.base).exists())

    def test_lid_closed_skips_quietly_and_touches_nothing(self):
        rc = self.tick(lid_skip_reason=lambda *a, **k: preflight.REASON_LID,
                       offline=lambda *a, **k: False)
        self.assertEqual(rc, 0)
        self.untouched()
        self.assertEqual(self.logs, ["tick[abc123456789] skipped: lid closed"])

    def test_offline_skips_without_counters_or_cursors(self):
        rc = self.tick(lid_skip_reason=lambda *a, **k: None,
                       offline=lambda *a, **k: True)
        self.assertEqual(rc, 0)
        self.untouched()
        self.assertEqual(self.logs, ["tick[abc123456789] skipped: offline"])
        # Nothing ran, so the routine is still due on the next tick.
        self.assertTrue(state.ScheduleStore(self.base).due(self.routine))

    def test_detection_error_runs_the_tick(self):
        def boom(cmd, **kwargs):
            raise OSError("ioreg missing")
        with mock.patch.object(preflight.subprocess, "run", boom), \
             mock.patch.object(preflight.sys, "platform", "darwin"):
            self.tick(offline=lambda *a, **k: False)
        self.run_mock.assert_called_once()
        self.assertIn("domain", state.ScheduleStore(self.base).entries)

    def test_dry_run_is_not_gated(self):
        args = SimpleNamespace(dry_run=True, refresh_labels=False, group="all")
        self.tick(args, lid_skip_reason=lambda *a, **k: preflight.REASON_LID)
        self.assertNotIn("skipped: lid closed", " ".join(self.logs))

    def test_explicit_run_is_not_gated_without_flag(self):
        stop = RuntimeError("past the gate")
        args = SimpleNamespace(
            dry_run=False, count_only=False, ignore_pause=False,
            routine=None, include_disabled=False)
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(daemon_cli, "log"), \
             mock.patch.object(
                 preflight, "check", side_effect=AssertionError("gated")), \
             mock.patch.object(daemon_cli.config, "secure_routine_files"), \
             mock.patch.object(daemon_cli.config, "discover", side_effect=stop):
            with self.assertRaises(RuntimeError) as ctx:
                daemon_cli.cmd_run(args)
        self.assertIs(ctx.exception, stop)

    def test_explicit_run_with_preflight_flag_skips(self):
        args = SimpleNamespace(
            dry_run=False, count_only=False, ignore_pause=False,
            routine=None, include_disabled=False, preflight=True)
        with mock.patch.object(daemon_cli, "BASE_DIR", self.base), \
             mock.patch.object(daemon_cli, "set_log_file"), \
             mock.patch.object(
                 daemon_cli, "log", side_effect=self.logs.append), \
             mock.patch.object(preflight, "check", return_value="offline"), \
             mock.patch.object(
                 daemon_cli.config, "discover",
                 side_effect=AssertionError("ran")):
            self.assertEqual(daemon_cli.cmd_run(args), 0)
        self.assertEqual(self.logs, ["run skipped: offline"])


class CatchUpTest(unittest.TestCase):
    """The first tick after a long sleep runs each due routine once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_long_gap_is_one_due_run_not_a_backlog(self):
        routine = {"id": "r", "enabled": True, "schedule": {"every": "15m"}}
        store = state.ScheduleStore(self.base)
        now = 1_800_000_000.0
        store.mark_attempted({"r"}, now=now - 3 * 86400)
        self.assertTrue(store.due(routine, now=now))
        store.mark_attempted({"r"}, now=now)
        # Three days of missed 15-minute intervals collapse into that one run.
        self.assertFalse(store.due(routine, now=now + 60))
        self.assertFalse(store.due(routine, now=now + 14 * 60))
        self.assertTrue(store.due(routine, now=now + 15 * 60))


class SleepClockTest(unittest.TestCase):
    def test_gap_between_wall_and_monotonic_means_sleep(self):
        wall, mono = [1000.0], [50.0]
        span = sleepclock.Span(lambda: wall[0], lambda: mono[0])
        wall[0] += 5
        mono[0] += 5
        self.assertFalse(span.slept())
        wall[0] += 600  # the machine slept ten minutes
        self.assertTrue(span.slept())
        self.assertAlmostEqual(span.slept_seconds(), 600)

    def test_backwards_wall_clock_is_not_sleep(self):
        wall, mono = [1000.0], [50.0]
        span = sleepclock.Span(lambda: wall[0], lambda: mono[0])
        wall[0] -= 100
        self.assertFalse(span.slept())


class Slept:
    def __init__(self, *a, **k):
        pass

    def slept(self, *a):
        return True


class NotSlept(Slept):
    def slept(self, *a):
        return False


class BridgeOutageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def attempt(self, span_cls):
        def timed_out(*a, **k):
            raise claude_bridge.BridgeError("claude run timed out after 180s")
        with mock.patch.object(claude_bridge, "_spawn", timed_out), \
             mock.patch.object(claude_bridge.sleepclock, "Span", span_cls):
            return claude_bridge.call_tools(
                "p", {"mcp__x__t": {}}, base_dir=self.base, attempts=1,
                connect_delay=0)

    def test_failure_across_sleep_is_an_outage(self):
        with self.assertRaises(claude_bridge.BridgeOutage) as ctx:
            self.attempt(Slept)
        self.assertTrue(sleepclock.is_outage(ctx.exception))
        self.assertTrue(runner._is_shared_dependency_failure(ctx.exception))

    def test_ordinary_failure_stays_a_plain_bridge_error(self):
        with self.assertRaises(claude_bridge.BridgeError) as ctx:
            self.attempt(NotSlept)
        self.assertNotIsInstance(ctx.exception, claude_bridge.BridgeOutage)

    def test_guard_stops_are_never_reclassified(self):
        self.assertTrue(
            issubclass(claude_bridge.BudgetExhausted, claude_bridge.BridgeError)
            and not issubclass(
                claude_bridge.BudgetExhausted, claude_bridge.BridgeOutage))


class SessionOutageTest(unittest.TestCase):
    """Lost results across a sleep never reach the lone/attempt counters."""

    def run_session(self, module, span_cls):
        cfg = {"model": "haiku"}
        calls = [{"channel": "C1"}, {"channel": "C2"}]
        with mock.patch.object(module.claude_bridge, "call_tools",
                               return_value=[]), \
             mock.patch.object(module.sleepclock, "Span", span_cls), \
             mock.patch.object(module, "_base_dir", None):
            return module._one_session(
                cfg, "mcp__x__read", {}, calls, "thread read", [])

    def test_slack_incomplete_session_after_sleep_is_an_outage(self):
        with self.assertRaises(claude_bridge.BridgeOutage):
            self.run_session(slack_mcp, Slept)

    def test_gmail_incomplete_session_after_sleep_is_an_outage(self):
        with self.assertRaises(claude_bridge.BridgeOutage):
            self.run_session(gmail_mcp, Slept)

    def test_without_sleep_missing_results_stay_per_item(self):
        self.assertEqual(
            self.run_session(slack_mcp, NotSlept), [None, None])

    def test_outage_does_not_bump_attempt_counters(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        batch = {"error": None, "threads": {}, "lone": None}
        candidate = {"id": "cand@1"}
        key = ("C1", "1.0")
        with mock.patch.object(slack_mcp, "_base_dir", tmp.name), \
             mock.patch.object(
                 slack_mcp, "_read_threads",
                 side_effect=claude_bridge.BridgeOutage("x (machine slept)")), \
             mock.patch.object(slack_mcp, "_bump_attempts") as bump:
            raw = {"channel": key[0], "anchor": key[1], "_batch": batch}
            with self.assertRaises(claude_bridge.BridgeOutage):
                slack_mcp.fetch({}, dict(candidate, raw=raw))
        bump.assert_not_called()


class FiberyUnknownOutcomeTest(tfo.OutboundBase):
    def test_outage_during_verification_keeps_unconfirmed_and_says_so(self):
        tfo.write_entry(self.store, "a1")
        original = self._call

        def call(prompt, tools, **kwargs):
            if self.writes() and tfo.RTOOL in tools:
                raise claude_bridge.BridgeOutage("timed out (machine slept)")
            return original(prompt, tools, **kwargs)

        with mock.patch.object(tfo.fto.claude_bridge, "call_tools", call):
            with self.assertRaises(RuntimeError) as ctx:
                self.run_once()
        self.assertIn("write outcome unknown (machine slept/offline)",
                      str(ctx.exception))
        self.assertEqual(self.items()["a1"]["state"], "attempted")
        self.assertTrue(runner._is_shared_dependency_failure(ctx.exception))
        # The next run still holds it as unconfirmed and never rewrites.
        self.run_once()
        self.assertEqual(len(self.writes()), 1)


class StatusSkipTest(unittest.TestCase):
    NOW = time.mktime(time.strptime("2026-01-10 12:00:00", "%Y-%m-%d %H:%M:%S"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.log = self.base / "run.log"

    def history(self, *lines):
        self.log.write_text("\n".join(lines) + "\n")
        return status.read_tick_history(self.log)

    def test_skip_is_recorded_without_touching_routines(self):
        history = self.history(
            "2026-01-10T09:00:00Z tick[a1](capture): due=r",
            "2026-01-10T09:00:30Z tick[a1](capture) done: 1 processed, "
            "0 already-seen, 0 error(s)",
            "2026-01-10T09:15:00Z tick[b2](capture) skipped: offline",
        )
        latest = history["latest_by_group"]["capture"]
        self.assertEqual(latest["state"], "preflight_skipped")
        self.assertEqual(latest["reason"], "offline")
        self.assertEqual(history["routines"]["r"]["state"], "ok")

    def test_tick_text_names_the_reason_and_age(self):
        latest = {"state": "preflight_skipped", "reason": "offline",
                  "at": "2026-01-10T11:50:00+00:00", "message": "m"}
        now = status._iso_epoch(latest["at"]) + 600
        self.assertEqual(
            status._tick_text(latest, None, now), "skipped (offline) 10m ago")
        self.assertTrue(status._latest_tick_healthy(
            dict(latest, state="preflight_skipped"), False))

    def test_any_skip_streak_warns_after_threshold(self):
        # Mixed reasons all night: one streak, not reset by the reason.
        history = self.history(
            "2026-01-10T00:00:00+00:00 tick[a](capture) skipped: lid closed",
            "2026-01-10T01:00:00+00:00 tick[b](capture) skipped: dark wake",
            "2026-01-10T02:00:00+00:00 tick[c](capture) skipped: offline",
        )
        since = status._iso_epoch("2026-01-10T00:00:00+00:00")
        self.assertEqual(history["skipping_since"],
                         {"capture": "2026-01-10T00:00:00+00:00"})
        self.assertEqual(
            status.skip_streak_problems(self.base, history, since + 3600), [])
        problems = status.skip_streak_problems(
            self.base, history, since + 13 * 3600)
        self.assertEqual(len(problems), 1)
        self.assertIn("offline", problems[0])

    def test_a_tick_that_ran_resets_the_streak(self):
        history = self.history(
            "2026-01-10T01:00:00+00:00 tick[a](capture) skipped: offline",
            "2026-01-10T02:00:00+00:00 tick[b](capture): no routines due",
            "2026-01-10T03:00:00+00:00 tick[c](capture) skipped: lid closed",
        )
        self.assertEqual(
            history["skipping_since"],
            {"capture": "2026-01-10T03:00:00+00:00"})
        later = status._iso_epoch("2026-01-10T03:00:00+00:00") + 13 * 3600
        self.assertEqual(len(
            status.skip_streak_problems(self.base, history, later)), 1)
        ran = self.history(
            "2026-01-10T01:00:00+00:00 tick[a](capture) skipped: offline",
            "2026-01-10T02:00:00+00:00 tick[b](capture): no routines due",
        )
        self.assertEqual(
            status.skip_streak_problems(self.base, ran, later), [])

    def test_tui_snapshot_includes_the_streak(self):
        history = self.history(
            "2026-01-10T00:00:00+00:00 tick[a](capture) skipped: lid closed")
        problems = status.skip_streak_problems(
            self.base, history,
            status._iso_epoch("2026-01-10T00:00:00+00:00") + 20 * 3600)
        self.assertTrue(problems)
        self.assertIn("skip_streak_problems", open(tui.__file__).read())


class ProxyAndDnsTest(unittest.TestCase):
    def test_proxy_target_is_probed_instead_of_default(self):
        cfg = preflight.defaults()
        env = {"https_proxy": "http://proxy.example:3128"}
        self.assertEqual(
            preflight.network_targets(cfg, env), ["proxy.example:3128"])
        self.assertEqual(
            preflight.network_targets(cfg, {"ALL_PROXY": "socks5://p.example"}),
            ["p.example:1080"])
        self.assertEqual(
            preflight.network_targets(cfg, {}), ["api.anthropic.com:443"])

    def test_explicit_hosts_win_over_proxy(self):
        cfg = preflight.defaults()
        cfg["network_check"].update(hosts=["a.example:443"],
                                    hosts_explicit=True)
        self.assertEqual(
            preflight.network_targets(cfg, {"HTTPS_PROXY": "http://p:1"}),
            ["a.example:443"])

    def test_check_probes_the_proxy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        seen = []

        def connect(target, timeout):
            seen.append(target)
            return mock.Mock()

        self.assertIsNone(preflight.check(
            tmp.name, "linux", fake_ioreg(), connect,
            {"HTTPS_PROXY": "proxy.example:8080"}))
        self.assertEqual(seen, [("proxy.example", 8080)])

    def test_hung_connect_is_bounded(self):
        import threading
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(target, timeout):
            release.wait(10)

        started = time.monotonic()
        self.assertTrue(preflight.offline(["h.example:443"], 0.1, hang))
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
