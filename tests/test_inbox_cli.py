"""`daemon.py inbox`: argument handling and the optional Textual extra.

Nothing here needs Textual; the missing-extra path is simulated."""
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import daemon
from workspace_daemon.inbox import cli


ROOT = Path(__file__).resolve().parent.parent


def run(*argv):
    err = io.StringIO()
    with redirect_stderr(err):
        code = daemon.main(["inbox", *argv])
    return code, err.getvalue()


class InboxCliTest(unittest.TestCase):
    def test_missing_textual_prints_install_hint_and_exits_two(self):
        with mock.patch.dict(sys.modules, {"textual": None}):
            code, err = run("--demo")
        self.assertEqual(code, 2)
        self.assertIn("pip install 'textual>=", err)

    def test_inbox_core_imports_without_textual_rich_or_bidi(self):
        code = (
            "import sys\n"
            "for name in ('textual', 'rich', 'bidi'):\n"
            "    sys.modules[name] = None\n"
            "from workspace_daemon.inbox import (actions, bidi, cli, demo, items,\n"
            "    models, settings, sources, store, themes)\n"
            "assert not bidi.available()\n"
            "assert bidi.display('abc') == 'abc'\n"
            "assert bidi.visual_lines('x' * 100, 40, True)[0] == 'x' * 40\n"
        )
        done = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_empty_needs_demo(self):
        code, err = run("--empty")
        self.assertEqual(code, 2)
        self.assertIn("--demo", err)

    def test_bad_base_dir(self):
        with mock.patch.object(cli, "textual_available", return_value=True), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True):
            code, err = run("--base-dir", "/no/such/dir-xyz")
        self.assertEqual(code, 2)
        self.assertIn("not a directory", err)

    def test_needs_a_terminal(self):
        with mock.patch.object(cli, "textual_available", return_value=True), \
                mock.patch("sys.stdin.isatty", return_value=False):
            code, err = run("--demo")
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)

    def test_broken_inbox_yaml_lists_problems_and_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "inbox.yaml").write_text("theme: neon\nbogus: 1\n")
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.run(Path(tmp))
        self.assertEqual(code, 2)
        self.assertIn("theme", err.getvalue())
        self.assertIn("bogus", err.getvalue())

    def test_demo_ignores_inbox_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "inbox.yaml").write_text("theme: neon" + chr(10))
            with mock.patch("pathlib.Path.read_text",
                            side_effect=AssertionError("demo read a file")):
                got = cli.load_settings(Path(tmp), demo=True)
            self.assertEqual(got["theme"], "dark")
            from workspace_daemon.inbox import settings
            with self.assertRaises(settings.SettingsError):
                cli.load_settings(Path(tmp), demo=False)

    def test_inbox_is_in_help(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), self.assertRaises(SystemExit):
            daemon.main(["--help"])
        self.assertIn("inbox", out.getvalue())


if __name__ == "__main__":
    unittest.main()
