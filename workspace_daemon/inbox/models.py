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
        draft=raw["draft"] or "", actions=list(raw["actions"]),
        link=raw["link"], meta=raw["meta"],
    )
