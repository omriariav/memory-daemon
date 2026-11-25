"""Modal screens: confirm-send, confirm-write, snooze picker, help, audit log,
the relay note and the ops log.

Anything that came from a source (names, text) is shown through Rich `Text`
objects, never markup strings, so a title like "[b]x[/]" cannot restyle or
break the UI.
"""
import datetime as dt
import re
import time

from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__, bidi
from .rowmenu import GROUPS, filter_entries, match_key
from .widgets import WordInput, vis


CHOICE_LABELS = {"draft": "Draft in Slack", "send": None,
                 "copy": "Copy for Slack"}
CHOICE_KEYS = {"draft": "d", "send": "y", "copy": "c"}


class WheelAnywhere:
    """A modal screen whose box has one scroll pane (`WHEEL_TARGET`, a
    selector). The box is narrower than the screen, so a wheel turned over the
    backdrop, the border, the padding or a title never reaches the pane: it
    lands on the screen. Here the pane takes it. A wheel over the pane itself
    is handled by the pane and never gets here."""

    WHEEL_TARGET = ""

    def on_mouse_scroll_down(self, event: events.MouseScrollDown):
        event.stop()
        self._wheel(1)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp):
        event.stop()
        self._wheel(-1)

    def _wheel(self, sign: int):
        # immediate: a burst of notches must add up, not queue behind one
        self.query_one(self.WHEEL_TARGET).scroll_relative(
            y=sign * self.app.scroll_sensitivity_y, animate=False,
            immediate=True)


class ConfirmSend(WheelAnywhere, ModalScreen):
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
    WHEEL_TARGET = "#confirm-scroll"
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


class ConfirmWrite(WheelAnywhere, ModalScreen):
    """Confirm an action that leaves the inbox (an RSVP, a save). Shows what
    will happen as label/value rows, in plain Text so item text can never
    restyle the screen. Cancel is focused by default; `y` confirms."""

    AUTO_FOCUS = "#cancel"
    WHEEL_TARGET = "#confirm-scroll"
    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("y", "confirm", "Confirm"),
                Binding("left,right", "toggle_focus", show=False),
                Binding("down,j", "scroll(1)", show=False),
                Binding("up,k", "scroll(-1)", show=False),
                Binding("pagedown", "scroll(10)", show=False),
                Binding("pageup", "scroll(-10)", show=False)]
    LABEL_MIN, LABEL_MAX = 8, 16      # the label column, in cells
    INNER = 61                        # label column + value, in cells: fits the box

    FIXED = ("Nothing happens until you confirm. Undo stays open for the "
             "grace period.")

    def __init__(self, title, rows, verb, note="", visual=False, fixed=None):
        super().__init__()
        self.title_text, self.rows, self.verb = title, rows, verb
        self.note, self.visual = note, visual
        self.fixed = self.FIXED if fixed is None else fixed

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
            yield Static(Text(self.fixed, Style(dim=True)), id="confirm-fixed")
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
            yield WordInput(placeholder="e.g. 90m, 2h, 1d, 17:30  (enter to confirm)", id="snooze-input")
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


class RowMenu(ModalScreen):
    """The row menu (Enter on a queue row): a which-key style list of what the
    selected row can do, each with its key. One press of an entry's key runs
    it; the arrows and Enter pick the highlighted one; `/` filters by text;
    esc closes (first esc: clears the filter). Every string is plain `Text`.
    Dismisses with the chosen `rowmenu.MenuEntry`, or None."""

    BINDINGS = [Binding("escape", "close", "Close"),
                Binding("slash", "filter", show=False),
                Binding("j", "move(1)", show=False),
                Binding("k", "move(-1)", show=False)]

    def __init__(self, title, entries, visual=False):
        super().__init__()
        self.title_text, self.entries, self.visual = title, list(entries), visual
        self.shown = list(entries)         # the entries the filter leaves

    def compose(self) -> ComposeResult:
        with Vertical(id="rowmenu"):
            yield Static(Text(vis(self.title_text, self.visual),
                              Style(bold=True)), id="rowmenu-title")
            yield WordInput(placeholder="/ filter…", id="rowmenu-filter")
            yield OptionList(id="rowmenu-list")
            yield Static(Text("a key runs it  ·  enter run  ·  / filter  ·  "
                              "esc close", Style(dim=True)), id="rowmenu-hint")

    def on_mount(self):
        self._build()
        self.query_one("#rowmenu-list", OptionList).focus()

    def _build(self):
        lst = self.query_one("#rowmenu-list", OptionList)
        lst.clear_options()
        options, first = [], None
        index = {id(e): n for n, e in enumerate(self.entries)}
        for group in GROUPS:
            rows = [e for e in self.shown if e.group == group]
            if not rows:
                continue
            options.append(Option(Text(group, Style(dim=True, bold=True)),
                                  disabled=True))
            for e in rows:
                if first is None:
                    first = len(options)
                prompt = Text()
                prompt.append(f" {e.shown:<7}", Style(bold=True))
                prompt.append(" " + vis(e.label, self.visual))
                if e.detail:
                    prompt.append("  " + vis(e.detail, self.visual),
                                  Style(dim=True))
                options.append(Option(prompt, id=str(index[id(e)])))
        if not options:
            options.append(Option(Text("nothing matches", Style(dim=True)),
                                  disabled=True))
        lst.add_options(options)
        lst.highlighted = first

    def on_key(self, event: events.Key):
        if isinstance(self.focused, Input):
            return
        entry = match_key(self.shown, event.character)
        if entry is not None:
            event.stop()
            event.prevent_default()
            self.dismiss(entry)

    def action_filter(self):
        box = self.query_one("#rowmenu-filter", Input)
        box.add_class("on")
        box.focus()

    def action_move(self, step):
        lst = self.query_one("#rowmenu-list", OptionList)
        if isinstance(self.focused, Input):
            return
        lst.action_cursor_down() if step > 0 else lst.action_cursor_up()

    def on_input_changed(self, e):
        self.shown = filter_entries(self.entries, e.value)
        self._build()

    def on_input_submitted(self, e):
        self.query_one("#rowmenu-list", OptionList).focus()

    def on_option_list_option_selected(self, e):
        if e.option.id is not None:
            self.dismiss(self.entries[int(e.option.id)])

    def action_close(self):
        box = self.query_one("#rowmenu-filter", Input)
        if box.has_class("on") or box.value:
            box.value = ""
            box.remove_class("on")
            self.query_one("#rowmenu-list", OptionList).focus()
            return
        self.dismiss(None)


class TaskScreen(ModalScreen):
    """Start a task from a row (`t`, 0.13.3): the name, the role chips, the
    model and effort chips, one goal line, a collapsed acceptance line, and the
    persona and template when the launcher's draft returned them. It opens at
    once with local defaults; `fill(draft)` puts the launcher's draft in, but
    only into fields the user has not touched (see `launcher.TaskForm`).

    Nothing has the focus at first, so the keys are chip keys: 1-9 role,
    `m` model, `e` effort, `t` template, `a` acceptance, and `n`, `g`, `p`
    jump into the name, goal and persona lines (`tab` moves on, and comes
    back to the chips; `enter` starts the task from anywhere, `esc` cancels).
    Dismisses with the `TaskForm`, or None."""

    AUTO_FOCUS = ""
    BINDINGS = [Binding("escape", "cancel", "Cancel"),
                Binding("enter", "start", "Start"),
                Binding("tab", "next_field(1)", show=False),
                Binding("shift+tab", "next_field(-1)", show=False)]
    INPUTS = {"task-name": "name", "task-goal": "goal",
              "task-persona": "persona"}

    def __init__(self, title, form, visual=False, drafting=True):
        super().__init__()
        self.title_text, self.form, self.visual = title, form, visual
        self.drafting = drafting
        self.status = ("drafting…  (enter starts with what is here)"
                       if drafting else "")
        self.expanded = False
        self._filling = False

    def compose(self) -> ComposeResult:
        with Vertical(id="task"):
            yield Static(Text(vis(self.title_text, self.visual),
                              Style(bold=True)), id="task-title")
            yield Static(Text("Name"), classes="task-label")
            yield WordInput(value=self.form.name, id="task-name",
                            max_length=41, select_on_focus=False)
            yield Static("", id="task-role", classes="task-chips")
            yield Static("", id="task-model", classes="task-chips")
            yield Static("", id="task-effort", classes="task-chips")
            yield Static("", id="task-template", classes="task-chips")
            yield Static(Text("Goal"), classes="task-label")
            yield WordInput(value=self.form.goal, id="task-goal", max_length=300,
                            select_on_focus=False)
            yield Static("", id="task-accept")
            yield Static(Text("Persona"), id="task-persona-label",
                         classes="task-label")
            yield WordInput(value=self.form.persona, id="task-persona",
                            max_length=400, select_on_focus=False)
            yield Static("", id="task-status")
            yield Static("", id="task-hint")

    def on_mount(self):
        self._redraw()

    # --- drawing
    def _chips(self, label, key, pool, blank=None):
        """One chip row: the choices, the chosen one reversed. `blank` is the
        chip for "no choice" (the launcher's default)."""
        text = Text()
        text.append(f"{label:<8}", Style(bold=True))
        cur = getattr(self.form, key)
        stops = ([("", blank)] if blank else []) + [(c, c) for c in pool]
        for n, (value, shown) in enumerate(stops, 1):
            num = f"{n} " if key == "role" and n < 10 else ""
            chip = f" {num}{shown} "
            text.append(chip, Style(reverse=True, bold=True)
                        if value == cur else Style(dim=True))
            text.append(" ")
        return text

    def _redraw(self):
        f = self.form
        q = self.query_one
        q("#task-role", Static).update(self._chips("Role", "role", f.roles))
        models = q("#task-model", Static)
        efforts = q("#task-effort", Static)
        models.display = bool(f.models)
        efforts.display = bool(f.efforts)
        if f.models:
            models.update(self._chips("Model", "model", f.models, "default"))
        if f.efforts:
            efforts.update(self._chips("Effort", "effort", f.efforts,
                                       "default"))
        tmpl = q("#task-template", Static)
        tmpl.display = bool(f.templates)
        if f.templates:
            tmpl.update(self._chips("Brief", "template", f.templates,
                                    "default"))
        for key in ("name", "goal", "persona"):
            box = q(f"#task-{key}", Input)
            if box.value != getattr(f, key):
                self._filling = True
                try:
                    box.value = getattr(f, key)
                finally:
                    self._filling = False
        show_persona = bool(f.persona) or "persona" in f.touched
        q("#task-persona-label").display = show_persona
        q("#task-persona", Input).display = show_persona
        acc = Text()
        acc.append("Acceptance  ", Style(bold=True))
        if not f.acceptance:
            acc.append("the launcher's default", Style(dim=True))
        elif self.expanded:
            for line in f.acceptance:
                acc.append("\n  - " + vis(line, self.visual))
        else:
            acc.append(vis(f.acceptance[0], self.visual))
            more = len(f.acceptance) - 1
            acc.append(f"  (+{more} more · a)" if more else "  (a)",
                       Style(dim=True))
        q("#task-accept", Static).update(acc)
        q("#task-status", Static).update(Text(self.status, Style(dim=True)))
        keys = ["1-9 role"]
        if f.models:
            keys.append("m model")
        if f.efforts:
            keys.append("e effort")
        if f.templates:
            keys.append("t brief")
        keys += ["n name", "g goal"]
        if show_persona:
            keys.append("p persona")
        keys += ["a acceptance", "tab next", "enter start", "esc cancel"]
        q("#task-hint", Static).update(Text("  ·  ".join(keys),
                                            Style(dim=True)))

    # --- the launcher's draft
    def fill(self, draft):
        """Put a `launcher.Draft` into the untouched fields; the names of the
        fields it filled."""
        self._sync_inputs()
        done = self.form.apply_draft(draft)
        self.status = ("draft ready" + (": " + ", ".join(done) if done else
                                        ": nothing to change"))
        if self.is_mounted:
            self._redraw()
        return done

    def _sync_inputs(self):
        """Take what the lines show into the form before a draft is applied:
        a key typed just now may not have reached `on_input_changed` yet, and
        the draft must never overwrite it."""
        if not self.is_mounted:
            return
        for box_id, key in self.INPUTS.items():
            found = self.query(f"#{box_id}")
            if found and found.first().value != getattr(self.form, key):
                self.form.edit(key, found.first().value)

    def draft_failed(self, message):
        """The model call failed or ran out of time: the local defaults
        stay."""
        self.status = ("no draft: " + (message or "failed")
                       + "  (the defaults stay)")
        if self.is_mounted:
            self._redraw()

    # --- keys
    def on_input_changed(self, e):
        field = self.INPUTS.get(e.input.id)
        if (field is None or self._filling
                or e.value == getattr(self.form, field)):
            return                  # not a change by the user (the mount)
        self.form.edit(field, e.value)

    def on_input_submitted(self, e):
        e.stop()
        self.action_start()

    def on_key(self, event: events.Key):
        if isinstance(self.focused, Input):
            return
        ch = event.character or ""
        f = self.form
        handled = True
        if len(ch) == 1 and ch in "123456789":
            handled = f.pick_role(int(ch))
        elif ch == "m" and f.models:
            f.cycle("model")
        elif ch == "e" and f.efforts:
            f.cycle("effort")
        elif ch == "t" and f.templates:
            f.cycle("template")
        elif ch == "a":
            self.expanded = not self.expanded
        elif ch in ("n", "g", "p"):
            target = {"n": "task-name", "g": "task-goal",
                      "p": "task-persona"}[ch]
            box = self.query_one(f"#{target}", Input)
            if not box.display:
                return
            event.stop()
            event.prevent_default()
            box.focus()
            box.cursor_position = len(box.value)
            return
        else:
            return
        event.stop()
        event.prevent_default()
        if handled:
            self._redraw()

    def _fields_order(self):
        order = [None, "task-name", "task-goal"]
        if self.query_one("#task-persona", Input).display:
            order.append("task-persona")
        return order

    def action_next_field(self, step):
        order = self._fields_order()
        cur = getattr(self.focused, "id", None)
        at = order.index(cur) if cur in order else 0
        nxt = order[(at + int(step)) % len(order)]
        if nxt is None:
            self.set_focus(None)
        else:
            self.query_one(f"#{nxt}", Input).focus()

    def action_start(self):
        why = self.form.problem()
        if why:
            self.status = why
            self.query_one("#task-status", Static).update(
                Text(why, Style(bold=True, color="red")))
            return
        self.dismiss(self.form)

    def action_cancel(self):
        self.dismiss(None)


POWER = "Power keys (also in enter)"

HELP = [
    ("The verbs", [
        ("a", "approve: send the reply, accept the invite, nudge, ack"),
        ("d", "done (dismiss on an FYI row)"),
        ("s", "later: snooze, or a task's due date"),
        ("t", "task: start a task session from this row (task_launcher: in inbox.yaml)"),
        ("o", "open the item's link (the Slack thread with slack_open:)"),
        ("enter", "more: a menu of what this row can do (a key runs it, / filters)"),
        ("u", "undo the last action (5 s)")]),
    ("What a, d and s do, by row", [
        ("Slack ask", "a send (confirm) · d done (with an outcome, when the source has close_outcome) · s snooze"),
        ("Waiting", "a nudge (confirm) · d done (with an outcome, when the source has close_outcome) · s snooze"),
        ("RSVP", "a accept · d done · s snooze"),
        ("Todo", "d close now · s due date"),
        ("Decision", "d asks for the decision, then closes · s due date"),
        ("Agent card", "a ack · d done / s snooze at the source (when the source supports it)"),
        ("FYI", "d dismiss · s snooze"),
        ("Alert", "a ack · d done · s snooze"),
        ("", "every old key still works; a row's others are in its enter menu")]),
    (POWER, [
        ("e", "edit the draft  (esc / ctrl+s done)"),
        ("n", "nudge (confirm first); the same as a on a waiting row"),
        ("a / X / T", "accept / decline / tentative on an invite (rsvp: in inbox.yaml)"),
        ("D", "Slack ask: draft a reply for me (summary: or polish:; fills the Draft pane, never sends) · todo: done with an outcome · review: copy a save hint"),
        ("P", "polish the draft in your tone (polish: in inbox.yaml); never sends"),
        ("E", "react to the message with an emoji (react: in inbox.yaml)"),
        ("w", "waits on: Me, a person in the thread, or Nobody (acts: waits_on)"),
        ("!", "relay the item to your work session, with an optional note (acts: relay)"),
        ("x", "mute this row: this kind from this routine / this routine / titles like it, for 1 day, 1 week or always; with acts: mute the conversation; a task: drop it"),
        ("-", "less like this: fold the routine's rows of a day into one (Feed)"),
        ("M", "rules: the ones in inbox.yaml and the ones made here (space switches a rule off, delete removes it)"),
        ("m", "save to memory by hand (save: in inbox.yaml; the daemon saves on its own, ◆ = already in memory, ◇ = its thread is); marks it done · on a task: remind me"),
        ("c", "show / hide the context pane (context: in inbox.yaml)"),
        ("C  (r in pane)", "fetch the context again"),
        ("i", "AI summary of the item, shown in the pane (summary: in inbox.yaml)"),
        ("I", "ask for a new summary even if one is cached"),
        ("y / Y", "copy this item (sender, where, gist, link) / copy every item in this group"),
        ("", "writes confirm first, then wait out the 5 s undo window; u cancels before anything is sent"),
        ("", "the send confirm: Draft in Slack d · Send y · Copy for Slack c (send: / copy: in inbox.yaml)"),
        ("", "context and summary are read-only: nothing is confirmed or changed"),
        ("mouse drag", "select text in the thread or draft pane"),
        ("", "L is the ops log: u on a row sends the inverse (reopen / unmute / the old due date) after a confirm")]),
    ("Views and moving", [
        ("1 2 3 4 5 6", "Inbox · Waiting · Tasks · Feed · Health · Agents"),
        ("[ / ]", "previous / next group in the view (section, person, routine)"),
        ("j / k  ↓ / ↑", "next / previous"), ("g / G", "top / bottom"),
        ("tab / shift+tab", "switch pane (tab on a Feed row: its details)"),
        ("/", "fuzzy filter"),
        ("f", "filter by source (in the Feed: by routine)"),
        ("R", "show / hide resolved alerts"),
        ("A", "show all: sources that support it, and Feed rows older than the window"),
        ("S", "Feed, filtered to saved to memory"),
        ("r", "refresh / poll now"),
        ("esc", "clear filters / back"),
        ("", "the same items in every view: marks and the undo work alike")]),
    ("Review (REVIEW in the Inbox)", [
        ("", "a capture the memory store refused as a near-duplicate: Not saved: near-duplicate of ..."),
        ("d", "dismiss: it is a duplicate, fine (a local mark)"),
        ("D", "copy a command-line hint for saving it anyway (a template: it needs the entry text; nothing is saved from here)")]),
    ("Tasks (a source's meta.kind: task rows)", [
        ("3", "the Tasks view: OVERDUE, DUE TODAY, THIS WEEK, LATER, DECIDE, NO DATE; overdue and due-today tasks are also in the Inbox"),
        ("d", "todo: close it now. decision: asks for the decision (required), then closes it"),
        ("D", "todo: asks for the outcome, then closes it"),
        ("s", "due date: tomorrow, next Sunday, +1 week, a date (YYYY-MM-DD) or none"),
        ("x", "drop it, with an optional reason (enter alone drops with none)"),
        ("m", "remind me: fills the Detail pane with the source, date, people, original words, last activity and related items (read-only; m or esc closes)"),
        ("tab", "the Detail card (j / k scroll it, esc back)"),
        ("", "needs acts: [close, drop, due] on the source; without it the keys only say so")]),
    ("Start a task (t; task_launcher: in inbox.yaml)", [
        ("t", "modal: name, role 1-9, model m, effort e, goal g, acceptance a; a draft fills only what you have not touched; enter queues it (5 s undo), esc cancels"),
        ("", "the row shows ▶ name · role · state; > goes to its task, and o opens the report when it is done (< the row's own link)"),
        ("", "without task_launcher: t says: no task launcher configured")]),
    ("Agents and the sessions strip (a source's meta.kind: agent rows and status lines)", [
        ("6", "the Agents view: every agent row, grouped NEEDS YOU / WORKING / DONE, with where, age, live or no session, old rules"),
        ("", "an agent row that waits for you is also in the Inbox; one that is working or queued is only here and in the strip"),
        ("", "the strip above the queue shows the status lines your sources send (4 lines, then +N more · 6); it is hidden when none do"),
        ("ctrl+p", "Show / hide sessions strip (no key of its own; strip_lines: in inbox.yaml sets how many lines)")]),
    ("App", [("ctrl+p", "command palette: this item's commands first, then the rest"),
             ("ctrl+t", "dark / light theme"), ("L", "audit log"),
             ("b", "toggle Hebrew order (logical/visual)"),
             ("v", "thread order: newest / oldest message first (saved)"),
             ("", "ctrl+p also has: copy section as Markdown"),
             ("?", "this help"), ("q", "quit")]),
]


class HelpScreen(WheelAnywhere, ModalScreen):
    WHEEL_TARGET = "#help-scroll"
    # The reference is taller than most terminals: the body is a focused
    # scroll container (arrows, PgUp/PgDn, Home/End from VerticalScroll; the
    # mouse wheel too), and j/k scroll a line like everywhere else.
    BINDINGS = [Binding("escape,question_mark,q", "close", "Close"),
                Binding("j", "scroll(1)", show=False),
                Binding("k", "scroll(-1)", show=False)]

    def __init__(self, extra=None, save_on=True):
        """`extra` is a list of (key, label) for the `actions:` of inbox.yaml.
        `save_on` False (no `save:` configured) leaves the `m` line out."""
        super().__init__()
        self.extra = list(extra or [])
        self.save_on = save_on

    def _section(self, title, rows):
        """A help section; without `save:` the power keys' `m` line says only
        what `m` does on a task."""
        if self.save_on or title != POWER:
            return title, rows
        return title, [("m", "on a task: remind me") if r[0] == "m" else r
                       for r in rows]

    def compose(self) -> ComposeResult:
        t = Text()
        sections = [self._section(title, rows) for title, rows in HELP]
        if self.extra:
            sections.insert(-1, ("Custom actions (actions: in inbox.yaml)", (
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
            with VerticalScroll(id="help-scroll"):
                yield Static(t, id="help-body")

    def on_mount(self):
        self.query_one("#help-scroll").focus()

    def action_scroll(self, step: int):
        pane = self.query_one("#help-scroll")
        pane.scroll_down(animate=False) if step > 0 else pane.scroll_up(animate=False)

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


class NoteScreen(ModalScreen):
    """A one-line note (the relay note): enter sends what is typed (it may be
    empty), esc cancels. Dismisses with the text, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, who, max_len=300,
                 placeholder="optional one-line note  (enter to send)",
                 hint="enter send  ·  esc cancel"):
        super().__init__()
        self.title_text, self.who, self.max_len = title, who, max_len
        self.placeholder, self.hint = placeholder, hint

    def compose(self) -> ComposeResult:
        head = Text()
        head.append(self.title_text + " ", Style(bold=True))
        head.append(self.who, Style(dim=True))
        with Vertical(id="note"):
            yield Static(head, id="note-title")
            yield WordInput(placeholder=self.placeholder,
                            id="note-input", max_length=self.max_len)
            yield Static(Text(self.hint, Style(dim=True)), id="note-hint")

    def on_mount(self):
        self.query_one("#note-input").focus()

    def action_cancel(self):
        self.dismiss(None)

    def on_input_submitted(self, e):
        self.dismiss(" ".join(e.value.split()))


class OutcomeScreen(NoteScreen):
    """The outcome of a Slack ask or a waiting row, asked on `d` (0.13.1).
    Required: enter on an empty line stays open and says so; esc cancels
    (nothing closes). It opens at once; `fill(text)` puts a suggested outcome
    in when the model call returns, but only while the owner has typed
    nothing (a typed key, even one deleted again, keeps the line theirs).
    Dismisses with the text, or None."""

    EMPTY = "an outcome is required  ·  esc cancel"

    def __init__(self, who, max_len=300, drafting=False):
        super().__init__(
            "Outcome:", who, max_len,
            placeholder=("drafting…  (type to write your own)" if drafting
                         else self.READY),
            hint="enter close  ·  esc cancel  ·  required")
        self.typed = False
        self.filled = False
        self._filling = False
        self._pending = ""         # a suggestion that came before the mount

    def on_input_changed(self, e):
        if not self._filling:
            self.typed = True

    def fill(self, text):
        """Put the suggestion in an untouched prompt. Returns True when it
        was used."""
        if self.typed or self.filled or not text:
            return False
        found = self.query("#note-input")
        if not found:               # not composed yet: on_mount puts it in
            self._pending = text
            return True
        field = found.first()
        self._filling = True
        try:
            field.value = text
            field.cursor_position = len(field.value)
        finally:
            self._filling = False
        self.filled = True
        return True

    def on_mount(self):
        self.query_one("#note-input").placeholder = self.placeholder
        pending, self._pending = self._pending, ""
        if pending:
            self.fill(pending)

    READY = "what happened, one line  (enter to close)"

    def stop_drafting(self):
        """The model call failed or ran out of time: keep the line empty and
        editable, and stop saying it is drafting."""
        self.placeholder = self.READY    # used if the prompt is not composed yet
        for field in self.query("#note-input"):
            field.placeholder = self.READY

    def on_input_submitted(self, e):
        e.prevent_default()           # NoteScreen's handler would dismiss
        text = " ".join(e.value.split())
        if not text:
            self.query_one("#note-hint", Static).update(
                Text(self.EMPTY, Style(bold=True)))
            return
        self.dismiss(text)


class DateScreen(ModalScreen):
    """A `YYYY-MM-DD` date (the task due date picker's "Pick date"). Enter
    accepts a real date and stays open on anything else, with the reason;
    esc cancels. Dismisses with the date string, or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title, who):
        super().__init__()
        self.title_text, self.who = title, who

    def compose(self) -> ComposeResult:
        head = Text()
        head.append(self.title_text + " ", Style(bold=True))
        head.append(self.who, Style(dim=True))
        with Vertical(id="note"):
            yield Static(head, id="note-title")
            yield WordInput(placeholder="YYYY-MM-DD  (enter to set)",
                            id="note-input", max_length=10)
            yield Static(Text("enter set  ·  esc cancel", Style(dim=True)),
                         id="note-hint")

    def on_mount(self):
        self.query_one("#note-input").focus()

    def action_cancel(self):
        self.dismiss(None)

    def on_input_submitted(self, e):
        from .tasks import parse_day
        text = e.value.strip()
        if parse_day(text) is None:
            self.query_one("#note-hint", Static).update(Text(
                "Not a date. Use YYYY-MM-DD, for example 2030-01-31",
                Style(bold=True, color="red")))
            return
        self.dismiss(text)


class OpsScreen(ModalScreen):
    """The ops log (`L` with acts on): what the inbox told a command source,
    newest first, with the result. `u` on a row asks for its inverse (reopen
    after a close or a drop, unmute after a mute, the old date after a due
    move); `a` shows the session's audit log.
    Dismisses with None, `("inverse", row)` or `("session", None)`."""

    BINDINGS = [Binding("escape,L,q", "close", "Close"),
                Binding("u", "inverse", "Undo in source"),
                Binding("a", "session", "Session log", show=False)]

    def __init__(self, rows, invertible=lambda row: None):
        super().__init__()
        self.rows = list(rows)
        self.invertible = invertible

    def compose(self) -> ComposeResult:
        head = Text()
        head.append("Ops log", Style(bold=True))
        head.append("  what the inbox sent to its sources, newest first  ·  "
                    "u undo in the source  ·  a session log  ·  esc close",
                    Style(dim=True))
        with Vertical(id="ops"):
            yield Static(head, id="ops-title")
            yield DataTable(id="ops-table", cursor_type="row",
                            zebra_stripes=True)

    def on_mount(self):
        tb = self.query_one(DataTable)
        tb.add_columns("Time", "Action", "Item", "Result", "Message")
        for n, r in enumerate(self.rows):
            when = dt.datetime.fromtimestamp(r.get("at") or 0).strftime(
                "%m-%d %H:%M:%S")
            result = {"pending": "not synced"}.get(r.get("result"),
                                                   r.get("result") or "")
            if r.get("retry"):
                result += " (retry)"
            action = str(r.get("action", ""))
            if r.get("inverse_of"):
                action += " (undo)"
            tb.add_row(when, Text(action), Text(str(r.get("who", ""))),
                       Text(result), Text(str(r.get("message", ""))),
                       key=str(n))
        if not self.rows:
            tb.add_row("", Text("no ops yet"), "", "", "")
        tb.focus()

    def _row(self):
        tb = self.query_one(DataTable)
        if not self.rows or tb.row_count == 0:
            return None
        try:
            key = tb.coordinate_to_cell_key(tb.cursor_coordinate).row_key.value
            return self.rows[int(key)]
        except Exception:   # an empty table, a row that went away
            return None

    def action_inverse(self):
        row = self._row()
        if row is None:
            return
        if self.invertible(row) is None:
            self.notify("This op has no inverse (only a close, drop, due or "
                        "mute that reached the source can be undone)",
                        timeout=4, markup=False)
            return
        self.dismiss(("inverse", row))

    def action_session(self):
        self.dismiss(("session", None))

    def action_close(self):
        self.dismiss(None)


class RulesScreen(ModalScreen):
    """The Feed rules: the ones from `inbox.yaml` (read-only) and the ones
    made in the UI, which can be switched off (space) or removed (delete)."""

    BINDINGS = [Binding("escape,M,q", "close", "Close"),
                Binding("space", "toggle", "Switch on/off", show=False),
                Binding("delete,backspace", "remove", "Remove", show=False),
                Binding("j", "cursor(1)", show=False),
                Binding("k", "cursor(-1)", show=False)]

    def __init__(self, config_rules, ui_rules, on_toggle, on_remove,
                 now=None):
        """`on_toggle(rule_id, enabled)` and `on_remove(rule_id)` write the
        change; `ui_rules` is a list the screen re-reads through `reload`."""
        super().__init__()
        self.config_rules, self.ui_rules = list(config_rules), list(ui_rules)
        self.on_toggle, self.on_remove = on_toggle, on_remove
        self.now = now or time.time()
        self.entries = []          # (origin, rule) per table row

    def compose(self) -> ComposeResult:
        head = Text()
        head.append("Feed rules", Style(bold=True))
        head.append("  tried top to bottom, the first match wins  ·  "
                    "space on/off  ·  delete remove  ·  esc back",
                    Style(dim=True))
        with Vertical(id="rules"):
            yield Static(head, id="rules-title")
            yield DataTable(id="rules-table", cursor_type="row",
                            zebra_stripes=True)

    def on_mount(self):
        tb = self.query_one("#rules-table", DataTable)
        tb.add_columns("From", "On", "Action", "Matches", "Until")
        self._fill()
        tb.focus()

    def _fill(self):
        from . import feedrules
        tb = self.query_one("#rules-table", DataTable)
        tb.clear()
        self.entries = ([("here", r) for r in sorted(
            self.ui_rules, key=lambda r: -r.get("created", 0))]
            + [("inbox.yaml", r) for r in self.config_rules])
        for origin, rule in self.entries:
            until = rule.get("until")
            if origin == "inbox.yaml":
                on, until_text = "yes", "-"
            else:
                expired = until is not None and until <= self.now
                on = "no" if (rule.get("enabled") is False) else (
                    "expired" if expired else "yes")
                until_text = ("always" if until is None else
                              dt.datetime.fromtimestamp(until).strftime("%a %H:%M"))
            tb.add_row(origin, on, rule["action"],
                       Text(feedrules.rule_label(rule)), until_text,
                       key=f"{origin}:{rule.get('id', id(rule))}")
        if not self.entries:
            tb.add_row("", "", "", Text("no rules yet: x on a Feed row makes one",
                                        Style(italic=True)), "")

    def reload(self, ui_rules):
        tb = self.query_one("#rules-table", DataTable)
        row = tb.cursor_row
        self.ui_rules = list(ui_rules)
        self._fill()
        if self.entries:
            tb.move_cursor(row=min(row, len(self.entries) - 1))

    def _current(self):
        tb = self.query_one("#rules-table", DataTable)
        if not self.entries or not 0 <= tb.cursor_row < len(self.entries):
            return None
        return self.entries[tb.cursor_row]

    def action_cursor(self, step):
        tb = self.query_one("#rules-table", DataTable)
        tb.action_cursor_down() if step > 0 else tb.action_cursor_up()

    def action_toggle(self):
        cur = self._current()
        if cur is None:
            return
        origin, rule = cur
        if origin != "here":
            self.notify("This rule is in inbox.yaml: edit it there",
                        timeout=3, markup=False)
            return
        self.on_toggle(rule["id"], rule.get("enabled") is False)

    def action_remove(self):
        cur = self._current()
        if cur is None:
            return
        origin, rule = cur
        if origin != "here":
            self.notify("This rule is in inbox.yaml: edit it there",
                        timeout=3, markup=False)
            return
        self.on_remove(rule["id"])

    def action_close(self):
        self.dismiss(None)
