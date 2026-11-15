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
    from .demo import DemoSource, demo_saved
    from .store import InboxStore
    from . import sources

    if demo:
        # The demo's fake poll brings one arrival per refresh: keep it lively.
        settings = dict(settings, poll_interval_s=60.0)
        store = InboxStore(None)
        app = InboxApp(DemoSource(empty=empty), store,
                       FakeActions(store=store), settings, demo=True,
                       saved=demo_saved, saved_days=2)
    else:
        base = Path(base_dir)
        store = InboxStore(base)
        events = next((s for s in settings["sources"]
                       if s["type"] == "events"), None)
        days = events["saved_days"] if events is not None else None

        def saved():
            return sources.load_saved(
                base, days=days, gmail_account=settings["gmail_account"])

        def fetch(show_all=False):
            return sources.collect(settings, base, show_all=show_all)

        app = InboxApp(fetch, store, LocalActions(store), settings,
                       stream_delay=0, saved=saved if events else None,
                       saved_days=days, units=sources.make_units(settings, base),
                       saved_watch=sources.saved_watch(base) if events else None)
    app.run()
    return 0
