"""Entry point for `daemon.py inbox`. Imports Textual lazily."""
import importlib
import sys
from pathlib import Path

INSTALL_HINT = ("the inbox needs Textual: pip install 'textual>=8.2,<9' "
                "(and, for Hebrew display, python-bidi)")


def textual_available():
    try:
        importlib.import_module("textual")
    except ImportError:
        return False
    return True


def load_settings(base_dir, demo=False, settings_path=None):
    """The settings to run with. `--demo` ignores inbox.yaml entirely (it
    must not read your files), so it always gets the defaults. Raises
    SettingsError for a broken inbox.yaml."""
    from . import settings as cfg
    if demo:
        return cfg.default_settings()
    return cfg.load(base_dir, settings_path)


def run(base_dir, demo=False, empty=False, settings_path=None):
    """Start the inbox. Returns a process exit code."""
    from . import settings as cfg
    try:
        settings = load_settings(base_dir, demo, settings_path)
    except cfg.SettingsError as exc:
        print("inbox.yaml problems:", file=sys.stderr)
        for problem in exc.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    from .actions import FakeActions, LocalActions
    from .app import InboxApp
    from .demo import DemoSource
    from .store import InboxStore
    from . import sources

    if demo:
        store = InboxStore(None)
        app = InboxApp(DemoSource(empty=empty), store,
                       FakeActions(store=store), settings, demo=True)
    else:
        base = Path(base_dir)
        store = InboxStore(base)
        app = InboxApp(lambda: sources.collect(settings, base), store,
                       LocalActions(store), settings, stream_delay=0)
    app.run()
    return 0
