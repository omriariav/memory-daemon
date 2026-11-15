"""The inbox TUI app. Needs Textual; import it only through `cli.run`."""
import asyncio
import time
from dataclasses import dataclass, field

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Input, Static, TextArea

from . import bidi, themes
from .actions import Result
from .models import (SECTION_KEYS, SECTION_TITLES, SECTIONS, SOURCES, Item,
                     from_v1)
from .screens import AuditScreen, ConfirmSend, HelpScreen, SnoozePicker
from .sources import SourceResult
from .widgets import (ITEM_PREFIX, DraftPane, HintBar, Queue, StatusHeader,
                      ThreadView, fmt_age, fuzzy, palette, vis)

NOT_YET = "Replying is not available yet (phase 2). Nothing is sent."


@dataclass
class Pending:
    kind: str                 # send | nudge | done | ack | snooze
    item: Item
    text: str = ""
    until: float = 0.0
    label: str = ""
    audit: dict = field(default_factory=dict)
    timer: object = None
    done: bool = False


class InboxApp(App):
    TITLE = "inbox"
    ENABLE_COMMAND_PALETTE = True
    BINDINGS = [
        Binding("j,down", "move(1)", "Down", show=False),
        Binding("k,up", "move(-1)", "Up", show=False),
        Binding("g", "edge(0)", "Top", show=False),
        Binding("G", "edge(1)", "Bottom", show=False),
        Binding("1", "section(1)", show=False), Binding("2", "section(2)", show=False),
        Binding("3", "section(3)", show=False), Binding("4", "section(4)", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("f", "source_filter", show=False),
        Binding("a", "approve", show=False), Binding("e", "edit", show=False),
        Binding("d", "done", show=False), Binding("s", "snooze", show=False),
        Binding("n", "nudge", show=False), Binding("o", "open", show=False),
        Binding("r", "refresh", show=False), Binding("u", "undo", show=False),
        Binding("L", "audit", show=False), Binding("b", "toggle_bidi", show=False),
        Binding("question_mark", "help", show=False),
        Binding("ctrl+t", "toggle_theme", show=False),
        Binding("ctrl+s", "save_edit", show=False),
        Binding("escape", "escape", show=False),
        Binding("q", "quit", show=False),
    ]
    CSS = """
    Screen { background: $background; color: $foreground; }
    #hdr { height: 1; background: $panel; }
    #main { height: 1fr; }
    #left { width: 42%; min-width: 38; max-width: 76; border: round $panel; border-title-color: $text-muted; }
    #left:focus-within { border: round $primary; border-title-color: $primary; }
    #right { width: 1fr; }
    #thread, #draft-pane { border: round $panel; border-title-color: $text-muted; padding: 0 1; }
    #thread:focus, #draft-pane:focus-within { border: round $primary; border-title-color: $primary; }
    #thread { height: 1fr; scrollbar-size-vertical: 1; }
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
    ConfirmSend, SnoozePicker, HelpScreen, AuditScreen { align: center middle; background: $background 55%; }
    #confirm { width: 74; max-width: 94%; height: auto; border: heavy $error; background: $surface; padding: 1 2; }
    #confirm-title { text-style: bold; color: $error; margin-bottom: 1; }
    #confirm-text { border: round $panel; padding: 0 1; margin: 1 0; background: $background; }
    #confirm-buttons { height: 3; align-horizontal: right; }
    #confirm-buttons Button { margin-left: 2; min-width: 18; }
    #snooze { width: 52; height: auto; border: heavy $primary; background: $surface; padding: 1 2; }
    #snooze-list { height: 6; background: transparent; border: none; }
    #snooze-hint { margin-top: 1; color: $text-muted; }
    #help { width: 66; max-width: 94%; height: auto; max-height: 94%; border: heavy $primary; background: $surface; padding: 1 2; }
    #help-title { margin-bottom: 1; }
    #audit { width: 96%; height: 80%; border: heavy $primary; background: $surface; padding: 1 2; }
    #audit-table { height: 1fr; }
    """

    def __init__(self, fetch, store, actions, settings=None, demo=False,
                 undo_s=5.0, leave_s=0.18, stream_delay=0.012, poll_s=None):
        """`fetch()` blocks and returns a list of SourceResult (it runs in a
        worker thread). `store` holds done/ack/snooze marks."""
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
        self.editing = False
        self.busy = 0
        mode = bidi.resolve(bidi.configured_mode(settings["bidi_mode"]))
        self.bidi_fallback = mode == "visual" and not bidi.available()
        self.bidi_mode = "logical" if self.bidi_fallback else mode
        self.pending = []
        self.audit = []
        self.stats = {"done": 0, "sent": 0, "snoozed": 0}
        self.known_ids = set()
        self.polled_once = False
        self.last_poll_at = None
        self.next_poll_at = time.time() + self.poll_s
        self.last_committed = None
        self.current_key = None
        self._last_problems = ()

    @property
    def bidi(self):
        """True when text is reordered to visual order (bidi_mode 'visual')."""
        return self.bidi_mode == "visual"

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        yield StatusHeader(id="hdr")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(placeholder="/ filter…", id="filter")
                yield Queue(id="queue")
                yield Static("", id="zero")
            with Vertical(id="right"):
                yield ThreadView(id="thread")
                yield DraftPane(id="draft-pane")
        yield HintBar(id="hints")

    def on_mount(self):
        for theme in themes.build_textual_themes():
            self.register_theme(theme)
        self.theme = themes.theme_id(self.settings["theme"])
        self.theme_changed_signal.subscribe(self, lambda _t: self.call_after_refresh(self.redraw))
        self.query_one("#left").border_title = "Queue"
        self.query_one("#thread").border_title = "Thread"
        self.query_one("#draft-pane").border_title = "Draft"
        self.set_interval(0.25, self.tick_header)
        self.set_interval(30, self.refilter)
        self.set_interval(self.poll_s, self.poll)
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

    def visible(self):
        out = []
        for it in self.items:
            if self.source_filter and it.source != self.source_filter:
                continue
            if self.filter_text:
                if fuzzy(self.filter_text, f"{it.who} {it.where}") is None \
                        and self.filter_text.lower() not in it.gist.lower():
                    continue
            out.append(it)
        order = {k: n for n, k in enumerate(SECTION_KEYS)}
        out.sort(key=lambda i: (order[i.section], i.ts if i.section == "waiting" else -i.ts))
        return out

    def refilter(self, redraw=True):
        """Rebuild the queue from the fetched rows: hide marked and pending
        items, wake snoozed ones that are due."""
        busy_keys = {p.item.key for p in self.pending if not p.done}
        now = time.time()
        shown = [i for i in self.all_rows
                 if i.key not in busy_keys and not self.store.is_hidden(i.key, now)]
        changed = [i.key for i in shown] != [i.key for i in self.items]
        self.items = shown
        if redraw and changed:
            try:
                self.redraw()
            except NoMatches:  # timer fired during shutdown
                pass
        return changed

    @property
    def current(self):
        for it in self.items:
            if it.key == self.current_key:
                return it
        return None

    def record(self, action, item, detail, status):
        row = {"time": time.time(), "action": action, "item": item.who if item else "",
               "detail": detail, "status": status}
        self.audit.append(row)
        return row

    @property
    def main_active(self):
        return len(self.screen_stack) == 1

    def check_action(self, action, parameters):
        if action in ("quit", "toggle_theme"):
            return True
        return self.main_active

    # ---------- rendering ----------
    def redraw(self, keep=None, ghost=None, ghost_at=None):
        q = self.query_one("#queue", Queue)
        vis_items = self.visible()
        counts = {k: sum(1 for i in vis_items if i.section == k) for k in SECTION_KEYS}
        keep = keep or self.current_key
        now = time.time()
        zero = self.query_one("#zero", Static)
        empty = not vis_items and ghost is None
        q.display = not empty
        zero.display = empty
        if empty:
            zero.update(self.zero_text())
        else:
            q.build(vis_items, counts, self.pal, now, keep=keep, ghost=ghost,
                    ghost_at=ghost_at, visual=self.bidi)
        left = self.query_one("#left")
        left.border_title = ("Queue" + (f"  /{self.filter_text}" if self.filter_text else "")
                             + (f"  [{SOURCES[self.source_filter][2]}]" if self.source_filter else ""))
        left.border_subtitle = f"{len(vis_items)}/{len(self.items)}" if (self.filter_text or self.source_filter) else ""
        self.sync_current()
        self.tick_header()
        self.update_hints()

    def zero_text(self):
        p = self.pal
        t = Text(justify="center")
        if (self.filter_text or self.source_filter) and self.items:
            what = f"'{self.filter_text}'" if self.filter_text else SOURCES[self.source_filter][2]
            t.append(f"No matches for {what}\n\n", Style(bold=True, color=p["warn"]))
            t.append("esc clears the filters", Style(color=p["muted"]))
            return t
        total = sum(self.stats.values())
        if not self.polled_once:
            t.append("Polling…\n\n", Style(bold=True, color=p["fg"]))
            t.append("r  poll now      ctrl+p  commands      ?  help", Style(color=p["muted"]))
            return t
        if total == 0:
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
            self.show_item()
        else:
            self.show_thread_only()

    def show_thread_only(self):
        self.query_one("#thread", ThreadView).show(self.current, self.pal, time.time(), self.bidi)

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

    def hints(self):
        f = self.focused
        if self.editing:
            return [("esc", "save & back"), ("ctrl+s", "save"), ("", "typing edits the draft")]
        if isinstance(f, Input):
            return [("enter", "keep filter"), ("esc", "clear"), ("", "type to fuzzy-match")]
        it = self.current
        if it is None:
            return [("r", "poll"), ("/", "filter"), ("L", "audit"), ("ctrl+p", "commands"), ("?", "help"), ("q", "quit")]
        base = [("j/k", "move")]
        opn = [("o", "open")] if it.link else []
        if f is not None and f.id in ("thread", "draft-pane"):
            return base + [("tab", "next pane"), ("e", "edit"), ("a", "approve"), ("esc", "queue"), ("?", "help")]
        if it.kind == "ack":
            return base + [("a", "ack")] + opn + [("s", "snooze"), ("d", "done"), ("f", "source"), ("u", "undo"), ("?", "help")]
        if it.kind == "fyi":
            return base + [("d", "dismiss")] + opn + [("s", "snooze"), ("u", "undo"), ("/", "filter"), ("?", "help")]
        if it.kind == "nudge":
            return base + [("n", "nudge"), ("e", "edit"), ("d", "done"), ("s", "snooze")] + opn + [("u", "undo"), ("?", "help")]
        return base + [("a", "approve"), ("e", "edit"), ("d", "done"), ("s", "snooze")] + opn + [
            ("u", "undo"), ("/", "filter"), ("f", "source"), ("?", "help")]

    def update_hints(self):
        self.query_one("#hints", HintBar).set_hints(self.hints(), self.pal)

    def on_descendant_focus(self, _e):
        self.update_hints()

    def on_resize(self, _e):
        self.call_after_refresh(self.redraw)

    # ---------- queue events ----------
    def on_option_list_option_highlighted(self, e):
        if e.option_list.id == "queue":
            self.sync_current()
            self.update_hints()

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
        elif f is not None and f.id in ("thread", "draft-pane"):
            f.scroll_down() if d > 0 else f.scroll_up()

    def action_edge(self, end):
        f = self.focused
        q = self.query_one("#queue", Queue)
        if f is not None and f.id == "queue" and q.rows:
            q.highlighted = q.get_option_index(ITEM_PREFIX + q.rows[-1 if end else 0].key)
        elif f is not None and f.id == "thread":
            f.scroll_end(animate=False) if end else f.scroll_home(animate=False)

    def action_section(self, n):
        key = SECTION_KEYS[n - 1]
        q = self.query_one("#queue", Queue)
        row = next((i for i in q.rows if i.section == key), None)
        if row is None:
            self.notify(f"No items in {SECTION_TITLES[key]}", timeout=2, markup=False)
            return
        q.focus()
        q.highlighted = q.get_option_index(ITEM_PREFIX + row.key)

    def _panes(self):
        return [self.query_one("#queue"), self.query_one("#thread"), self.query_one("#draft-pane")]

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
        if self.query_one("#queue").display is False:
            panes = panes[1:]
            idx = max(0, idx - 1)
        panes[(idx + d) % len(panes)].focus()

    # ---------- filter ----------
    def action_source_filter(self):
        present = [k for k in SOURCES if any(i.source == k for i in self.items)]
        order = [None] + present
        cur = order.index(self.source_filter) if self.source_filter in order else 0
        self.source_filter = order[(cur + 1) % len(order)]
        self.notify("Source: " + (SOURCES[self.source_filter][2] if self.source_filter else "all"),
                    timeout=2, markup=False)
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
            self.query_one("#queue").focus()
            return
        f = self.query_one("#filter", Input)
        if self.filter_text or f.has_class("on") or self.source_filter:
            f.value = ""
            self.filter_text = ""
            self.source_filter = None
            f.remove_class("on")
            self.query_one("#queue").focus()
            self.redraw()
            return
        self.query_one("#queue").focus()

    # ---------- editing ----------
    def action_edit(self):
        it = self.current
        if it is None or it.kind in ("fyi", "ack"):
            self.notify("Nothing to edit here", timeout=2, markup=False)
            return
        if not self.actions.supports_send:
            self.notify(NOT_YET, timeout=3, markup=False)
            return
        ta = self.query_one("#draft-edit", TextArea)
        ta.text = it.draft
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
        if save and it is not None and ta.text != it.draft:
            it.draft, it.streamed = ta.text, True
            self.record("edit", it, f"{len(ta.text)} chars", "saved")
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
        sec_items = [i for i in vis_before if i.section == it.section]
        ghost_at = sec_items.index(it) if it in sec_items else None
        self.refilter(redraw=False)       # the pending item is already listed
        after = self.visible()
        keep = after[min(pos, len(after) - 1)].key if after else None
        self.current_key = keep
        if self.leave_s > 0:
            self.redraw(keep=keep, ghost=it, ghost_at=ghost_at)
            self.set_timer(self.leave_s, lambda: self.redraw(keep=self.current_key))
        else:
            self.redraw(keep=keep)

    def _enqueue(self, kind, it, toast, text="", until=0.0, detail=""):
        p = Pending(kind=kind, item=it, text=text, until=until, label=toast)
        p.audit = self.record(kind, it, detail, "queued")
        self.pending.append(p)
        self._remove_optimistic(it)
        if kind in ("send", "nudge"):
            self.stats["sent"] += 1
        elif kind in ("done", "ack"):
            self.stats["done"] += 1
        else:
            self.stats["snoozed"] += 1
        p.timer = self.set_timer(self.undo_s, lambda: self._commit(p))
        self.notify(f"{toast}\nu to undo ({int(self.undo_s)}s)", timeout=self.undo_s,
                    title="Queued", markup=False)
        self.redraw()

    def _commit(self, p):
        if p.done:
            return
        p.done = True
        if p in self.pending:
            self.pending.remove(p)
        self.busy += 1
        self.run_worker(lambda: self._run_in_thread(p), thread=True, group="commit")

    def _run(self, p):
        a, it = self.actions, p.item
        if p.kind == "send":
            return a.send(it, p.text)
        if p.kind == "nudge":
            return a.nudge(it, p.text)
        if p.kind == "done":
            return a.mark_done(it)
        if p.kind == "ack":
            return a.ack(it)
        return a.snooze(it, p.until)

    def _run_in_thread(self, p):
        try:
            res = self._run(p)
        except Exception as exc:  # a failing store must not kill the app
            res = Result(False, f"{type(exc).__name__}: {exc}")
        self.call_from_thread(self._committed, p, res)

    def _committed(self, p, res):
        self.busy = max(0, self.busy - 1)
        p.audit["status"] = "committed" if res.ok else "failed"
        p.audit["detail"] = (p.audit["detail"] + " · " + res.detail).strip(" ·")
        self.last_committed = p
        if not res.ok:
            # The mark did not stick: the item must come back, not vanish.
            self.refilter()
            self.notify(f"{p.kind} failed: {res.detail}", severity="error", timeout=6, markup=False)
        elif p.kind in ("send", "nudge"):
            self.notify(f"{res.detail or 'would send'}  ->  {p.item.destination}",
                        title="Demo", timeout=4, markup=False)

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
        if p.kind in ("send", "nudge"):
            self.stats["sent"] -= 1
        elif p.kind in ("done", "ack"):
            self.stats["done"] -= 1
        else:
            self.stats["snoozed"] -= 1
        p.audit["status"] = "undone"
        self.record("undo", it, p.kind, "ok")
        self.current_key = it.key
        self.refilter(redraw=False)
        self.redraw(keep=it.key)
        self.notify(f"Restored {it.who}", timeout=2, title="Undone", markup=False)

    def action_approve(self):
        it = self.current
        if it is None:
            return
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
        if it.kind != "nudge":
            self.notify("n nudges items under 'Waiting on them'", timeout=3, markup=False)
            return
        if not self.actions.supports_send:
            self.notify(NOT_YET, timeout=3, markup=False)
            return
        if not it.draft:
            self.notify(f"Nudge not due yet (waiting {fmt_age(time.time() - it.ts)})", timeout=3, markup=False)
            return
        self._confirm_send("nudge", it, "Send nudge")

    def _confirm_send(self, kind, it, verb):
        text = it.draft
        if not text.strip():
            self.notify("No draft to send. Press e to write one.", timeout=3, severity="warning", markup=False)
            return

        def after(ok):
            if ok:
                self._enqueue(kind, it, f"{verb}: {it.destination}", text=text, detail=text[:60])
            else:
                self.record(kind, it, "cancelled at confirm", "cancelled")
                self.notify("Cancelled. Nothing sent.", timeout=2, markup=False)
        self.push_screen(ConfirmSend(verb, it.destination, text, self.bidi), after)

    def action_done(self):
        it = self.current
        if it is None:
            return
        self._enqueue("done", it, f"{'Dismissed' if it.kind == 'fyi' else 'Done'}: {it.who}")

    def action_snooze(self):
        it = self.current
        if it is None:
            return

        def after(res):
            if res:
                until, label = res
                self._enqueue("snooze", it, f"Snoozed {label}: {it.who}", until=until, detail=label)
        self.push_screen(SnoozePicker(it.who), after)

    def action_open(self):
        it = self.current
        if it is None:
            return
        res = self.actions.open_link(it)
        self.record("open", it, res.detail, "ok" if res.ok else "failed")
        self.notify(res.detail if res.ok else f"Not opened: {res.detail}", timeout=3,
                    severity="information" if res.ok else "warning", markup=False)

    def action_refresh(self):
        self.poll(manual=True)

    def action_audit(self):
        self.push_screen(AuditScreen(self.audit))

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_toggle_theme(self):
        dark = self.theme == themes.theme_id("dark")
        self.theme = themes.theme_id("light" if dark else "dark")

    def action_toggle_bidi(self):
        if self.bidi_mode == "logical" and not bidi.available():
            self.notify("python-bidi is not installed: visual order is unavailable.",
                        timeout=3, severity="warning", markup=False)
            return
        self.bidi_mode = "logical" if self.bidi else "visual"
        self.notify(f"Hebrew order: {self.bidi_mode}", timeout=2, markup=False)
        self.items_redraw_all()

    def items_redraw_all(self):
        """Re-render every widget that shows text (queue, thread, draft, header)."""
        self.redraw()
        it = self.current
        if it is not None and not self.editing:
            self.query_one("#draft-view", Static).update(self.draft_text(it, it.draft, False))

    async def action_quit(self):
        """Quitting cancels pending sends/nudges (nothing is sent). Done, ack
        and snooze are committed."""
        cancelled = 0
        for p in list(self.pending):
            if p.timer:
                p.timer.stop()
            p.done = True
            self.pending.remove(p)
            if p.kind in ("send", "nudge"):
                p.audit["status"] = "cancelled on quit"
                cancelled += 1
            else:
                try:
                    res = self._run(p)
                except Exception as exc:
                    res = Result(False, str(exc))
                p.audit["status"] = "committed" if res.ok else "failed"
        self.exit(message=f"Nothing sent: {cancelled} pending send(s) cancelled on quit." if cancelled else None)

    # ---------- poll ----------
    def _safe_fetch(self):
        try:
            return list(self.fetch())
        except Exception as exc:  # a source bug must never end the session
            return [SourceResult("inbox", [], False, None,
                                 f"poll failed: {type(exc).__name__}: {exc}")]

    @work(exclusive=True, group="poll")
    async def poll(self, manual=False):
        self.next_poll_at = time.time() + self.poll_s
        self.busy += 1
        try:
            results = await asyncio.to_thread(self._safe_fetch)
        finally:
            self.busy -= 1
        self.apply_results(results, manual=manual)

    def apply_results(self, results, manual=False):
        """Merge a poll into the queue. Runs on the UI thread."""
        self.results = list(results)
        self.last_poll_at = time.time()
        self.next_poll_at = self.last_poll_at + self.poll_s
        old = {i.key: i for i in self.all_rows}
        rows = []
        for res in self.results:
            for raw in res.items:
                it = from_v1(raw)
                prev = old.get(it.key)
                if prev is not None and prev.streamed:
                    it.draft, it.streamed = prev.draft, True
                rows.append(it)
        self.all_rows = rows
        fresh = [i for i in rows if i.key not in self.known_ids
                 and i.section == "urgent" and not self.store.is_hidden(i.key)]
        self.known_ids.update(i.key for i in rows)
        first = not self.polled_once
        self.polled_once = True
        self.refilter(redraw=False)
        self.redraw()
        problems = tuple(self.source_problems())
        if problems and problems != self._last_problems:
            self.notify("\n".join(problems[:3]), title="Source problem", severity="warning",
                        timeout=8, markup=False)
        self._last_problems = problems
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
            ("Nudge", "Send the nudge draft (confirm first)", self.action_nudge),
            ("Open link", "Open the item's link in the browser", self.action_open),
            ("Undo last action", "Within the 5 s window", self.action_undo),
            ("Poll now", "Poll every source", self.action_refresh),
            ("Toggle dark/light theme", "ctrl+t", self.action_toggle_theme),
            ("Show audit log", "Every action with its time", self.action_audit),
            ("Toggle Hebrew order", "logical / visual (python-bidi)", self.action_toggle_bidi),
            ("Filter queue", "Fuzzy filter", self.action_filter),
            ("Keyboard help", "?", self.action_help),
        ] + [(f"Jump to {t}", f"key {n}", (lambda n=n: self.action_section(n)))
             for n, (_k, t) in enumerate(SECTIONS, 1)]
        for title, helptext, cb in cmds:
            yield SystemCommand(title, helptext, cb)
