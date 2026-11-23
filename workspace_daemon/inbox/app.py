"""The inbox TUI app. Needs Textual; import it only through `cli.run`."""
import asyncio
import datetime
import inspect
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field, replace

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, ScreenStackError, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.command import DiscoveryHit, Hit
from textual.content import Content
from textual.css.query import NoMatches
from textual.system_commands import SystemCommandsProvider
from textual.widgets import Input, Static, TextArea

from . import acts as actsmod
from . import bidi, customactions, feedrules, invites, rowmenu, themes, views
from . import context as ctxmod
from . import health as healthmod
from . import items as itm_items
from . import draftme as draftme_mod
from . import outcome as outcome_mod
from . import polish as polishmod
from . import tasks as tasksmod
from . import writes as writes_mod
from . import summary as summod
from .actions import Result, act_meta
from .models import (SOURCES, Item, copy_item_text, copy_section_markdown,
                     copy_section_text, from_v1)
from .screens import (ActionBusy, AuditScreen, ChoicePicker, ConfirmSend,
                      ConfirmWrite, DateScreen, HelpScreen, NoteScreen, OutcomeScreen,
                      OpsScreen,
                      PolishScreen, RowMenu, RulesScreen, SnoozePicker)
from .sources import (SourceResult, annotate_memory, merge, units_answers,
                      units_memory_index)
from .mrkdwn import to_slack_mrkdwn
from .term import no_scroll_driver
from .watch import Watcher
from .widgets import (ITEM_PREFIX, ContextPane, DraftPane, HealthStrip,
                      HealthView, HintBar, Queue, RoutinePane, StatusHeader,
                      ThreadView, fmt_age, fuzzy, palette, render_health,
                      render_routines, scheduler_title, vis)

NOT_YET = "Replying is not available yet (phase 2). Nothing is sent."
RSVP_OFF = "RSVP not configured (set rsvp: in inbox.yaml)"
SAVE_OFF = "Save not configured (set save: in inbox.yaml)"
CONTEXT_OFF = "Context not configured (set context: in inbox.yaml)"
SUMMARY_OFF = "Summary not configured (set summary: in inbox.yaml)"
REACT_OFF = "React not configured (set react: in inbox.yaml)"
POLISH_OFF = "Polish not configured (set polish: in inbox.yaml)"
SEND_KINDS = ("send", "nudge")
# Every write that goes to Slack through the bridge's one-call path: only one
# runs at a time, and quitting reports a running one.
SLACK_WRITE_KINDS = SEND_KINDS + ("draft",)
# 0.7.0 toasts (the second form is for an item `o` has nothing to open for).
DRAFT_TOAST = "Draft placed in Slack: open it with o and press Enter."
DRAFT_TOAST_NO_OPEN = ("Draft placed in Slack: open the conversation and "
                       "press Enter.")
COPY_TOAST = "Copied for Slack: open it with o and paste"
COPY_TOAST_NO_OPEN = "Copied for Slack: open the conversation and paste"
CONTEXT_DEBOUNCE_S = 0.4      # a new selection waits this long before fetching
CONTEXT_MAX_FETCHES = 2       # context commands running at once
NOT_CONFIRMED = "RSVP not confirmed by calendar"
# What each RSVP key answers: key -> (menu word, answer sent, modal title).
RSVP_KEYS = {
    "accept": ("accept", "accepted", "Accept invite"),
    "decline": ("decline", "declined", "Decline invite"),
    "tentative": ("tentative", "tentative", "Answer tentative"),
}
# Kinds that act outside the inbox: quitting inside the undo window cancels
# them (nothing is sent), unlike the local marks, which quitting commits.
EXTERNAL_KINDS = ("send", "nudge", "draft", "rsvp", "save", "action", "react",
                  "waits_on", "relay", "due")
# 0.10.0 acts. `done`, `snooze` and `mute` write a local mark and then tell the
# source (quitting commits the mark and keeps the op for a retry: it never
# calls the source); `waits_on` and `relay` have no local mark (they are in
# EXTERNAL_KINDS: quitting cancels them). 0.12.0 (tasks): `drop` is a mark kind
# like `done`; `due` has no store mark (the new date shows at once, kept in
# memory until the next list refresh) and, like `waits_on`, is cancelled by
# quitting inside the undo window.
ACT_MARK_KINDS = ("done", "snooze", "mute", "drop")
NO_MARK_ACT_KINDS = ("waits_on", "relay", "due")
ACT_KINDS = ACT_MARK_KINDS + NO_MARK_ACT_KINDS
ACTS_OFF = ("Acts are not enabled for this source (set acts: on its command "
            "source in inbox.yaml)")
CONFLICT_TOAST = "New message since you looked; refresh"
TASK_CONFLICT_TOAST = "The task changed since you looked; refresh"
TASK_KEYS_TOAST = ("A task: d done, D outcome, s due date, x drop, o open, "
                   "m remind me")
REMIND_TASK_ONLY = "Remind me works on a task (the 3 Tasks view)"
DECISION_REQUIRED = "A decision needs its text. Nothing sent."
SHADOW_WORD = "shadow"


def _stub(entry):
    """A row-shaped stand-in for an op's item, for the audit log."""
    return Item(key=str(entry.get("id", "")), section="fyi",
                who=str(entry.get("who", "")), where=str(entry.get("where", "")),
                ts=0.0, gist="")


def _takes_show_all(fetch):
    """True when `fetch` accepts a `show_all` keyword argument."""
    try:
        params = inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False
    return "show_all" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def review_cli_hint(source_id, title):
    """The command line `D` copies on a REVIEW row. A template: run it in the
    memory store directory and add --type, --date and the entry text (--body).
    The event carries no body, so the inbox cannot write the entry itself."""
    return ("# run in your memory store directory; add --type, --date and "
            "--body (the entry text).\n"
            "# npx may reach the network to fetch tsx.\n"
            "npx tsx src/cli.ts add --force-new "
            f"--source-ids={shlex.quote(source_id)} "
            f"--title={shlex.quote(title)}")


@dataclass
class Pending:
    kind: str                 # send | nudge | draft | done | ack | snooze | mute | drop | due | waits_on | relay | rsvp | save | action | react
    item: Item
    text: str = ""
    until: float = 0.0
    label: str = ""
    audit: dict = field(default_factory=dict)
    timer: object = None
    done: bool = False
    answer: str = ""          # rsvp: the word sent (accepted, declined, tentative)
    force: bool = False       # save: "save anyway" after a duplicate answer
    hide: bool = True         # False: the item stays listed while it waits (save)
    name: str = ""            # action: the label of the actions: entry
    choice: str = ""          # action: the id of the chosen choice; react: the emoji name
    state: str = ""           # action: the opaque state prepare returned
    mark_done: bool = False   # action: a successful run marks the item done
    act: str = ""             # 0.10.0: the act to send after the mark (close, snooze, ...)
    act_args: dict = field(default_factory=dict)   # its args
    act_meta: object = None   # 0.12.0: the request meta fixed at enqueue (a due move: the date shown before it)
    react: str = ""           # 0.10.0: done reaction emoji sent after the act


THIS_ITEM = "This item"


class InboxCommands(SystemCommandsProvider):
    """The command palette's source (it replaces Textual's, and lists the same
    commands): with nothing typed, a "This item" group comes first (what the
    selected row can do, the same entries as its enter menu), then the rest
    of the commands. A typed query matches both."""

    def _groups(self):
        app = self.app
        item = app.item_commands(self.screen)
        # Each entry names the palette titles it stands for: those commands
        # run the same method, so only the "This item" one is listed.
        skip = app.item_covered_titles(self.screen) | {c.title for c in item}
        rest = [c for c in app.get_system_commands(self.screen)
                if c.title not in skip]
        return item, rest

    async def discover(self):
        item, rest = self._groups()
        for c in item:
            yield DiscoveryHit(
                Text.assemble((f"{THIS_ITEM}  ", Style(dim=True)), c.title),
                c.callback, text=f"{THIS_ITEM} {c.title}", help=c.help)
        for c in sorted(rest, key=lambda c: c.title):
            if c.discover:
                yield DiscoveryHit(c.title, c.callback, text=c.title,
                                   help=c.help)

    async def search(self, query):
        matcher = self.matcher(query)
        item, rest = self._groups()
        for group, cmds in ((THIS_ITEM, item), ("", rest)):
            for c in cmds:
                score = matcher.match(c.title)
                if score > 0:
                    shown = matcher.highlight(c.title)
                    if group:
                        shown = Content.assemble(
                            (f"{group}  ", "dim"), shown)
                    yield Hit(score, shown, c.callback, text=c.title,
                              help=c.help)


class InboxApp(App):
    TITLE = "inbox"
    ENABLE_COMMAND_PALETTE = True
    COMMANDS = {InboxCommands}

    def get_driver_class(self):
        """The platform driver, wrapped so no write ever scrolls the screen
        (a frame that outlives a shrinking resize would push the header rows
        into the terminal scrollback; see term.py)."""
        return no_scroll_driver(super().get_driver_class())

    BINDINGS = [
        Binding("j,down", "move(1)", "Down", show=False),
        Binding("k,up", "move(-1)", "Up", show=False),
        Binding("g", "edge(0)", "Top", show=False),
        Binding("G", "edge(1)", "Bottom", show=False),
        Binding("1", "view(1)", show=False), Binding("2", "view(2)", show=False),
        Binding("3", "view(3)", show=False), Binding("4", "view(4)", show=False),
        Binding("5", "view(5)", show=False),
        Binding("left_square_bracket", "group(-1)", show=False),
        Binding("right_square_bracket", "group(1)", show=False),
        Binding("x", "mute", show=False), Binding("minus", "less", show=False),
        Binding("M", "rules", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("f", "source_filter", show=False),
        Binding("a", "approve", show=False), Binding("e", "edit", show=False),
        Binding("d", "done", show=False), Binding("s", "snooze", show=False),
        Binding("n", "nudge", show=False), Binding("o", "open", show=False),
        Binding("r", "refresh", show=False), Binding("u", "undo", show=False),
        Binding("L", "audit", show=False), Binding("b", "toggle_bidi", show=False),
        Binding("v", "toggle_thread_order", show=False),
        Binding("y", "copy_item", show=False), Binding("Y", "copy_section", show=False),
        Binding("R", "toggle_resolved", show=False),
        Binding("X", "rsvp_decline", show=False), Binding("T", "rsvp_tentative", show=False),
        Binding("m", "save_memory", show=False),
        Binding("w", "waits_on", show=False), Binding("D", "draft_me", show=False),
        Binding("exclamation_mark", "relay", show=False),
        Binding("E", "react", show=False), Binding("P", "polish", show=False),
        Binding("c", "context", show=False), Binding("C", "context_refresh", show=False),
        Binding("i", "summary(False)", show=False), Binding("I", "summary(True)", show=False),
        Binding("A", "toggle_all", show=False), Binding("S", "saved", show=False),
        Binding("question_mark", "help", show=False),
        Binding("ctrl+t", "toggle_theme", show=False),
        Binding("ctrl+s", "save_edit", show=False),
        Binding("escape", "escape", show=False),
        Binding("q", "quit", show=False),
    ]
    CSS = """
    Screen { background: $background; color: $foreground; }
    #hdr { height: 1; background: $panel; }
    #strip { height: 1; }
    #main { height: 1fr; }
    #routines { width: 26; border: round $panel; border-title-color: $text-muted; padding: 0 1; display: none; }
    #health { height: 1fr; border: round $panel; border-title-color: $text-muted; padding: 0 1; display: none; scrollbar-size-vertical: 1; }
    #health:focus { border: round $primary; border-title-color: $primary; }
    #left { width: 42%; min-width: 38; max-width: 76; border: round $panel; border-title-color: $text-muted; }
    #left:focus-within { border: round $primary; border-title-color: $primary; }
    #right { width: 1fr; }
    #thread, #draft-pane { border: round $panel; border-title-color: $text-muted; padding: 0 1; }
    #thread:focus, #draft-pane:focus-within { border: round $primary; border-title-color: $primary; }
    #thread { height: 1fr; scrollbar-size-vertical: 1; }
    #context { height: 1fr; min-height: 6; display: none; border: round $panel; border-title-color: $text-muted; padding: 0 1; scrollbar-size-vertical: 1; }
    #context:focus { border: round $primary; border-title-color: $primary; }
    #draft-pane { height: auto; min-height: 5; max-height: 40%; overflow-y: auto; scrollbar-size-vertical: 1; }
    #queue { background: transparent; border: none; padding: 0; height: 1fr; scrollbar-size-vertical: 1; }
    #queue > .option-list--option-disabled { color: $foreground; text-opacity: 100%; }
    #queue > .option-list--option-highlighted { background: $primary 22%; text-style: none; }
    #queue:focus > .option-list--option-highlighted { background: $primary 38%; text-style: none; }
    #queue > .option-list--option-hover { background: $primary 12%; }
    #filter { margin: 0 0 0 0; border: none; height: 1; padding: 0 1; background: $panel; display: none; }
    #filter.on { display: block; }
    #zero { height: 1fr; content-align: center middle; text-align: center; display: none; }
    #draft-bar { height: 1; }
    #badge { width: auto; }
    #draft-status { width: 1fr; text-align: right; color: $text-muted; }
    #draft-scroll { height: auto; overflow-y: hidden; }
    #draft-view { height: auto; }
    #draft-edit { height: auto; min-height: 3; max-height: 14; display: none; border: none; background: $surface; }
    #draft-edit.on { display: block; }
    #hints { height: 1; background: $panel; }
    ConfirmSend, ConfirmWrite, SnoozePicker, HelpScreen, AuditScreen, ActionBusy, ChoicePicker, PolishScreen, RulesScreen, NoteScreen, DateScreen, OpsScreen, RowMenu { align: center middle; background: $background 55%; }
    #confirm { width: 74; max-width: 94%; height: auto; border: heavy $error; background: $surface; padding: 1 2; }
    #confirm-title { text-style: bold; color: $error; margin-bottom: 1; }
    #confirm-warn { margin: 1 0 0 0; }
    #confirm-scroll { height: auto; max-height: 16; margin: 1 0; border: round $panel; background: $background; scrollbar-size-vertical: 1; }
    #confirm-text { border: none; padding: 0 1; margin: 0; background: $background; height: auto; }
    #polish { width: 80; max-width: 96%; height: auto; max-height: 96%; border: heavy $primary; background: $surface; padding: 1 2; }
    #polish-title { text-style: bold; margin-bottom: 1; }
    #polish-old-scroll, #polish-new-scroll { height: auto; max-height: 8; border: round $panel; background: $background; scrollbar-size-vertical: 1; }
    #polish-old, #polish-new { padding: 0 1; height: auto; background: $background; }
    #polish-note { margin: 1 0; }
    #polish-buttons { height: 3; align-horizontal: right; }
    #polish-buttons Button { margin-left: 2; min-width: 16; }
    #confirm-buttons { height: 3; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 2; min-width: 18; }
    #confirm.multi { width: 96; }
    #confirm.multi #confirm-buttons Button { margin-left: 1; min-width: 10; padding: 0 1; }
    #confirm.multi.narrow { width: 100%; padding: 0 1; }
    #confirm.multi.narrow #confirm-scroll { margin: 0; }
    #confirm.multi.narrow #confirm-title { margin-bottom: 0; }
    #confirm.multi.narrow #confirm-buttons { layout: grid; grid-size: 2; grid-gutter: 0 1; grid-rows: 3; height: auto; align-horizontal: left; }
    #confirm.multi.narrow #confirm-buttons Button { width: 1fr; min-width: 0; margin-left: 0; }
    #snooze { width: 52; height: auto; border: heavy $primary; background: $surface; padding: 1 2; }
    #snooze-list { height: 6; background: transparent; border: none; }
    #snooze-hint { margin-top: 1; color: $text-muted; }
    #picker { width: 70; max-width: 94%; height: auto; max-height: 80%; border: heavy $primary; background: $surface; padding: 1 2; }
    #picker-list { height: auto; max-height: 16; background: transparent; border: none; }
    #picker-hint { margin-top: 1; color: $text-muted; }
    #actbusy { width: 52; height: auto; border: heavy $primary; background: $surface; padding: 1 2; }
    #actbusy-hint { margin-top: 1; color: $text-muted; }
    #rowmenu { width: 66; max-width: 94%; height: auto; max-height: 94%; border: heavy $primary; background: $surface; padding: 1 2; }
    #rowmenu-title { margin-bottom: 1; }
    #rowmenu-filter { display: none; height: 1; border: none; padding: 0 1; background: $panel; margin-bottom: 1; }
    #rowmenu-filter.on { display: block; }
    #rowmenu-list { height: auto; max-height: 28; background: transparent; border: none; scrollbar-size-vertical: 1; }
    #rowmenu-hint { margin-top: 1; color: $text-muted; }
    #help { width: 66; max-width: 94%; height: auto; max-height: 94%; border: heavy $primary; background: $surface; padding: 1 2; }
    #help-title { dock: top; margin-bottom: 1; }
    #help-scroll { height: auto; max-height: 100%; scrollbar-size-vertical: 1; }
    #help-body { height: auto; }
    #audit { width: 96%; height: 80%; border: heavy $primary; background: $surface; padding: 1 2; }
    #audit-table { height: 1fr; }
    #ops { width: 96%; height: 80%; border: heavy $primary; background: $surface; padding: 1 2; }
    #ops-table { height: 1fr; }
    #note { width: 70; max-width: 94%; height: auto; border: heavy $primary; background: $surface; padding: 1 2; }
    #note-hint { margin-top: 1; color: $text-muted; }
    #rules { width: 96%; height: 70%; border: heavy $primary; background: $surface; padding: 1 2; }
    #rules-title { margin-bottom: 1; }
    #rules-table { height: 1fr; }
    """

    def __init__(self, fetch, store, actions, settings=None, demo=False,
                 undo_s=5.0, leave_s=0.18, stream_delay=0.012, poll_s=None,
                 saved=None, saved_days=None, units=None,
                 context_debounce_s=CONTEXT_DEBOUNCE_S, context_cache=None,
                 health=None, view="inbox", today=None):
        """`fetch()` blocks and returns a list of SourceResult (it runs in a
        worker thread). `store` holds done/ack/snooze marks. A `fetch` that
        takes a `show_all` argument gets the state of the A toggle.
        `saved()` returns `(rows, error)`: the rows of the Feed's "saved to
        memory" routine (S jumps to it); None leaves that routine out.
        `health()` blocks and returns a health snapshot (see health.py); it
        runs in a worker thread, every 30 s; None leaves the health strip and
        the Health view empty. `view` is the view the app starts in. `today()`
        returns the local date a task's due date is counted from (default
        `datetime.date.today`; tests inject a fixed one).

        `units` (a list of `sources.Unit`) switches on change-driven refresh:
        every `watch_interval_s` the files each unit depends on are
        stat-checked and only a unit with a changed file is re-run; the
        fallback timer (`poll_interval_s`) and `r` run them all, and `fetch`
        is then unused. The saved rows are read with every refresh.

        `context_debounce_s` is how long a new selection must stay before the
        open Context pane fetches its context; `context_cache` replaces the
        in-memory cache (tests)."""
        super().__init__()
        settings = settings or {"poll_interval_s": 60.0, "theme": themes.DEFAULT,
                                "bidi_mode": "auto"}
        self.fetch, self.store, self.actions = fetch, store, actions
        self.settings, self.demo = settings, demo
        self.all_rows = []                 # every fetched item, before marks
        self.items = []                    # what the queue shows
        self.results = []                  # latest SourceResult per source
        self.poll_s = float(poll_s or settings["poll_interval_s"])
        self.undo_s, self.leave_s, self.stream_delay = undo_s, leave_s, stream_delay
        self.filter_text = ""
        self.source_filter = None
        self.show_resolved = False         # R: list resolved alerts under fyi
        self.show_all = False              # A: ask `supports_all` sources for everything
        self.saved, self.saved_days = saved, saved_days
        self.saved_rows = []               # the last `saved()` rows (dicts)
        self.saved_error = None            # what the last `saved()` said went wrong
        self.view = view if view in views.VIEW_KEYS else "inbox"
        self.feed_routine = None           # f in the Feed: one routine
        self.feed_cfg = dict(settings.get("feed") or {})
        self.feed_cfg.setdefault("window_h", views.DEFAULT_WINDOW_H)
        self.feed_cfg.setdefault("rules", [])
        self.feed_data = views.Feed([], [], [], 0)
        # Only the rows of the events source age out of the Feed.
        self._event_origins = tuple(
            s["name"] for s in settings.get("sources") or []
            if s.get("type") == "events") or views.EVENT_ORIGINS
        self._views = {"inbox": [], "waiting": [], "tasks": [], "feed": []}
        self.today_fn = today or datetime.date.today
        self._due_marks = {}               # task key -> a due date set here, until the next refresh
        self._applying_since = None        # start of the fetch being applied
        self._list_started = {}            # units mode: source name -> when its list was last fetched (not re-evaluated)
        self._rows = {}                    # key -> row, digest and saved rows too
        self._groups_sig = None
        self._list_sig = None              # (view, filters) the queue was last built for
        self.health_fetch = health
        self.health_snap = None
        self._health_loading = False
        self.rules_screen = None
        self._saved_fresh = None
        self._queued = None                # a refresh asked for while one ran
        self._fetch_takes_all = _takes_show_all(fetch)
        self.units = list(units) if units is not None else None
        self._unit_results = {}            # unit name -> its latest raw results
        self.updated_at = {}               # source/unit name -> time of its last refresh
        self.watch_s = float(settings.get("watch_interval_s", 3.0))
        self.watcher = None
        if self.units is not None:
            self.watcher = Watcher(list(self.units))
        self.saved_screen = None
        self.editing = False
        self.busy = 0
        self.polling = False
        mode = bidi.resolve(bidi.configured_mode(settings["bidi_mode"]))
        saved_order = store.get_pref("thread_order") \
            if hasattr(store, "get_pref") else None
        self.thread_order = saved_order or settings.get("thread_order", "newest")
        self.bidi_fallback = mode == "visual" and not bidi.available()
        self.bidi_mode = "logical" if self.bidi_fallback else mode
        self.pending = []
        self.running = []                  # committed actions still executing
        self._quit_armed = False           # first q with writes running was pressed
        self._quitting = False             # the app is exiting: workers must not call back
        self.dups = {}                     # item key -> (item, result) awaiting "save anyway"
        self.audit = []
        self.stats = {"done": 0, "sent": 0, "snoozed": 0}
        self.known_ids = set()
        self.polled_once = False
        self.last_poll_at = None
        self.next_poll_at = time.time() + self.poll_s
        self.last_committed = None
        self.current_key = None
        self._last_problems = ()
        self.context_open = False          # c: the Context pane is showing
        self.context_debounce_s = float(context_debounce_s)
        self.ctx_cache = (ctxmod.ContextCache() if context_cache is None
                          else context_cache)
        self._ctx_shown = None             # (item id, ts) the pane follows
        self._ctx_timer = None             # the pending debounced fetch
        self._ctx_loading = set()          # (item id, ts) with a fetch running
        self._ctx_gen = 0                  # bumped on every selection change
        self.summaries = {}                # summary cache key -> view state
        self._sum_running = None           # cache key of the running summary
        self._ctx_pending = []             # items waiting for a fetch slot
        self._ctx_sig = None               # what the pane last drew
        self._cancel = threading.Event()   # set on quit: stops context commands
        self.remind_key = None             # m on a task: its "Remind me" card is showing
        self._custom = list(getattr(actions, "custom_actions", None) or [])
        self._act_op = None                # the running choices/prepare lookup
        self._act_gen = 0                  # bumped on every lookup
        self._act_screen = None            # the "looking up" modal
        self._polish_op = None             # the polish the user is waiting for
        self._polish_gen = 0               # bumped on every polish
        self._polish_screen = None         # its "polishing" modal
        self._polish_running = False       # a polish thread is still alive
        self._pre_op = None                # the send precheck being waited for
        self._pre_gen = 0                  # bumped on every precheck
        self._pre_screen = None            # its "checking" modal
        self.act_pending = 0               # ops waiting for a retry (health strip)
        self.act_shadow = False            # the last act reply said shadow
        self._retrying = False             # a pending-ops retry thread is alive
        self._draft_running = set()        # item keys with a `D` draft running
        self._inverse_running = 0          # ops-log inverses being sent
        for n, cfg in enumerate(self._custom):
            self.bind(cfg["key"], f"custom_action({n})", show=False)

    @property
    def bidi(self):
        """True when text is reordered to visual order (bidi_mode 'visual')."""
        return self.bidi_mode == "visual"

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield StatusHeader(id="hdr")
        yield HealthStrip(id="strip")
        with Horizontal(id="main"):
            yield RoutinePane("", id="routines")
            with Vertical(id="left"):
                yield Input(placeholder="/ filter…", id="filter")
                yield Queue(id="queue")
                yield Static("", id="zero")
            with Vertical(id="right"):
                yield ThreadView(id="thread")
                yield ContextPane(id="context")
                yield DraftPane(id="draft-pane")
        yield HealthView(id="health")
        yield HintBar(id="hints")

    def on_mount(self):
        for theme in themes.build_textual_themes():
            self.register_theme(theme)
        self.theme = themes.theme_id(self.settings["theme"])
        self.theme_changed_signal.subscribe(self, lambda _t: self.call_after_refresh(self.redraw))
        self.query_one("#left").border_title = "Inbox"
        self.query_one("#routines").border_title = "Routines"
        self.query_one("#health").border_title = "Health"
        self.query_one("#thread").border_title = "Thread"
        self.query_one("#context").border_title = "Context"
        self.query_one("#draft-pane").border_title = "Draft"
        self.set_interval(0.25, self.tick_header)
        self.set_interval(30, self.refilter)
        self.set_interval(30, self.reeval)
        self.set_interval(1.0, self.context_tick)   # expiry, spinner, ages
        self.set_interval(self.poll_s, self.poll)   # the fallback full refresh
        self.set_interval(healthmod.REFRESH_S, self.health_refresh)
        self.health_refresh()
        if self._acts_any():
            fn = getattr(self.actions, "last_act_shadow", None)
            try:
                self.act_shadow = bool(fn()) if fn else False
            except Exception:
                self.act_shadow = False
            self._refresh_act_count()
        if self.watcher is not None:
            self.set_interval(self.watch_s, self.watch_tick)
        self.query_one("#queue").focus()
        if self.bidi_fallback:
            self.notify("python-bidi is not installed: Hebrew stays in logical order.",
                        severity="warning", timeout=6, markup=False)
        self.call_after_refresh(self.redraw)
        self.poll()

    # ---------- state helpers ----------
    @property
    def pal(self):
        return palette(self)

    def oldest_update_age(self):
        """Seconds since the least recently refreshed source was refreshed,
        or None before the first refresh."""
        if not self.updated_at:
            return None
        return max(0.0, time.time() - min(self.updated_at.values()))

    def countdown_s(self):
        """Real seconds until the next poll, clamped to [0, poll interval]."""
        return min(float(self.poll_s), max(0.0, self.next_poll_at - time.time()))

    def source_problems(self):
        out = []
        for r in self.results:
            if r.error:
                out.append(f"{r.name}: {r.error}")
            elif r.stale:
                age = f" {int(r.age_min)}m" if r.age_min is not None else ""
                out.append(f"{r.name}: stale{age}")
        return out

    def _passes(self, it):
        """The text and source filters of the queue."""
        if self.source_filter and it.source != self.source_filter:
            return False
        if self.filter_text:
            if fuzzy(self.filter_text, f"{it.who} {it.where}") is None \
                    and self.filter_text.lower() not in it.gist.lower():
                return False
        return True

    def view_groups(self, view=None):
        """The groups of the view (the open one by default) as the queue
        shows them: the Feed's routine filter, the text and the source filter
        applied. The rows are the same `Item` objects in every view."""
        view = view or self.view
        groups = self._views.get(view, [])
        if view == "feed":
            groups = views.only_routine(groups, self.feed_routine)
        if not (self.filter_text or self.source_filter):
            return list(groups)
        return [replace(g, items=self._filtered(g.items), tail="")
                for g in groups]

    def _filtered(self, rows):
        """`rows` that pass the filters. A digest row is rebuilt from the
        members that pass, so a mark made on it reaches only those."""
        out = []
        for i in rows:
            row = (views.digest_subset(i, self._passes) if views.is_digest(i)
                   else (i if self._passes(i) else None))
            if row is not None:
                out.append(row)
        return out

    def visible(self):
        """The rows of the open view, in the order the queue lists them."""
        return views.view_rows(self.view_groups())

    def view_counts(self):
        """Rows per view for the tab bar (before the text and source
        filters); Health counts what needs you."""
        return {"inbox": len(views.view_rows(self._views["inbox"])),
                "waiting": len(views.view_rows(self._views["waiting"])),
                "tasks": tasksmod.due_count(self.items, self._today()),
                "feed": self.feed_data.total,
                "health": self.health_badge()}

    def _today(self):
        """The local date task due dates are counted from."""
        try:
            return self.today_fn()
        except Exception:      # an injected clock must never end the session
            return datetime.date.today()

    def _ui_rules(self):
        fn = getattr(self.store, "feed_rules", None)
        return fn() if fn is not None else []

    def _classify_feed(self, rows, now):
        """Set `feed` (and its label) on the fyi rows from the Feed rules."""
        rules = feedrules.ordered(self._ui_rules(), self.feed_cfg["rules"],
                                  now)
        for it in rows:
            it.feed, it.feed_label = "show", ""
            if (it.section == "fyi" and rules and not tasksmod.is_task(it)
                    and not views.is_review(it)):
                action, rule = feedrules.classify(it, rules, now)
                it.feed = action
                if rule is not None:
                    it.feed_label = feedrules.rule_label(rule)

    def _saved_items(self, visible):
        """The saved-to-memory rows as Feed rows, leaving out the ones a row
        that is visible in the views already shows (same `source_id`), the
        ones marked done and the ones a pending mark is hiding."""
        have = {i.meta.get("source_id") for i in visible
                if i.meta.get("source_id")}
        now = time.time()
        busy = self._busy_keys()
        out = []
        for row in self.saved_rows:
            if row.get("source_id") in have:
                continue
            it = views.saved_item(row)
            if it.key not in busy and not self.store.is_hidden(it.key, now):
                out.append(it)
        return out

    def _build_views(self, now):
        """Group the shown rows into the views. Returns a signature of the
        result, so a caller can tell whether anything moved."""
        def feed(saved=()):
            return views.feed_view(
                self.items, now, window_h=self.feed_cfg["window_h"],
                show_all=self.show_all, saved=saved,
                windowed=self._event_origins)
        today = self._today()
        inbox = views.inbox_groups(self.items, today)
        waiting = views.waiting_groups(self.items, now)
        tasks = views.task_groups(self.items, today)
        data = feed()
        shown = (views.view_rows(inbox) + views.view_rows(waiting)
                 + [m for r in views.view_rows(data.groups)
                    for m in (r.members or [r])])
        saved = self._saved_items(shown)
        if saved:
            data = feed(saved)
        self.feed_data = data
        self._views = {"inbox": inbox, "waiting": waiting, "tasks": tasks,
                       "feed": data.groups}
        self._rows = {i.key: i for i in self.items}
        for g in self.feed_data.groups:
            for i in g.items:
                self._rows.setdefault(i.key, i)
        # The date is part of the signature: the due cells and ages redraw
        # when midnight passes.
        return (today, tuple(
            (v, tuple((g.key, tuple(i.key for i in g.items)) for g in gs))
            for v, gs in self._views.items()))

    def refilter(self, redraw=True):
        """Rebuild the views from the fetched rows: hide marked and pending
        items, wake snoozed ones that are due, apply the Feed rules."""
        busy_keys = self._busy_keys()
        now = time.time()
        for i in self.all_rows:
            i.local_due = self._due_marks.get(i.key, {}).get("due")
            # One mark for "this is in memory": saved here with `m`, or
            # saved by the daemon (meta.memory_id).
            i.saved = self.store.is_saved(i.key) or bool(i.memory_id)
            i.hint = (NOT_CONFIRMED if self.store.rsvp_unconfirmed(i.key, i.meta)
                      else "")
        shown = [i for i in self.all_rows
                 if i.key not in busy_keys and not self.store.is_hidden(i.key, now, i.meta)
                 and (self.show_resolved or not i.resolved)]
        changed = [i.key for i in shown] != [i.key for i in self.items]
        self.items = shown
        self._classify_feed(shown, now)
        sig = self._build_views(now)
        changed = changed or sig != self._groups_sig
        self._groups_sig = sig
        if redraw and changed:
            try:
                self.redraw()
            except NoMatches:  # timer fired during shutdown
                pass
        return changed

    def _busy_keys(self):
        """Items that are queued for, or executing, an action that hides them
        (a digest row stands for its members)."""
        out = set()
        for p in list(self.pending) + list(self.running):
            if p.hide and not (p.done and p in self.pending):
                out.add(p.item.key)
                out.update(m.key for m in p.item.members)
        return out

    @property
    def resolved_hidden(self):
        """How many resolved alerts R would reveal."""
        if self.show_resolved:
            return 0
        busy_keys = self._busy_keys()
        now = time.time()
        return sum(1 for i in self.all_rows
                   if i.resolved and i.key not in busy_keys
                   and not self.store.is_hidden(i.key, now, i.meta))

    @property
    def current(self):
        it = self._rows.get(self.current_key)
        if it is not None:
            if views.is_digest(it) and (self.filter_text or self.source_filter):
                return views.digest_subset(it, self._passes)
            return it
        for it in self.items:
            if it.key == self.current_key:
                return it
        return None

    # ---------- health ----------
    def alert_infos(self):
        """The active alert rows' details by routine (cause, remedy, ...)."""
        return healthmod.alert_info(self.all_rows)

    def needs_you(self):
        """The active alerts that need attention, for the Health view and its
        tab badge: `[{key, routine, since, info}]`. An alert whose only
        issues are benign run states never needs you."""
        snap = self.health_snap
        if not snap:
            return []
        infos = self.alert_infos()
        benign = {i.meta.get("key") for i in self.all_rows
                  if i.source == "alert" and i.meta.get("benign")}
        by_key = {v["key"]: v for v in infos.values() if v.get("key")}
        return [{"key": a["key"], "routine": a["routine"],
                 "since": a["since"], "info": by_key.get(a["key"], {})}
                for a in snap.get("alerts") or [] if a["key"] not in benign]

    def health_badge(self):
        """The number on the Health tab: the alerts that need you, plus the
        failing routine groups (`✗` in the strip) that no such alert names yet
        (an alert only fires after a streak of failed runs, so a group can be
        failing before one exists)."""
        snap = self.health_snap
        if not snap:
            return 0
        needs = self.needs_you()
        covered = {n["routine"] for n in needs if n["routine"]}
        groups = {r["group"] for r in snap.get("routines") or []
                  if r["id"] in covered}
        failing = [g for g in snap.get("groups") or []
                   if g["state"] == healthmod.FAIL
                   and g["name"] not in groups]
        return len(needs) + len(failing)

    def health_refresh(self):
        """Read the daemon's state in a worker (every 30 s, and on `r`). The
        reader caches by file mtimes; the UI thread does no file I/O."""
        if (self.health_fetch is None or self._health_loading
                or self._quitting):
            return
        self._health_loading = True
        self._health_worker()

    @work(exclusive=True, group="health")
    async def _health_worker(self):
        try:
            snap = await asyncio.to_thread(self.health_fetch)
        except Exception as exc:  # a reader bug must not end the session
            snap = {"at": time.time(),
                    "error": f"health read failed: {type(exc).__name__}",
                    "scheduler": {}, "routines": [], "groups": [],
                    "alerts": [], "connectors": [], "spend": {},
                    "heartbeats": []}
        self._health_loading = False
        if self._quitting:
            return
        self.health_snap = snap
        try:
            self.tick_header()
            self.render_health_view()
        except NoMatches:
            pass

    def render_health_view(self):
        """Draw the Health pane (when it is the open view)."""
        if self.view != "health":
            return
        pane = self.query_one("#health", HealthView)
        now = time.time()
        pane.border_title = scheduler_title(self.health_snap, now)
        pane.show(render_health(self.health_snap, self.needs_you(), self.pal,
                                now, pane.size.width or self.size.width,
                                self.bidi))

    def record(self, action, item, detail, status):
        row = {"time": time.time(), "action": action, "item": item.who if item else "",
               "detail": detail, "status": status}
        self.audit.append(row)
        return row

    @property
    def main_active(self):
        return len(self.screen_stack) == 1

    def on_app_focus(self, _e):
        """The terminal regained focus: after Textual restored its own
        widget, give the queue the focus only if nothing has it (a blur
        during the first poll)."""
        self.call_after_refresh(self._focus_queue_if_idle)

    def _focus_queue_if_idle(self):
        try:
            q = self.query_one("#queue", Queue)
        except NoMatches:
            return
        if (self.focused is None and self.main_active and q.display
                and self.view in views.QUEUE_VIEWS):
            q.focus()

    def check_action(self, action, parameters):
        if action in ("quit", "toggle_theme"):
            return True
        return self.main_active

    # ---------- rendering ----------
    def _sync_panes(self):
        """Show the widgets of the open view (the queue and thread, the
        Feed's routine list, or the Health pane)."""
        queue_view = self.view in views.QUEUE_VIEWS
        self.query_one("#main").display = queue_view
        self.query_one("#routines").display = self.view == "feed"
        self.query_one("#health").display = self.view == "health"

    def _focus_view(self):
        """Give the focus to the open view's main widget."""
        if self.view in views.QUEUE_VIEWS:
            q = self.query_one("#queue", Queue)
            if q.display:
                q.focus()
        elif self.view == "health":
            self.query_one("#health").focus()
        else:
            self.set_focus(None)

    def redraw(self, keep=None, ghost=None, ghost_at=None, ghost_group=None):
        self._sync_panes()
        if self.view not in views.QUEUE_VIEWS:
            self.current_key = None
            self.render_health_view()
            self.tick_header()
            self.update_hints()
            return
        q = self.query_one("#queue", Queue)
        groups = self.view_groups()
        vis_items = views.view_rows(groups)
        keep = keep or self.current_key
        now = time.time()
        zero = self.query_one("#zero", Static)
        empty = not vis_items and ghost is None
        q.display = not empty
        zero.display = empty
        if empty:
            zero.update(self.zero_text())
        else:
            list_sig = (self.view, self.filter_text, self.source_filter,
                        self.feed_routine if self.view == "feed" else None)
            reset = list_sig != self._list_sig
            self._list_sig = list_sig
            q.build(groups, self.pal, now, keep=keep, ghost=ghost,
                    ghost_group=ghost_group, ghost_at=ghost_at,
                    visual=self.bidi, today=self._today(),
                    reset_scroll=reset)
            # While the queue was hidden (the first poll still running) it
            # lost focus; give it back, or the arrows do nothing until a
            # click. Not while the terminal is in the background: Textual
            # clears the focus then and restores the widget on regain.
            if (self.focused is None and self.main_active
                    and getattr(self, "app_focus", True)):
                q.focus()
        left = self.query_one("#left")
        title = views.VIEW_TITLES[self.view]
        if self.view == "feed":
            title += f" · last {self.feed_cfg['window_h']:g}h"
            if self.show_all:
                title += " · all"
            self.query_one("#routines", RoutinePane).update(render_routines(
                self.feed_data.counts, self.feed_data.muted,
                self.feed_routine, self.pal, self.feed_data.total))
        left.border_title = (title + (f"  /{self.filter_text}" if self.filter_text else "")
                             + (f"  [{SOURCES[self.source_filter][2]}]" if self.source_filter else "")
                             + (f"  [{self.feed_routine}]" if self.view == "feed" and self.feed_routine else ""))
        left.border_subtitle = f"{len(vis_items)}" if (
            self.filter_text or self.source_filter or self.feed_routine) else ""
        self.sync_current()
        self.tick_header()
        self.update_hints()

    def zero_text(self):
        p = self.pal
        t = Text(justify="center")
        if (self.filter_text or self.source_filter or (
                self.view == "feed" and self.feed_routine)) and self.items:
            what = (f"'{self.filter_text}'" if self.filter_text else
                    SOURCES[self.source_filter][2] if self.source_filter
                    else self.feed_routine)
            t.append(f"No matches for {what}\n\n", Style(bold=True, color=p["warn"]))
            if self.feed_routine == views.SAVED_ROUTINE and self.saved_error:
                t.append(self.saved_error + "\n", Style(color=p["warn"]))
            t.append("esc clears the filters", Style(color=p["muted"]))
            return t
        total = sum(self.stats.values())
        if not self.polled_once:
            t.append("Polling…\n\n", Style(bold=True, color=p["fg"]))
            t.append("r  poll now      ctrl+p  commands      ?  help", Style(color=p["muted"]))
            return t
        if self.view == "tasks":
            t.append("No open tasks\n\n", Style(bold=True, color=p["fg"]))
            t.append("A command source lists tasks with meta.kind: task.\n",
                     Style(color=p["muted"]))
            t.append("r  poll now", Style(color=p["muted"]))
            return t
        if self.view == "waiting":
            t.append("Nobody owes you a reply\n\n", Style(bold=True, color=p["fg"]))
            t.append("Nothing you asked for is still open.", Style(color=p["muted"]))
            return t
        if self.view == "feed":
            t.append("The Feed is quiet\n\n", Style(bold=True, color=p["fg"]))
            t.append(f"Nothing in the last {self.feed_cfg['window_h']:g}h"
                     " (A shows older rows).\n", Style(color=p["muted"]))
            t.append("M  rules      r  poll now", Style(color=p["muted"]))
            return t
        if total == 0 and not self.items:
            t.append("Nothing here\n\n", Style(bold=True, color=p["fg"]))
            t.append("Every source was polled and returned nothing open.\n", Style(color=p["muted"]))
            t.append("r  poll now      ctrl+p  commands      ?  help", Style(color=p["muted"]))
            return t
        t.append("   ✦  ·  ✧     ✦\n", Style(color=p["accent"]))
        t.append("  ✓ INBOX ZERO\n", Style(bold=True, color=p["ok"]))
        t.append("   ✧     ✦  ·  ✧\n\n", Style(color=p["accent"]))
        t.append("Nice. Everything is handled.\n\n", Style(color=p["fg"]))
        t.append(f"{self.stats['sent']} sent  ·  {self.stats['done']} done  ·  {self.stats['snoozed']} snoozed\n\n",
                 Style(color=p["muted"]))
        t.append("u  undo last      L  audit log      r  poll now", Style(color=p["muted"]))
        return t

    def sync_current(self):
        q = self.query_one("#queue", Queue)
        key = None
        if q.display and q.highlighted is not None:
            opt = q.get_option_at_index(q.highlighted)
            if opt.id and opt.id.startswith(ITEM_PREFIX):
                key = opt.id[len(ITEM_PREFIX):]
        if key != self.current_key or key is None:
            self.current_key = key
            if self.remind_key != key:
                self.remind_key = None     # "Remind me" belongs to one task
            self.show_item()
        else:
            self.show_thread_only()
        self.context_sync()

    def show_thread_only(self):
        it = self.current
        task = it is not None and tasksmod.is_task(it)
        # A task has a detail card, not a thread, and nothing to draft.
        self.query_one("#thread").border_title = "Detail" if task else "Thread"
        self.query_one("#draft-pane").display = not task
        remind = (self._remind_view(it) if task and self.remind_key == it.key
                  else None)
        self.query_one("#thread", ThreadView).show(
            it, self.pal, time.time(), self.bidi,
            self.thread_order == "newest", today=self._today(),
            remind=remind)

    def show_item(self):
        self.show_thread_only()
        it = self.current
        if self.editing:
            self.end_edit(save=False)
        self.start_draft(it)

    def tick_header(self):
        try:
            h = self.query_one("#hdr", StatusHeader)
        except NoMatches:  # a timer fired while the app is shutting down
            return
        h.update(h.render_header(self, self.pal, self.size.width))
        strip = self.query_one("#strip", HealthStrip)
        # With acts on the strip also tells how many ops are not synced.
        strip.display = (self.health_fetch is not None
                         or bool(self.act_pending or self.act_shadow))
        if strip.display:
            strip.update(strip.render_strip(self, self.pal, self.size.width))

    def hints(self):
        """The footer. For the selected row (the queue has the focus) at most
        `rowmenu.FOOTER_MAX` hints, the same few on every kind of row:
        `a <label>`, `d done`, `s later`, `o open`, `enter more`, `? help`
        (one slot stays free). Everything else is in the row menu (enter),
        the command palette and `?`; every key still works."""
        return self._base_hints()

    def _base_hints(self):
        f = self.focused
        if self.editing:
            return [("esc", "save & back"), ("ctrl+s", "save"), ("", "typing edits the draft")]
        if isinstance(f, Input):
            return [("enter", "keep filter"), ("esc", "clear"), ("", "type to fuzzy-match")]
        if self.view == "health":
            return [("r", "refresh"), ("1-5", "views"), ("ctrl+p", "commands"),
                    ("?", "help"), ("q", "quit")]
        it = self.current
        if it is None:
            return [("r", "poll"), ("/", "filter"), ("A", "all"), ("S", "saved"), ("R", "resolved"), ("L", "audit"), ("ctrl+p", "commands"), ("?", "help"), ("q", "quit")]
        base = [("j/k", "move")]
        if tasksmod.is_task(it) and f is not None and f.id in ("thread", "draft-pane"):
            # the card has the focus: its keys scroll it
            keys = []
            if self._acts_on(it, "close"):
                keys.append(("d", "done"))
            if self._acts_on(it, "due"):
                keys.append(("s", "due date"))
            keys.append(("m", "close remind" if self.remind_key == it.key
                         else "remind me"))
            return [("j/k", "scroll"), ("esc", "close" if self.remind_key
                                        == it.key else "queue")] + keys + [
                ("?", "help")]
        if f is not None and f.id == "context":
            return base + [("C", "refresh"), ("i", "summary"), ("c", "close"),
                           ("tab", "next pane"), ("esc", "queue"), ("?", "help")]
        if f is not None and f.id in ("thread", "draft-pane"):
            return base + [("tab", "next pane"), ("y", "copy"), ("e", "edit"), ("a", "approve"), ("esc", "queue"), ("?", "help")]
        return rowmenu.verb_hints(self, it)

    @property
    def actions_save_on(self):
        return bool(getattr(self.actions, "save_configured", False))

    def _rsvp_on(self, it):
        """True for a pending invite when RSVP is configured: a/X/T answer it."""
        return (bool(getattr(self.actions, "rsvp_configured", False))
                and invites.rsvp_target(it) is not None)

    def _react_on(self, it):
        """True when `react:` is configured and the item has a message to
        react to."""
        return (bool(getattr(self.actions, "react_configured", False))
                and self.actions.react_plan(it) is not None)

    def _polish_on(self, it):
        """True when `polish:` is configured and the item has a draft."""
        return (bool(getattr(self.actions, "polish_configured", False))
                and bool(it.draft and it.draft.strip()))

    def update_hints(self):
        try:
            hints = self.query_one("#hints", HintBar)
        except (NoMatches, ScreenStackError):   # a message during teardown
            return
        hints.set_hints(self.hints(), self.pal)

    def on_descendant_focus(self, _e):
        self.update_hints()

    def on_resize(self, _e):
        self.call_after_refresh(self.redraw)

    # ---------- queue events ----------
    def on_option_list_option_selected(self, e):
        """A click on a Feed row: details (the thread pane takes the focus).
        Enter does not select (it opens the row menu); tab is the key."""
        if e.option_list.id == "queue" and self.view == "feed":
            self.query_one("#thread").focus()

    # ---------- the row menu (0.13.0) ----------
    def row_menu_entries(self, it=None):
        """The entries of the selected row's menu (what is valid now)."""
        it = it if it is not None else self.current
        return rowmenu.build_entries(self, it) if it is not None else []

    def item_commands(self, screen=None):
        """The selected row's valid commands for the palette's "This item"
        group (`SystemCommand`s from the row menu's entries); empty when the
        palette was opened over something other than the main screen."""
        if (self.view not in views.QUEUE_VIEWS or self.current is None
                or (screen is not None and screen is not self.screen_stack[0])):
            return []
        return [SystemCommand(
            e.label, f"{e.shown}: {e.detail}" if e.detail else e.shown,
            e.run, True) for e in self.row_menu_entries(self.current)]

    def item_covered_titles(self, screen=None):
        """The palette titles the "This item" group replaces."""
        if (self.view not in views.QUEUE_VIEWS or self.current is None
                or (screen is not None and screen is not self.screen_stack[0])):
            return set()
        return {t for e in self.row_menu_entries(self.current)
                for t in e.titles}

    def action_row_menu(self):
        """Enter: a menu of what the selected row can do, each entry with its
        key. It dispatches to the same methods the keys call."""
        it = self.current
        if (it is None or self.editing or not self.main_active
                or self.view not in views.QUEUE_VIEWS):
            return
        entries = self.row_menu_entries(it)
        if not entries:
            self.notify("Nothing to do on this row", timeout=2, markup=False)
            return
        title = f"{it.who} · {it.where}" if it.where else it.who

        def chosen(entry):
            if entry is None:
                return
            if self.current_key != it.key:
                self.notify("The selection changed; nothing was run",
                            timeout=3, markup=False)
                return
            entry.run()
        self.push_screen(RowMenu(title, entries, self.bidi), chosen)

    def on_option_list_option_highlighted(self, e):
        if e.option_list.id == "queue":
            try:
                self.sync_current()
                self.update_hints()
            except NoMatches:   # a highlight message that outlived the screen
                pass

    # ---------- draft pane ----------
    def draft_text(self, it, text, streaming):
        p = self.pal
        if it is None:
            return Text("")
        if not text and not streaming:
            t = Text(style=Style(color=p["muted"], italic=True))
            label = SOURCES.get(it.source, SOURCES["other"])[2].lower()
            t.append("No draft. " + (f"Nothing to send for a {label} item: a acknowledges{', o opens' if it.link else ''}." if it.kind == "ack"
                                       else "FYI items need no reply." if it.kind == "fyi"
                                       else "Not due for a nudge yet." if it.kind == "nudge"
                                       else "Press e to write one."))
            return t
        width = max(20, (self.query_one("#draft-scroll").size.width or 50) - 2)
        t = Text()
        for ln in text.split("\n"):
            vl = bidi.visual_lines(ln, width, self.bidi) if ln else [""]
            right = bidi.is_rtl_paragraph(ln) and self.bidi and bidi.has_rtl(ln)
            for v in vl:
                pad = " " * max(0, width - cell_len(v)) if right else ""
                t.append(pad + v + "\n")
        t.rstrip()
        if streaming:
            t.append("▌", Style(color=p["accent"], bold=True))
        return t

    def set_badge(self, it, state):
        p = self.pal
        b = Text()
        if it is None or (not it.draft and it.kind != "reply"):
            b.append(" no draft ", Style(color=p["muted"]))
        else:
            label = "nudge" if it.kind == "nudge" else "draft"
            b.append(f" ✎ {label} · from {it.drafted_by} ", Style(color=p["bg"], bgcolor=p["mine"], bold=True))
        self.query_one("#badge", Static).update(b)
        self.query_one("#draft-status", Static).update(state)

    @work(exclusive=True, group="draft")
    async def start_draft(self, it):
        view = self.query_one("#draft-view", Static)
        if it is None:
            view.update("")
            self.set_badge(None, "")
            return
        if it.streamed or not it.draft or not self.stream_delay:
            it.streamed = True
            self.set_badge(it, "ready" if it.draft else "")
            view.update(self.draft_text(it, it.draft, False))
            return
        self.busy += 1
        try:
            self.set_badge(it, "drafting…")
            text = await asyncio.to_thread(self.actions.draft, it)
            text = text or it.draft
            step = 4
            for i in range(0, len(text) + step, step):
                view.update(self.draft_text(it, text[:i], i < len(text)))
                await asyncio.sleep(self.stream_delay)
            it.draft, it.streamed = text, True
            self.set_badge(it, "ready")
            self.update_hints()
        finally:
            self.busy -= 1

    # ---------- movement ----------
    def action_move(self, d):
        f = self.focused
        if f is not None and f.id == "queue":
            self.query_one("#queue", Queue).action_cursor_down() if d > 0 else self.query_one("#queue", Queue).action_cursor_up()
        elif f is not None and f.id in ("thread", "draft-pane", "context", "health"):
            f.scroll_down() if d > 0 else f.scroll_up()

    def action_edge(self, end):
        f = self.focused
        q = self.query_one("#queue", Queue)
        if f is not None and f.id == "queue" and q.rows:
            q.highlighted = q.get_option_index(ITEM_PREFIX + q.rows[-1 if end else 0].key)
            q.scroll_to_highlight()    # also when the cursor was already there
        elif f is not None and f.id in ("thread", "context", "health"):
            f.scroll_end(animate=False) if end else f.scroll_home(animate=False)

    def action_view(self, n):
        """1-5: switch the view (Inbox, Waiting, Tasks, Feed, Health)."""
        self.set_view(views.VIEW_KEYS[int(n) - 1])

    def set_view(self, key, routine=None):
        """Open a view. `routine` (Feed only) sets the Feed's routine filter,
        else it is left as it was."""
        if self.editing:
            self.end_edit(save=True)
        if key == "feed" and routine is not None:
            self.feed_routine = routine or None
        if key == "feed" and self.view != "feed" and self.source_filter:
            self.source_filter = None   # `f` filters by routine here
        self.view = key
        self.redraw(keep=self.current_key)
        self._focus_view()
        if key == "health":
            self.health_refresh()
        self.update_hints()

    def action_group(self, step):
        """[ and ]: jump to the first row of the previous / next group of the
        open view (a section, a person, a routine)."""
        if self.view not in views.QUEUE_VIEWS:
            return
        groups = [g for g in self.view_groups() if g.items]
        if not groups:
            self.notify("Nothing to jump to", timeout=2, markup=False)
            return
        cur = views.group_of(groups, self.current) if self.current else None
        if cur is None:
            target = groups[0 if step > 0 else -1]
        else:
            n = groups.index(cur) + step
            if not 0 <= n < len(groups):
                self.notify("Last group" if step > 0 else "First group",
                            timeout=2, markup=False)
                return
            target = groups[n]
        q = self.query_one("#queue", Queue)
        q.focus()
        q.highlighted = q.get_option_index(ITEM_PREFIX + target.items[0].key)

    def _panes(self):
        if self.view == "health":
            return [self.query_one("#health")]
        panes = [self.query_one("#queue"), self.query_one("#thread")]
        if self.context_open:
            panes.append(self.query_one("#context"))
        draft = self.query_one("#draft-pane")
        return panes + ([draft] if draft.display else [])

    def action_focus_next(self):
        self._cycle(1)

    def action_focus_previous(self):
        self._cycle(-1)

    def _cycle(self, d):
        if not self.main_active:
            return
        if self.editing:
            self.end_edit(save=True)
        panes = self._panes()
        cur = self.focused
        idx = 0
        for n, p in enumerate(panes):
            if cur is p or (cur is not None and p in cur.ancestors):
                idx = n
        if self.view in views.QUEUE_VIEWS and \
                self.query_one("#queue").display is False:
            panes = panes[1:]
            idx = max(0, idx - 1)
        if not panes:
            return
        panes[(idx + d) % len(panes)].focus()

    # ---------- filter ----------
    def action_source_filter(self):
        """f: cycle the sources present; in the Feed, the routines."""
        if self.view == "feed":
            return self._cycle_routine()
        present = [k for k in SOURCES if any(i.source == k for i in self.items)]
        order = [None] + present
        cur = order.index(self.source_filter) if self.source_filter in order else 0
        self.source_filter = order[(cur + 1) % len(order)]
        self.notify("Source: " + (SOURCES[self.source_filter][2] if self.source_filter else "all"),
                    timeout=2, markup=False)
        self.redraw(keep=None)

    def _cycle_routine(self):
        names = [None] + [n for n, _ in self.feed_data.counts]
        cur = names.index(self.feed_routine) if self.feed_routine in names else 0
        self.feed_routine = names[(cur + 1) % len(names)]
        self.notify("Routine: " + (self.feed_routine or "all"), timeout=2,
                    markup=False)
        self.redraw(keep=None)

    def action_filter(self):
        f = self.query_one("#filter", Input)
        f.add_class("on")
        f.focus()

    def on_input_changed(self, e):
        if e.input.id == "filter":
            self.filter_text = e.value.strip()
            self.redraw(keep=None)

    def on_input_submitted(self, e):
        if e.input.id == "filter":
            self.query_one("#queue").focus()
            if not self.filter_text:
                e.input.remove_class("on")

    def action_escape(self):
        if self.editing:
            self.end_edit(save=True)
            self._focus_view()
            return
        if self.remind_key is not None:      # leave the "Remind me" card
            self.remind_key = None           # (one esc: closed, back on the queue)
            self.show_thread_only()
            self._focus_view()
            self.update_hints()
            return
        f = self.query_one("#filter", Input)
        routine = self.view == "feed" and self.feed_routine
        if self.filter_text or f.has_class("on") or self.source_filter or routine:
            f.value = ""
            self.filter_text = ""
            self.source_filter = None
            if routine:
                self.feed_routine = None
            f.remove_class("on")
            self._focus_view()
            self.redraw()
            return
        self._focus_view()

    # ---------- editing ----------
    def _synthetic(self, it):
        """True (with a notice) for a row the views made: a saved-to-memory
        row takes o, d, s and y; a digest row takes d and s (on its rows), o
        (when its rows share one link), y, and x or - (a rule from its newest
        row). Nothing else acts on them. A task row (0.12.0) takes only its
        own keys (d, D, s, x, o)."""
        if it is not None and tasksmod.is_task(it):
            self.notify(TASK_KEYS_TOAST, timeout=3, markup=False)
            return True
        if it is None or not views.is_synthetic(it):
            return False
        self.notify("A saved-to-memory row: only o, d, s and y work on it"
                    if views.is_saved_row(it) else
                    "A digest row: d and s act on its rows, o and y on the "
                    "row, x and - on its newest", timeout=3, markup=False)
        return True

    def action_edit(self):
        it = self.current
        if self._synthetic(it):
            return
        if it is None or it.kind in ("fyi", "ack"):
            self.notify("Nothing to edit here", timeout=2, markup=False)
            return
        if not self.actions.supports_send:
            self.notify(NOT_YET, timeout=3, markup=False)
            return
        self._open_editor(it.draft)

    def _open_editor(self, text):
        """Open the draft editor on `text` (the current item's draft)."""
        ta = self.query_one("#draft-edit", TextArea)
        ta.text = text
        ta.read_only = False
        ta.add_class("on")
        self.query_one("#draft-scroll").display = False
        self.editing = True
        ta.focus()
        ta.move_cursor(ta.document.end)
        self.update_hints()

    def end_edit(self, save):
        it = self.current
        ta = self.query_one("#draft-edit", TextArea)
        # Editor text is cleaned the way drafts are (controls and bidi
        # overrides removed), so what is shown is what can be sent.
        text = itm_items.clean_text(ta.text)
        if save and it is not None and text != it.draft:
            it.draft, it.streamed = text, True
            self.record("edit", it, f"{len(text)} chars", "saved")
        ta.read_only = True
        ta.remove_class("on")
        self.query_one("#draft-scroll").display = True
        self.editing = False
        if it is not None:
            self.query_one("#draft-view", Static).update(self.draft_text(it, it.draft, False))
            self.set_badge(it, "edited" if save else "ready")
        self.update_hints()

    def action_save_edit(self):
        if self.editing:
            self.end_edit(save=True)
            self.notify("Draft saved", timeout=2, markup=False)
            self.query_one("#queue").focus()

    # ---------- actions with optimistic update + undo ----------
    def _remove_optimistic(self, it):
        vis_before = self.visible()
        pos = vis_before.index(it) if it in vis_before else 0
        home = views.group_of(self.view_groups(), it)
        ghost_group = home.key if home is not None else None
        ghost_at = home.items.index(it) if home is not None else None
        self.refilter(redraw=False)       # the pending item is already listed
        after = self.visible()
        keep = after[min(pos, len(after) - 1)].key if after else None
        if (self.current_key not in (None, it.key)
                and any(i.key == self.current_key for i in after)):
            keep = self.current_key   # a late mark must not move the cursor
        self.current_key = keep
        if self.leave_s > 0:
            self.redraw(keep=keep, ghost=it, ghost_at=ghost_at,
                        ghost_group=ghost_group)
            self.set_timer(self.leave_s, lambda: self.redraw(keep=self.current_key))
        else:
            self.redraw(keep=keep)

    def _enqueue(self, kind, it, toast, text="", until=0.0, detail="",
                 answer="", hide=True, force=False, name="", choice="",
                 state="", mark_done=False, act="", act_args=None, react="",
                 meta=None):
        p = Pending(kind=kind, item=it, text=text, until=until, label=toast,
                    answer=answer, hide=hide, force=force, name=name,
                    choice=choice, state=state, mark_done=mark_done, act=act,
                    act_args=dict(act_args or {}), react=react, act_meta=meta)
        p.audit = self.record(kind, it, detail, "queued")
        self.pending.append(p)
        if hide:
            self._remove_optimistic(it)
            self._count(kind, 1)
        p.timer = self.set_timer(self.undo_s, lambda: self._commit(p))
        self.notify(f"{toast}\nu to undo ({int(self.undo_s)}s)", timeout=self.undo_s,
                    title="Queued", markup=False)
        self.redraw()
        return p

    def _count(self, kind, step):
        """Session tallies shown on the empty screen (a save hides nothing)."""
        if kind in ("send", "nudge"):
            self.stats["sent"] += step
        elif kind in ("done", "ack", "rsvp", "drop"):
            self.stats["done"] += step
        elif kind == "snooze":
            self.stats["snoozed"] += step

    def _commit(self, p):
        if p.done:
            return
        p.done = True
        if p in self.pending:
            self.pending.remove(p)
        self.running.append(p)
        self.busy += 1
        self.run_worker(lambda: self._run_in_thread(p), thread=True, group="commit")

    def _run(self, p, send=True):
        """Do a committed action. `send` False (the app is quitting) writes
        the local marks only: an act is kept for a retry, never sent."""
        a, it = self.actions, p.item
        if p.kind == "send":
            return a.send(it, p.text)
        if p.kind == "nudge":
            return a.nudge(it, p.text)
        if p.kind == "draft":
            return a.place_draft(it, p.text)
        if p.kind == "done":
            return self._after_mark(p, self._each(it, a.mark_done), send)
        if p.kind == "mute":
            res = (a.snooze(it, p.until) if p.until else a.mark_done(it))
            return self._after_mark(p, res, send)
        if p.kind == "drop":
            return self._after_mark(p, self._each(it, a.mark_done), send)
        if p.kind in NO_MARK_ACT_KINDS:
            return self._after_mark(p, Result(), send)
        if p.kind == "ack":
            return self._each(it, a.ack)
        if p.kind == "rsvp":
            return a.rsvp(it, p.answer)
        if p.kind == "save":
            return a.save(it, force=True) if p.force else a.save(it)
        if p.kind == "action":
            return a.run_action(p.name, it, p.choice, p.state)
        if p.kind == "react":
            return a.react(it, p.choice)
        return self._after_mark(
            p, self._each(it, lambda row: a.snooze(row, p.until)), send)

    def _after_mark(self, p, res, send):
        """After the local mark of a done, snooze or mute (or with none, for
        waits-on and relay): tell the source (`p.act`) and add the done
        reaction (`p.react`). The act may take as long as the source's timeout:
        this runs in a worker, never on the UI thread."""
        if not res.ok or not (p.act or p.react):
            return res
        a, it = self.actions, p.item
        if not send:                      # quitting: nothing may block
            if p.act:
                a.queue_act(it, p.act, p.act_args, p.act_meta)
            return res
        note, out = [], None
        if p.act:
            started = time.time()
            out = a.send_act(it, p.act, p.act_args, p.act_meta)
            if out.dropped:
                note.append(f"{out.dropped} unsynced op(s) older than "
                            f"{actsmod.PENDING_MAX_AGE_S // 86400} days or "
                            "over the cap were dropped")
            if out.status == actsmod.CONFLICT:
                if p.kind in ACT_MARK_KINDS:
                    # The local mark goes back, unless a newer one was made.
                    a.undo_marks(it, since=started)
                if p.react:
                    note.append("reaction skipped (conflict)")
                return Result(True, out.message, sync=actsmod.CONFLICT,
                              op_id=out.op_id, shadow=out.shadow,
                              note="; ".join(note))
        if p.react:
            # The reaction says "handled": only when the source took it for
            # real (or there is no act). Not after a shadow, a failure kept
            # for a retry, or a lost op.
            if out is None or out.status == actsmod.OK:
                done = a.react_done(it, p.react)
                note.append(f"Reacted :{p.react}:" if done.ok
                            else f"Reaction failed: {done.detail}")
            else:
                why = {actsmod.SHADOW: "shadow", actsmod.PENDING: "not synced",
                       actsmod.LOST: "not synced"}.get(out.status, out.status)
                note.append(f"reaction skipped ({why})")
        return Result(True, out.message if out else "",
                      sync=out.status if out else "",
                      op_id=out.op_id if out else "",
                      shadow=bool(out and out.shadow), note="; ".join(note))

    @staticmethod
    def _each(it, fn):
        """Apply a local mark to the item, or to every row a digest row
        stands for; the first failure is the result."""
        result = Result()
        for row in (it.members or [it]):
            res = fn(row)
            if not res.ok:
                return res
            result = res
        return result

    def _run_in_thread(self, p):
        try:
            res = self._run(p)
        except Exception as exc:  # a failing store must not kill the app
            res = Result(False, f"{type(exc).__name__}: {exc}")
        if self._quitting:
            # The app is gone; the mark (if any) is already written. A Slack
            # write that was still running says how it ended.
            self._late_report(p, res)
            return
        if p.kind in ("action", "send", "nudge", "draft", "react") or (
                p.kind in ACT_KINDS and (p.act or p.react)):
            # These may run for a long time: never wait on the UI loop.
            self._post(self._committed, p, res)
            return
        try:
            self.call_from_thread(self._committed, p, res)
        except Exception:  # the app quit between the check and the call
            pass

    def _late_report(self, p, res):
        """A send, nudge or reaction that finished after the app began to
        quit: close its audit row and print one line to stderr."""
        if p.kind not in SLACK_WRITE_KINDS and p.kind != "react":
            return
        p.audit["status"] = "committed" if res.ok else "failed"
        p.audit["detail"] = (p.audit["detail"] + " · " + (res.detail or "done")
                             ).strip(" ·")
        try:
            print(f"inbox: {p.label}: "
                  f"{'done' if res.ok else 'failed'}"
                  f"{': ' + res.detail if res.detail else ''}", file=sys.stderr)
        except (OSError, ValueError):
            pass

    def _committed(self, p, res):
        self.busy = max(0, self.busy - 1)
        if p in self.running:
            self.running.remove(p)
        p.audit["status"] = "committed" if res.ok else "failed"
        p.audit["detail"] = (p.audit["detail"] + " · " + res.detail).strip(" ·")
        self.last_committed = p
        if p.kind == "due":
            self._due_settled(p, res)
        if res.ok and (res.sync or res.note) and p.kind in ACT_KINDS:
            self._synced(p, res)
        elif p.kind == "save" and not res.ok and res.duplicate and not p.force:
            # Not a failure: the command found a likely duplicate. A modal
            # opened here could stack on whatever the user is doing, so tell
            # them instead; `m` on the item opens the question.
            p.audit["status"] = "duplicate"
            self.dups[p.item.key] = (p.item, res)
            self.refilter()
            self.notify(f"Possible duplicate: {p.item.who}\npress m to review",
                        title="Save", severity="warning", timeout=8, markup=False)
        elif not res.ok:
            # The mark did not stick: the item must come back, not vanish.
            self.refilter()
            self.notify(f"{p.name or p.kind} failed: {res.detail}", severity="error",
                        timeout=10 if p.kind in SLACK_WRITE_KINDS else 6,
                        markup=False)
        elif p.kind == "rsvp":
            self.refilter()
            self.notify(f"{res.detail}: {p.item.gist}", title="RSVP sent", timeout=5, markup=False)
        elif p.kind == "save":
            self.refilter(redraw=False)
            self.redraw()
            self.notify(f"{res.detail}: {p.item.who}", timeout=5, markup=False,
                        title="Already in memory" if res.duplicate else "Saved to memory")
            if getattr(self.actions, "save_mark_done", False):
                self._auto_done(p.item, "after save")
        elif p.kind == "action":
            self.refilter(redraw=False)
            self.redraw()
            self.notify(f"{res.detail}: {p.item.who}", title=p.name,
                        timeout=5, markup=False)
            if p.mark_done:
                self._auto_done(p.item, f"after {p.name}")
        elif p.kind == "react":
            self.refilter(redraw=False)
            self.redraw()
            self.notify(f"{res.detail}: {p.item.who}", title="Reaction sent",
                        timeout=5, markup=False)
            if p.mark_done:
                self._auto_done(p.item, "after react")
        elif p.kind == "draft":
            # A draft is not a send: the item stays, nothing is marked done.
            if res.detail:
                self.notify(res.detail, title="Draft in Slack", timeout=8,
                            severity="warning", markup=False)
            self.notify(DRAFT_TOAST if self._open_target(p.item)
                        else DRAFT_TOAST_NO_OPEN, title="Draft in Slack",
                        timeout=8, markup=False)
        elif p.kind in SEND_KINDS and self._real_send():
            self.refilter(redraw=False)
            self.redraw()
            self.notify(f"{p.label}\n{res.detail}", title="Sent to Slack",
                        timeout=6, markup=False)
        elif p.kind in SEND_KINDS:
            self.notify(f"{res.detail or 'would send'}  ->  {p.item.destination}",
                        title="Demo", timeout=4, markup=False)

    # ---------- acts (0.10.0) ----------
    def act_status(self):
        """`(not_synced, shadow)` for the health strip: how many ops wait for a
        retry and whether the last act reply said shadow."""
        return self.act_pending, self.act_shadow

    def _acts_on(self, it, action):
        """True when the row's source lists the act (False for rows the views
        made, and for every backend without acts)."""
        fn = getattr(self.actions, "act_enabled", None)
        try:
            return bool(fn and fn(it, action))
        except Exception:
            return False

    def _acts_on_source(self, it):
        """True when the row's source lists any act."""
        fn = getattr(self.actions, "act_names", None)
        try:
            return bool(fn and fn(it))
        except Exception:
            return False

    def _acts_any(self):
        return bool(getattr(self.actions, "acts_configured", False))

    def _refresh_act_count(self):
        fn = getattr(self.actions, "pending_count", None)
        try:
            self.act_pending = int(fn()) if fn else 0
        except Exception:
            self.act_pending = 0
        self.tick_header()

    def _synced(self, p, res):
        """UI thread: the source answered (or the done reaction ran). ok keeps
        the mark; a conflict has already put it back; a failure keeps it and
        the op waits for a retry."""
        if res.sync in (actsmod.OK, actsmod.SHADOW):
            self.act_shadow = res.sync == actsmod.SHADOW
        self._refresh_act_count()
        self.refilter(redraw=False)
        self.redraw(keep=p.item.key if res.sync == actsmod.CONFLICT
                    else self.current_key)
        what = p.act or p.kind
        who = (p.item.gist if tasksmod.is_task(p.item) else p.item.who)
        if res.sync == actsmod.CONFLICT:
            p.audit["status"] = "conflict"
            self.notify(self._conflict_text(
                {"task": tasksmod.is_task(p.item)}), title="Not applied",
                        severity="warning", timeout=8, markup=False)
        elif res.sync == actsmod.LOST:
            p.audit["status"] = "lost"
            self.notify(f"{what}: not synced AND not saved for a retry "
                        f"({res.detail}). The source never got it; do it "
                        "again once it works.", title="Op lost",
                        severity="error", timeout=12, markup=False)
        elif res.sync == actsmod.PENDING:
            p.audit["status"] = "not synced"
            self.notify(f"{what}: saved here, not synced: {res.detail}\n"
                        f"{self.act_pending} not synced; retried on refresh",
                        title="Not synced", severity="warning", timeout=8,
                        markup=False)
        elif res.sync in (actsmod.OK, actsmod.SHADOW):
            word = SHADOW_WORD if res.sync == actsmod.SHADOW else "✓ synced"
            p.audit["status"] = "shadow" if res.sync == actsmod.SHADOW \
                else "synced"
            self.notify(f"{word}: {what} · {who}", title="Source",
                        timeout=4, markup=False)
            if (p.kind == "waits_on" or (
                    p.kind == "due" and res.sync == actsmod.OK)
                    ) and self.polled_once:
                self.poll(quiet=True, names=(
                    {p.item.origin} if self.units is not None else None))
        if res.note:
            self.notify(res.note, timeout=6, markup=False,
                        severity="warning" if "ailed" in res.note
                        or "dropped" in res.note else "information")

    @staticmethod
    def _is_task_row(row):
        """An ops-log row (or a pending entry's request) of a task op: the
        row's own `task` flag, else the kind in its meta."""
        meta = row.get("meta") if isinstance(row, dict) else None
        return bool(isinstance(row, dict) and (
            row.get("task") is True
            or (isinstance(meta, dict) and meta.get("kind") == tasksmod.KIND)))

    @staticmethod
    def _conflict_text(row):
        """The conflict toast for an ops-log row (or anything shaped like
        one: a `task` flag, a `meta`)."""
        return (TASK_CONFLICT_TOAST if InboxApp._is_task_row(row)
                else CONFLICT_TOAST)

    # A due date set here shows at once and stays until the next list refresh
    # (the source's list is the truth after that).
    def _set_due_mark(self, it, date_str, p=None):
        self._due_marks[it.key] = {"due": (date_str,), "op": p,
                                   "origin": it.origin, "settled_at": None}
        self.refilter(redraw=False)

    def _clear_due_mark(self, key, redraw=True):
        if self._due_marks.pop(key, None) is not None:
            self.refilter(redraw=redraw)

    def _due_settled(self, p, res):
        """UI thread: the due op finished. A conflict, a failed op or a lost
        one takes the shown date back. An op kept for a retry (not synced)
        keeps the date, like a close keeps its mark, until the retry settles
        (`_due_retried`). An ok is stamped: the mark goes once the source's
        list was fetched after it (`_list_fresh`)."""
        entry = self._due_marks.get(p.item.key)
        if entry is None or entry.get("op") is not p:
            return
        if (not res.ok or res.sync in (actsmod.CONFLICT, actsmod.LOST)):
            self._clear_due_mark(p.item.key, redraw=False)
        elif res.sync != actsmod.PENDING:
            entry["settled_at"] = time.time()

    def _due_retried(self, entry, out):
        """A pending due op was retried: an ok stamps its mark, a conflict
        takes the shown date back."""
        key = entry.get("id")
        mark = self._due_marks.get(key)
        if mark is None or entry.get("action") != "due":
            return
        if out.status == actsmod.CONFLICT:
            self._due_marks.pop(key, None)
        elif out.status in (actsmod.OK, actsmod.SHADOW):
            mark["settled_at"] = time.time()

    def _list_fresh(self, mark, since):
        """True when the list the mark's source answers with was fetched after
        the mark was settled, so it already holds the new date. With units
        that is the time the source's own unit last ran (a re-evaluation or a
        poll of another unit proves nothing); otherwise the start of the fetch
        being applied (`since`)."""
        settled = mark.get("settled_at")
        if settled is None:
            return False
        if self.units is not None:
            fetched = self._list_started.get(mark.get("origin"))
        else:
            fetched = since
        return fetched is not None and fetched >= settled

    def _retry_ops(self):
        """After a refresh: resend the ops that could not be delivered, in a
        worker (never on the UI thread), at most every 5 minutes each."""
        fn = getattr(self.actions, "retry_pending", None)
        if (fn is None or self._retrying or self._quitting
                or not self.act_pending):
            return
        self._retrying = True
        threading.Thread(target=self._retry_thread, args=(fn,),
                         name="inbox-acts-retry", daemon=True).start()

    def _retry_thread(self, fn):
        try:
            report = fn(cancel=self._cancel)
        except Exception:
            report = None
        self._retrying = False
        if report is not None and not self._quitting:
            self._post(self._retried, report)

    def _retried(self, report):
        self._refresh_act_count()
        done = [(e, o) for e, o in report.results
                if o.status != actsmod.PENDING]
        for entry, out in report.results:
            self._due_retried(entry, out)
            self.record("retry", _stub(entry), f"{entry.get('action')}: "
                        f"{out.message}", out.status)
            if out.status in (actsmod.OK, actsmod.SHADOW):
                self.act_shadow = out.status == actsmod.SHADOW
        conflicts = [e for e, o in done if o.status == actsmod.CONFLICT]
        task_conflict = any(
            self._is_task_row(e.get("request"))
            or (e.get("log_extra") or {}).get("task") is True
            for e in conflicts)
        moved = {e.get("source") for e, o in done
                 if e.get("action") == "due" and o.status == actsmod.OK}
        if moved and self.polled_once:
            # The source took a due move kept for a retry: fetch its list.
            self.poll(quiet=True, names=(moved if self.units is not None
                                         else None))
        if report.dropped:
            self.notify(f"{report.dropped} unsynced op(s) older than "
                        f"{actsmod.PENDING_MAX_AGE_S // 86400} days were "
                        "dropped", severity="warning", timeout=8, markup=False)
        if conflicts:
            self.refilter(redraw=False)
            self.redraw(keep=self.current_key)
            self.notify(TASK_CONFLICT_TOAST if task_conflict else CONFLICT_TOAST,
                        title="Not applied", severity="warning", timeout=8,
                        markup=False)
        elif done:
            self.notify(f"✓ synced {len(done)} earlier op(s)", timeout=4,
                        markup=False)

    def _auto_done(self, it, why):
        """Mark a handled item done the way `d` does (store mark, audit, the
        5 s undo), unless it is already hidden, already being marked, or no
        longer listed."""
        if self._quitting or self.store.is_hidden(it.key, time.time(), it.meta):
            return
        if any(p.item.key == it.key and p.hide and not p.done
               for p in self.pending):
            return
        if not any(i.key == it.key for i in self.items):
            return
        self._enqueue("done", it, f"Done: {it.who}", detail=why)

    def action_undo(self):
        p = next((x for x in reversed(self.pending) if not x.done), None)
        if p is None:
            msg = "Too late to undo: already committed" if self.last_committed else "Nothing to undo"
            self.notify(msg, timeout=2, severity="warning", markup=False)
            return
        if p.timer:
            p.timer.stop()
        self.pending.remove(p)
        p.done = True
        it = p.item
        if p.hide:
            self._count(p.kind, -1)
        p.audit["status"] = "undone"
        if p.kind == "due":
            self._clear_due_mark(it.key, redraw=False)
        self.record("undo", it, p.kind, "ok")
        self.current_key = it.key
        self.refilter(redraw=False)
        self.redraw(keep=it.key)
        self.notify(f"Restored {it.who}", timeout=2, title="Undone", markup=False)

    def action_approve(self):
        it = self.current
        if it is None:
            return
        if self._synthetic(it):
            return
        if self._rsvp_on(it):          # an invite: `a` accepts it
            return self._rsvp("accept", it)
        if it.kind == "ack":
            self._enqueue("ack", it, f"Acknowledged: {it.who} ({SOURCES.get(it.source, SOURCES['other'])[2]})")
            return
        if it.kind == "fyi":
            self.notify("FYI items have no draft. d to dismiss.", timeout=3, markup=False)
            return
        if it.kind == "nudge":
            return self.action_nudge()
        if not self.actions.supports_send:
            self.notify(NOT_YET, timeout=3, markup=False)
            return
        self._confirm_send("send", it, "Send email reply" if it.source == "email" else "Send reply")

    def action_nudge(self):
        it = self.current
        if it is None:
            return
        if self._synthetic(it):
            return
        if it.kind != "nudge":
            self.notify("n nudges items under 'Waiting on them'", timeout=3, markup=False)
            return
        if not self.actions.supports_send:
            self.notify(NOT_YET, timeout=3, markup=False)
            return
        text = None
        if not it.draft:
            fn = getattr(self.actions, "act_nudge_text", None)
            if fn is not None and self._acts_any() and getattr(
                    it, "origin", "") and self._acts_on_source(it):
                # 0.10.0: a row of an acts source with no stored draft falls
                # back to the `nudge_template` text; it goes through the same
                # confirm and send path (draft mode stays a Slack draft). The
                # text is only the confirm screen's: it is never put in the
                # row's draft, so cancelling leaves the row as it was (and D
                # is not blocked).
                text = fn(it)
            else:
                self.notify(f"Nudge not due yet (waiting {fmt_age(time.time() - it.ts)})", timeout=3, markup=False)
                return
        self._confirm_send("nudge", it, "Send nudge", text=text)

    def _real_send(self):
        """True when sends go to Slack (`send:` is configured), False in the
        demo, where they are only pretended."""
        return bool(getattr(self.actions, "reply_configured",
                            getattr(self.actions, "send_configured", False)))

    def _send_busy(self):
        """A send, nudge or draft is waiting out its undo window or running."""
        return (any(p.kind in SLACK_WRITE_KINDS and not p.done
                    for p in self.pending)
                or any(p.kind in SLACK_WRITE_KINDS for p in self.running))

    def _open_target(self, it):
        """The URL `o` opens for `it` (None when there is none)."""
        fn = getattr(self.actions, "open_target", None)
        try:
            return fn(it) if fn else None
        except Exception:
            return None

    def _reply_options(self):
        """The actions the confirm screen offers: draft, send, copy. The demo
        (a backend without `reply_options`) only pretends to send."""
        fn = getattr(self.actions, "reply_options", None)
        if not self._real_send() or fn is None:
            return ("send",)
        return tuple(fn())

    def _confirm_send(self, kind, it, verb, text=None):
        if self.editing:
            self.end_edit(save=True)      # send what the draft pane holds
        if text is None:
            text = it.draft
        if not text.strip():
            self.notify("No draft to send. Press e to write one.", timeout=3, severity="warning", markup=False)
            return
        dest, note, route = it.destination, "", ""
        if self._real_send():
            if self._send_busy():
                self.notify("A Slack write is already in progress; wait for "
                            "it (only one runs at a time).", timeout=4,
                            severity="warning", markup=False)
                return
            plan = self.actions.send_plan(it, text)
            if not plan.ok:
                self.record(kind, it, plan.error, "refused")
                self.notify(plan.error, timeout=6, severity="warning", markup=False)
                return
            text, dest, route = plan.text, plan.destination, plan.route
            if getattr(plan, "copy_only", False):
                # No usable destination, but the text is fine: only Copy for
                # Slack is offered (no precheck: nothing is sent or drafted).
                warn = plan.error if any(
                    o in ("draft", "send") for o in self._reply_options()) else ""
                self._open_confirm_send(kind, it, verb, text, dest, note, warn,
                                        route, only=("copy",))
                return

        if self._real_send() and getattr(self.actions, "precheck_configured", False):
            self._precheck_begin(kind, it, verb, text, dest, note, route)
            return
        self._open_confirm_send(kind, it, verb, text, dest, note, "", route)

    def _open_confirm_send(self, kind, it, verb, text, dest, note, warn="",
                           route="", only=None):
        options = tuple(o for o in self._reply_options()
                        if only is None or o in only)
        if not options:
            self.notify("Nothing to do: set send.mode / send.draft_tool or "
                        "copy: in inbox.yaml", timeout=5, severity="warning",
                        markup=False)
            return
        multi = options != ("send",)
        open_url = (self._open_target(it) or "") if multi else ""

        def after(choice):
            if choice == "send":
                self._enqueue(kind, it, f"{verb}: {dest}", text=text,
                              detail=f"{dest}: {text[:60]}")
            elif choice == "draft":
                self._enqueue("draft", it, f"Draft in Slack: {dest}",
                              text=text, hide=False, name="Draft",
                              detail=f"{dest}: {text[:60]}")
            elif choice == "copy":
                self._copy_for_slack(kind, it, text, dest)
            else:
                self.record(kind, it, "cancelled at confirm", "cancelled")
                self.notify(("Cancelled. Nothing sent." if not multi
                             else "Cancelled. Nothing sent, drafted or copied."),
                            timeout=2, markup=False)
        title = verb if not multi else (
            "Nudge in Slack" if kind == "nudge" else "Reply in Slack")
        self.push_screen(ConfirmSend(verb, dest, text, self.bidi, note, warn,
                                     route, options=options, open_url=open_url,
                                     title=title,
                                     on_open=lambda: self._open_item(it)), after)

    def _copy_for_slack(self, kind, it, text, dest):
        """Copy the draft as Slack mrkdwn (OSC 52). Purely local: no tool
        call, no undo window; the item stays."""
        converted, error = ("", "")
        fn = getattr(self.actions, "copy_text", None)
        if fn is None:
            converted = to_slack_mrkdwn(text)
        else:
            converted, error = fn(it, text)
        if error or not converted:
            self.record("copy", it, error or "nothing to copy", "refused")
            self.notify(error or "Nothing to copy", severity="warning",
                        timeout=5, markup=False)
            return
        self.copy_to_clipboard(converted)
        self.record("copy", it, f"{dest}: {converted[:60]}", "ok")
        self.notify(COPY_TOAST if self._open_target(it) else COPY_TOAST_NO_OPEN,
                    title="Copied for Slack", timeout=6, markup=False)

    # ---------- the optional send precheck (read-only advice) ----------
    def _precheck_begin(self, kind, it, verb, text, dest, note, route=""):
        """Run `send.precheck` in a thread (modal "checking", esc cancels the
        whole send), then open the confirm modal with its warning."""
        self._pre_gen += 1
        gen = self._pre_gen
        cancel = threading.Event()
        self._pre_op = {"gen": gen, "cancel": cancel}
        self._pre_screen = ActionBusy(
            verb, "Checking for a newer reply…",
            lambda: self._precheck_cancel(gen, kind, it))
        self.push_screen(self._pre_screen)
        self.busy += 1
        # Not a daemon thread: the command is stopped (SIGTERM, then SIGKILL)
        # when the check is cancelled or the app quits, so it ends.
        threading.Thread(
            target=self._precheck_thread,
            args=(kind, it, verb, text, dest, note, route, it.draft, gen,
                  cancel),
            name="inbox-precheck").start()

    def _precheck_cancel(self, gen, kind, it):
        op = self._pre_op
        if op is not None and op["gen"] == gen:
            op["cancel"].set()
            self._pre_op = None
            self._pre_screen = None
            self.record(kind, it, "cancelled at precheck", "cancelled")
            self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)

    def _precheck_thread(self, kind, it, verb, text, dest, note, route, draft,
                         gen, cancel):
        try:
            out = self.actions.send_precheck(it, text, cancel=cancel)
        except Exception:  # a backend bug must not end the session
            out = writes_mod.Precheck(failed=True)
        if self._quitting:
            return
        self._post(self._precheck_done, kind, it, verb, text, dest, note,
                   route, draft, gen, out)

    def _precheck_done(self, kind, it, verb, text, dest, note, route, draft,
                       gen, out):
        """UI thread: the check finished (a failed one never blocks the
        send). A cancelled one is dropped."""
        self.busy = max(0, self.busy - 1)
        if self._pre_op is None or self._pre_op["gen"] != gen:
            return
        self._pre_op = None
        screen, self._pre_screen = self._pre_screen, None
        if screen is not None and self.screen is screen:
            self.pop_screen()
        live = self._live_item(it.key)
        if live is None or live is not self.current or live.draft != draft:
            self.record(kind, it, "draft or selection changed", "discarded")
            self.notify("The draft or the selection changed while checking; "
                        "nothing was sent.", severity="warning", timeout=5,
                        markup=False)
            return
        if self._send_busy():
            self.notify("A Slack write is already in progress; wait for it.",
                        timeout=4, severity="warning", markup=False)
            return
        warn = out.warn or (writes_mod.PRECHECK_FAILED if out.failed else "")
        if warn:
            self.record(kind, it, f"precheck: {warn}",
                        "check failed" if out.failed and not out.warn else "warned")
        self._open_confirm_send(kind, it, verb, text, dest, note, warn, route)

    # ---------- react and polish (0.6.0, opt-in) ----------
    def action_react(self):
        """E: pick an emoji, confirm, wait out the undo window, then add the
        reaction to the item's message through the configured tool."""
        it = self.current
        if it is None:
            return
        if self._synthetic(it):
            return
        if not getattr(self.actions, "react_configured", False):
            self.notify(REACT_OFF, timeout=3, severity="warning", markup=False)
            return
        target = self.actions.react_plan(it)
        if target is None:
            self.notify("This item has no message to react to "
                        "(meta.reply_to.message_ts or thread_ts)", timeout=4,
                        severity="warning", markup=False)
            return
        if any(p.kind == "react" and p.item.key == it.key and not p.done
               for p in self.pending) or any(
                p.kind == "react" and p.item.key == it.key for p in self.running):
            self.notify("react already in progress", timeout=2, markup=False)
            return
        emojis, suggested = self.actions.react_choices(it)
        choices = [{"id": e, "label": f":{e}:",
                    "detail": "suggested" if e == suggested else ""}
                   for e in emojis]

        def picked(chosen):
            if chosen is None:
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
                return
            self._confirm_react(it, chosen["id"], target)
        self.push_screen(ChoicePicker("React to message", choices, self.bidi,
                                      selected=suggested), picked)

    def _confirm_react(self, it, emoji, target):
        rows = [("Where", it.where), ("From", it.who),
                ("Message", it.gist), ("Time stamp", target[1]),
                ("Reaction", f":{emoji}:")]

        def after(ok):
            if ok:
                self._enqueue("react", it, f"React :{emoji}: {it.who}",
                              hide=False, detail=f":{emoji}:", name="React",
                              choice=emoji,
                              mark_done=bool(self.actions.react_mark_done))
            else:
                self.record("react", it, "cancelled at confirm", "cancelled")
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
        self.push_screen(ConfirmWrite(
            "React to message", rows, "React",
            "Adds the reaction as you. Nothing else is posted.",
            self.bidi), after)

    def action_draft_me(self):
        """D: a draft reply for me, from the thread. One no-tools bridge call
        in a worker (the UI stays live; the result is kept in the store so an
        unchanged item shows it again). It fills the Draft pane and never
        sends."""
        it = self.current
        if it is not None and tasksmod.is_task(it):
            return self._task_done(it, outcome=True)
        if it is not None and views.is_review(it):
            return self._review_save(it)
        if it is None or self._synthetic(it):
            return
        if "reply" not in it.actions and "nudge" not in it.actions:
            self.notify("D drafts a reply: this row has none to write",
                        timeout=3, severity="warning", markup=False)
            return
        if not getattr(self.actions, "draft_me_configured", False):
            self.notify("Draft for me needs summary: or polish: in inbox.yaml "
                        "(its model and budget)", timeout=4,
                        severity="warning", markup=False)
            return
        if it.key in self._draft_running:
            self.notify("A draft is already being written for this row",
                        timeout=2, markup=False)
            return
        if self.editing:
            self.end_edit(save=True)
        cached = self.actions.cached_draft_me(it)
        mine = bool(cached) and it.draft == cached
        if it.draft and it.draft.strip() and not mine:
            self.notify("This row already has a draft: edit it (e) or polish "
                        "it (P). D never overwrites one.", timeout=5,
                        severity="warning", markup=False)
            return
        if cached and not mine:
            self._draft_filled(it, cached, True)
            return
        self._draft_running.add(it.key)
        self.busy += 1
        self.set_badge(it, "drafting for you…")
        threading.Thread(target=self._draft_thread, args=(it, mine, it.draft),
                         name="inbox-draft-me").start()

    def _draft_thread(self, it, force, base):
        try:
            out = self.actions.draft_for_me(it, force=force)
        except Exception as exc:  # a backend bug must not end the session
            out = draftme_mod.Outcome(False, "", f"cannot run: {type(exc).__name__}")
        if self._quitting:
            return
        self._post(self._draft_done, it, out, base)

    def _draft_done(self, it, out, base):
        self.busy = max(0, self.busy - 1)
        self._draft_running.discard(it.key)
        live = self._live_item(it.key)
        if not out.ok:
            self.record("draft-me", it, out.detail, "failed")
            self.notify(f"Draft for me failed: {out.detail}", severity="warning",
                        timeout=6, markup=False)
            if live is not None and live is self.current:
                self.set_badge(live, "")
            return
        if live is None:
            self.record("draft-me", it, "item gone", "discarded")
            return
        if live.draft and live.draft.strip() and live.draft != base:
            self.record("draft-me", live, "a draft appeared meanwhile",
                        "discarded")
            self.notify("The row got a draft while this one was written; "
                        "kept yours.", timeout=4, severity="warning",
                        markup=False)
            return
        self._draft_filled(live, out.text, out.cached)

    def _draft_filled(self, it, text, cached):
        it.draft, it.streamed, it.drafted_by = text, True, "model"
        self.record("draft-me", it, f"{len(text)} chars"
                    + (" (kept)" if cached else ""), "filled")
        if it is self.current:
            self.query_one("#draft-view", Static).update(
                self.draft_text(it, it.draft, False))
            self.set_badge(it, "from the store" if cached else "drafted")
            self.update_hints()
        self.notify("Draft filled in. Nothing was sent." + (
            " D again writes a new one." if cached else ""), timeout=4,
            markup=False)

    def action_polish(self):
        """P: rewrite the draft in your tone (tone guidance from the
        configured tool, then a no-tools rewrite). Shows old and new text:
        accept, edit or cancel. Never sends anything."""
        it = self.current
        if it is None:
            return
        if self._synthetic(it):
            return
        if not getattr(self.actions, "polish_configured", False):
            self.notify(POLISH_OFF, timeout=3, severity="warning", markup=False)
            return
        if self._polish_running:
            self.notify("A polish is already running", timeout=2, markup=False)
            return
        if self.editing:
            self.end_edit(save=True)
        draft = it.draft
        if not draft or not draft.strip():
            self.notify("No draft to polish", timeout=3, severity="warning",
                        markup=False)
            return
        self._polish_gen += 1
        gen = self._polish_gen
        self._polish_op = {"gen": gen}
        self._polish_running = True
        self._polish_screen = ActionBusy(
            "Polish draft", "Fetching the tone, then rewriting…",
            lambda: self._polish_cancel(gen))
        self.push_screen(self._polish_screen)
        self.busy += 1
        # Not a daemon thread: the bridge calls must reach their `finally` so
        # the spend is logged. Quitting while it runs asks first (see
        # action_quit) and waits, for at most polish.timeout_s per call.
        threading.Thread(target=self._polish_thread, args=(it, draft, gen),
                         name="inbox-polish").start()

    def _polish_cancel(self, gen):
        op = self._polish_op
        if op is not None and op["gen"] == gen:
            self._polish_op = None
            self._polish_screen = None
            self.notify("Cancelled. The draft is unchanged.", timeout=2,
                        markup=False)

    def _polish_thread(self, it, draft, gen):
        try:
            out = self.actions.polish(it, draft)
        except Exception as exc:  # a backend bug must not end the session
            out = polishmod.Outcome(False, "", f"cannot run: {type(exc).__name__}")
        if self._quitting:
            return
        self._post(self._polish_done, it, draft, gen, out)

    def _live_item(self, key):
        return next((i for i in self.items if i.key == key), None)

    def _polish_done(self, it, draft, gen, out):
        """UI thread: the polish finished. A cancelled one is dropped."""
        self.busy = max(0, self.busy - 1)
        self._polish_running = False
        if self._polish_op is None or self._polish_op["gen"] != gen:
            return
        self._polish_op = None
        screen, self._polish_screen = self._polish_screen, None
        if screen is not None and self.screen is screen:
            self.pop_screen()
        if not out.ok:
            self.record("polish", it, out.detail, "failed")
            self.notify(f"Polish failed: {out.detail}", severity="warning",
                        timeout=6, markup=False)
            return
        live = self._live_item(it.key)
        if live is None or live is not self.current or live.draft != draft:
            self.record("polish", it, "draft or selection changed", "discarded")
            self.notify("The draft or the selection changed while polishing; "
                        "the result was discarded.", severity="warning",
                        timeout=5, markup=False)
            return

        def chosen(choice):
            now = self._live_item(it.key)
            if choice is None:
                self.record("polish", it, "cancelled at review", "cancelled")
                self.notify("Cancelled. The draft is unchanged.", timeout=2,
                            markup=False)
                return
            if now is None or now is not self.current or now.draft != draft:
                self.record("polish", it, "draft or selection changed",
                            "discarded")
                self.notify("The draft or the selection changed; the result "
                            "was discarded.", severity="warning", timeout=4,
                            markup=False)
                return
            if choice == "accept":
                now.draft, now.streamed = out.text, True
                self.record("polish", now, f"{len(out.text)} chars", "accepted")
                self.query_one("#draft-view", Static).update(
                    self.draft_text(now, now.draft, False))
                self.set_badge(now, "polished")
                self.update_hints()
                self.notify("Draft replaced. Nothing was sent.", timeout=3,
                            markup=False)
            else:
                self.record("polish", now, f"{len(out.text)} chars", "edit")
                self._open_editor(out.text)
        self.push_screen(PolishScreen(draft, out.text, self.bidi,
                                      out.tone_cached), chosen)

    # ---------- RSVP and save (the first writes) ----------
    def action_rsvp_decline(self):
        self._rsvp("decline")

    def action_rsvp_tentative(self):
        self._rsvp("tentative")

    def _rsvp(self, key, it=None):
        """a / X / T on a pending invite: confirm, wait out the undo window,
        then answer through the bridge."""
        it = it or self.current
        if it is None:
            return
        if self._synthetic(it):
            return
        if not getattr(self.actions, "rsvp_configured", False):
            self.notify(RSVP_OFF, timeout=3, severity="warning", markup=False)
            return
        target = invites.rsvp_target(it)
        if target is None:
            self.notify("Not a pending calendar invite", timeout=2, markup=False)
            return
        _event_id, _calendar_id, recurring = target
        word, answer, title = RSVP_KEYS[key]
        when = it.meta.get("when")
        name = it.meta.get("title")
        rows = [("Event", name if isinstance(name, str) and name else it.gist),
                ("When", when if isinstance(when, str) else ""),
                ("From", it.who if it.who != "calendar invite" else ""),
                ("Answer", f"{word} ({answer})")]
        note = ("Recurring: answers the next instance only, not the series."
                if recurring else "")

        def after(ok):
            if ok:
                label = f"RSVP {answer}: {it.meta.get('title') or it.who}"
                self._enqueue("rsvp", it, label, answer=answer, detail=answer)
            else:
                self.record("rsvp", it, "cancelled at confirm", "cancelled")
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
        self.push_screen(ConfirmWrite(title, rows, word.capitalize(), note, self.bidi), after)

    def _confirm_save_anyway(self, it, res):
        cand = res.candidate or {}
        score = cand.get("score")
        rows = [("Said", res.detail), ("Match", cand.get("title") or ""),
                ("Match id", cand.get("id") or ""),
                ("Score", f"{score:g}" if score is not None else "")]

        def after(ok):
            self.dups.pop(it.key, None)
            if ok:
                self._enqueue("save", it, f"Save anyway: {it.who}", hide=False,
                              detail="save anyway", force=True)
            else:
                self.record("save", it, "duplicate: not saved", "cancelled")
                self.notify("Not saved: looks like a duplicate.", timeout=3, markup=False)
        self.push_screen(ConfirmWrite(
            "Possible duplicate", rows, "Save anyway",
            "Saving anyway adds another memory.", self.bidi), after)

    def _review_save(self, it):
        """D on a REVIEW row: copy a command-line hint for saving the skipped
        capture anyway. It never runs the `save:` command: that command gets
        this row (the review text), not the captured content, and the event
        does not carry the body. A real re-save needs the daemon to recapture
        the source."""
        title = str(it.meta.get("title") or it.who)
        sid = str(it.meta.get("source_id") or "")
        self.copy_to_clipboard(review_cli_hint(sid, title))
        self.record("save", it, "duplicate review: CLI hint copied",
                    "cancelled")
        self.notify("Copied a save hint (a command template: it needs the "
                    "entry text). Nothing was saved.", timeout=6,
                    markup=False)

    def _confirm_thread_saved(self, it):
        """The item's Slack thread is already in memory, this message may not
        be: the same choice as a likely duplicate."""
        rows = [("Said", "The thread is already in memory"),
                ("Thread memory", it.thread_memory_id), ("Gist", it.gist)]

        def after(ok):
            if ok:
                self._enqueue("save", it, f"Save anyway: {it.who}", hide=False,
                              detail="save anyway", force=True)
            else:
                self.record("save", it, "thread in memory: not saved",
                            "cancelled")
                self.notify("Not saved: the thread is already in memory.",
                            timeout=3, markup=False)
        self.push_screen(ConfirmWrite(
            "Thread already in memory", rows, "Save anyway",
            "Saving anyway adds another memory.", self.bidi), after)

    def action_save_memory(self):
        """m: save the item to memory through the configured command."""
        it = self.current
        if it is None:
            return
        if tasksmod.is_task(it):      # a task is not saved: m reminds
            return self.action_remind()
        if self._synthetic(it):
            return
        if views.is_review(it):
            return      # not bound here: D copies a save hint
        if it.memory_id:      # the daemon (or an earlier save) already did it
            self.notify(f"Already in memory ({it.memory_id})", timeout=4,
                        markup=False)
            return
        if not getattr(self.actions, "save_configured", False):
            self.notify(SAVE_OFF, timeout=3, severity="warning", markup=False)
            return
        if any(p.kind == "save" and p.item.key == it.key and not p.done
               for p in self.pending) or any(
                p.kind == "save" and p.item.key == it.key for p in self.running):
            self.notify("save already in progress", timeout=2, markup=False)
            return
        if it.key in self.dups:        # the command asked about a duplicate
            self._confirm_save_anyway(*self.dups[it.key])
            return
        if it.thread_memory_id:        # only the thread is in memory
            self._confirm_thread_saved(it)
            return
        label = SOURCES.get(it.source, SOURCES["other"])[2]
        rows = [("Source", f"{label} · {it.where}"), ("From", it.who),
                ("Gist", it.gist), ("Link", it.link or "")]

        def after(ok):
            if ok:
                self._enqueue("save", it, f"Save to memory: {it.who}", hide=False,
                              detail=it.gist[:60])
            else:
                self.record("save", it, "cancelled at confirm", "cancelled")
                self.notify("Cancelled. Nothing saved.", timeout=2, markup=False)
        self.push_screen(ConfirmWrite("Save to memory", rows, "Save",
                                      "The item is passed to your save command as JSON on stdin.",
                                      self.bidi), after)

    # ---------- custom actions (the `actions:` of inbox.yaml) ----------
    def action_custom_action(self, index):
        """The key of an `actions:` entry: look up the choices (read-only),
        pick one, prepare (read-only, a fresh read), confirm, wait out the undo
        window, then run once."""
        if not 0 <= index < len(self._custom):
            return
        cfg = self._custom[index]
        it = self.current
        if it is None:
            self.notify("Nothing selected", timeout=2, markup=False)
            return
        if self._synthetic(it):
            return
        if self._act_op is not None:
            self.notify("An action lookup is already running", timeout=2,
                        markup=False)
            return
        if any(p.kind == "action" and p.item.key == it.key and not p.done
               for p in self.pending) or any(
                p.kind == "action" and p.item.key == it.key
                for p in self.running):
            self.notify("action already in progress", timeout=2, markup=False)
            return
        self._act_begin("choices", cfg, it, None)

    def _act_begin(self, phase, cfg, it, choice):
        """Start a read-only lookup in a thread and show the busy modal."""
        self._act_gen += 1
        gen = self._act_gen
        cancel = threading.Event()
        self._act_op = {"gen": gen, "cancel": cancel}
        what = ("Looking up choices…" if phase == "choices"
                else "Preparing (reading fresh data)…")
        self._act_screen = ActionBusy(cfg["label"], what,
                                      lambda: self._act_cancel(gen))
        self.push_screen(self._act_screen)
        self.busy += 1
        # Not a daemon thread: the command is stopped (SIGTERM, then SIGKILL)
        # when the lookup is cancelled or the app quits, so it ends.
        threading.Thread(target=self._act_thread,
                         args=(phase, cfg, it, choice, gen, cancel),
                         name=f"inbox-action-{phase}").start()

    def _act_cancel(self, gen):
        op = self._act_op
        if op is not None and op["gen"] == gen:
            op["cancel"].set()
            self._act_op = None
            self._act_screen = None
            self.notify("Cancelled. Nothing changed.", timeout=2, markup=False)

    def _act_thread(self, phase, cfg, it, choice, gen, cancel):
        name = cfg["label"]
        try:
            if phase == "choices":
                out = self.actions.action_choices(name, it, cancel=cancel)
            else:
                out = self.actions.action_prepare(name, it, choice["id"],
                                                  cancel=cancel)
        except Exception as exc:  # a backend bug must not end the session
            why = f"cannot run: {type(exc).__name__}"
            out = (customactions.Choices(False, [], why) if phase == "choices"
                   else customactions.Prepared(False, [], "", why, False))
        if self._quitting:
            return
        self._post(self._act_result, phase, cfg, it, choice, gen, out)

    def _act_close_busy(self):
        screen, self._act_screen = self._act_screen, None
        if screen is not None and self.screen is screen:
            self.pop_screen()

    def _act_result(self, phase, cfg, it, choice, gen, out):
        """UI thread: a lookup finished. A cancelled one is dropped."""
        self.busy = max(0, self.busy - 1)
        if self._act_op is None or self._act_op["gen"] != gen:
            return
        self._act_op = None
        self._act_close_busy()
        name = cfg["label"]
        if phase == "choices":
            if not out.ok:
                self.record("action", it, f"{name}: {out.message}", "failed")
                self.notify(f"{name}: {out.message}", severity="warning",
                            timeout=5, markup=False)
                return

            def picked(chosen):
                if chosen is None:
                    self.notify("Cancelled. Nothing changed.", timeout=2,
                                markup=False)
                    return
                self._act_begin("prepare", cfg, it, chosen)
            self.push_screen(ChoicePicker(name, out.choices, self.bidi), picked)
            return
        if out.duplicate:       # checked before ok: not an error
            msg = out.message or "Nothing to add"
            self.record("action", it, f"{name}: {msg}", "duplicate")
            self.notify(msg, title="Already there", timeout=5, markup=False)
            return
        if not out.ok:
            msg = out.message or "failed"
            self.record("action", it, f"{name}: {msg}", "failed")
            self.notify(f"{name}: {msg}", severity="error", timeout=6,
                        markup=False)
            return
        rows = list(out.rows) or [("Action", name), ("From", it.who)]
        detail = f"{name}: {choice['label']}"

        def after(ok):
            if ok:
                self._enqueue(
                    "action", it, f"{name}: {it.who}", hide=False,
                    detail=detail, name=name, choice=choice["id"],
                    state=out.state,
                    mark_done=bool(self.actions.action_mark_done(name)))
            else:
                self.record("action", it, f"{detail}: cancelled at confirm",
                            "cancelled")
                self.notify("Cancelled. Nothing changed.", timeout=2,
                            markup=False)
        self.push_screen(ConfirmWrite(
            name, rows, "Confirm",
            "The item is passed to your action command as JSON on stdin.",
            self.bidi), after)

    def _outcome_on(self, it):
        """0.13.1: `d` asks for an outcome on a Slack ask (I OWE) or a waiting
        row of a source that lists `close` and declared `close_outcome: true`.
        Every other row (FYI, feed, RSVP, alert, review, digest, saved, agent
        card, task) keeps its `d`."""
        if (it is None or tasksmod.is_task(it) or views.is_review(it)
                or views.is_synthetic(it)
                or it.kind not in ("reply", "nudge")
                or it.section not in ("needs_reply", "urgent", "waiting")
                or (isinstance(it.meta, dict)
                    and it.meta.get("kind") == "invite")):
            return False
        fn = getattr(self.actions, "close_outcome_enabled", None)
        try:
            return bool(fn and fn(it))
        except Exception:
            return False

    def _ask_outcome(self, it):
        """d on an outcome row: the `Outcome:` prompt opens at once; one
        background no-tools call may fill it in (never over typed text). An
        empty answer is refused, esc closes nothing. The text goes to the
        source as `close` `{"outcome": <text>}` after the undo window."""
        drafting = bool(getattr(self.actions, "draft_me_configured", False))
        screen = OutcomeScreen(it.who, actsmod.NOTE_MAX, drafting=drafting)

        def answered(text):
            if text is None:
                self.notify("Cancelled. Nothing closed.", timeout=2,
                            markup=False)
                return
            self._enqueue("done", it, f"Done: {it.who}",
                          detail=text[:60], act="close",
                          act_args={"outcome": actsmod.clean_note(text)},
                          react=self._done_emoji(it))
        self.push_screen(screen, answered)
        if drafting:
            threading.Thread(target=self._outcome_thread, args=(it, screen),
                             name="inbox-outcome", daemon=True).start()

    def _done_emoji(self, it):
        fn = getattr(self.actions, "done_reaction_for", None)
        try:
            return (fn(it) if fn else None) or ""
        except Exception:
            return ""

    def _outcome_thread(self, it, screen):
        try:
            out = self.actions.draft_outcome(it)
        except Exception as exc:  # a backend bug must not end the session
            out = outcome_mod.Outcome(False, "", f"cannot run: "
                                                 f"{type(exc).__name__}")
        if self._quitting:
            return
        self._post(self._outcome_drafted, it, screen, out)

    def _outcome_drafted(self, it, screen, out):
        """UI thread: fill the prompt if it is still open and untouched."""
        if screen not in self.screen_stack:
            return
        if out.ok and screen.fill(out.text):
            self.record("outcome-draft", it, f"{len(out.text)} chars",
                        "filled")
        else:
            screen.stop_drafting()
            if not out.ok:
                self.record("outcome-draft", it, out.detail, "failed")

    def action_done(self):
        it = self.current
        if it is None:
            return
        if tasksmod.is_task(it):
            return self._task_done(it, outcome=False)
        if self._outcome_on(it):
            return self._ask_outcome(it)
        # 0.10.0: a row whose source lists `close` also closes there, and with
        # `done_reaction:` the message gets its emoji. Neither runs before the
        # undo window ends; with neither configured this is the old `d`.
        act = "close" if self._acts_on(it, "close") else ""
        fn = getattr(self.actions, "done_reaction_for", None)
        emoji = ""
        try:
            emoji = (fn(it) if fn else None) or ""
        except Exception:
            emoji = ""
        self._enqueue("done", it, f"{'Dismissed' if it.kind == 'fyi' or views.is_review(it) else 'Done'}: {it.who}",
                      act=act, react=emoji)

    def action_snooze(self):
        it = self.current
        if it is None:
            return
        if tasksmod.is_task(it):
            return self._task_due(it)

        def after(res):
            if res:
                until, label = res
                act = "snooze" if self._acts_on(it, "snooze") else ""
                self._enqueue("snooze", it, f"Snoozed {label}: {it.who}", until=until,
                              detail=label, act=act,
                              act_args={"until": until} if act else None)
        self.push_screen(SnoozePicker(it.who), after)

    def action_open(self):
        self._open_item(self.current)

    def _open_item(self, it):
        """Open `it`'s URL (the Slack template URL or its link) and audit it
        for that item, whatever is selected now. `open -a` can take seconds,
        so the open runs in a worker thread, never on the UI loop."""
        if it is None:
            return
        if tasksmod.is_task(it) and not it.link:
            self.notify("no link", timeout=3, markup=False)
            return
        self.run_worker(lambda: self._open_in_thread(it), thread=True,
                        group="open")

    def _open_in_thread(self, it):
        try:
            res = self.actions.open_link(it)
        except Exception as exc:  # a backend bug must not end the session
            res = Result(False, f"{type(exc).__name__}: {exc}")
        self._post(self._open_done, it, res)

    def _open_done(self, it, res):
        self.record("open", it, res.detail, "ok" if res.ok else "failed")
        self.notify(res.detail if res.ok else f"Not opened: {res.detail}", timeout=3,
                    severity="information" if res.ok else "warning", markup=False)

    def action_refresh(self):
        """r polls every source, except in the Context pane, where it
        re-fetches that item's context (like C)."""
        f = self.focused
        if self.context_open and f is not None and f.id == "context":
            self.action_context_refresh()
            return
        self.poll(manual=True)
        self.health_refresh()

    # ---------- context pane and AI summary (read-only) ----------
    def _ctx_on(self):
        return bool(getattr(self.actions, "context_configured", False))

    def _sum_on(self):
        return bool(getattr(self.actions, "summary_configured", False))

    def _ident(self, it):
        return (it.key, it.ts) if it is not None else None

    def _summary_key(self, it):
        return self.actions.summary_key(it)

    def action_context(self):
        """c: show or hide the Context pane. An open pane always closes;
        opening needs `context:`."""
        if self.context_open:
            self._set_context_open(False)
            return
        if not self._ctx_on():
            self.notify(CONTEXT_OFF, timeout=3, severity="warning", markup=False)
            return
        self._set_context_open(True)

    def _set_context_open(self, on):
        if on == self.context_open:
            return
        self.context_open = on
        pane = self.query_one("#context", ContextPane)
        pane.display = on
        self._stop_ctx_timer()
        self._ctx_gen += 1
        self._ctx_shown = None
        self._ctx_pending = []
        self._ctx_sig = None
        f = self.focused
        if not on and f is not None and f.id == "context":
            self.query_one("#queue").focus()
        if on:
            self.context_sync(immediate=True)
        self.update_hints()

    def _stop_ctx_timer(self):
        if self._ctx_timer is not None:
            self._ctx_timer.stop()
            self._ctx_timer = None

    def context_sync(self, immediate=False):
        """Make the open pane follow the selection. A selection that has no
        fresh result starts a fetch after the debounce, and a newer selection
        replaces that wait; what is cached shows at once."""
        if not self.context_open:
            return
        it = self.current
        ident = self._ident(it)
        if ident == self._ctx_shown:
            return self.context_render()
        self._ctx_shown = ident
        self._ctx_gen += 1
        self._stop_ctx_timer()
        # A fetch still waiting for a free slot is for an item that is no
        # longer selected: it never starts.
        self._ctx_pending = [p for p in self._ctx_pending
                             if self._ident(p) == ident]
        if (it is not None and self._ctx_on()
                and not views.is_synthetic(it) and ident not in self._ctx_loading
                and self.ctx_cache.get(*ident) is None):
            if immediate or self.context_debounce_s <= 0:
                self._ctx_start(it)
            else:
                gen = self._ctx_gen
                self._ctx_timer = self.set_timer(
                    self.context_debounce_s, lambda: self._ctx_fire(it, gen))
        self.context_render()

    def _ctx_fire(self, it, gen):
        self._ctx_timer = None
        if gen == self._ctx_gen and self.context_open:
            self._ctx_start(it)

    def _ctx_start(self, it):
        """Fetch the context of `it` in a worker thread, at most
        CONTEXT_MAX_FETCHES at once; the rest wait for a slot."""
        ident = self._ident(it)
        if ident in self._ctx_loading or any(
                self._ident(p) == ident for p in self._ctx_pending):
            return
        if len(self._ctx_loading) >= CONTEXT_MAX_FETCHES:
            self._ctx_pending.append(it)
        else:
            self._ctx_spawn(it, ident)
        self.context_render()

    def _ctx_spawn(self, it, ident):
        self._ctx_loading.add(ident)
        # Not a daemon thread: quitting sets `_cancel` and the command is
        # stopped (SIGTERM, then SIGKILL after the grace), so the exit waits
        # a few seconds at most and no child is left running.
        threading.Thread(target=self._ctx_thread, args=(it, ident),
                         name="inbox-context-fetch").start()

    def _post(self, fn, *args):
        """Run `fn(*args)` on the UI thread, from a context or summary thread,
        without ever blocking. (`call_from_thread` waits for the UI loop to run
        the call; if the app has already gone, that wait never ends, and these
        threads are not daemons, so the process would never exit.) Returns
        False, and does nothing, when the app is quitting or gone."""
        loop = self._loop
        if self._quitting or not self.is_running or loop is None \
                or loop.is_closed():
            return False

        def run():
            if self._quitting or not self.is_running:
                return
            with self._context():
                try:
                    fn(*args)
                except NoMatches:   # a widget went away during shutdown
                    pass
        try:
            loop.call_soon_threadsafe(run)
        except RuntimeError:        # the loop closed between the check and now
            return False
        return True

    def _ctx_thread(self, it, ident):
        try:
            out = self.actions.fetch_context(it, cancel=self._cancel)
        except Exception as exc:  # a backend bug must not end the session
            out = ctxmod.Outcome(False, [], f"cannot run: {type(exc).__name__}")
        if self._quitting:
            return
        self._post(self._ctx_done, ident, out)

    def _ctx_done(self, ident, out):
        """UI thread. The result is cached under the item it was fetched for;
        it is drawn only if that item is still the one shown."""
        self._ctx_loading.discard(ident)
        self.ctx_cache.put(*ident, out)
        shown = self._ctx_shown
        while self._ctx_pending and len(self._ctx_loading) < CONTEXT_MAX_FETCHES:
            nxt = self._ctx_pending.pop(0)
            nid = self._ident(nxt)
            if (((self.context_open and nid == shown)
                 or self.remind_key == nid[0])
                    and self.ctx_cache.get(*nid) is None
                    and nid not in self._ctx_loading):
                self._ctx_spawn(nxt, nid)
        if self.remind_key == ident[0] and self.current_key == ident[0]:
            self.show_thread_only()      # the "Remind me" card has its answer
        self.context_render()

    # ---------- "Remind me" on a task (0.12.3, read-only) ----------
    def action_remind(self):
        """m on a task: fill the Detail pane with where the task came from,
        the original words, the last activity and the related items (the
        context command, in a worker, with a loading line). m or esc closes."""
        it = self.current
        if it is None or not tasksmod.is_task(it):
            self.notify(REMIND_TASK_ONLY, timeout=3, markup=False)
            return
        if self.remind_key == it.key:
            self.remind_key = None
        else:
            self.remind_key = it.key
        self.show_thread_only()
        self.update_hints()

    def _remind_view(self, it):
        """What the "Remind me" card draws: the related items' state. The
        context command is read-only; its answer is cached like the Context
        pane's (so the pane and this card share one fetch)."""
        if not self._ctx_on():
            return {"ctx": {"state": "off"}}
        ident = self._ident(it)
        if (self.ctx_cache.get(*ident) is None
                and ident not in self._ctx_loading
                and not any(self._ident(p) == ident for p in self._ctx_pending)):
            self._ctx_start(it)
        entry = self.ctx_cache.get(*ident, stale=True)
        if entry is None:
            return {"ctx": {"state": "loading"}}
        return {"ctx": dict(entry, state="ready" if entry["ok"] else "error",
                            message=entry["message"] or "")}

    def context_tick(self):
        """Every second: an entry of the shown item that has expired is
        fetched once more (the old rows stay drawn, marked stale, until the
        answer arrives); then redraw if the pane's picture changed. A failed
        fetch is itself cached for 30 s, so a broken command is retried at
        most that often."""
        if not self.context_open:
            return
        it = self.current
        if (it is not None and self._ctx_on()
                and not views.is_synthetic(it)
                and self._ident(it) == self._ctx_shown):
            ident = self._ident(it)
            if (self.ctx_cache.get(*ident) is None
                    and ident not in self._ctx_loading
                    and self._ctx_timer is None
                    and not any(self._ident(p) == ident for p in self._ctx_pending)):
                self._ctx_start(it)
        self.context_render()

    def action_context_refresh(self):
        """C (or r inside the pane): fetch this item's context again."""
        if not self._ctx_on():
            self.notify(CONTEXT_OFF, timeout=3, severity="warning", markup=False)
            return
        if not self.context_open:
            self.notify("Context pane is closed (c opens it)", timeout=2, markup=False)
            return
        it = self.current
        if it is None:
            return
        if self._synthetic(it):
            return
        ident = self._ident(it)
        if ident in self._ctx_loading:
            self.notify("Context is already loading", timeout=2, markup=False)
            return
        self._stop_ctx_timer()
        self.ctx_cache.drop(*ident)
        self._ctx_start(it)

    def context_view(self):
        """What the pane draws: the item, its context state and its summary."""
        it = self.current
        ident = self._ident(it)
        ctx = {"state": "off"}
        if (it is not None and self._ctx_on()
                and not views.is_synthetic(it)):
            entry = self.ctx_cache.get(*ident, stale=True)
            busy = (ident in self._ctx_loading or self._ctx_timer is not None
                    or any(self._ident(p) == ident for p in self._ctx_pending))
            if entry is not None:
                ctx = dict(entry, state="ready" if entry["ok"] else "error",
                           message=entry["message"] or "",
                           refreshing=entry["stale"] and busy)
            elif busy:
                ctx = {"state": "loading"}
            else:
                ctx = {"state": "idle"}
        summ = {"state": "off"}
        if it is not None and self._sum_on():
            summ = self._summary_state(it, ctx)
        return {"item": it, "ident": ident, "context": ctx, "summary": summ}

    def _summary_state(self, it, ctx):
        skey = self._summary_key(it)
        view = self.summaries.get(skey)
        if view is None or view["state"] == "none":
            cached = self.actions.cached_summary(it)
            if cached is None:
                return {"state": "none"}
            view = {"state": "ready", "text": cached.text, "at": cached.at,
                    "cached": True, "ctx": cached.with_context, "detail": ""}
        view = dict(view, note="")
        if (view["state"] == "ready" and not view.get("ctx")
                and ctx.get("state") == "ready" and ctx.get("sections")):
            view["note"] = "written without the context above (I to redo)"
        return view

    @staticmethod
    def _view_signature(view, now):
        """Everything that changes what the pane draws: its state, the data
        and the age labels (per minute), so an unchanged pane is not rebuilt
        (a rebuild would drop a mouse selection)."""
        ctx, summ = view["context"], view["summary"]
        loading = (ctx["state"] == "loading" or ctx.get("refreshing")
                   or summ["state"] == "loading")
        parts = [view["ident"], ctx["state"], ctx.get("at"), ctx.get("stale"),
                 ctx.get("refreshing"), ctx.get("message"),
                 summ["state"], summ.get("at"), summ.get("text"),
                 summ.get("note"), summ.get("detail"), summ.get("cached"),
                 int(now) if loading else None]
        for at in (ctx.get("at"), summ.get("at")):
            parts.append(int((now - at) // 60) if at else None)
        return tuple(parts)

    def context_render(self):
        if not self.context_open:
            return
        try:
            pane = self.query_one("#context", ContextPane)
        except NoMatches:  # a timer fired while the app is shutting down
            return
        now = time.time()
        view = self.context_view()
        sig = (self._view_signature(view, now), self.theme, self.bidi_mode,
               pane.size.width)
        if sig == self._ctx_sig:
            return
        try:
            pane.show(view, self.pal, now, self.bidi)
        except NoMatches:   # the pane's body is already gone (shutting down)
            return
        self._ctx_sig = sig

    def action_summary(self, force=False):
        """i: AI summary of the item (cached on disk); I: ask again."""
        if not self._sum_on():
            self.notify(SUMMARY_OFF, timeout=3, severity="warning", markup=False)
            return
        it = self.current
        if it is None:
            self.notify("Nothing selected", timeout=2, markup=False)
            return
        if self._synthetic(it):
            return
        self._set_context_open(True)
        skey = self._summary_key(it)
        if not force:
            # A cached summary needs no call, so it shows even while another
            # summary is running.
            cached = self.actions.cached_summary(it, touch=True)
            if cached is not None:
                self.summaries[skey] = {
                    "state": "ready", "text": cached.text, "at": cached.at,
                    "cached": True, "ctx": cached.with_context, "detail": ""}
                self.context_render()
                return
        if self._sum_running is not None:
            self.notify("A summary is already running", timeout=2, markup=False)
            return
        entry = self.ctx_cache.get(*self._ident(it)) if self._ctx_on() else None
        sections = entry["sections"] if entry and entry["ok"] else []
        self._sum_running = skey
        self.summaries[skey] = {"state": "loading", "text": "", "at": 0,
                                "cached": False, "ctx": False, "detail": ""}
        self.busy += 1
        # Not a daemon thread: the bridge call must reach its `finally` so the
        # spend is logged, and its child must not be orphaned. Quitting while
        # it runs asks first (see action_quit) and then waits for it, for at
        # most summary.timeout_s.
        threading.Thread(target=self._summary_thread,
                         args=(it, skey, sections, force),
                         name="inbox-summary").start()
        self.context_render()

    def _summary_thread(self, it, skey, sections, force):
        try:
            out = self.actions.summarize(it, sections, force=force)
        except Exception as exc:  # a backend bug must not end the session
            out = summod.Outcome(False, "", f"cannot run: {type(exc).__name__}")
        if self._quitting:
            return
        self._post(self._summary_done, it, skey, out, force)

    def _summary_done(self, it, skey, out, force):
        self.busy = max(0, self.busy - 1)
        self._sum_running = None
        if out.ok:
            self.summaries[skey] = {"state": "ready", "text": out.text,
                                    "at": out.at, "cached": out.cached,
                                    "ctx": out.with_context, "detail": ""}
            if not out.cached:
                self.record("summary", it, "forced" if force else "new", "ok")
        else:
            self.summaries[skey] = {"state": "error", "text": "", "at": 0,
                                    "cached": False, "ctx": False,
                                    "detail": out.detail}
            self.record("summary", it, out.detail, "failed")
            self.notify(f"Summary failed: {out.detail}", severity="warning",
                        timeout=5, markup=False)
        self.context_render()

    # ---------- copy ----------
    def _copy(self, text, count=1):
        """Put `text` on the clipboard (OSC 52) and say so."""
        self.copy_to_clipboard(text)
        self.notify("Copied", title=f"{count} items" if count > 1 else "",
                    timeout=2, markup=False)

    def _section_items(self):
        it = self.current
        if it is None:
            return []
        home = views.group_of(self.view_groups(), it)
        return list(home.items) if home is not None else []

    def action_copy_item(self):
        it = self.current
        if it is None:
            self.notify("Nothing to copy", timeout=2, markup=False)
            return
        self._copy(copy_item_text(it))

    def action_copy_section(self):
        items = self._section_items()
        if not items:
            self.notify("Nothing to copy", timeout=2, markup=False)
            return
        self._copy(copy_section_text(items), len(items))

    def action_copy_section_markdown(self):
        items = self._section_items()
        if not items:
            self.notify("Nothing to copy", timeout=2, markup=False)
            return
        self._copy(copy_section_markdown(items), len(items))

    def action_toggle_resolved(self):
        self.show_resolved = not self.show_resolved
        self.refilter(redraw=False)
        self.redraw()
        self.notify("Resolved alerts: " + ("shown" if self.show_resolved else "hidden"),
                    timeout=2, markup=False)

    def action_toggle_all(self):
        """A: ask the sources that declared `supports_all` for everything, not
        just what is open. Per session; nothing is saved."""
        self.show_all = not self.show_all
        self.notify("Show all: " + ("on (sources that support it)" if self.show_all else "off"),
                    timeout=2, markup=False)
        self.tick_header()
        names = None
        if self.units is not None:   # only a source that supports it can change
            names = {u.name for u in self.units if u.uses_all}
            if not names:
                return
        self.poll(quiet=True, names=names)   # queued when one is running

    def action_saved(self):
        """S: the Feed, filtered to what was saved to memory (the Saved view
        of earlier versions is this routine of the Feed)."""
        if self.saved is None:
            self.notify("The saved view is not available here", timeout=2, markup=False)
            return
        self.set_view("feed", routine=views.SAVED_ROUTINE)

    def _load_saved_rows(self):
        """Worker thread: the saved-to-memory rows for the Feed."""
        if self.saved is None:
            return None
        try:
            rows, error = self.saved()
        except Exception as exc:  # never let a reader bug end the session
            return (self.saved_rows,
                    f"cannot read saved items: {type(exc).__name__}")
        return list(rows or []), error

    # ---------- Feed rules ----------
    def _feed_row(self):
        """The selected Feed row a rule can be made from, or None (with a
        toast)."""
        it = self.current
        if self.view != "feed" or it is None:
            self.notify("x and - work on a row of the Feed", timeout=3, markup=False)
            return None
        if views.is_saved_row(it):
            self.notify("Saved-to-memory rows have no rule", timeout=3, markup=False)
            return None
        if views.is_digest(it):
            it = it.members[-1]
        return it

    def action_mute(self):
        """x on a Feed row: pick what to mute (this kind from this routine,
        this routine, titles like this), then for how long. The rule is saved
        in the inbox state, never in inbox.yaml."""
        cur = self.current
        if cur is not None and tasksmod.is_task(cur):
            return self._task_drop(cur)
        conv = cur is not None and self._acts_on(cur, "mute")
        if conv and not (self.view == "feed" and not views.is_saved_row(cur)):
            # Not a Feed row: only the source's conversation mute is offered.
            return self._mute_picker(cur, [])
        it = self._feed_row()
        if it is None:
            return
        routine, _src, kind, title, _st = feedrules.facts(it)
        scopes = [{"id": "kind", "label": f"This kind from {routine}",
                   "detail": kind or "no kind on this row"},
                  {"id": "routine", "label": f"Everything from {routine}",
                   "detail": ""},
                  {"id": "title", "label": "Titles like this",
                   "detail": feedrules.title_pattern(title)}]
        if not kind:
            scopes = scopes[1:]
        if conv:
            return self._mute_picker(cur, scopes, it)

        def scoped(scope):
            if scope is None:
                self.notify("Cancelled. Nothing muted.", timeout=2, markup=False)
                return
            self._pick_duration(it, scope["id"], "hide")
        self.push_screen(ChoicePicker("Mute: what", scopes, self.bidi), scoped)

    def _mute_picker(self, cur, scopes, rule_item=None):
        """x on a row whose source lists `mute`: the Feed rule choices (when
        the row is in the Feed) and, after them, "Mute this conversation" for
        a day, a week or until untracked, which tells the source."""
        convs = [{"id": f"conv:{key}", "label": f"Mute this conversation: {label}",
                  "detail": "tells the source" + (
                      "; stops tracking it" if key == "never" else "")}
                 for key, label, _s in actsmod.MUTE_CHOICES]

        def scoped(scope):
            if scope is None:
                self.notify("Cancelled. Nothing muted.", timeout=2, markup=False)
                return
            if scope["id"].startswith("conv:"):
                self._mute_conversation(cur, scope["id"][5:])
            else:
                self._pick_duration(rule_item, scope["id"], "hide")
        self.push_screen(ChoicePicker("Mute: what", list(scopes) + convs,
                                      self.bidi), scoped)

    def _mute_conversation(self, it, key):
        seconds = next((s for k, _l, s in actsmod.MUTE_CHOICES if k == key),
                       None)
        until = time.time() + seconds if seconds else None
        label = next(l for k, l, _s in actsmod.MUTE_CHOICES if k == key)
        if self._act_busy("mute", it):
            return
        self._enqueue("mute", it, f"Muted ({label}): {it.who}",
                      until=until or 0.0, detail=label, act="mute",
                      act_args={"until": until})

    def _act_busy(self, kind, it):
        """True (with a notice) when this act is already waiting or running
        for the item."""
        if any(p.kind == kind and p.item.key == it.key and not p.done
               for p in self.pending) or any(
                p.kind == kind and p.item.key == it.key for p in self.running):
            self.notify(f"{kind} already in progress", timeout=2, markup=False)
            return True
        return False

    def _act_gate(self, it, action):
        """True when `it` may take the act; otherwise the reason is toasted."""
        if it is None:
            return False
        if self._synthetic(it):
            return False
        if self._acts_on(it, action):
            return True
        listed = getattr(self.actions, "act_names", lambda _i: ())(it)
        self.notify(ACTS_OFF if not listed else
                    f"This source does not list the {action} act", timeout=4,
                    severity="warning", markup=False)
        return False

    def action_waits_on(self):
        """w: who the next move is with. Choices: Me, the people in the
        thread (not you) and Nobody (FYI); sent as the `waits_on` act."""
        it = self.current
        if not self._act_gate(it, "waits_on") or self._act_busy("waits_on", it):
            return
        people = []
        for m in it.messages:
            if not m.mine and m.who and m.who not in people:
                people.append(m.who)
        if not people and it.who and it.who not in ("You", "you", "me"):
            people.append(it.who)
        choices = [{"id": "me", "label": "Me", "detail": "I owe the next move"}]
        choices += [{"id": f"p:{n}", "label": n, "detail": "waiting on them"}
                    for n in people[:7]]
        choices.append({"id": "nobody", "label": "Nobody (FYI)",
                        "detail": "nothing is waiting"})

        def picked(choice):
            if choice is None:
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
                return
            who = ("me" if choice["id"] == "me" else "nobody"
                   if choice["id"] == "nobody" else choice["id"][2:])
            self._enqueue("waits_on", it, f"Waits on {choice['label']}: {it.who}",
                          hide=False, detail=choice["label"], act="waits_on",
                          act_args={"who": who[:actsmod.WHO_MAX]})
        self.push_screen(ChoicePicker("Waits on", choices, self.bidi), picked)

    # ---------- tasks (0.12.0) ----------
    def _task_gate(self, it, action):
        """True when the task row may take the act: its source lists it and
        no other act on the row is waiting or running. Otherwise the reason is
        toasted (a source with no `acts:` is read-only)."""
        if it is None:
            return False
        if not self._acts_on(it, action):
            listed = getattr(self.actions, "act_names", lambda _i: ())(it)
            self.notify(ACTS_OFF if not listed else
                        f"This source does not list the {action} act",
                        timeout=4, severity="warning", markup=False)
            return False
        live = [p for p in self.pending if not p.done] + list(self.running)
        if any(p.item.key == it.key and p.kind in ACT_KINDS for p in live):
            self.notify("An act on this task is already in progress",
                        timeout=3, markup=False)
            return False
        return True

    def _task_done(self, it, outcome):
        """d: a todo closes at once (note ""), a decision asks for its text
        (required). D: a todo asks for the outcome (optional) and a decision
        asks for its text, like d."""
        if not self._task_gate(it, "close"):
            return
        decision = tasksmod.task_type(it) == tasksmod.DECISION
        if not decision and not outcome:
            return self._task_close(it, "")

        def noted(note):
            if note is None:
                self.notify("Cancelled. Nothing sent.", timeout=2,
                            markup=False)
                return
            note = actsmod.clean_note(note)
            if decision and not note:
                self.notify(DECISION_REQUIRED, timeout=3,
                            severity="warning", markup=False)
                return
            self._task_close(it, note)
        if decision:
            screen = NoteScreen(
                "Decision:", it.gist, actsmod.NOTE_MAX,
                placeholder="required: the decision, one line  (enter to send)",
                hint="enter send  ·  esc cancel  ·  required")
        else:
            screen = NoteScreen(
                "Outcome:", it.gist, actsmod.NOTE_MAX,
                placeholder="the outcome, one line  (enter to close)",
                hint="enter close  ·  esc cancel")
        self.push_screen(screen, noted)

    def _task_close(self, it, note):
        if not self._task_gate(it, "close"):
            return
        word = "Decided" if tasksmod.task_type(it) == tasksmod.DECISION \
            else "Done"
        self._enqueue("done", it, f"{word}: {it.gist}", detail=note[:60],
                      act="close", act_args={"note": note})

    def _task_drop(self, it):
        """x: drop the task, with an optional one-line reason."""
        if not self._task_gate(it, "drop"):
            return

        def noted(note):
            if note is None:
                self.notify("Cancelled. Nothing sent.", timeout=2,
                            markup=False)
                return
            note = actsmod.clean_note(note)
            if self._task_gate(it, "drop"):
                self._enqueue("drop", it, f"Dropped: {it.gist}",
                              detail=note[:60] or "no reason", act="drop",
                              act_args={"note": note})
        self.push_screen(NoteScreen(
            "Reason:", it.gist, actsmod.NOTE_MAX,
            placeholder="optional one-line reason  (enter drops)",
            hint="enter drop  ·  esc cancel"), noted)

    def _task_due(self, it):
        """s: the due date picker (tomorrow, next Sunday, +1 week, a typed
        date, no date). Sends `due`; the row moves at once."""
        if not self._task_gate(it, "due"):
            return
        today = self._today()
        options = tasksmod.due_choices(today)
        by_id = {o[0]: o for o in options}
        choices = [{"id": key, "label": label, "detail": ""}
                   for key, label, _day in options]

        def send(day):
            if not self._task_gate(it, "due"):
                return
            date_str = day.isoformat() if day else None
            label = date_str or "no date"
            p = self._enqueue(
                "due", it, f"Due {label}: {it.gist}", hide=False,
                detail=label, act="due", act_args={"date": date_str},
                meta=act_meta(it))     # the date shown before this move
            self._set_due_mark(it, date_str, p)
            self.redraw(keep=it.key)

        def typed(text):
            if text is None:
                self.notify("Cancelled. Nothing sent.", timeout=2,
                            markup=False)
                return
            send(tasksmod.parse_day(text))

        def picked(choice):
            if choice is None:
                self.notify("Cancelled. Nothing sent.", timeout=2,
                            markup=False)
            elif choice["id"] == "pick":
                self.push_screen(DateScreen("Due date", it.gist), typed)
            else:
                send(by_id[choice["id"]][2])
        self.push_screen(ChoicePicker("Due date", choices, self.bidi), picked)

    def action_relay(self):
        """!: hand the item to your work session, with an optional one-line
        note (the `relay` act)."""
        it = self.current
        if not self._act_gate(it, "relay") or self._act_busy("relay", it):
            return

        def noted(note):
            if note is None:
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
                return
            note = actsmod.clean_note(note)
            self._enqueue("relay", it, f"Relay to your work session: {it.who}",
                          hide=False, detail=note[:60] or "no note",
                          act="relay", act_args={"note": note})
        self.push_screen(NoteScreen("Relay", it.who, actsmod.NOTE_MAX), noted)

    def _pick_duration(self, it, scope, action):
        choices = [{"id": str(n), "label": label, "detail": ""}
                   for n, (label, _s) in enumerate(feedrules.DURATIONS)]

        def timed(choice):
            if choice is None:
                self.notify("Cancelled. Nothing muted.", timeout=2, markup=False)
                return
            seconds = feedrules.DURATIONS[int(choice["id"])][1]
            self._add_rule(it, scope, action, seconds)
        self.push_screen(ChoicePicker("Mute: for how long", choices, self.bidi),
                         timed)

    def _add_rule(self, it, scope, action, seconds):
        rule = feedrules.new_ui_rule(it, scope, action, seconds, time.time())
        if rule is None:
            self.notify("Nothing to match on for this row", severity="warning",
                        timeout=3, markup=False)
            return
        try:
            self.store.add_feed_rule(rule)
        except (OSError, ValueError) as exc:
            self.notify(f"Rule not saved: {getattr(exc, 'strerror', None) or exc}",
                        severity="warning", timeout=4, markup=False)
            return
        self.record("feed-rule", it, f"{rule['action']}: "
                    f"{feedrules.rule_label(rule)}", "saved")
        self.refilter(redraw=False)
        self.redraw(keep=None)
        word = "Muted" if action == "hide" else "Less of"
        self.notify(f"{word}: {feedrules.rule_label(rule)}\nM lists the rules",
                    timeout=4, markup=False)

    def action_less(self):
        """- on a Feed row: fewer of these (a rule that folds the routine's
        rows of a day into one row instead of hiding them)."""
        it = self._feed_row()
        if it is None:
            return
        scope = "kind" if feedrules.facts(it)[2] else "routine"
        self._add_rule(it, scope, "digest", None)

    def action_rules(self):
        """M: the Feed rules, from inbox.yaml and from here."""
        if self.rules_screen is not None:
            return

        def guarded(write, *args):
            try:
                write(*args)
            except (OSError, ValueError) as exc:
                self.notify("Rule not changed: "
                            f"{getattr(exc, 'strerror', None) or exc}",
                            severity="warning", timeout=4, markup=False)
                return
            self._rules_changed()

        def toggled(rule_id, enabled):
            guarded(self.store.set_feed_rule_enabled, rule_id, enabled)

        def removed(rule_id):
            guarded(self.store.remove_feed_rule, rule_id)
        screen = RulesScreen(self.feed_cfg["rules"], self._ui_rules(),
                             toggled, removed)
        self.rules_screen = screen
        self.push_screen(screen, lambda _r: setattr(self, "rules_screen", None))

    def _rules_changed(self):
        self.refilter(redraw=False)
        self.redraw(keep=self.current_key)
        if self.rules_screen is not None:
            self.rules_screen.reload(self._ui_rules())

    def action_audit(self):
        """L: the audit log. With acts on, the ops log (what was sent to the
        sources, with `u` to undo one there); `a` in it shows the session's
        audit log."""
        if not self._acts_any():
            self.push_screen(AuditScreen(self.audit))
            return
        self._ops_screen()

    def _ops_screen(self):
        def closed(result):
            if not result:
                return
            kind, row = result
            if kind == "session":
                self.push_screen(AuditScreen(self.audit),
                                 lambda _r: self._ops_screen())
            elif kind == "inverse":
                self._confirm_inverse(row)
        self.push_screen(OpsScreen(self.actions.ops_rows(),
                                   self.actions.invertible), closed)

    def _confirm_inverse(self, row):
        action = self.actions.invertible(row)
        if action is None:
            return
        rows = [("Item", str(row.get("who", ""))),
                ("Where", str(row.get("where", ""))),
                ("Was", f"{row.get('action')} ({row.get('result')})"),
                ("Send", action)]
        if row.get("action") == "due":
            rows.append(("Back to", str(row.get("prev_due") or "no date")))
        rows.append(("To source", str(row.get("source", ""))))

        def after(ok):
            if not ok:
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
                return
            self.busy += 1
            self._inverse_running += 1
            threading.Thread(target=self._inverse_thread, args=(row, action),
                             name="inbox-acts-inverse").start()
        self.push_screen(ConfirmWrite(
            "Undo in the source", rows, "Send",
            f"Sends {action} for this item and brings it back here.",
            self.bidi, fixed="Nothing is sent until you confirm. This cannot "
                             "be taken back."), after)

    def _inverse_thread(self, row, action):
        try:
            out = self.actions.send_inverse(row)
        except Exception as exc:
            out = None
            detail = f"{type(exc).__name__}"
        else:
            detail = out.message
        finally:
            self._inverse_running = max(0, self._inverse_running - 1)
        if self._quitting:
            return
        self._post(self._inverse_done, row, action, out, detail)

    def _inverse_done(self, row, action, out, detail):
        self.busy = max(0, self.busy - 1)
        stub = _stub(row)
        status = out.status if out is not None else "failed"
        self.record(action, stub, detail, status)
        if out is not None and out.status in (actsmod.OK, actsmod.SHADOW):
            self.act_shadow = bool(out.shadow)
            self._due_marks.pop(str(row.get("id", "")), None)
            self.refilter(redraw=False)
            self.redraw(keep=self.current_key)
            if (row.get("action") in ("close", "drop", "due")
                    and self._is_task_row(row) and self.polled_once):
                # The list is the truth for tasks: fetch the reopened or
                # moved one back.
                src = str(row.get("source", ""))
                self.poll(quiet=True, names=(
                    {src} if self.units is not None else None))
            self.notify(("shadow" if out.status == actsmod.SHADOW
                         else "✓ synced") + f": {action} · {row.get('who', '')}",
                        title="Source", timeout=4, markup=False)
        elif out is not None and out.status == actsmod.CONFLICT:
            self.notify(self._conflict_text(row), severity="warning",
                        timeout=6, markup=False)
        else:
            self.notify(f"{action} not sent: {detail}", severity="warning",
                        timeout=6, markup=False)
        self._refresh_act_count()

    def action_help(self):
        self.push_screen(HelpScreen(
            [(c["key"], c["label"]) for c in self._custom],
            save_on=self.actions_save_on))

    def action_toggle_theme(self):
        dark = self.theme == themes.theme_id("dark")
        self.theme = themes.theme_id("light" if dark else "dark")

    def action_toggle_bidi(self):
        if self.bidi_mode == "logical" and not bidi.available():
            self.notify("python-bidi is not installed: visual order is unavailable.",
                        timeout=3, severity="warning", markup=False)
            return
        self.bidi_mode = "logical" if self.bidi else "visual"
        self.screen.clear_selection()  # it was made on the other text order
        self.notify(f"Hebrew order: {self.bidi_mode}", timeout=2, markup=False)
        self.items_redraw_all()

    def action_toggle_thread_order(self):
        """v: the thread pane lists the newest message first, or the oldest.
        Saved in the inbox state, so a restart keeps it. The queue is not
        affected."""
        self.thread_order = "oldest" if self.thread_order == "newest" else "newest"
        try:
            self.store.set_pref("thread_order", self.thread_order)
        except (OSError, ValueError) as exc:
            self.notify(f"Order not saved: {getattr(exc, 'strerror', None) or exc}",
                        severity="warning", timeout=4, markup=False)
        self.show_thread_only()
        self.notify(f"Thread order: {self.thread_order} first", timeout=2,
                    markup=False)

    def items_redraw_all(self):
        """Re-render every widget that shows text (queue, thread, draft, header)."""
        self.redraw()
        it = self.current
        if it is not None and not self.editing:
            self.query_one("#draft-view", Static).update(self.draft_text(it, it.draft, False))

    async def action_quit(self):
        """Quitting cancels pending sends, nudges, RSVPs and saves (nothing
        leaves the inbox). Done, ack and snooze are committed."""
        busy = []
        if self.running:
            busy.append(f"{len(self.running)} write(s) still running")
        if self._sum_running is not None:
            busy.append("summary still running")
        if self._polish_running:
            busy.append("polish still running")
        if self._draft_running:
            busy.append("draft for me still running")
        if self._inverse_running:
            busy.append("undo in the source still running")
        if busy and not self._quit_armed:
            self._quit_armed = True
            self.set_timer(8, self._disarm_quit)
            tail = ("it finishes first" if len(busy) == 1 and not self.running
                    else "they finish first")
            self.notify("; ".join(busy) + f"; q again to quit ({tail})",
                        severity="warning", timeout=6, markup=False)
            return
        cancelled = 0
        react_skipped = 0
        for p in list(self.pending):
            if p.timer:
                p.timer.stop()
            p.done = True
            self.pending.remove(p)
            if p.kind in EXTERNAL_KINDS:
                p.audit["status"] = "cancelled on quit"
                cancelled += 1
            else:
                try:
                    res = self._run(p, send=False)
                except Exception as exc:
                    res = Result(False, str(exc))
                p.audit["status"] = "committed" if res.ok else "failed"
                if p.react:
                    react_skipped += 1
        self._quitting = True
        self._cancel.set()      # context commands get SIGTERM and wind down
        if self._act_op is not None:   # a choices/prepare lookup: read-only
            self._act_op["cancel"].set()
        if self._pre_op is not None:   # the send precheck: read-only
            self._pre_op["cancel"].set()
        self._ctx_pending = []
        parts = []
        if cancelled:
            parts.append(f"Nothing sent: {cancelled} pending action(s) cancelled on quit.")
        if react_skipped:
            parts.append(f"{react_skipped} done reaction(s) skipped: quitting "
                         "never sends one.")
        if self.running:
            parts.append(f"{len(self.running)} write(s) still finishing; the process "
                         "exits when they end.")
        for p in self.running:
            if p.kind in ACT_KINDS and (p.act or p.react):
                p.audit["status"] = "running at quit"
                parts.append(f"{p.label}: still running; it may already have "
                             "reached the source" + (
                                 f", and the done reaction (:{p.react}:) may "
                                 "already have been posted, check Slack"
                                 if p.react else "") + ".")
        for p in self.running:
            if p.kind in SLACK_WRITE_KINDS or p.kind == "react":
                did = "placed in your Slack drafts" if p.kind == "draft" \
                    else "sent"
                p.audit["status"] = "running at quit"
                p.audit["detail"] = (p.audit["detail"]
                                     + f" · may have been {did}").strip(" ·")
                parts.append(f"{p.label}: still running; it may have been "
                             f"{did}, check Slack.")
        if self._sum_running is not None:
            parts.append("The summary is still finishing; the process exits "
                         "when it ends.")
        if self._polish_running:
            parts.append("The polish is still finishing; the process exits "
                         "when it ends.")
        if self._draft_running:
            parts.append("The draft for you is still finishing; the process "
                         "exits when it ends.")
        if self._inverse_running:
            parts.append("An undo in the source is still finishing; the "
                         "process exits when it ends.")
        self.exit(message=" ".join(parts) or None)

    def _disarm_quit(self):
        self._quit_armed = False

    # ---------- poll ----------
    def _run_units(self, names, show_all, reeval=False):
        """Run the named units (all when `names` is None) and merge them with
        the cached results of the others. Worker thread. With `reeval`, a unit
        rebuilds its results for the current time from what it already read
        (no file is touched) and one that cannot is left as it was."""
        now = time.time()
        for unit in self.units:
            if names is not None and unit.name not in names:
                continue
            try:
                if reeval:
                    batch = unit.reeval(now) if unit.reeval else None
                    batch = list(batch) if batch is not None else None
                else:
                    started = time.time()
                    batch = list(unit.run(now, show_all))
                    for res in batch:
                        self._list_started[res.name] = started
            except Exception as exc:  # a source bug must never end the session
                batch = [SourceResult(unit.name, [], False, None,
                                      f"poll failed: {type(exc).__name__}: {exc}")]
            if batch is not None:
                self._unit_results[unit.name] = batch
        results = merge((self._unit_results[u.name] for u in self.units
                         if u.name in self._unit_results),
                        units_answers(self.units, self.store.rsvp_series()),
                        self.store.closed_ids())
        return annotate_memory(results, units_memory_index(self.units))

    def _safe_fetch(self, show_all=None, names=None, reeval=False):
        """Runs in a worker thread: pick up other panes' marks, fetch, and
        stamp the marks of items that are still listed."""
        show_all = self.show_all if show_all is None else show_all
        try:
            self.store.load()
        except OSError:
            pass
        try:
            if self.units is not None:
                results = self._run_units(names, show_all, reeval)
            else:
                results = list(self.fetch(show_all=show_all) if self._fetch_takes_all
                               else self.fetch())
        except Exception as exc:  # a source bug must never end the session
            return [SourceResult("inbox", [], False, None,
                                 f"poll failed: {type(exc).__name__}: {exc}")]
        if not reeval:
            self._saved_fresh = self._load_saved_rows()
        try:
            # Age-prune marks only after a full refresh in which every source
            # answered cleanly.
            self.store.note_listed(
                (i["id"] for r in results for i in r.items),
                prune=names is None and all(r.error is None for r in results))
        except OSError:
            pass
        return results

    def _queue(self, names, quiet):
        """Remember a refresh asked for while another one runs; it starts as
        soon as that one ends. Requests merge: everything beats a subset."""
        old = self._queued
        if old is None:
            self._queued = {"names": None if names is None else set(names),
                            "quiet": quiet}
            return
        old["quiet"] = old["quiet"] and quiet
        if old["names"] is not None:
            old["names"] = None if names is None else old["names"] | set(names)

    def poll(self, manual=False, quiet=False, names=None, reeval=False):
        """Start a refresh unless one is already running (they never overlap,
        so a slow source cannot pile up cancelled fetches). `names` limits it
        to those units (None: everything). A `quiet` refresh (after the A
        toggle) does not announce the items it reveals. A refresh asked for
        while one runs is queued to run right after it (a manual `r` is told
        instead); a `reeval` pass, which only re-applies the clock, is not."""
        if self.polling:
            if manual:
                self.notify("Poll already running", timeout=2, markup=False)
            elif not reeval:
                self._queue(names, quiet)
            return
        self.polling = True
        if names is None and not reeval:
            self.next_poll_at = time.time() + self.poll_s
            if self.watcher is not None:   # baseline: later changes are due again
                self.watcher.commit()
        self._poll_worker(manual, quiet, self.show_all, names, reeval)

    def reeval(self):
        """Every 30 s: re-apply the clock (invites that ended, the window,
        alert age, urgent-soon rules) to what the sources already read,
        without reading any file."""
        if self.units is None or self.polling or not self.polled_once:
            return
        names = {u.name for u in self.units if u.reeval}
        if names:
            self.poll(quiet=True, names=names, reeval=True)

    def watch_tick(self):
        """Every `watch_interval_s`: stat the watched files and refresh only
        the units whose files changed. A tick that finds a refresh running
        leaves the change uncommitted, so the next tick picks it up."""
        if self.watcher is None:
            return
        try:
            due = self.watcher.due()
        except Exception:  # stat-only and tolerant, but never end the session
            return
        # A watched source whose files did not change is as fresh as its files.
        now = time.time()
        for name in self.watcher.unchanged:
            if name in self.updated_at:
                self.updated_at[name] = now
        names = set(due)
        if names and not self.polling:
            self.watcher.commit(names)
            self.poll(names=names)

    @work(exclusive=False, group="poll")
    async def _poll_worker(self, manual, quiet=False, show_all=False, names=None,
                           reeval=False):
        self.busy += 1
        started = time.time()
        try:
            results = await asyncio.to_thread(self._safe_fetch, show_all, names, reeval)
        finally:
            self.busy -= 1
            self.polling = False
        stamp = time.time()
        self._applying_since = started
        if reeval:
            pass                    # no file was read: nothing became fresher
        elif self.units is not None:
            for unit in self.units:
                if names is None or unit.name in names:
                    self.updated_at[unit.name] = stamp
        else:
            for res in results:
                self.updated_at[res.name] = stamp
        self.apply_results(results, manual=manual, quiet=quiet)
        self._applying_since = None
        queued, self._queued = self._queued, None
        if queued:
            self.poll(quiet=queued["quiet"], names=queued["names"])

    def _kept_draft(self, it):
        """A draft `D` made for this item and thread earlier (kept in the
        store) comes back as its draft. Cheap: only items with one are hashed."""
        has = getattr(self.store, "has_draft", None)
        fn = getattr(self.actions, "cached_draft_me", None)
        if has is None or fn is None or not has(it.key):
            return False
        try:
            text = fn(it)
        except Exception:
            return False
        if not text:
            return False
        it.draft, it.streamed, it.drafted_by = text, True, "model"
        return True

    def apply_results(self, results, manual=False, quiet=False):
        """Merge a poll into the queue. Runs on the UI thread."""
        self.results = list(results)
        if self._saved_fresh is not None:
            (self.saved_rows, self.saved_error), self._saved_fresh = \
                self._saved_fresh, None
        self.last_poll_at = time.time()
        self.next_poll_at = self.last_poll_at + self.poll_s
        old = {i.key: i for i in self.all_rows}
        # A due date set here lasts until a list fetched after the source took
        # it replaces it (one fetched before it was settled is still stale).
        since = self._applying_since
        self._due_marks = {k: v for k, v in self._due_marks.items()
                           if not self._list_fresh(v, since)}
        rows = []
        for res in self.results:
            for raw in res.items:
                it = from_v1(raw)
                it.origin = res.name
                prev = old.get(it.key)
                if prev is not None and prev.streamed:
                    it.draft, it.streamed = prev.draft, True
                elif not it.draft:
                    self._kept_draft(it)
                rows.append(it)
        self.all_rows = rows
        fresh = [i for i in rows if i.key not in self.known_ids
                 and i.section == "urgent" and not tasksmod.is_task(i)
                 and not self.store.is_hidden(i.key, meta=i.meta)]
        self.known_ids.update(i.key for i in rows)
        if self._acts_any():
            self._refresh_act_count()
            self._retry_ops()      # each op at most every 5 minutes
        first = not self.polled_once
        self.polled_once = True
        self.refilter(redraw=False)
        self.redraw()
        problems = tuple(self.source_problems())
        if problems and problems != self._last_problems:
            self.notify("\n".join(problems[:3]), title="Source problem", severity="warning",
                        timeout=8, markup=False)
        self._last_problems = problems
        if quiet:
            return
        if first or not fresh:
            if manual and not fresh:
                self.notify("Polled: nothing new", timeout=2, markup=False)
            return
        for it in fresh:
            self.record("poll", it, "new urgent item", "arrived")
        self.bell()
        lead = fresh[0]
        more = f"\n+{len(fresh) - 1} more" if len(fresh) > 1 else ""
        self.notify(f"{lead.who} · {lead.where}\n{vis(lead.gist, self.bidi)}{more}",
                    title="New urgent", severity="warning", timeout=8, markup=False)

    # ---------- command palette ----------
    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        cmds = [
            ("Approve draft / acknowledge", "Confirm-send modal, or ack for calendar and alerts", self.action_approve),
            ("Filter by source", "f: cycle the sources present", self.action_source_filter),
            ("Edit draft", "Edit the draft text", self.action_edit),
            ("Mark done", "Remove from the queue", self.action_done),
            ("Snooze…", "Pick a snooze time", self.action_snooze),
            ("Accept invite", "a on a calendar invite (needs rsvp: in inbox.yaml)", lambda: self._rsvp("accept")),
            ("Decline invite", "X (needs rsvp: in inbox.yaml)", self.action_rsvp_decline),
            ("Tentative", "T: answer an invite tentatively (needs rsvp: in inbox.yaml)", self.action_rsvp_tentative),
            ("React to message", "E: add an emoji reaction (needs react: in inbox.yaml)", self.action_react),
            ("Polish draft", "P: rewrite the draft in your tone, never sends (needs polish: in inbox.yaml)", self.action_polish),
            ("Draft for me", "D: write a reply from the thread, never sends (needs summary: or polish:)", self.action_draft_me),
            ("Waits on…", "w: who the next move is with (needs acts: waits_on on the source)", self.action_waits_on),
            ("Relay to work session…", "!: hand the item over with a note (needs acts: relay on the source)", self.action_relay),
            ("Remind me", "m on a task: where it came from, the original words, related items (read-only)", self.action_remind),
            ("Toggle context pane", "c: context for the selected item (needs context: in inbox.yaml)", self.action_context),
            ("Refresh context", "C (or r inside the pane): fetch the context again", self.action_context_refresh),
            ("Summarize item", "i: AI summary in the context pane (needs summary: in inbox.yaml)", lambda: self.action_summary(False)),
            ("Summarize item (refresh)", "I: ask for a new summary even if one is cached", lambda: self.action_summary(True)),
            ("Nudge", "Send, draft or copy the nudge (confirm first)", self.action_nudge),
            ("Open link", "Open the item's link in the browser", self.action_open),
            ("Copy item", "y: sender, where, gist and the link", self.action_copy_item),
            ("Copy section", "Y: every item in this group, one per line", self.action_copy_section),
            ("Copy section as Markdown", "A - **sender** (where): gist list", self.action_copy_section_markdown),
            ("Show / hide resolved alerts", "R", self.action_toggle_resolved),
            ("Show all (toggle)", "A: sources that support it list everything, not just what is open", self.action_toggle_all),
            ("Saved to memory", "S: the Feed, filtered to what the daemon saved", self.action_saved),
            ("Undo last action", "Within the 5 s window", self.action_undo),
            ("Poll now", "Poll every source", self.action_refresh),
            ("Toggle dark/light theme", "ctrl+t", self.action_toggle_theme),
            ("Show audit log", "Every action with its time", self.action_audit),
            ("Toggle Hebrew order", "logical / visual (python-bidi)", self.action_toggle_bidi),
            ("Toggle thread order", "v: newest or oldest message first in the thread pane", self.action_toggle_thread_order),
            ("Filter queue", "Fuzzy filter", self.action_filter),
            ("Row menu", "enter: what the selected row can do, with a key for each", self.action_row_menu),
            ("Keyboard help", "?", self.action_help),
        ] + ([("Save item to memory", "m: save this item by hand (the daemon saves on its own, ◆ = already in memory)", self.action_save_memory)] if self.actions_save_on else []) + [
            (f"Go to {t}", f"key {n}", (lambda n=n: self.action_view(n)))
             for n, (_k, t) in enumerate(views.VIEWS, 1)] + [
            ("Next group", "]: the next section, person or routine", lambda: self.action_group(1)),
            ("Previous group", "[: the previous section, person or routine", lambda: self.action_group(-1)),
            ("Mute (Feed)", "x: mute this kind, this routine or titles like this", self.action_mute),
            ("Less like this (Feed)", "-: fold the routine's rows of a day into one", self.action_less),
            ("Feed rules", "M: the rules from inbox.yaml and the ones made here", self.action_rules)]
        cmds += [(c["label"], f"{c['key']}: custom action (actions: in inbox.yaml)",
                  (lambda n=n: self.action_custom_action(n)))
                 for n, c in enumerate(self._custom)]
        for title, helptext, cb in cmds:
            yield SystemCommand(title, helptext, cb)
