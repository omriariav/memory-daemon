"""Modal screens: confirm-send, snooze picker, help, audit log.

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

from . import bidi


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
    ("Move", [("j / k  ↓ / ↑", "next / previous"), ("g / G", "top / bottom"), ("1-4", "jump to section"),
              ("tab / shift+tab", "switch pane"), ("/", "fuzzy filter"), ("f", "filter by source"), ("esc", "clear filters / back")]),
    ("Act", [("a", "approve draft (confirm first) / ack"), ("e", "edit draft  (esc / ctrl+s done)"),
             ("d", "done / dismiss"), ("s", "snooze"), ("n", "nudge (confirm first)"),
             ("o", "open the item's link"), ("u", "undo last action (5 s)"), ("r", "refresh / poll now")]),
    ("Copy", [("y", "copy this item (sender, where, gist, link)"), ("Y", "copy every item in this section"),
              ("R", "show / hide resolved alerts"), ("ctrl+p", "palette: copy section as Markdown"),
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
