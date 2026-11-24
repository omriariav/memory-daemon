"""The row menu (0.13.0): what Enter opens, and the footer's verbs.

No Textual import. A row menu lists only what the selected row can do now,
each entry with its key (the key still works on its own: the menu only
dispatches to the same app methods) and a clear label. Overloaded keys get
distinct labels (`D` is "Done with outcome" on a todo and "Draft for me" on a
Slack ask). `build_entries(app, item)` reads the app's state (what is
configured, what the row's source lists) and returns the entries;
`verb_hints(app, item)` returns the footer: at most `FOOTER_MAX` hints.
"""
from dataclasses import dataclass, field

from . import items as itm_items
from . import tasks as tasksmod
from . import views

FOOTER_MAX = 7          # a send, d, s, t, o, enter and ? on a row that has all of them

# The menu's groups, in the order they are listed.
G_ACT, G_SOURCE, G_FEED, G_OPEN, G_MEMORY, G_CUSTOM = (
    "Act", "Source", "Feed", "Open and copy", "Memory and context", "Custom")
GROUPS = (G_ACT, G_SOURCE, G_FEED, G_OPEN, G_MEMORY, G_CUSTOM)


@dataclass
class MenuEntry:
    key: str                  # shown as the entry's key (the alias)
    label: str
    group: str
    run: object               # a no-argument callable: the app method
    also: tuple = ()          # more keys that run it (the `n` of a nudge)
    detail: str = ""
    titles: tuple = ()        # the palette commands this entry stands for
    keys: tuple = field(init=False)

    def __post_init__(self):
        self.keys = (self.key,) + tuple(self.also)

    @property
    def shown(self):
        return " / ".join(self.keys)


# The command-palette titles (InboxApp.get_system_commands) that run the same
# method as an entry's key: the palette's "This item" group replaces them, so
# no command is listed twice.
KEY_TITLES = {
    "e": ("Edit draft",), "d": ("Mark done",), "s": ("Snooze…",),
    "X": ("Decline invite",), "T": ("Tentative",), "E": ("React to message",),
    "P": ("Polish draft",), "D": ("Draft for me",), "w": ("Waits on…",),
    "!": ("Relay to work session…",), "o": ("Open link",),
    "y": ("Copy item",), "Y": ("Copy section",),
    "t": ("Start a task…",), ">": ("Go to task",),
    "c": ("Toggle context pane",), "C": ("Refresh context",),
    "i": ("Summarize item",), "I": ("Summarize item (refresh)",),
    "u": ("Undo last action",), "M": ("Feed rules",),
    "R": ("Show / hide resolved alerts",), "-": ("Less like this (Feed)",),
    "x": ("Mute (Feed)",),
}
APPROVE_TITLES = {
    "send": ("Approve draft / acknowledge",),
    "ack": ("Approve draft / acknowledge",),
    "accept": ("Approve draft / acknowledge", "Accept invite"),
    "nudge": ("Approve draft / acknowledge", "Nudge"),
}


def filter_entries(entries, text):
    """The entries whose key, label, group or detail contain every word of
    `text` (case-insensitive). An empty filter keeps them all."""
    words = [w for w in (text or "").lower().split() if w]
    if not words:
        return list(entries)
    out = []
    for e in entries:
        hay = " ".join((e.shown, e.label, e.group, e.detail)).lower()
        if all(w in hay for w in words):
            out.append(e)
    return out


def match_key(entries, char):
    """The entry a typed character runs (case-sensitive), or None."""
    if not char:
        return None
    return next((e for e in entries if char in e.keys), None)


def approve_label(app, it):
    """What `a` does on this row, as the footer says it (send, accept, nudge,
    ack), following `app.action_approve`; None when `a` does nothing useful
    (a task, a digest or saved row, a review row, an FYI row, or a reply or
    nudge when nothing can send)."""
    if it is None or tasksmod.is_task(it) or views.is_synthetic(it) \
            or views.is_review(it):
        return None
    if app._rsvp_on(it):
        return "accept"
    kind = it.kind
    if kind == "ack":
        return "ack"
    if kind == "fyi":
        return None
    if not getattr(app.actions, "supports_send", False):
        return None
    if kind == "nudge":
        return "nudge" if _nudge_text_ok(app, it) else None
    # `_confirm_send` refuses an empty draft ("No draft to send")
    return "send" if _has_draft(it) else None


def _has_draft(it):
    return bool(it.draft and it.draft.strip())


def _nudge_text_ok(app, it):
    """True when `action_nudge` gets to the confirm screen with text: the
    stored draft, or (no draft at all) the `nudge_template` text of a row of
    an acts source."""
    if _has_draft(it):
        return True
    if it.draft:                      # blank text: the confirm refuses it
        return False
    fn = getattr(app.actions, "act_nudge_text", None)
    if not (fn is not None and app._acts_any() and getattr(it, "origin", "")
            and app._acts_on_source(it)):
        return False
    try:
        return bool((fn(it) or "").strip())
    except Exception:
        return False


def draft_me_ok(app, it):
    """True when `D` would write a draft: `draft_me:` is usable, the row has a
    reply or nudge, no draft for this row is being written, and the row has no
    draft, or only the one `D` wrote (which it may redo). A draft written by
    hand or by the source is never overwritten."""
    if not getattr(app.actions, "draft_me_configured", False):
        return False
    if "reply" not in it.actions and "nudge" not in it.actions:
        return False
    if it.key in app._draft_running:
        return False
    if not _has_draft(it):
        return True
    fn = getattr(app.actions, "cached_draft_me", None)
    try:
        cached = fn(it) if fn else None
    except Exception:
        cached = None
    return bool(cached) and it.draft == cached


def report_item(it):
    """The agent row of a finished task started from this row when it has a
    report link (`o` opens it), else None."""
    task = getattr(it, "task", None)
    if task and task[1].completed and task[1].link:
        return task[0]
    return None


def task_refusal(app, it):
    """Why `t` cannot start a task from this row, or "" when it can: not a
    digest, saved, review or agent row, not an invite or an alert, and not a
    row whose task is still running (or being started)."""
    if it is None:
        return "Nothing selected"
    if views.is_synthetic(it) or views.is_review(it):
        return "A task starts from a single row, not from this one"
    if views.is_agent(it):
        return "This row is a task already"
    meta = it.meta if isinstance(it.meta, dict) else {}
    if it.source == "alert" or meta.get("kind") == "invite":
        return "A task cannot start from an alert or an invite"
    card = getattr(it, "task", None)
    if card and not card[1].completed:
        return f"{card[1].name} is already running (enter, Go to task)"
    if app.task_starting(it):
        return "A task is already being started from this row"
    return ""


def task_ok(app, it):
    """True when the footer and the menu offer `t` on this row: a task
    launcher is configured and the row can start one."""
    return (it is not None
            and bool(getattr(app.actions, "task_launcher_configured", False))
            and not task_refusal(app, it))


def can_open(app, it):
    """True when `o` has something to open: the finished task's report, the
    item's link, or the Slack open template's URL."""
    return report_item(it) is not None or can_open_source(app, it)


def can_open_source(app, it):
    """True when the row's own link (or the Slack open template's URL) can be
    opened."""
    if it is None:
        return False
    if tasksmod.is_task(it) and not it.link:
        return False                  # `_open_item` says "no link"
    fn = getattr(app.actions, "open_target", None)
    if fn is None:                    # the demo backend opens `item.link`
        return itm_items.valid_link(it.link)
    try:
        return bool(fn(it))           # the open template's URL or a safe link
    except Exception:
        return False


def _closable(app, it):
    return not tasksmod.is_task(it) or app._acts_on(it, "close")


def verb_hints(app, it):
    """The footer of a selected row: at most FOOTER_MAX hints, in this order:
    a <label>, d done, s later, t task, o open, enter more, ? help. A task
    shows `d` and `s` only when its source lists the act (the keys only say so
    otherwise); `t` shows with a configured task launcher on a row that can
    start one."""
    hints = []
    label = approve_label(app, it)
    if label:
        hints.append(("a", label))
    if _closable(app, it):
        hints.append(("d", "done"))
    if not tasksmod.is_task(it) or app._acts_on(it, "due"):
        hints.append(("s", "later"))
    if task_ok(app, it):
        hints.append(("t", "task"))
    if can_open(app, it):
        hints.append(("o", "open"))
    hints.append(("⏎", "more"))
    hints.append(("?", "help"))
    return hints[:FOOTER_MAX]


def build_entries(app, it):
    """The menu of `it`: only what is valid for this row now."""
    entries = []

    def add(group, key, label, run, also=(), detail="", titles=None):
        if titles is None:
            titles = ((label,) if group == G_CUSTOM
                      else KEY_TITLES.get(key, ()))
        entries.append(MenuEntry(key, label, group, run, tuple(also), detail,
                                 tuple(titles)))

    if it is None:
        return entries
    task = tasksmod.is_task(it)
    review = views.is_review(it)
    synthetic = views.is_synthetic(it)
    in_feed = app.view == "feed"
    acts = app._acts_on

    if task:
        _task_entries(app, it, add)
    elif review:
        add(G_ACT, "d", "Dismiss (a duplicate, fine)", app.action_done)
        add(G_ACT, "s", "Snooze…", app.action_snooze)
        add(G_ACT, "D", "Copy a save hint (command line)",
            app.action_draft_me,
            detail="a template; nothing is saved from here")
    elif synthetic:
        add(G_ACT, "d", "Dismiss" if in_feed else "Done", app.action_done)
        add(G_ACT, "s", "Snooze…", app.action_snooze)
        if views.is_digest(it):
            add(G_FEED, "x", "Mute…", app.action_mute,
                detail="the newest row's kind, routine or title")
            add(G_FEED, "-", "Less like this", app.action_less,
                detail="fold the routine's rows of a day into one")
    else:
        _row_entries(app, it, add, in_feed)

    if in_feed and not task and not review:
        add(G_FEED, "M", "Feed rules…", app.action_rules,
            detail="the rules from inbox.yaml and the ones made here")

    # start a task, and the task started from this row
    if task_ok(app, it):
        add(G_ACT, "t", "Start a task…", app.action_task,
            detail="a task session works on it; the row stays")
    if getattr(it, "task", None):
        add(G_ACT, ">", "Go to task", app.action_go_to_task,
            detail="its row in the Agents view")

    # open and copy
    if report_item(it) is not None:
        add(G_OPEN, "o", "Open task report", app.action_open)
        if can_open_source(app, it):
            add(G_OPEN, "O", "Open the source link", app.action_open_source)
    elif can_open(app, it):
        add(G_OPEN, "o", "Open link", app.action_open)
    add(G_OPEN, "y", "Copy item", app.action_copy_item)
    add(G_OPEN, "Y", "Copy section", app.action_copy_section,
        detail="every item in this group")

    # memory and context
    if (not synthetic and not task and not review and app.actions_save_on
            and not it.memory_id):
        add(G_MEMORY, "m", "Save to memory", app.action_save_memory,
            detail="by hand; the daemon saves on its own",
            titles=("Save item to memory",))
    if not synthetic and not review:
        if app._ctx_on():
            add(G_MEMORY, "c", "Context pane", app.action_context)
            if app.context_open:      # `C` says "pane is closed" otherwise
                add(G_MEMORY, "C", "Refresh context",
                    app.action_context_refresh)
        if app._sum_on():
            add(G_MEMORY, "i", "Summary", lambda: app.action_summary(False))
            add(G_MEMORY, "I", "Summary (ask again)",
                lambda: app.action_summary(True))

    # the source's own acts (not on a task: its keys are above)
    if not synthetic and not task and not review:
        if acts(it, "waits_on"):
            add(G_SOURCE, "w", "Waits on…", app.action_waits_on,
                detail="who the next move is with")
        if acts(it, "relay"):
            add(G_SOURCE, "!", "Relay to work session…", app.action_relay)
        if acts(it, "mute") and not in_feed:
            add(G_SOURCE, "x", "Mute conversation…", app.action_mute,
                detail="1 day, 1 week or untrack")
        if it.source == "alert":
            add(G_FEED, "R", "Show / hide resolved alerts",
                app.action_toggle_resolved)

    # the user's `actions:` of inbox.yaml
    if not synthetic and not task and not review:
        for n, cfg in enumerate(app._custom):
            add(G_CUSTOM, cfg["key"], cfg["label"],
                (lambda n=n: app.action_custom_action(n)),
                detail="actions: in inbox.yaml")

    # undo while something waits out its window
    if any(not p.done for p in app.pending):
        add(G_ACT, "u", "Undo last action", app.action_undo,
            detail="within the 5 s window")
    return entries


def _task_entries(app, it, add):
    acts = app._acts_on
    todo = tasksmod.task_type(it) == tasksmod.TODO
    if acts(it, "close"):
        add(G_ACT, "d", "Done" if todo else "Done with decision…",
            app.action_done,
            detail="" if todo else "the decision is required")
        if todo:
            add(G_ACT, "D", "Done with outcome…", app.action_draft_me,
                detail="asks for the outcome, then closes it")
    if acts(it, "due"):
        add(G_ACT, "s", "Due date…", app.action_snooze)
    if acts(it, "drop"):
        add(G_ACT, "x", "Drop…", app.action_mute,
            detail="with an optional reason")
    add(G_ACT, "m", "Close remind me" if app.remind_key == it.key
        else "Remind me", app.action_remind,
        detail="the source, the original words, related items",
        titles=("Remind me",))


def _row_entries(app, it, add, in_feed):
    label = approve_label(app, it)
    if label:
        long = {"send": "Send reply…", "accept": "Accept invite",
                "nudge": "Nudge…", "ack": "Acknowledge"}[label]
        also = ("n",) if label == "nudge" else ()
        add(G_ACT, "a", long, app.action_approve, also=also,
            detail="confirm first" if label in ("send", "nudge") else "",
            titles=APPROVE_TITLES[label])
    rsvp = app._rsvp_on(it)
    if rsvp:
        add(G_ACT, "X", "Decline invite", app.action_rsvp_decline)
        add(G_ACT, "T", "Tentative", app.action_rsvp_tentative)
    if (it.kind in ("reply", "nudge") and not rsvp
            and getattr(app.actions, "supports_send", False)):
        add(G_ACT, "e", "Edit draft", app.action_edit)
    if app._outcome_on(it):
        add(G_ACT, "d", "Done with outcome…", app.action_done,
            detail="asks what happened; saved to memory before it closes")
    else:
        add(G_ACT, "d", "Dismiss" if it.kind == "fyi" else "Done",
            app.action_done)
    add(G_ACT, "s", "Snooze…", app.action_snooze)
    if draft_me_ok(app, it):
        add(G_ACT, "D", "Draft for me", app.action_draft_me,
            detail="writes a reply from the thread; never sends")
    if app._polish_on(it):
        add(G_ACT, "P", "Polish draft", app.action_polish,
            detail="in your tone; never sends")
    if app._react_on(it):
        add(G_ACT, "E", "React with an emoji…", app.action_react)
    if in_feed:
        add(G_FEED, "x", "Mute…", app.action_mute,
            detail="this kind, this routine or titles like this")
        add(G_FEED, "-", "Less like this", app.action_less,
            detail="fold the routine's rows of a day into one")

