"""Modal screens: confirm-send, confirm-write, snooze picker, help, audit log.

Anything that came from a source (names, text) is shown through Rich `Text`
objects, never markup strings, so a title like "[b]x[/]" cannot restyle or
break the UI.
"""
import datetime as dt
import re
import time

from rich.style import Style
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__, bidi
from .models import SOURCES
from .widgets import fit, fmt_age, palette, vis


class ConfirmSend(ModalScreen):
    """Shows the exact destination and text. Cancel is focused by default."""

    AUTO_FOCUS = "#cancel"
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "send", "Send"),
                Binding("left,right", "toggle_focus", show=False)]

    def __init__(self, verb, destination, text, visual=False):
        super().__init__()
        self.verb, self.destination, self.text, self.visual = verb, destination, text, visual

    def compose(self) -> ComposeResult:
        body = Text()
        for ln in bidi.visual_lines(self.text, 64, self.visual):
            body.append(ln + "\n")
        body.rstrip()
        dest = Text()
        dest.append("To  ", Style(dim=True))
        dest.append(self.destination, Style(bold=True))
        with Vertical(id="confirm"):
            yield Static(Text(self.verb, Style(bold=True)), id="confirm-title")
            yield Static(dest, id="confirm-dest")
            yield Static(body, id="confirm-text")
            yield Static(Text("Nothing is sent until you confirm. Undo stays open for the grace period.", Style(dim=True)),
                         id="confirm-note")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel  esc", id="cancel", variant="default")
                yield Button(f"{self.verb}  y", id="send", variant="error")

    def on_button_pressed(self, e):
        self.dismiss(e.button.id == "send")

    def action_cancel(self):
        self.dismiss(False)

    def action_send(self):
        self.dismiss(True)

    def action_toggle_focus(self):
        self.focus_next()


class ConfirmWrite(ModalScreen):
    """Confirm an action that leaves the inbox (an RSVP, a save). Shows what
    will happen as label/value rows, in plain Text so item text can never
    restyle the screen. Cancel is focused by default; `y` confirms."""

    AUTO_FOCUS = "#cancel"
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "confirm", "Confirm"),
                Binding("left,right", "toggle_focus", show=False)]

    def __init__(self, title, rows, verb, note="", visual=False):
        super().__init__()
        self.title_text, self.rows, self.verb = title, rows, verb
        self.note, self.visual = note, visual

    def compose(self) -> ComposeResult:
        body = Text()
        for label, value in self.rows:
            if not value:
                continue
            body.append(f"{label:<8}", Style(dim=True))
            lines = bidi.visual_lines(value, 56, self.visual) or [""]
            body.append(lines[0] + "\n", Style(bold=True))
            for ln in lines[1:]:
                body.append(" " * 8 + ln + "\n", Style(bold=True))
        body.rstrip()
        with Vertical(id="confirm"):
            yield Static(Text(self.title_text, Style(bold=True)), id="confirm-title")
            yield Static(body, id="confirm-text")
            if self.note:
                yield Static(Text(self.note, Style(dim=True)), id="confirm-note")
            yield Static(Text("Nothing happens until you confirm. Undo stays open for the grace period.",
                              Style(dim=True)), id="confirm-fixed")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel  esc", id="cancel", variant="default")
                yield Button(f"{self.verb}  y", id="send", variant="error")

    def on_button_pressed(self, e):
        self.dismiss(e.button.id == "send")

    def action_cancel(self):
        self.dismiss(False)

    def action_confirm(self):
        self.dismiss(True)

    def action_toggle_focus(self):
        self.focus_next()


def parse_snooze(s, now):
    """'90m', '2h', '1d', '17:30' -> epoch, else None."""
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(m|min|h|hr|d)?", s)
    if m:
        n, u = int(m.group(1)), (m.group(2) or "m")
        return now + n * {"m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}[u]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        t = dt.datetime.fromtimestamp(now).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t.timestamp() <= now:
            t += dt.timedelta(days=1)
        return t.timestamp()
    return None


def tomorrow_0900(now):
    d = dt.datetime.fromtimestamp(now) + dt.timedelta(days=1)
    return d.replace(hour=9, minute=0, second=0, microsecond=0).timestamp()


class SnoozePicker(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("1", "pick(0)", show=False),
                Binding("2", "pick(1)", show=False), Binding("3", "pick(2)", show=False),
                Binding("4", "pick(3)", show=False)]

    def __init__(self, who, now=None):
        super().__init__()
        self.who, self.now = who, now or time.time()

    def compose(self) -> ComposeResult:
        title = Text()
        title.append("Snooze ", Style(bold=True))
        title.append(self.who, Style(dim=True))
        with Vertical(id="snooze"):
            yield Static(title, id="snooze-title")
            yield OptionList(
                Option("[b]1[/b]  In 1 hour", id="1h"), Option("[b]2[/b]  In 3 hours", id="3h"),
                Option("[b]3[/b]  Tomorrow 09:00", id="tom"), Option("[b]4[/b]  Custom…", id="custom"),
                id="snooze-list")
            yield Input(placeholder="e.g. 90m, 2h, 1d, 17:30  (enter to confirm)", id="snooze-input")
            yield Static(Text("enter pick  ·  1-4 quick pick  ·  esc cancel", Style(dim=True)), id="snooze-hint")

    def on_mount(self):
        self.query_one("#snooze-input").display = False
        self.query_one("#snooze-list").focus()

    def action_cancel(self):
        self.dismiss(None)

    def action_pick(self, i):
        if self.query_one("#snooze-input").display:
            return
        self._choose(["1h", "3h", "tom", "custom"][i])

    def on_option_list_option_selected(self, e):
        self._choose(e.option.id)

    def _choose(self, key):
        if key == "1h":
            self.dismiss((self.now + 3600, "1h"))
        elif key == "3h":
            self.dismiss((self.now + 3 * 3600, "3h"))
        elif key == "tom":
            self.dismiss((tomorrow_0900(self.now), "tomorrow 09:00"))
        else:
            self.query_one("#snooze-list").display = False
            inp = self.query_one("#snooze-input")
            inp.display = True
            inp.focus()

    def on_input_submitted(self, e):
        t = parse_snooze(e.value, self.now)
        if t is None:
            self.query_one("#snooze-hint", Static).update(
                Text("Not understood. Try 90m, 2h, 1d or 17:30", Style(bold=True, color="red")))
            return
        self.dismiss((t, e.value.strip()))


HELP = [
    ("Move / filter", [("j / k  ↓ / ↑", "next / previous"), ("g / G", "top / bottom"), ("1-4", "jump to section"),
              ("tab / shift+tab", "switch pane"), ("/", "fuzzy filter"), ("f", "filter by source"),
              ("R", "show / hide resolved alerts"),
              ("A", "show all: ask sources that support it for everything"),
              ("S", "saved: what the daemon saved to memory (read-only)"),
              ("esc", "clear filters / back")]),
    ("Act", [("a", "approve draft (confirm first) / ack / accept an invite"), ("e", "edit draft  (esc / ctrl+s done)"),
             ("d", "done / dismiss"), ("s", "snooze"), ("n", "nudge (confirm first)"),
             ("o", "open the item's link"), ("u", "undo last action (5 s)"), ("r", "refresh / poll now")]),
    ("RSVP / save", [("a / X / T", "accept / decline / tentative on an invite (rsvp: in inbox.yaml)"),
                     ("m", "save the item to memory (save: in inbox.yaml); ◆ marks a saved item"),
                     ("", "both confirm first, then wait out the 5 s undo window")]),
    ("Context / summary", [("c", "show / hide the context pane (context: in inbox.yaml)"),
                           ("C  (r in pane)", "fetch the context again"),
                           ("i", "AI summary of the item, shown in the pane (summary: in inbox.yaml)"),
                           ("I", "ask for a new summary even if one is cached"),
                           ("", "read-only: nothing is confirmed or changed")]),
    ("Copy", [("y", "copy this item (sender, where, gist, link)"), ("Y", "copy every item in this section"),
              ("ctrl+p", "palette: copy section as Markdown"),
              ("mouse drag", "select text in the thread or draft pane")]),
    ("App", [("ctrl+p", "command palette"), ("ctrl+t", "dark / light theme"), ("L", "audit log"),
             ("b", "toggle Hebrew order (logical/visual)"), ("?", "this help"), ("q", "quit")]),
]


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "close", "Close")]

    def compose(self) -> ComposeResult:
        t = Text()
        for title, rows in HELP:
            t.append(f"{title}\n", Style(bold=True, underline=True))
            for k, d in rows:
                t.append(f"  {k:<16}", Style(bold=True))
                t.append(f"{d}\n")
            t.append("\n")
        t.rstrip()
        head = Text()
        head.append("inbox", Style(bold=True))
        head.append(f" v{__version__}", Style(dim=True))
        head.append("  keyboard reference  (esc to close)", Style(dim=True))
        with Vertical(id="help"):
            yield Static(head, id="help-title")
            yield Static(t, id="help-body")

    def action_close(self):
        self.dismiss(None)


class AuditScreen(ModalScreen):
    BINDINGS = [Binding("escape,L,q", "close", "Close")]

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        head = Text()
        head.append("Audit log", Style(bold=True))
        head.append("  every action, newest first (esc to close)", Style(dim=True))
        with Vertical(id="audit"):
            yield Static(head, id="audit-title")
            yield DataTable(id="audit-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self):
        tb = self.query_one(DataTable)
        tb.add_columns("Time", "Action", "Item", "Detail", "Status")
        for r in reversed(self.rows):
            tb.add_row(dt.datetime.fromtimestamp(r["time"]).strftime("%H:%M:%S"),
                       Text(r["action"]), Text(r["item"]), Text(r["detail"]), Text(r["status"]))
        if not self.rows:
            tb.add_row("", Text("no actions yet"), "", "", "")
        tb.focus()

    def action_close(self):
        self.dismiss(None)


class SavedScreen(ModalScreen):
    """What the daemon saved to memory recently. Read-only: these rows are not
    inbox items, so nothing here can be acted on except opening a link."""

    BINDINGS = [Binding("escape,S,q", "close", "Close"), Binding("o", "open", "Open"),
                Binding("j", "cursor(1)", show=False), Binding("k", "cursor(-1)", show=False)]

    def __init__(self, rows, error=None, days=None, visual=False, opener=None):
        super().__init__()
        self.rows, self.error, self.days = rows, error, days
        self.visual, self.opener = visual, opener

    def compose(self) -> ComposeResult:
        head = Text()
        head.append("Saved to memory", Style(bold=True))
        span = f"  last {self.days:g} days" if self.days else ""
        head.append(f"{span}  ·  o open  ·  esc back  (read-only)", Style(dim=True))
        with Vertical(id="saved"):
            yield Static(head, id="saved-title")
            yield DataTable(id="saved-table", cursor_type="row", zebra_stripes=True)

    def on_mount(self):
        tb = self.query_one("#saved-table", DataTable)
        tb.add_columns("", "Title", "Time", "Kind")
        self._fill(tb)
        tb.focus()

    def _fill(self, tb):
        pal = palette(self.app)
        now = time.time()
        for row in self.rows:
            glyph, color, _label = SOURCES.get(row["source"], SOURCES["other"])
            when = dt.datetime.fromtimestamp(row["ts"]).strftime("%a %H:%M")
            tb.add_row(Text(glyph, Style(color=pal[color], bold=True)),
                       Text(fit(vis(row["title"], self.visual), 90)),
                       Text(f"{when}  {fmt_age(now - row['ts'])} ago"),
                       Text(row["kind"]), key=row["id"])
        if not self.rows:
            msg = self.error or "nothing saved in this period"
            tb.add_row("", Text(msg, Style(italic=True)), "", "")

    def reload(self, rows, error=None):
        """Replace the rows (the daemon saved something new), keeping the
        cursor on the same row when it is still listed."""
        tb = self.query_one("#saved-table", DataTable)
        current = self._current_row()
        keep = current["id"] if current else None
        self.rows, self.error = rows, error
        tb.clear()
        self._fill(tb)
        if keep is not None:
            for n, row in enumerate(self.rows):
                if row["id"] == keep:
                    tb.move_cursor(row=n)
                    break

    def _current_row(self):
        tb = self.query_one("#saved-table", DataTable)
        if not self.rows or tb.row_count == 0:
            return None
        index = tb.cursor_row
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def action_cursor(self, step):
        tb = self.query_one("#saved-table", DataTable)
        tb.action_cursor_down() if step > 0 else tb.action_cursor_up()

    def action_open(self):
        row = self._current_row()
        if row is None or not row.get("link"):
            self.notify("This row has no link", timeout=2, markup=False)
            return
        if self.opener is not None:
            self.opener(row)

    def action_close(self):
        self.dismiss(None)
