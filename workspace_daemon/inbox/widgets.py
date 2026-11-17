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
from . import health as healthmod
from .models import SOURCES, fmt_age, thread_fixed, thread_note
from .views import VIEWS

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SAVED_GLYPH = "◆"  # an item in memory (saved here with m, or by the daemon)
THREAD_GLYPH = "◇"  # only the item's Slack thread is in memory
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


def rel(now, ts):
    """`3m ago` for the past, `in 2h` for a time ahead (an invite's start)."""
    if ts > now + 60:
        return "in " + fmt_age(ts - now)
    return f"{fmt_age(now - ts)} ago"


def ago(now, ts):
    """`just now` under a minute, else `3m ago`."""
    return "just now" if now - ts < 60 else f"{fmt_age(now - ts)} ago"


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
    """The grouped queue. Rows are Options keyed `i:<item id>`; the group
    headers (a section, a person, a routine) are disabled Options."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.rows = []
        self.first_idx = {}

    def build(self, groups, pal, now, keep=None, ghost=None, ghost_group=None,
              ghost_at=None, visual=False):
        """Draw `groups` (`views.Group`). A group with no rows is skipped,
        unless it is `keep_empty` (then a dash stands in). `ghost` is a row
        that just left `ghost_group` at position `ghost_at`."""
        width = max(24, (self.size.width or 40) - 1)
        opts = []
        self.rows = []
        first_idx = {}
        for g in groups:
            rows = g.items
            if not rows and not g.keep_empty and not (
                    ghost is not None and ghost_group == g.key):
                continue
            col = pal["urgent"] if g.hot else pal["muted"]
            head = Text()
            head.append(g.title, Style(color=col, bold=True))
            head.append("  " + (g.tail or str(len(rows))),
                        Style(color=col if g.hot else pal["muted"]))
            opts.append(Option(head, disabled=True, id=f"hdr:{g.key}"))
            first_idx[g.key] = len(opts)
            if not rows and ghost is None:
                opts.append(Option(Text("  –", Style(color=pal["muted"])),
                                   disabled=True, id=f"empty:{g.key}"))
                first_idx[g.key] = -1
                continue
            for n, it in enumerate(rows):
                if ghost is not None and ghost_group == g.key and ghost_at == n:
                    opts.append(self._ghost_opt(ghost, width, pal, visual))
                opts.append(Option(self._row(it, width, pal, now, visual),
                                   id=ITEM_PREFIX + it.key))
                self.rows.append(it)
            if (ghost is not None and ghost_group == g.key
                    and ghost_at is not None and ghost_at >= len(rows)):
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
        arrow = it.direction
        if arrow:      # ← waits on you, → you wait on them
            t.append(arrow + " ", Style(color=pal["muted"], bold=True))
        who = fit(vis(it.who, visual), max(8, width // 2 - 2))
        t.append(who, Style(bold=True, color=pal["urgent"] if urgent else None))
        if it.meta.get("kind") == "invite":
            t.append(" · invite", Style(color=pal["muted"]))
        if it.hint:
            t.append(" !", Style(color=pal["warn"], bold=True))
        if it.saved:  # saved to memory (m): a small glyph, the row stays
            t.append(" " + SAVED_GLYPH, Style(color=pal["ok"]))
        elif it.thread_memory_id:  # its thread is in memory, not this message
            t.append(" " + THREAD_GLYPH, Style(color=pal["muted"]))
        marked = it.saved or bool(it.thread_memory_id)
        where = fit(it.where, max(0, width - cell_len(who) - cell_len(age) - 9
                                  - (2 if arrow else 0)
                                  - (9 if it.meta.get("kind") == "invite" else 0)
                                  - (2 if marked else 0) - (2 if it.hint else 0)))
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

    def show(self, item, pal, now, visual, newest_first=False):
        """Draw the item and its thread. The thread is stored oldest first;
        with `newest_first` the latest message is drawn first (the note and
        the item header stay on top) and a dim marker closes the list at the
        thread's first message."""
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
        if item.direction == "←":
            out.append(f"← {vis(item.who, visual)} waits on you · asked "
                       f"{ago(now, item.ts)}\n", Style(color=pal["muted"]))
        elif item.direction == "→":
            out.append(f"→ you wait on {vis(item.who, visual)} · asked "
                       f"{ago(now, item.ts)}\n", Style(color=pal["muted"]))
        if item.hint:  # e.g. an RSVP the calendar never confirmed
            out.append("! " + item.hint + "\n", Style(color=pal["warn"], bold=True))
        if item.memory_id:  # already saved to memory (the ◆ of the row)
            out.append(f"{SAVED_GLYPH} In memory: {item.memory_id}\n",
                       Style(color=pal["ok"]))
        elif item.thread_memory_id:
            out.append(f"{THREAD_GLYPH} Thread in memory: "
                       f"{item.thread_memory_id}\n", Style(color=pal["muted"]))
        note = thread_note(item)
        if note:  # one dim line above the thread; alone when the thread is empty
            out.append(vis(note, visual) + "\n", Style(color=pal["muted"], dim=True))
            if item.messages:
                out.append("\n")
        elif not item.messages:
            out.append(vis(item.gist, visual) + "\n", Style(color=pal["fg"]))
        newest_first = newest_first and not thread_fixed(item)
        messages = list(reversed(item.messages)) if newest_first \
            else item.messages
        for m in messages:
            when = dt.datetime.fromtimestamp(m.ts).strftime("%H:%M")
            out.append(f"{vis(m.who, visual)}", Style(bold=True, color=pal["mine"] if m.mine else pal["fg"]))
            out.append(f"  {when}  {rel(now, m.ts)}\n", Style(color=pal["muted"]))
            out.append_text(body_text(item, m.text, pal, width, visual))
            out.append("\n\n")
        if newest_first and len(messages) > 1 and not note:
            # With a thread note the list may be a cut one (the note says so):
            # the last message drawn is then not known to be the first.
            out.append("─ thread start ─\n", Style(color=pal["muted"], dim=True))
        body.update(out)
        self.scroll_home(animate=False)


def _wrapped(out, text, width, visual, style=None, indent=""):
    """Append `text` wrapped to `width` cells (visual order when `visual`)."""
    for line in bidi.visual_lines(text, max(10, width - len(indent)), visual) or [""]:
        out.append(indent + line + "\n", style)


class ContextPane(VerticalScroll):
    """The Context pane: the AI summary on top, then the sections the context
    command returned. It only draws a view built by the app (see
    `InboxApp.context_view`); every string in it is source text and goes in as
    plain `Text`, never markup."""

    can_focus = True

    def __init__(self, **kw):
        super().__init__(**kw)
        self._ident = None

    def compose(self) -> ComposeResult:
        yield SelectableText("", id="context-body")

    def show(self, view, pal, now, visual):
        body = self.query_one("#context-body", Static)
        width = max(30, (self.size.width or 60) - 4)
        out = Text()
        muted = Style(color=pal["muted"])
        head = Style(bold=True, color=pal["accent"])
        summ, ctx = view["summary"], view["context"]
        if summ["state"] != "off":
            out.append("Summary", head)
            if summ["state"] == "loading":
                out.append("  " + SPINNER[int(now * 10) % len(SPINNER)]
                           + " summarising…\n", muted)
            elif summ["state"] == "ready":
                age = ""
                if summ["at"]:
                    age = ("cached, " if summ["cached"] else "") + ago(now, summ["at"])
                out.append(f"  {age}\n", muted)
                for ln in summ["text"].split("\n"):
                    _wrapped(out, ln, width, visual, indent="  ")
                if summ["note"]:
                    out.append("  " + summ["note"] + "\n", Style(color=pal["warn"], italic=True))
            elif summ["state"] == "error":
                out.append("\n")
                _wrapped(out, "! " + summ["detail"], width, visual,
                         Style(color=pal["warn"], bold=True), "  ")
                out.append("  I retries\n", muted)
            else:
                out.append("  press i to summarise\n", muted)
            out.append("\n")
        if view["item"] is None:
            out.append("Nothing selected.\n", Style(color=pal["muted"], italic=True))
        elif ctx["state"] == "off":
            out.append("Context not configured (set context: in inbox.yaml).\n",
                       Style(color=pal["muted"], italic=True))
        elif ctx["state"] == "loading":
            out.append(SPINNER[int(now * 10) % len(SPINNER)] + " Loading context…\n", muted)
        elif ctx["state"] == "idle":
            out.append("Context not loaded. C to load it.\n", muted)
        elif ctx["state"] == "error":
            out.append("Context\n", head)
            _wrapped(out, "! " + ctx["message"], width, visual,
                     Style(color=pal["warn"], bold=True), "  ")
            out.append(f"  failed {ago(now, ctx['at'])}  ·  "
                       + ("retrying…" if ctx.get("refreshing") else "C retries")
                       + "\n", muted)
        else:
            for sec in ctx["sections"]:
                _wrapped(out, sec["title"], width, visual, head)
                for ln in sec["lines"]:
                    _wrapped(out, ln, width, visual, indent="  ")
                out.append("\n")
            if not ctx["sections"]:
                out.append("No context for this item.\n", muted)
                if ctx.get("message"):
                    _wrapped(out, ctx["message"], width, visual, muted)
            if ctx.get("stale"):
                out.append(f"context stale, fetched {ago(now, ctx['at'])}"
                           + ("  ·  refreshing…" if ctx.get("refreshing")
                              else "  ·  C refresh") + "\n",
                           Style(color=pal["warn"], italic=True))
            else:
                out.append(f"context {'cached, ' if now - ctx['at'] >= 60 else ''}"
                           f"fetched {ago(now, ctx['at'])}  ·  C refresh\n", muted)
        body.update(out)
        if view["ident"] != self._ident:
            self._ident = view["ident"]
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


STATE_COLORS = {healthmod.OK: "ok", healthmod.WARN: "warn",
                healthmod.FAIL: "urgent", healthmod.BUSY: "muted",
                healthmod.OFF: "muted"}


class StatusHeader(Static):
    """The tab bar: the version, the five views with their counts, then the
    freshness and the clock."""

    def render_header(self, app, pal, width):
        now = time.time()
        counts = app.view_counts()
        left = Text(no_wrap=True)
        left.append(" inbox ", Style(color=pal["bg"], bgcolor=pal["primary"], bold=True))
        left.append(f" {__version__} ", Style(color=pal["muted"], dim=True))
        if app.demo:
            left.append(" demo ", Style(color=pal["bg"], bgcolor=pal["warn"], bold=True))
        for n, (key, title) in enumerate(VIEWS, 1):
            on = app.view == key
            count = counts.get(key)
            label = f"{n} {title}" + (f" {count}" if count else "")
            if key == "health" and counts.get("health"):
                label = f"{n} {title} ⚠{counts['health']}"
            if on:
                left.append(f" [{label}]", Style(color=pal["primary"], bold=True))
            else:
                hot = key == "health" and counts.get("health")
                left.append(f"  {label}", Style(color=pal["warn"] if hot else pal["muted"]))
        if getattr(app, "show_all", False):
            left.append("  all ", Style(color=pal["bg"], bgcolor=pal["accent"], bold=True))
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
            mid.append("  ⚠ " + fit(line, 36), Style(color=pal["warn"], bold=True))
        right = Text(no_wrap=True)
        if width >= 150:
            right.append(f"bidi: {app.bidi_mode}  ", Style(color=pal["muted"]))
        right.append(dt.datetime.now().strftime("%H:%M" if width < 120 else "%H:%M:%S") + " ",
                     Style(color=pal["fg"], bold=True))
        gap1 = max(1, (width - left.cell_len - mid.cell_len - right.cell_len) // 2)
        gap2 = max(1, width - left.cell_len - gap1 - mid.cell_len - right.cell_len)
        out = Text(no_wrap=True, overflow="crop")
        out.append_text(left)
        out.append(" " * gap1)
        out.append_text(mid)
        out.append(" " * gap2)
        out.append_text(right)
        return out


class HealthStrip(Static):
    """One line under the tab bar: a cell per routine group, the spend and
    the heartbeats, from the latest health snapshot."""

    @staticmethod
    def act_part(app, pal):
        """The acts part of the strip (0.10.0): `N not synced` while ops wait
        for a retry, and `shadow` when the source's last reply said it only
        logged the op. Empty (and so invisible) without acts."""
        fn = getattr(app, "act_status", None)
        pending, shadow = fn() if fn else (0, False)
        out = Text(no_wrap=True, overflow="crop")
        if pending:
            out.append(f" {pending} not synced",
                       Style(color=pal["warn"], bold=True))
        if shadow:
            out.append(" shadow", Style(color=pal["accent"], bold=True))
        return out

    def render_strip(self, app, pal, width):
        lead = self.act_part(app, pal)
        if not lead.plain:                  # no acts part: the 0.9.0 strip
            return self._render_health(app, pal, width)
        if app.health_fetch is None:
            return lead
        out = Text(no_wrap=True, overflow="crop")
        out.append_text(lead)
        out.append_text(self._render_health(
            app, pal, max(10, width - lead.cell_len)))
        return out

    def _render_health(self, app, pal, width):
        snap = app.health_snap
        out = Text(no_wrap=True, overflow="crop")
        if snap is None:
            out.append(" health: reading…", Style(color=pal["muted"]))
            return out
        cells = healthmod.strip_cells(snap, time.time(), app.alert_infos())
        if not cells:
            msg = snap.get("error") or "no routines found"
            out.append(" health: " + fit(msg, max(10, width - 10)),
                       Style(color=pal["warn"] if snap.get("error") else pal["muted"]))
            return out
        out.append(" ")
        used = 1
        for n, (state, text) in enumerate(cells):
            piece = ("  " if n else "") + text
            if used + cell_len(piece) > width - 1:
                out.append(" …", Style(color=pal["muted"]))
                break
            if n:
                out.append("  ")
            out.append(text, Style(color=pal[STATE_COLORS[state]],
                                   bold=state in (healthmod.WARN, healthmod.FAIL)))
            used += cell_len(piece)
        return out


def render_routines(counts, muted, selected, pal, total):
    """The Feed's routine list: `all`, each routine with its count, then the
    muted rows (hidden by a rule)."""
    out = Text(no_wrap=True, overflow="ellipsis")
    mark = lambda on: "▸ " if on else "  "
    out.append(mark(selected is None) + f"{'all':<16}{total:>4}\n",
               Style(bold=selected is None, color=pal["primary"] if selected is None else None))
    for name, n in counts:
        on = name == selected
        out.append(mark(on) + f"{fit(name, 16):<16}{n:>4}\n",
                   Style(bold=on, color=pal["primary"] if on else None))
    if muted:
        out.append("── muted ────────────\n", Style(color=pal["muted"]))
        for label, n in muted:
            out.append(f"  {fit(label, 16):<16}{n:>4} ⊘\n", Style(color=pal["muted"]))
    return out


def _age_since(now, epoch):
    return fmt_age(now - epoch) if epoch else "-"


def render_health(snap, needs, pal, now, width, visual=False):
    """The Health view: NEEDS YOU, the routine table, the connectors, the
    spend and the heartbeats. `needs` is `app.needs_you()`."""
    out = Text()
    muted = Style(color=pal["muted"])
    if snap is None:
        out.append("Reading the daemon state…\n", muted)
        return out
    if snap.get("error"):
        out.append("! " + snap["error"] + "\n", Style(color=pal["warn"], bold=True))
    out.append(f"NEEDS YOU {len(needs)}\n", Style(bold=True, color=pal["urgent"] if needs else pal["muted"]))
    if not needs:
        out.append("  nothing needs you\n", muted)
    from .sources import cause_label
    for entry in needs:
        info = entry["info"]
        name = entry["routine"] or entry["key"]
        why = cause_label(info.get("cause")) if info.get("cause") else ""
        if why and info.get("connector"):
            why += f" ({info['connector']})"
        text = why or info.get("title") or entry["key"]
        since = info.get("since") if isinstance(info.get("since"), float) else entry["since"]
        when = f" (since {_age_since(now, since)} ago)" if since else ""
        out.append("✗ ", Style(color=pal["urgent"], bold=True))
        out.append(f"{fit(vis(name, visual), 22):<22}", Style(bold=True))
        out.append(vis(text, visual) + when + "\n")
        for label, field in (("last error", "last_error"), ("→", "remedy")):
            if info.get(field):
                lead = " " * 24 + (f"{label}: " if label != "→" else "→ ")
                out.append(lead, muted)
                out.append(fit(vis(info[field], visual), max(20, width - len(lead) - 4)) + "\n")
    out.append("\n")
    nw = min(22, max([len(r["id"]) for r in snap["routines"]] + [8]))
    out.append(f"  {'ROUTINES':<{nw}}  {'state':<9} {'every':<12} {'last run':<9} {'last capture':<13} note\n", muted)
    for r in snap["routines"]:
        glyph = healthmod.GLYPHS[r["state"]]
        col = pal[STATE_COLORS[r["state"]]]
        state_word = {"ok": "ok", "warn": "attention", "fail": "failing",
                      "busy": "busy", "off": "off"}[r["state"]]
        out.append(f"{glyph} ", Style(color=col, bold=True))
        out.append(f"{fit(r['id'], nw):<{nw}}  ")
        out.append(f"{state_word:<9} ", Style(color=col))
        out.append(f"{fit(r['every'], 12):<12} {fit(r['last_run'], 9):<9} "
                   f"{fit(r['last_capture'], 13):<13} ")
        out.append((r["note"] or "-") + "\n", muted)
    if not snap["routines"]:
        out.append("  no routines configured\n", muted)
    out.append("\n")
    out.append("CONNECTORS  ", Style(bold=True))
    conns = snap.get("connectors") or []
    if not conns:
        out.append("none seen\n", muted)
    for n, c in enumerate(conns):
        if n:
            out.append(" · ", muted)
        out.append(healthmod.connector_label(c["name"]) + " ")
        out.append(healthmod.GLYPHS[c["state"]], Style(color=pal[STATE_COLORS[c["state"]]], bold=True))
    if conns:
        out.append("\n")
    spend = snap.get("spend") or {}
    out.append("SPEND       ", Style(bold=True))
    if spend.get("total") is None:
        out.append("not recorded\n", muted)
    else:
        out.append(f"${spend['total']:.2f} today"
                   + (f" of ${spend['cap']:g} cap" if spend.get("cap") is not None else "") + "\n")
    beats = snap.get("heartbeats") or []
    if beats:
        out.append("HEARTBEATS  ", Style(bold=True))
        for n, b in enumerate(beats):
            if n:
                out.append(" · ", muted)
            out.append(f"{b['name']} ")
            out.append(healthmod.GLYPHS[b["state"]], Style(color=pal[STATE_COLORS[b["state"]]], bold=True))
            if b.get("age_s") is not None:
                out.append(f" {fmt_age(b['age_s'])}", muted)
            if b.get("note"):
                out.append(f" {b['note']}", muted)
        out.append("\n")
    return out


def scheduler_title(snap, now):
    """The Health pane's title: scheduler state, the last tick and the spend."""
    if snap is None:
        return "Health"
    sch = snap.get("scheduler") or {}
    bits = ["Health"]
    if sch.get("paused"):
        bits.append("PAUSED")
    elif sch.get("armed") is True:
        bits.append("scheduler armed")
    elif sch.get("armed") is False:
        bits.append("scheduler not armed")
    if sch.get("last_tick"):
        bits.append(f"last tick {fmt_age(now - sch['last_tick'])}")
    spend = snap.get("spend") or {}
    if spend.get("total") is not None:
        bits.append(f"spend ${spend['total']:.0f}"
                    + (f" of ${spend['cap']:.0f}" if spend.get("cap") is not None else ""))
    return " · ".join(bits)


class RoutinePane(Static):
    """The Feed's left pane (a list the app draws; `f` moves the marker)."""


class HealthView(VerticalScroll):
    can_focus = True

    def compose(self) -> ComposeResult:
        yield SelectableText("", id="health-body")

    def show(self, text):
        self.query_one("#health-body", Static).update(text)
