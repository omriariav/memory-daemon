import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
RUN_SCRIPT = ROOT / "run.sh"


class RunScriptTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "daemon.py").write_text(
            "import os, sys\n"
            "print('daemon ' + ' '.join(sys.argv[1:]))\n"
            "if os.environ.get('DAEMON_TEST_FAIL'):\n"
            "    raise SystemExit(2)\n",
            encoding="utf-8",
        )
        status = self.repo / "memory-daemon-status.sh"
        status.write_text("#!/bin/sh\necho status-ok\n", encoding="utf-8")
        status.chmod(0o755)
        self.plist = self.root / "com.memory-daemon.plist"
        self.plist.write_text("plist", encoding="utf-8")
        self.maintenance_plist = self.root / "com.memory-daemon-maintenance.plist"
        self.maintenance_plist.write_text("plist", encoding="utf-8")
        self.log = self.root / "launchctl.log"
        self.launchctl = self.root / "launchctl"

    def write_launchctl(self, loaded):
        print_exit = 0 if loaded else 1
        self.launchctl.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/sh
                echo "$*" >> "$LAUNCHCTL_TEST_LOG"
                if [ "$1" = print ]; then
                    exit {print_exit}
                fi
                if [ "${{LAUNCHCTL_FAIL_ON:-}}" = "$1" ]; then
                    exit 7
                fi
                exit 0
                """
            ),
            encoding="utf-8",
        )
        self.launchctl.chmod(0o755)

    def run_helper(self, **extra_env):
        env = {
            **os.environ,
            "MEMORY_DAEMON_DIR": str(self.repo),
            "MEMORY_DAEMON_LAUNCHD_DOMAIN": "gui/123",
            "MEMORY_DAEMON_PLIST": str(self.plist),
            "MEMORY_DAEMON_MAINTENANCE_PLIST": str(self.maintenance_plist),
            "MEMORY_DAEMON_LAUNCHCTL": str(self.launchctl),
            "LAUNCHCTL_TEST_LOG": str(self.log),
            **extra_env,
        }
        return subprocess.run(
            ["bash", str(RUN_SCRIPT)],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )

    def launchctl_calls(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def test_loads_unloaded_scheduler_then_starts_tick(self):
        self.write_launchctl(loaded=False)

        result = self.run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("daemon validate", result.stdout)
        self.assertIn("status-ok", result.stdout)
        self.assertEqual(
            self.launchctl_calls(),
            [
                "print gui/123/com.memory-daemon",
                "enable gui/123/com.memory-daemon",
                f"bootstrap gui/123 {self.plist}",
                "print gui/123/com.memory-daemon-maintenance",
                "enable gui/123/com.memory-daemon-maintenance",
                f"bootstrap gui/123 {self.maintenance_plist}",
                "kickstart gui/123/com.memory-daemon",
                "kickstart gui/123/com.memory-daemon-maintenance",
            ],
        )

    def test_loaded_scheduler_is_reloaded_from_current_plist(self):
        self.write_launchctl(loaded=True)

        result = self.run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.launchctl_calls(),
            [
                "print gui/123/com.memory-daemon",
                "bootout gui/123/com.memory-daemon",
                "enable gui/123/com.memory-daemon",
                f"bootstrap gui/123 {self.plist}",
                "print gui/123/com.memory-daemon-maintenance",
                "bootout gui/123/com.memory-daemon-maintenance",
                "enable gui/123/com.memory-daemon-maintenance",
                f"bootstrap gui/123 {self.maintenance_plist}",
                "kickstart gui/123/com.memory-daemon",
                "kickstart gui/123/com.memory-daemon-maintenance",
            ],
        )

    def test_missing_plist_blocks_bootstrap(self):
        self.write_launchctl(loaded=False)
        self.plist.unlink()

        result = self.run_helper()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LaunchAgent plist not found", result.stderr)
        self.assertFalse(self.log.exists())

    def test_validation_failure_does_not_call_launchctl(self):
        self.write_launchctl(loaded=False)

        result = self.run_helper(DAEMON_TEST_FAIL="1")

        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_enable_failure_stops_before_kickstart_and_status(self):
        self.write_launchctl(loaded=True)

        result = self.run_helper(LAUNCHCTL_FAIL_ON="enable")

        self.assertEqual(result.returncode, 7)
        self.assertNotIn("status-ok", result.stdout)
        self.assertEqual(
            self.launchctl_calls(),
            [
                "print gui/123/com.memory-daemon",
                "bootout gui/123/com.memory-daemon",
                "enable gui/123/com.memory-daemon",
            ],
        )

    def test_bootstrap_failure_stops_before_kickstart_and_status(self):
        self.write_launchctl(loaded=False)

        result = self.run_helper(LAUNCHCTL_FAIL_ON="bootstrap")

        self.assertEqual(result.returncode, 7)
        self.assertNotIn("status-ok", result.stdout)
        self.assertEqual(
            self.launchctl_calls(),
            [
                "print gui/123/com.memory-daemon",
                "enable gui/123/com.memory-daemon",
                f"bootstrap gui/123 {self.plist}",
            ],
        )

    def test_kickstart_failure_stops_before_status(self):
        self.write_launchctl(loaded=True)

        result = self.run_helper(LAUNCHCTL_FAIL_ON="kickstart")

        self.assertEqual(result.returncode, 7)
        self.assertNotIn("status-ok", result.stdout)
        self.assertEqual(
            self.launchctl_calls(),
            [
                "print gui/123/com.memory-daemon",
                "bootout gui/123/com.memory-daemon",
                "enable gui/123/com.memory-daemon",
                f"bootstrap gui/123 {self.plist}",
                "print gui/123/com.memory-daemon-maintenance",
                "bootout gui/123/com.memory-daemon-maintenance",
                "enable gui/123/com.memory-daemon-maintenance",
                f"bootstrap gui/123 {self.maintenance_plist}",
                "kickstart gui/123/com.memory-daemon",
            ],
        )

    def test_attention_status_does_not_misreport_start_as_failed(self):
        self.write_launchctl(loaded=True)
        status = self.repo / "memory-daemon-status.sh"
        status.write_text("#!/bin/sh\necho attention\nexit 1\n", encoding="utf-8")

        result = self.run_helper()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("may still be running", result.stderr)


if __name__ == "__main__":
    unittest.main()
