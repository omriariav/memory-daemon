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
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__, bidi
from .models import SOURCES
from .widgets import fit, fmt_age, palette, vis


CHOICE_LABELS = {"draft": "Draft in Slack", "send": None,
                 "copy": "Copy for Slack"}
CHOICE_KEYS = {"draft": "d", "send": "y", "copy": "c"}


class ConfirmSend(ModalScreen):
    """Shows the exact destination and text. Cancel is focused by default.

    The text is shown line by line as it will be sent (only wrapped to the
    box, never joined), in a scroll pane: down / up (j / k) and the page keys
    scroll it, the buttons never move. `note` is one extra line (for example
    that the text was cut).

    0.7.0: `options` is the actions on offer, in order, from `draft`, `send`
    and `copy` (default just `send`: the old modal). Each has a button and a
    key (`d`, `y`, `c`); only an offered key does anything. The modal
    dismisses with the chosen name, or False for Cancel. `open_url` is what
    `o` will open, shown under the route."""

    AUTO_FOCUS = "#cancel"
    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("y", "choose('send')", "Send"),
                Binding("d", "choose('draft')", "Draft", show=False),
                Binding("c", "choose('copy')", "Copy", show=False),
                Binding("o", "open_url", "Open", show=False),
                Binding("left,right", "toggle_focus", show=False),
                Binding("down,j", "scroll(1)", show=False),
                Binding("up,k", "scroll(-1)", show=False),
                Binding("pagedown", "scroll(10)", show=False),
                Binding("pageup", "scroll(-10)", show=False)]
    INNER = 64

    def __init__(self, verb, destination, text, visual=False, note="",
                 warn="", route="", options=("send",), open_url="",
                 title=None, on_open=None):
        """`route` is the real destination (channel id and thread timestamp),
        shown under the `destination` label."""
        super().__init__()
        self.verb, self.destination, self.text, self.visual = verb, destination, text, visual
        self.note, self.warn, self.route = note, warn, route
        self.options = tuple(o for o in ("draft", "send", "copy")
                             if o in options)
        self.open_url = open_url
        self.on_open = on_open   # opens the item this screen was built for
        self.title_text = title or verb

    def label(self, name):
        if name == "send" and self.options != ("send",):
            return "Send"             # short, so four buttons fit
        return CHOICE_LABELS[name] or self.verb

    def compose(self) -> ComposeResult:
        body = Text()
        for para in self.text.split("\n"):
            for ln in bidi.visual_lines(para, self.INNER, self.visual) or [""]:
                body.append(ln + "\n")
        body.rstrip()
        dest = Text()
        dest.append("To  ", Style(dim=True))
        dest.append(self.destination, Style(bold=True))
        only_send = self.options == ("send",)
        with Vertical(id="confirm", classes="" if only_send else "multi"):
            yield Static(Text(self.title_text, Style(bold=True)), id="confirm-title")
            yield Static(dest, id="confirm-dest")
            if self.route:
                yield Static(Text("Slack  " + self.route, Style(dim=True)),
                             id="confirm-route")
            if self.open_url:
                yield Static(Text("Open (o)  " + self.open_url,
                                  Style(dim=True)), id="confirm-open")
            if self.warn:
                yield Static(Text("⚠ " + self.warn, Style(bold=True, color="yellow")),
                             id="confirm-warn")
            with VerticalScroll(id="confirm-scroll"):
                yield Static(body, id="confirm-text")
            if self.note:
                yield Static(Text(self.note, Style(dim=True)), id="confirm-note")
            if only_send:
                footer = ("Nothing is sent until you confirm. Undo stays open "
                          "for the grace period.")
            elif self.options == ("copy",):
                footer = "Nothing is sent or drafted. Copy is local."
            else:
                footer = ("Nothing happens until you choose. Draft and Send "
                          "wait out the undo window; Copy is local.")
            yield Static(Text(footer, Style(dim=True)), id="confirm-fixed")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel  esc", id="cancel", variant="default")
                for name in self.options:
                    yield Button(f"{self.label(name)}  {CHOICE_KEYS[name]}",
                                 id=name,
                                 variant="error" if name == "send" else "primary")

    def on_button_pressed(self, e):
        self.dismiss(e.button.id if e.button.id in self.options else False)

    # Below this terminal width four buttons do not fit in one row: the
    # button row becomes a two-column grid instead of clipping.
    NARROW = 100

    def on_mount(self):
        self._fit()

    def on_resize(self, _event):
        self._fit()

    def _fit(self):
        try:
            box = self.query_one("#confirm")
        except Exception:
            return
        box.set_class(self.app.size.width < self.NARROW, "narrow")
        self.call_after_refresh(self._cap_scroll)

    def _cap_scroll(self):
        """Size the text pane to the screen: the title and the destination
        lines above it, and the buttons below, must stay visible however long
        the draft is (the pane scrolls)."""
        try:
            pane = self.query_one("#confirm-scroll")
        except Exception:
            return
        pane.styles.max_height = 16
        self.refresh(layout=True)
        self.call_after_refresh(self._cap_scroll_now)

    def _cap_scroll_now(self):
        try:
            box = self.query_one("#confirm")
            pane = self.query_one("#confirm-scroll")
        except Exception:
            return
        chrome = box.region.height - pane.region.height
        room = self.app.size.height - chrome
        pane.styles.max_height = max(3, min(16, room))

    def action_open_url(self):
        """`o` opens the URL shown (the modal stays). It opens the item this
        screen was built for, never whatever is selected by now."""
        if self.open_url and self.on_open is not None:
            self.on_open()

    def action_cancel(self):
        self.dismiss(False)

    def action_choose(self, name):
        if name in self.options:
            self.dismiss(name)

    def action_send(self):
        self.action_choose("send")

    def action_toggle_focus(self):
        self.focus_next()

    def action_scroll(self, step):
        pane = self.query_one("#confirm-scroll")
        if step > 0:
            pane.scroll_down(animate=False) if step == 1 else pane.scroll_page_down(animate=False)
        else:
            pane.scroll_up(animate=False) if step == -1 else pane.scroll_page_up(animate=False)


class ConfirmWrite(ModalScreen):
    """Confirm an action that leaves the inbox (an RSVP, a save). Shows what
    will happen as label/value rows, in plain Text so item text can never
    restyle the screen. Cancel is focused by default; `y` confirms."""

    AUTO_FOCUS = "#cancel"
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "confirm", "Confirm"),
                Binding("left,right", "toggle_focus", show=False),
                Binding("down,j", "scroll(1)", show=False),
                Binding("up,k", "scroll(-1)", show=False),
                Binding("pagedown", "scroll(10)", show=False),
                Binding("pageup", "scroll(-10)", show=False)]
    LABEL_MIN, LABEL_MAX = 8, 16      # the label column, in cells
    INNER = 61                        # label column + value, in cells: fits the box

    def __init__(self, title, rows, verb, note="", visual=False):
        super().__init__()
        self.title_text, self.rows, self.verb = title, rows, verb
        self.note, self.visual = note, visual

    def compose(self) -> ComposeResult:
        shown = [(label, value) for label, value in self.rows if value]
        # The label column fits the longest label, within LABEL_MIN..LABEL_MAX;
        # a longer label is cut to it (with an ellipsis), never overflowing
        # into the value.
        col = min(self.LABEL_MAX, max(
            self.LABEL_MIN, *(len(label) + 1 for label, _ in shown))) \
            if shown else self.LABEL_MIN
        body = Text()
        for label, value in shown:
            label = label if len(label) < col else label[:col - 2] + "…"
            body.append(f"{label:<{col}}", Style(dim=True))
            lines = []
            for para in value.split("\n"):    # a value may have several lines
                lines.extend(bidi.visual_lines(para, self.INNER - col, self.visual)
                             or [""])
            body.append(lines[0] + "\n", Style(bold=True))
            for ln in lines[1:]:
                body.append(" " * col + ln + "\n", Style(bold=True))
        body.rstrip()
        with Vertical(id="confirm"):
            yield Static(Text(self.title_text, Style(bold=True)), id="confirm-title")
            with VerticalScroll(id="confirm-scroll"):
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

    def action_scroll(self, step):
        """The rows may be longer than the box: down / up (j / k) and the page
        keys scroll them; the buttons never move."""
        pane = self.query_one("#confirm-scroll")
        if step > 0:
            pane.scroll_down(animate=False) if step == 1 else pane.scroll_page_down(animate=False)
        else:
            pane.scroll_up(animate=False) if step == -1 else pane.scroll_page_up(animate=False)


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


class ActionBusy(ModalScreen):
    """Shown while an action's command is looking something up (its choices,
    its confirmation rows). `esc` cancels: the app stops the command."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, what, on_cancel):
        super().__init__()
        self.title_text, self.what, self.on_cancel = title, what, on_cancel
        self._frame = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="actbusy"):
            yield Static(Text(self.title_text, Style(bold=True)),
                         id="actbusy-title")
            yield Static("", id="actbusy-body")
            yield Static(Text("esc cancel", Style(dim=True)),
                         id="actbusy-hint")

    def on_mount(self):
        self._tick()
        self.set_interval(0.1, self._tick)

    def _tick(self):
        frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        self._frame += 1
        self.query_one("#actbusy-body", Static).update(
            Text(f"{frames[self._frame % len(frames)]} {self.what}"))

    def action_cancel(self):
        self.on_cancel()
        self.dismiss(None)


class ChoicePicker(ModalScreen):
    """A generic picker over the choices an action command returned. Shown
    even for one choice. Every string is plain `Text`. Dismisses with the
    chosen choice dict, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")] + [
        Binding(str(n), f"pick({n - 1})", show=False) for n in range(1, 10)]

    def __init__(self, title, choices, visual=False, selected=None):
        """`selected` is the id of the choice that starts highlighted (enter
        picks it); it is never picked without a key press."""
        super().__init__()
        self.title_text, self.choices, self.visual = title, choices, visual
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(Text(self.title_text, Style(bold=True)),
                         id="picker-title")
            yield OptionList(*[self._option(n, c)
                               for n, c in enumerate(self.choices)],
                             id="picker-list")
            yield Static(Text("enter pick  ·  1-9 quick pick  ·  esc cancel",
                              Style(dim=True)), id="picker-hint")

    def _option(self, n, choice):
        prompt = Text()
        if n < 9:
            prompt.append(f"{n + 1}  ", Style(bold=True))
        prompt.append(vis(choice["label"], self.visual))
        if choice.get("detail"):
            prompt.append("  " + vis(choice["detail"], self.visual),
                          Style(dim=True))
        return Option(prompt, id=str(n))

    def on_mount(self):
        pick = self.query_one("#picker-list", OptionList)
        pick.focus()
        for n, choice in enumerate(self.choices):
            if self.selected is not None and choice["id"] == self.selected:
                pick.highlighted = n

    def action_cancel(self):
        self.dismiss(None)

    def action_pick(self, index):
        if 0 <= index < len(self.choices):
            self.dismiss(self.choices[index])

    def on_option_list_option_selected(self, e):
        self.dismiss(self.choices[int(e.option.id)])


class PolishScreen(ModalScreen):
    """The old and the new text of a polished draft, and three choices:
    accept (replace the draft), edit (open the editor with the new text) or
    cancel. Cancel is focused by default. Nothing is sent from here.
    Dismisses with "accept", "edit" or None."""

    AUTO_FOCUS = "#cancel"
    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("a", "accept", "Accept"), Binding("e", "edit", "Edit"),
                Binding("left,right", "toggle_focus", show=False),
                Binding("down,j", "scroll(1)", show=False),
                Binding("up,k", "scroll(-1)", show=False)]
    INNER = 66

    def __init__(self, old, new, visual=False, cached_tone=False):
        super().__init__()
        self.old, self.new, self.visual = old, new, visual
        self.cached_tone = cached_tone

    def _body(self, text):
        out = Text()
        for para in text.split("\n"):
            for ln in bidi.visual_lines(para, self.INNER, self.visual) or [""]:
                out.append(ln + "\n")
        out.rstrip()
        return out

    def compose(self) -> ComposeResult:
        with Vertical(id="polish"):
            yield Static(Text("Polish draft", Style(bold=True)), id="polish-title")
            yield Static(Text("Old", Style(dim=True)), id="polish-old-label")
            with VerticalScroll(id="polish-old-scroll"):
                yield Static(self._body(self.old), id="polish-old")
            yield Static(Text("New", Style(bold=True)), id="polish-new-label")
            with VerticalScroll(id="polish-new-scroll"):
                yield Static(self._body(self.new), id="polish-new")
            yield Static(Text("Nothing is sent. Accept replaces the draft here; "
                              "edit opens it in the editor.", Style(dim=True)),
                         id="polish-note")
            with Horizontal(id="polish-buttons"):
                yield Button("Cancel  esc", id="cancel", variant="default")
                yield Button("Edit  e", id="edit", variant="primary")
                yield Button("Accept  a", id="accept", variant="success")

    def on_button_pressed(self, e):
        self.dismiss({"accept": "accept", "edit": "edit"}.get(e.button.id))

    def action_cancel(self):
        self.dismiss(None)

    def action_accept(self):
        self.dismiss("accept")

    def action_edit(self):
        self.dismiss("edit")

    def action_toggle_focus(self):
        self.focus_next()

    def action_scroll(self, step):
        pane = self.query_one("#polish-new-scroll")
        pane.scroll_down(animate=False) if step > 0 else pane.scroll_up(animate=False)


HELP = [
    ("Move / filter", [("j / k  ↓ / ↑", "next / previous"), ("g / G", "top / bottom"), ("1-4", "jump to section"),
              ("tab / shift+tab", "switch pane"), ("/", "fuzzy filter"), ("f", "filter by source"),
              ("R", "show / hide resolved alerts"),
              ("A", "show all: ask sources that support it for everything"),
              ("S", "saved: what the daemon saved to memory (read-only)"),
              ("esc", "clear filters / back")]),
    ("Act", [("a", "approve draft (confirm first) / ack / accept an invite"), ("e", "edit draft  (esc / ctrl+s done)"),
             ("d", "done / dismiss"), ("s", "snooze"), ("n", "nudge (confirm first)"),
             ("o", "open the item's link (the Slack thread with slack_open:)"), ("u", "undo last action (5 s)"), ("r", "refresh / poll now")]),
    ("RSVP / save", [("a / X / T", "accept / decline / tentative on an invite (rsvp: in inbox.yaml)"),
                     ("m", "save to memory by hand (save: in inbox.yaml; the daemon saves on its own, ◆ = already in memory, ◇ = its thread is); marks it done"),
                     ("", "both confirm first, then wait out the 5 s undo window")]),
    ("Slack send / react / polish", [
        ("a / n", "reply or nudge: Draft in Slack (d), Send (y) or Copy for Slack (c) (send: / copy: in inbox.yaml)"),
        ("E", "react to the message with an emoji (react: in inbox.yaml)"),
        ("P", "polish the draft in your tone (polish: in inbox.yaml); never sends"),
        ("", "draft, send and react confirm first, then wait out the 5 s undo window; copy is local")]),
    ("Context / summary", [("c", "show / hide the context pane (context: in inbox.yaml)"),
                           ("C  (r in pane)", "fetch the context again"),
                           ("i", "AI summary of the item, shown in the pane (summary: in inbox.yaml)"),
                           ("I", "ask for a new summary even if one is cached"),
                           ("", "read-only: nothing is confirmed or changed")]),
    ("Copy", [("y", "copy this item (sender, where, gist, link)"), ("Y", "copy every item in this section"),
              ("ctrl+p", "palette: copy section as Markdown"),
              ("mouse drag", "select text in the thread or draft pane")]),
    ("App", [("ctrl+p", "command palette"), ("ctrl+t", "dark / light theme"), ("L", "audit log"),
             ("b", "toggle Hebrew order (logical/visual)"),
             ("v", "thread order: newest / oldest message first (saved)"), ("?", "this help"), ("q", "quit")]),
]


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "close", "Close")]

    def __init__(self, extra=None, save_on=True):
        """`extra` is a list of (key, label) for the `actions:` of inbox.yaml.
        `save_on` False (no `save:` configured) leaves the `m` line out."""
        super().__init__()
        self.extra = list(extra or [])
        self.save_on = save_on

    def _section(self, title, rows):
        """A help section; without `save:` the `m` line, the "save" in the
        title and the "both" in the note are left out."""
        if self.save_on or title != "RSVP / save":
            return title, rows
        return "RSVP", [
            r if r[0] != "" else ("", "confirms first, then waits out the 5 s undo window")
            for r in rows if r[0] != "m"]

    def compose(self) -> ComposeResult:
        t = Text()
        sections = [self._section(title, rows) for title, rows in HELP]
        if self.extra:
            sections.insert(-2, ("Custom actions (actions: in inbox.yaml)", (
                [(k, f"{label}: pick, confirm, then wait out the 5 s undo window")
                 for k, label in self.extra])))
        for title, rows in sections:
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
