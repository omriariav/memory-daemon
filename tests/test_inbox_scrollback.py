"""Inbox 0.12.3: the app never scrolls the terminal.

A frame Textual built for the old height lands on a shrunk terminal and its
row separators (`\\n`) scroll the alternate screen; iTerm2 keeps each scrolled
off header row in its scrollback. The driver is wrapped so no newline is ever
written. The plain classes need no Textual; the real-terminal class runs the
demo under a pty (POSIX only) and skips without Textual. Synthetic data."""
import fcntl
import importlib.util
import io
import os
import select
import signal
import struct
import sys
import termios
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from workspace_daemon.inbox import term

ROOT = Path(__file__).resolve().parent.parent
HAVE_TEXTUAL = importlib.util.find_spec("textual") is not None
try:
    import pty
except ImportError:  # pragma: no cover - Windows
    pty = None


class NoScrollTest(unittest.TestCase):
    def test_a_newline_becomes_a_move_that_stops_at_the_bottom(self):
        self.assertNotIn("\n", term.no_scroll("a\nb\n\nc"))
        self.assertEqual(term.no_scroll("a\nb"), "a" + term.NEWLINE + "b")
        self.assertEqual(term.NEWLINE, "\x1b[B\x1b[G")

    def test_text_without_a_newline_is_untouched(self):
        s = "\x1b[3;1Hhello\x1b[0m"
        self.assertEqual(term.no_scroll(s), s)

    def test_the_wrapped_driver_writes_no_newline_and_is_cached(self):
        class Base:
            def __init__(self):
                self.out = []

            def write(self, data):
                self.out.append(data)

        wrapped = term.no_scroll_driver(Base)
        self.assertIs(term.no_scroll_driver(Base), wrapped)
        self.assertTrue(issubclass(wrapped, Base))
        d = wrapped()
        d.write("\x1b[1;1Hrow one\n\x1b[2;1Hrow two")
        d.write("plain")
        self.assertEqual(d.out, ["\x1b[1;1Hrow one" + term.NEWLINE
                                 + "\x1b[2;1Hrow two", "plain"])


@unittest.skipUnless(HAVE_TEXTUAL, "needs Textual")
class AppDriverTest(unittest.TestCase):
    def test_the_app_uses_the_wrapped_driver(self):
        from workspace_daemon.inbox.app import InboxApp
        app = InboxApp(lambda show_all=False: None, None, None,
                       {"poll_interval_s": 300.0, "theme": "dark",
                        "bidi_mode": "logical"})
        self.assertTrue(app.driver_class.__name__.startswith("NoScroll"))
        self.assertIn(app.driver_class, term._cache.values())

    def test_cli_runs_full_screen_not_inline(self):
        src = (ROOT / "workspace_daemon" / "inbox" / "cli.py").read_text(
            encoding="utf-8")
        self.assertIn("app.run()", src)
        self.assertNotIn("inline", src)


def setsize(fd, rows, cols):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@unittest.skipUnless(HAVE_TEXTUAL and pty is not None, "needs Textual and a pty")
class RealTerminalTest(unittest.TestCase):
    """`daemon.py inbox --demo` on a pty that shrinks and grows."""

    def run_demo(self):
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - the child
            os.environ["TERM"] = "xterm-256color"
            os.chdir(ROOT)
            os.execv(sys.executable, [sys.executable, "daemon.py", "inbox",
                                      "--demo"])
        buf = bytearray()
        alive = [True]

        def pump(seconds=0.0, until=None, timeout=30.0):
            """Read for `seconds`, or (with `until`) until `until(buf)` is
            true, at most `timeout` seconds; returns whether it was met."""
            end = time.time() + (timeout if until else seconds)
            while time.time() < end and alive[0]:
                if until and until(buf):
                    return True
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        alive[0] = False
                        break
                    if not chunk:
                        alive[0] = False
                        break
                    buf.extend(chunk)
            return bool(until and until(buf))

        def drawn(b):
            return b"\x1b[?1049h" in b and b"\x1b[1;1H" in b

        def repainted(start, rows):
            """A frame that runs from the top row to the new bottom row has
            arrived after byte `start`."""
            def check(b):
                top = b.find(b"\x1b[1;1H", start)
                return top >= 0 and b.find(
                    b"\x1b[%d;1H" % rows, top) >= 0
            return check

        def resize(rows):
            """Resize, then wait for a frame to come after it."""
            before = len(buf)
            setsize(fd, rows, 100)
            os.kill(pid, signal.SIGWINCH)
            self.assertTrue(pump(until=repainted(before, rows)),
                            f"no repaint after resizing to {rows} rows")

        try:
            setsize(fd, 30, 100)
            self.assertTrue(pump(until=drawn), "the app never drew a frame")
            for rows in (20, 40, 25, 18):
                resize(rows)
            for key in (b"j", b"2", b"1"):
                before = len(buf)
                os.write(fd, key)
                pump(until=lambda b: len(b) > before)
            os.write(fd, b"q")
            pump(until=lambda b: b"\x1b[?1049l" in b)
        finally:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)
        return bytes(buf)

    def test_no_newline_is_written_and_everything_stays_on_the_alt_screen(self):
        data = self.run_demo()
        self.assertGreater(len(data), 1000, "the app drew nothing")
        # Nothing reaches the normal screen before the alternate one opens.
        self.assertEqual(data.find(b"\x1b[?1049h"), 0)
        # Not one linefeed: a `\n` on the bottom row scrolls the screen (the
        # tty turns `\n` into `\r\n`, so look for the LF).
        self.assertEqual(data.count(b"\n"), 0)
        # Nothing clears the scrollback or resets the terminal.
        self.assertNotIn(b"\x1b[3J", data)
        self.assertNotIn(b"\x1bc", data)
        # Everything after the alternate screen closes is mode resets only.
        tail = data[data.rfind(b"\x1b[?1049l"):]
        self.assertTrue(tail.startswith(b"\x1b[?1049l"))
        self.assertNotIn(b"inbox", tail)


@unittest.skipUnless(HAVE_TEXTUAL, "needs Textual")
class SilentRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_pilot_run_writes_nothing_to_stdout_or_stderr(self):
        from workspace_daemon.inbox.actions import FakeActions
        from workspace_daemon.inbox.app import InboxApp
        from workspace_daemon.inbox.demo import DemoSource
        from workspace_daemon.inbox.store import InboxStore
        store = InboxStore(None)
        app = InboxApp(DemoSource(latency=0), store, FakeActions(store=store),
                       {"poll_interval_s": 300.0, "theme": "dark",
                        "bidi_mode": "logical"}, demo=True, stream_delay=0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.3)
                for size in ((100, 18), (100, 40), (90, 24)):
                    await pilot.resize_terminal(*size)
                    await pilot.pause(0.2)
                for key in ("j", "k", "2", "1", "r"):
                    await pilot.press(key)
                    await pilot.pause(0.1)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
