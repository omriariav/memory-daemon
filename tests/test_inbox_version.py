"""The inbox version is one SemVer string that the changelog and CLI follow.

Nothing here needs Textual."""
import importlib.util
import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import daemon
from workspace_daemon.inbox import __version__

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "check_inbox_version", ROOT / "tools" / "check_inbox_version.py")
check_inbox_version = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_inbox_version)
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$")


class InboxVersionTest(unittest.TestCase):
    def test_version_is_semver(self):
        self.assertRegex(__version__, SEMVER)

    def test_top_released_changelog_heading_matches_version(self):
        text = (ROOT / "docs" / "inbox-changelog.md").read_text(encoding="utf-8")
        released = re.findall(r"^## \[(\d[^\]]*)\] - \d{4}-\d{2}-\d{2}$", text, re.M)
        self.assertTrue(released, "no released version heading in the changelog")
        self.assertEqual(
            released[0], __version__,
            "bump __version__ in workspace_daemon/inbox/__init__.py and add a "
            "matching changelog heading")
        self.assertIn("## [Unreleased]", text)

    def test_version_flag_prints_and_exits_zero(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = daemon.main(["inbox", "--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), f"memory-daemon inbox {__version__}")

    def test_version_flag_works_without_textual(self):
        code = ("import sys\nsys.modules['textual'] = None\n"
                "import daemon\nraise SystemExit(daemon.main(['inbox', '--version']))\n")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), f"memory-daemon inbox {__version__}")

    def test_importing_version_does_not_import_textual(self):
        code = ("import sys\nfrom workspace_daemon.inbox import __version__\n"
                "assert 'textual' not in sys.modules\n")
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


class CheckInboxVersionTest(unittest.TestCase):
    """tools/check_inbox_version.py against a throwaway git repo."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        self.git("init", "-q", "-b", "main")
        self.write("workspace_daemon/inbox/__init__.py", '__version__ = "0.1.0"\n')
        self.write("workspace_daemon/inbox/app.py", "x = 1\n")
        self.write("docs/inbox-changelog.md", "## [Unreleased]\n")
        self.write("other.py", "y = 1\n")
        self.commit("base")
        self.git("checkout", "-q", "-b", "pr")

    def git(self, *args):
        subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                        *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def check(self):
        return check_inbox_version.check("main", cwd=self.repo)[0]

    def test_no_inbox_change_passes(self):
        self.write("other.py", "y = 2\n")
        self.commit("c")
        self.assertTrue(self.check())

    def test_inbox_change_alone_fails(self):
        self.write("workspace_daemon/inbox/app.py", "x = 2\n")
        self.commit("c")
        self.assertFalse(self.check())

    def test_inbox_change_with_changelog_passes(self):
        self.write("workspace_daemon/inbox/app.py", "x = 2\n")
        self.write("docs/inbox-changelog.md", "## [Unreleased]\n- fix\n")
        self.commit("c")
        self.assertTrue(self.check())

    def test_inbox_change_with_version_bump_passes(self):
        self.write("workspace_daemon/inbox/__init__.py", '__version__ = "0.1.1"\n')
        self.commit("c")
        self.assertTrue(self.check())

    def test_file_moved_out_of_inbox_fails(self):
        self.git("mv", "workspace_daemon/inbox/app.py", "other_app.py")
        self.commit("c")
        self.assertFalse(self.check())

    def test_init_change_without_version_change_fails(self):
        self.write("workspace_daemon/inbox/__init__.py",
                   '__version__ = "0.1.0"\n# note\n')
        self.commit("c")
        self.assertFalse(self.check())


if __name__ == "__main__":
    unittest.main()
