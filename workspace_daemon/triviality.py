"""Deterministic triviality check for chat threads (no model call)."""
import re

CHAT_KINDS = ("slack", "slack_mcp", "gchat")
DEFAULT_MAX_CHARS = 120
# A question this short is conversational ("on the 3rd floor, right?").
SHORT_QUESTION_CHARS = 40
# Several people exchanging this much text is a discussion, not an ack.
MULTI_AUTHOR_CHARS = 80

_MESSAGE_RE = re.compile(r"^\[[^\]\n]*\] ([^:\n]{1,80}): ?(.*)$")
_REACTIONS_RE = re.compile(r"(?i)\breactions:.*$")
_EMOJI_RE = re.compile(r":[A-Za-z0-9_+'-]+:")
_MENTION_RE = re.compile(r"<@[A-Za-z0-9]+(?:\|[^>]*)?>")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_FILE_RE = re.compile(
    r"(?im)^\s*(?:files?|attachments?|\[(?:file|attachment|image)[^\]]*\])\b"
)
# Conservative substance signals: any hit keeps a short message.
_SUBSTANCE_RE = re.compile(
    r"(?i)\b(?:decid\w*|decision|agree\w*|approv\w*|reject\w*|option\s+\w+|"
    r"go(?:ing)?\s+with|we(?:'ll|\s+will|\s+should|\s+need|\s+chose|\s+pick\w*)|"
    r"let'?s|deadline|launch\w*|ship\w*|deploy\w*|rollout|migrat\w*|budget|"
    r"priorit\w*|blocker|blocked|assign\w*|owner|todo|action\s+item|"
    r"commit\w*|promise\w*|cancel\w*|renew\w*|contract|invoice|incident|"
    r"outage|bug|hire|hiring|offer|salary|raise|promotion|feedback|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|"
    r"january|february|march|april|june|july|august|september|october|"
    r"november|december)\b"
    r"|\b\d{1,2}[:/.-]\d{1,2}\b|\b\d+\s?(?:%|k|m|usd|eur|ils|\$)"
    r"|[$€£]\s?\d"
)
_ACRONYM_RE = re.compile(r"\b(?!OK\b)[A-Z]{2,}\b")


def is_chat_kind(kind):
    return kind in CHAT_KINDS


def _clean(text):
    text = _REACTIONS_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = _MENTION_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _messages(body):
    """Split a rendered chat body into (author, raw text) messages."""
    out = []
    for line in (body or "").splitlines():
        match = _MESSAGE_RE.match(line)
        if match:
            out.append([match.group(1).strip(), match.group(2)])
        elif out:
            out[-1][1] += "\n" + line
        else:
            out.append(["?", line])
    return out


def human_text(body):
    """(total cleaned text, {author: cleaned chars}) for a rendered body."""
    per_author = {}
    parts = []
    for author, raw in _messages(body):
        cleaned = " ".join(_clean(line) for line in raw.splitlines()).strip()
        if not cleaned:
            continue
        parts.append(cleaned)
        per_author[author] = per_author.get(author, 0) + len(cleaned)
    return " ".join(parts), per_author


def is_borderline(item, max_chars=DEFAULT_MAX_CHARS):
    """Short chat text, whatever the substance guards say."""
    if not is_chat_kind(item.get("source_kind")):
        return False
    text, _ = human_text(item.get("body", ""))
    return len(text) <= max_chars


def assess(item, max_chars=DEFAULT_MAX_CHARS):
    """Return (is_trivial, reason) for a chat item; conservative by design."""
    if not is_chat_kind(item.get("source_kind")):
        return False, "not a chat source"
    body = item.get("body", "")
    text, per_author = human_text(body)
    if len(text) > max_chars:
        return False, "long text"
    if _URL_RE.search(body):
        return False, "contains a link"
    if _FILE_RE.search(body):
        return False, "contains a file or attachment"
    if "?" in text and len(text) > SHORT_QUESTION_CHARS:
        return False, "substantive question"
    if len(per_author) >= 2 and len(text) > MULTI_AUTHOR_CHARS:
        return False, "multi-author discussion"
    if _SUBSTANCE_RE.search(text) or _ACRONYM_RE.search(text):
        return False, "decision/date/commitment signal"
    return True, f"{len(text)} chars of small talk"
