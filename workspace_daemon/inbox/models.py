"""UI row model built from item format v1 dicts (no Textual import)."""
from collections import namedtuple
from dataclasses import dataclass, field
from typing import List, Optional

from .items import MEMORY_ID

SECTIONS = [
    ("urgent", "Urgent"),
    ("needs_reply", "Needs reply"),
    ("waiting", "Waiting on them / nudge due"),
    ("fyi", "FYI / digest"),
]
SECTION_KEYS = [k for k, _ in SECTIONS]
SECTION_TITLES = dict(SECTIONS)

# source -> (glyph, palette key, label)
SOURCES = {
    "slack": ("#", "primary", "Slack"),
    "email": ("✉", "mine", "Email"),
    "calendar": ("◷", "ok", "Calendar"),
    "recording": ("◉", "ok", "Recording"),
    "alert": ("▲", "warn", "Alert"),
    "drive": ("▤", "accent", "Drive"),
    "sheet": ("▦", "accent", "Sheet"),
    "memory": ("◈", "mine", "Memory"),
    "other": ("•", "muted", "Other"),
}


def fmt_age(secs):
    """`now`, `5m`, `2h05m`, `3d`: a short age for a row."""
    s = max(0, int(secs))
    if s < 60:
        return "now"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h" if h >= 3 or not m else f"{h}h{m:02d}m"
    return f"{s // 86400}d"


@dataclass
class Msg:
    who: str
    ts: float
    text: str
    mine: bool = False


@dataclass
class Item:
    key: str                       # the v1 item id
    section: str
    who: str                       # sender
    where: str
    ts: float
    gist: str
    source: str = "other"
    messages: List[Msg] = field(default_factory=list)
    draft: str = ""
    actions: List[str] = field(default_factory=list)
    link: Optional[str] = None
    meta: dict = field(default_factory=dict)
    drafted_by: str = "source"
    streamed: bool = False         # UI-only: the draft animation already ran
    saved: bool = False            # UI-only: marked saved to memory (local state)
    hint: str = ""                 # UI-only: a line shown above the thread (warn colour)
    origin: str = ""               # UI-only: the name of the source that listed it
    feed: str = "show"             # UI-only: the Feed rule outcome (show/digest/hide/inbox)
    feed_label: str = ""           # UI-only: what the matching rule says (muted list)
    local_due: Optional[tuple] = None   # UI-only (tasks): `(date or None,)`, a due date set here that the list has not caught up with yet
    task: Optional[tuple] = field(default=None, repr=False, compare=False)   # UI-only: `(agent row, launcher.Card)` of the task started from this row
    members: List["Item"] = field(default_factory=list, repr=False,
                                  compare=False)   # UI-only: a digest row's items
    raw: Optional[dict] = field(default=None, repr=False, compare=False)

    @property
    def memory_id(self):
        """The memory entry this item is already saved as (`meta.memory_id`,
        set by the daemon's capture or by the inbox's match), else ""."""
        return self._memory_key("memory_id")

    @property
    def thread_memory_id(self):
        """The memory entry the item's Slack thread was saved as, when the
        item itself is not known to be in memory (`meta.thread_memory_id`)."""
        return "" if self.memory_id else self._memory_key("thread_memory_id")

    def _memory_key(self, key):
        mid = self.meta.get(key) if isinstance(self.meta, dict) else None
        return mid if isinstance(mid, str) and MEMORY_ID.match(mid) else ""

    @property
    def resolved(self):
        """A daemon alert whose condition has ended (hidden unless R is on).
        Only alerts: another source's `meta.resolved` means nothing here."""
        return self.source == "alert" and self.meta.get("resolved") is True

    @property
    def routine(self):
        """The routine (or source name) the Feed groups this row under."""
        name = self.meta.get("routine") if isinstance(self.meta, dict) else None
        return name if isinstance(name, str) and name else (
            self.origin or self.source)

    @property
    def direction(self):
        """`←` someone waits on you, `→` you wait on someone, else ''."""
        if self.section == "waiting":
            return "→"
        if (self.section in ("needs_reply", "urgent")
                and "reply" in self.actions
                and self.meta.get("kind") != "invite"):
            return "←"
        return ""

    @property
    def kind(self):
        """What the main action key does: nudge | reply | ack | fyi."""
        if self.section == "waiting" and "nudge" in self.actions:
            return "nudge"
        if "reply" in self.actions:
            return "reply"
        if "ack" in self.actions:
            return "ack"
        return "fyi"

    @property
    def destination(self):
        if self.source == "email":
            return f"email to {self.who} ({self.where})"
        if self.where.startswith("#"):
            return f"{self.where} (thread with {self.who})"
        return f"{self.where} with {self.who}"


def from_v1(raw):
    """A v1 item dict (already validated) to an Item."""
    return Item(
        key=raw["id"], section=raw["section"], who=raw["sender"],
        where=raw["where"], ts=float(raw["ts"]), gist=raw["gist"],
        source=raw["source"],
        messages=[Msg(m["who"], float(m["ts"]), m["text"],
                      mine=m["who"] in ("You", "you", "me"))
                  for m in raw["thread"]],
        draft=raw["draft"] or "",
        # `open` needs somewhere to go: hide it for an item with no link.
        actions=[a for a in raw["actions"]
                 if a != "open" or raw["link"] is not None],
        link=raw["link"], meta=raw["meta"], raw=raw,
    )


THREAD_NOTE_MAX = 200


def thread_note(item):
    """The optional `meta.thread_note`: one cleaned line of up to 200
    characters, or "" (absent, not a string, or blank after cleaning)."""
    from .items import clean_line, truncate
    note = item.meta.get("thread_note") if isinstance(item.meta, dict) else None
    if not isinstance(note, str):
        return ""
    return truncate(clean_line(note), THREAD_NOTE_MAX)


OWE_ACTION_KEYS = ("task", "todo", "ask")   # `meta` keys, first one set wins
OWE_ACTION_MAX = 200
OWE_DRAFT_MAX = 100
OWE_QUOTE_LINES = 4          # logical lines of the quoted message
OWE_QUOTE_CHARS = 400        # and a cap in characters (before wrapping)


def owes(item):
    """True for a row of the Inbox's I OWE group that has something to show:
    a `needs_reply` item that is not a task and not a fixed-entry thread."""
    return (item.section == "needs_reply"
            and not (isinstance(item.meta, dict)
                     and (item.meta.get("kind") == "task"
                          or item.meta.get("thread_fixed") is True)))


Owed = namedtuple("Owed", "who ts text by_sender nudges")
# Words that make a very short message a nudge, not an ask ("any update?").
NUDGE_WORDS = frozenset((
    "ping", "pinging", "bump", "bumping", "any", "update", "updates", "news",
    "thoughts", "hello", "hi", "hey", "still", "there", "anyone", "reminder",
    "gentle", "friendly", "nudge", "again", "please", "pls", "thanks", "thx"))
NUDGE_MAX_WORDS = 3


def is_nudge(text):
    """True for a very short message with no content of its own: at most
    three words, each a nudge word or without letters or digits (`?`, `ping`,
    `any update?`). A three-word ask such as `approve the budget` is not."""
    words = text.split()
    if not words or len(words) > NUDGE_MAX_WORDS:
        return False
    for word in words:
        bare = "".join(c for c in word.lower() if c.isalnum())
        if bare and bare not in NUDGE_WORDS:
            return False
    return True


def same_person(a, b):
    """True when two display names are the same person: equal without case,
    or one is a prefix of the other (`Dana` and `Dana Example`), or the first
    names match."""
    a, b = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not a or not b:
        return False
    if a == b or a.startswith(b + " ") or b.startswith(a + " "):
        return True
    return a.split(" ")[0] == b.split(" ")[0] and len(a.split(" ")[0]) >= 2


def owe_message(item):
    """The message that creates the obligation of an I OWE item, an `Owed`
    `(who, ts, text, by_sender, nudges)`: the first substantive message after
    your last reply from the item's sender (the match is loose, see
    `same_person`), else from anyone else. A very short nudge (`?`, `ping`,
    `any update`) is skipped when choosing; the nudges that came after the
    chosen message are `nudges`, a list of their times. With only nudges the
    newest one is chosen. With no message after your reply it is the item's
    gist (`who`, `ts` of the item), the source's own account of what is
    waiting. An invite has no message: its gist is the invitation.
    `by_sender` is False when the author is not the item's sender."""
    if (item.meta.get("kind") if isinstance(item.meta, dict) else None) \
            == "invite":
        return Owed(item.who, item.ts, item.gist, True, [])
    last_mine = max((n for n, m in enumerate(item.messages) if m.mine),
                    default=-1)
    after = [m for m in item.messages[last_mine + 1:] if not m.mine
             and m.text.strip()]
    if not after:
        return Owed(item.who, item.ts, item.gist, True, [])
    from_sender = [m for m in after if same_person(m.who, item.who)]
    pool = from_sender or after
    substantive = [m for m in pool if not is_nudge(m.text)]
    if substantive:
        chosen = substantive[0]
        nudges = [m.ts for m in pool[pool.index(chosen) + 1:]
                  if is_nudge(m.text)]
    else:
        chosen, nudges = pool[-1], []
    return Owed(chosen.who, chosen.ts, chosen.text, bool(from_sender), nudges)


def owe_action(item):
    """What is to be done, as one line, or "" when the item does not say: the
    first of `meta.task`, `meta.todo` and `meta.ask` that is a non-blank
    string, else `RSVP` for an invite, else the drafted reply (its first
    line), else `acknowledge` for an item that only takes an ack."""
    from .items import clean_line, truncate
    meta = item.meta if isinstance(item.meta, dict) else {}
    for key in OWE_ACTION_KEYS:
        value = meta.get(key)
        if isinstance(value, str):
            line = truncate(clean_line(value), OWE_ACTION_MAX)
            if line:
                return line
    if meta.get("kind") == "invite":
        return "RSVP"
    if item.draft and item.draft.strip():
        first = next((ln for ln in item.draft.split("\n") if ln.strip()), "")
        return "reply: " + truncate(clean_line(first), OWE_DRAFT_MAX)
    if "reply" not in item.actions and "ack" in item.actions:
        return "acknowledge"
    return ""


def thread_fixed(item):
    """True when `meta.thread_fixed` is set: the thread is a fixed list of
    labelled entries (an alert's), shown top-down in either thread order."""
    return isinstance(item.meta, dict) and item.meta.get("thread_fixed") is True


def to_v1(item):
    """The v1 dict of an item: the validated one it was built from, else a
    rebuild from its fields (an item made by the UI itself)."""
    if item.raw is not None:
        return dict(item.raw)
    return {
        "v": 1, "id": item.key, "source": item.source,
        "section": item.section,
        "urgency": "urgent" if item.section == "urgent" else "digest",
        "sender": item.who, "where": item.where, "gist": item.gist,
        "ts": item.ts, "link": item.link,
        "thread": [{"who": m.who, "ts": m.ts, "text": m.text}
                   for m in item.messages],
        "draft": item.draft or None, "actions": list(item.actions),
        "meta": dict(item.meta),
    }


# --- clipboard text ---------------------------------------------------------
# Built from the item's own logical strings (never the bidi-reordered display
# text) and never from markup, so what is copied is what the source sent.

COPY_SEP = " — "
_MD_SPECIAL = "\\*_~&[]`<"


def item_line(item):
    """`sender — where — gist`, the one-line form of an item."""
    return COPY_SEP.join((item.who, item.where, item.gist))


def copy_item_text(item):
    """One item for `y`: the line, then the link on its own line."""
    return item_line(item) + (f"\n{item.link}" if item.link else "")


def copy_section_text(items):
    """A section for `Y`: one item per line, the link (if any) after a
    separator on the same line."""
    return "\n".join(item_line(i) + (COPY_SEP + i.link if i.link else "")
                     for i in items)


def _md_escape(text):
    return "".join("\\" + c if c in _MD_SPECIAL else c for c in text)


def copy_section_markdown(items):
    """A section as a Markdown list: `- **sender** (where): gist [link](url)`."""
    lines = []
    for i in items:
        line = f"- **{_md_escape(i.who)}** ({_md_escape(i.where)}): {_md_escape(i.gist)}"
        if i.link:
            line += f" [link]({i.link.replace('(', '%28').replace(')', '%29')})"
        lines.append(line)
    return "\n".join(lines)
