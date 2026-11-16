"""Widgets: header, queue, thread view, draft pane, hint bar."""
import datetime as dt
import time

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import OptionList, Static, TextArea
from textual.widgets.option_list import Option

from . import __version__, bidi, slackfmt
from .models import SECTIONS, SOURCES, thread_note

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SAVED_GLYPH = "◆"  # an item saved to memory
ITEM_PREFIX = "i:"  # Option id of an item row; item ids are arbitrary text


def palette(app):
    from textual.color import Color
    v = app.get_css_variables()
    g = v.get
    muted = Color.parse(g("foreground", "#ffffff")).blend(
        Color.parse(g("background", "#000000")), 0.42).hex
    return {
        "fg": g("foreground", "#ffffff"), "muted": muted,
        "primary": g("primary", "#5588ff"), "urgent": g("error", "#ff5555"),
        "warn": g("warning", "#ffaa00"), "ok": g("success", "#55dd77"),
        "accent": g("accent", "#55ccff"), "bg": g("background", "#000000"),
        "panel": g("panel", "#222222"), "mine": g("secondary", "#bb99ff"),
        "link": g("accent", "#55ccff"), "chip_fg": g("background", "#000000"),
        "chip_bg": g("primary", "#5588ff"), "code_bg": g("panel", "#222222"),
        "code_fg": g("warning", "#ffaa00"),
    }


def fmt_age(secs):
    s = max(0, int(secs))
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h" if h >= 3 or not m else f"{h}h{m:02d}m"
    return f"{s // 86400}d"


def rel(now, ts):
    """`3m ago` for the past, `in 2h` for a time ahead (an invite's start)."""
    if ts > now + 60:
        return "in " + fmt_age(ts - now)
    return f"{fmt_age(now - ts)} ago"


def fit(s, width):
    """Truncate to `width` cells with an ellipsis (logical order)."""
    if width <= 0:
        return ""
    if cell_len(s) <= width:
        return s
    while s and cell_len(s) > width - 1:
        s = s[:-1]
    return s + "…"


def vis(s, visual):
    """Single-line version of s for display: visual order when `visual`."""
    return bidi.visual_lines(s, 10**6, visual)[0]


def fuzzy(query, hay):
    """Subsequence match score (higher is better), None when no match."""
    q, h = query.lower(), hay.lower()
    pos, score, prev = 0, 0, -2
    for ch in q:
        i = h.find(ch, pos)
        if i < 0:
            return None
        score += 3 if i == prev + 1 else 1
        if i == 0 or h[i - 1] in " #-_:":
            score += 2
        prev, pos = i, i + 1
    return score


def body_text(item, text, pal, width, visual):
    """A thread message body: Slack mrkdwn for Slack, plain otherwise."""
    if item.source == "slack":
        return slackfmt.render(text, pal, width, visual)
    return slackfmt.render_plain(text, pal, width, visual)


class SelectableText(Static):
    """Static text with Textual's mouse selection (a drag starting here stays
    inside this widget's parent pane; ctrl+c copies it).

    Off in visual bidi mode: the displayed Hebrew is already reordered, so a
    selection would copy backwards text. `y`/`Y` copy the logical text.
    """

    @property
    def allow_select(self):
        return not getattr(self.app, "bidi", False)


class Queue(OptionList):
    """Sectioned queue. Rows are Options keyed `i:<item id>`; headers are
    disabled Options."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rows = []
        self.first_idx = {}

    def build(self, items, counts, pal, now, keep=None, ghost=None,
              ghost_at=None, visual=False):
        width = max(24, (self.size.width or 40) - 1)
        opts = []
        self.rows = []
        first_idx = {}
        by_sec = {k: [i for i in items if i.section == k] for k, _ in SECTIONS}
        for k, title in SECTIONS:
            col = pal["urgent"] if k == "urgent" else pal["muted"]
            head = Text()
            head.append(f"{title.upper()}", Style(color=col, bold=True))
            head.append(f"  {counts.get(k, 0)}", Style(color=col if k == "urgent" else pal["muted"]))
            opts.append(Option(head, disabled=True, id=f"hdr:{k}"))
            rows = by_sec[k]
            first_idx[k] = len(opts)
            if not rows:
                opts.append(Option(Text("  –", Style(color=pal["muted"])), disabled=True, id=f"empty:{k}"))
                first_idx[k] = -1
                continue
            for n, it in enumerate(rows):
                if ghost is not None and ghost.section == k and ghost_at == n:
                    opts.append(self._ghost_opt(ghost, width, pal, visual))
                opts.append(Option(self._row(it, width, pal, now, visual), id=ITEM_PREFIX + it.key))
                self.rows.append(it)
            if ghost is not None and ghost.section == k and ghost_at is not None and ghost_at >= len(rows):
                opts.append(self._ghost_opt(ghost, width, pal, visual))
        self.first_idx = first_idx
        self.clear_options()
        self.add_options(opts)
        target = None
        if keep:
            try:
                target = self.get_option_index(ITEM_PREFIX + keep)
            except Exception:
                target = None
        if target is None and self.rows:
            target = self.get_option_index(ITEM_PREFIX + self.rows[0].key)
        self.highlighted = target

    def _row(self, it, width, pal, now, visual):
        urgent = it.section == "urgent"
        age = rel(now, it.ts) if it.ts > now + 60 else fmt_age(now - it.ts)
        if it.kind == "nudge" and it.draft:
            age = "⏰ " + age
        mark = "●" if urgent else ("↻" if it.kind == "nudge" else ("·" if it.kind == "fyi" else "○"))
        resolved = it.resolved
        if resolved:
            mark = "✓"
        t = Text(no_wrap=True, overflow="ellipsis")
        t.append(f" {mark} ", Style(color=pal["ok"] if resolved else pal["urgent"] if urgent else pal["primary"]))
        glyph, scol, _ = SOURCES.get(it.source, SOURCES["other"])
        t.append(glyph + " ", Style(color=pal[scol], bold=True))
        who = fit(vis(it.who, visual), max(8, width // 2 - 2))
        t.append(who, Style(bold=True, color=pal["urgent"] if urgent else None))
        if it.saved:  # saved to memory (m): a small glyph, the row stays
            t.append(" " + SAVED_GLYPH, Style(color=pal["ok"]))
        where = fit(it.where, max(0, width - cell_len(who) - cell_len(age) - 9
                                  - (2 if it.saved else 0)))
        if where:
            t.append("  " + where, Style(color=pal["muted"]))
        pad = max(1, width - t.cell_len - cell_len(age))
        t.append(" " * pad + age, Style(color=pal["urgent"] if urgent else pal["muted"]))
        t.append("\n")
        gist = fit(it.gist, width - 4)
        t.append("   " + vis(gist, visual), Style(color=pal["muted"]))
        return t

    def _ghost_opt(self, it, width, pal, visual):
        t = Text(no_wrap=True)
        t.append(" ✓ ", Style(color=pal["ok"], bold=True))
        t.append(fit(vis(it.who, visual), width - 6), Style(color=pal["muted"], strike=True))
        t.append("\n   handled", Style(color=pal["ok"], italic=True))
        return Option(t, disabled=True, id="ghost")


class ThreadView(VerticalScroll):
    can_focus = True

    def compose(self) -> ComposeResult:
        yield SelectableText("", id="thread-body")

    def show(self, item, pal, now, visual):
        body = self.query_one("#thread-body", Static)
        width = max(30, (self.size.width or 60) - 4)
        if item is None:
            body.update(Text("Nothing selected.", Style(color=pal["muted"], italic=True)))
            return
        out = Text()
        out.append(vis(item.who, visual), Style(bold=True, color=pal["urgent"] if item.section == "urgent" else pal["primary"]))
        glyph, scol, label = SOURCES.get(item.source, SOURCES["other"])
        out.append("  " + glyph + " ", Style(color=pal[scol], bold=True))
        out.append(f"{label} · {item.where}  ·  {rel(now, item.ts)}\n", Style(color=pal["muted"]))
        out.append("─" * min(width, 60) + "\n", Style(color=pal["muted"]))
        note = thread_note(item)
        if note:  # one dim line above the thread; alone when the thread is empty
            out.append(vis(note, visual) + "\n", Style(color=pal["muted"], dim=True))
            if item.messages:
                out.append("\n")
        elif not item.messages:
            out.append(vis(item.gist, visual) + "\n", Style(color=pal["fg"]))
        for m in item.messages:
            when = dt.datetime.fromtimestamp(m.ts).strftime("%H:%M")
            out.append(f"{vis(m.who, visual)}", Style(bold=True, color=pal["mine"] if m.mine else pal["fg"]))
            out.append(f"  {when}  {rel(now, m.ts)}\n", Style(color=pal["muted"]))
            out.append_text(body_text(item, m.text, pal, width, visual))
            out.append("\n\n")
        body.update(out)
        self.scroll_home(animate=False)


class DraftPane(Vertical):
    can_focus = True

    def compose(self) -> ComposeResult:
        with Horizontal(id="draft-bar"):
            yield Static("", id="badge")
            yield Static("", id="draft-status")
        with VerticalScroll(id="draft-scroll"):
            yield SelectableText("", id="draft-view")
        yield TextArea("", id="draft-edit", soft_wrap=True, show_line_numbers=False, read_only=True)


class HintBar(Static):
    def set_hints(self, hints, pal):
        t = Text(no_wrap=True, overflow="ellipsis")
        for k, label in hints:
            t.append(f" {k} ", Style(color=pal["bg"], bgcolor=pal["muted"], bold=True))
            t.append(f" {label}  ", Style(color=pal["muted"]))
        self.update(t)


class StatusHeader(Static):
    def render_header(self, app, pal, width):
        now = time.time()
        n_open = len(app.items)
        n_urg = sum(1 for i in app.items if i.section == "urgent")
        left = Text(no_wrap=True)
        left.append(" inbox ", Style(color=pal["bg"], bgcolor=pal["primary"], bold=True))
        left.append(f" v{__version__}", Style(color=pal["muted"], dim=True))
        if app.demo:
            left.append(" demo ", Style(color=pal["bg"], bgcolor=pal["warn"], bold=True))
        left.append(f"  {n_open} open", Style(color=pal["fg"]))
        if getattr(app, "show_all", False):
            left.append(" all ", Style(color=pal["bg"], bgcolor=pal["accent"], bold=True))
        if n_urg:
            left.append(f"  ● {n_urg} urgent", Style(color=pal["urgent"], bold=True))
        hidden = app.resolved_hidden
        if hidden:
            left.append(f"  ✓ {hidden} resolved (R)", Style(color=pal["muted"]))
        mid = Text(no_wrap=True)
        if app.busy:
            mid.append(SPINNER[int(now * 10) % len(SPINNER)] + " ", Style(color=pal["accent"]))
        age = app.oldest_update_age()
        if age is None:
            mid.append("polling…", Style(color=pal["muted"]))
        else:
            mid.append(f"updated {fmt_age(age) if age >= 60 else f'{int(age)}s'} ago", Style(color=pal["muted"]))
        for line in app.source_problems()[:1]:
            mid.append("  ⚠ " + fit(line, 48), Style(color=pal["warn"], bold=True))
        right = Text(no_wrap=True)
        right.append(f"bidi: {app.bidi_mode}  ", Style(color=pal["muted"]))
        right.append(dt.datetime.now().strftime("%H:%M:%S") + " ", Style(color=pal["fg"], bold=True))
        if width < 120:
            right = Text(dt.datetime.now().strftime("%H:%M:%S") + " ", Style(bold=True))
        gap1 = max(1, (width - left.cell_len - mid.cell_len - right.cell_len) // 2)
        gap2 = max(1, width - left.cell_len - gap1 - mid.cell_len - right.cell_len)
        out = Text(no_wrap=True, overflow="crop")
        out.append_text(left)
        out.append(" " * gap1)
        out.append_text(mid)
        out.append(" " * gap2)
        out.append_text(right)
        return out
