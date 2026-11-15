"""UI row model built from item format v1 dicts (no Textual import)."""
from dataclasses import dataclass, field
from typing import List, Optional

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
    "other": ("•", "muted", "Other"),
}


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

    @property
    def resolved(self):
        """A daemon alert whose condition has ended (hidden unless R is on).
        Only alerts: another source's `meta.resolved` means nothing here."""
        return self.source == "alert" and self.meta.get("resolved") is True

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
        link=raw["link"], meta=raw["meta"],
    )


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
