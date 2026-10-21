"""Deterministic triviality check for chat threads (no model call).

Allowlist based: a thread is small talk only when every remaining word is a
known acknowledgement/greeting. Anything else is kept for the model.
"""
import re
import unicodedata

CHAT_KINDS = ("slack", "slack_mcp", "gchat")
DEFAULT_MAX_CHARS = 120

_WORDS = """
thanks thank thx ty you ok okay sure cool great nice np lol haha hi hey hello
morning good sounds yes yep yup problem welcome got it all right
alright fine perfect awesome appreciate appreciated
bye cheers worries
תודה רבה סבבה אחלה מעולה יופי בסדר היי הי בוקר טוב כן אוקי בעיה
בכיף שלום ערב לילה אשמח תודה! הבנתי קיבלתי
"""
ALLOWLIST = frozenset(_WORDS.split())

# Any request/question form keeps the thread.
_REQUEST_RE = re.compile(
    r"(?i)\b(?:can|could|would|will)\s+(?:you|u)\b|\bplease\b|\bpls\b|\bplz\b"
    r"|תוכל|תוכלי|תעביר|תעבירי|תשלח|תשלחי|בבקשה"
)
_MESSAGE_RE = re.compile(r"^\[[^\]\n]*\] ([^:\n]{1,80}): ?(.*)$")
_REACTIONS_RE = re.compile(r"(?i)\breactions:.*$")
_EMOJI_RE = re.compile(r":[A-Za-z0-9_+'-]+:")
_MENTION_RE = re.compile(r"<@[A-Za-z0-9]+(?:\|[^>]*)?>")
_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_FILE_RE = re.compile(
    r"(?im)^\s*(?:files?|attachments?|\[(?:file|attachment|image)[^\]]*\])\b"
)


def is_chat_kind(kind):
    return kind in CHAT_KINDS


def _clean(text):
    text = _REACTIONS_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    return _MENTION_RE.sub("", text)


def _messages(body):
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
    """Cleaned human text of a rendered chat body (emoji/mentions/reactions out)."""
    parts = []
    for _author, raw in _messages(body):
        cleaned = " ".join(_clean(line) for line in raw.splitlines()).strip()
        if cleaned:
            parts.append(cleaned)
    return " ".join(parts)


def _tokens(text):
    """Lowercased word tokens; punctuation, symbols and pictographs dropped."""
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat[0] in "LNM" or ch.isspace() or ch in "'’":
            kept.append(ch)
        else:
            kept.append(" ")
    words = [t.strip("'’") for t in "".join(kept).lower().split() if t.strip("'’")]
    # "no"/"לא" alone can answer a question or decline; only the set phrases
    # "no problem/worries" and "אין בעיה" count as small talk. A commitment
    # such as "will do" or "done" is never small talk.
    out = []
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if word == "no" and nxt in ("problem", "worries"):
            out.append("np")
        elif word == "אין" and nxt == "בעיה":
            out.append("np")
        else:
            out.append(word)
    return out


def is_borderline(item, max_chars=DEFAULT_MAX_CHARS):
    """Short chat text, whatever the substance guards say."""
    if not is_chat_kind(item.get("source_kind")):
        return False
    return len(human_text(item.get("body", ""))) <= max_chars


def guard_reason(item):
    """Why a chat item has substance the guards always keep, else None.

    Link, file/attachment and request-form checks shared with ``assess``;
    used to keep a local model from ever judging such items.
    """
    body = item.get("body") or ""
    if _URL_RE.search(body):
        return "contains a link"
    if _FILE_RE.search(body):
        return "contains a file or attachment"
    if _REQUEST_RE.search(human_text(body)):
        return "request form"
    return None


def assess(item, max_chars=DEFAULT_MAX_CHARS, extra_words=()):
    """Return (is_trivial, reason) for a chat item; conservative by design."""
    if not is_chat_kind(item.get("source_kind")):
        return False, "not a chat source"
    body = item.get("body", "")
    evidence = (item.get("frontmatter") or {}).get("slack_owner_evidence") or []
    if "mentioned" in evidence:
        return False, "mentions the owner"
    if _URL_RE.search(body):
        return False, "contains a link"
    if _FILE_RE.search(body):
        return False, "contains a file or attachment"
    text = human_text(body)
    if len(text) > max_chars:
        return False, "long text"
    if _REQUEST_RE.search(text):
        return False, "request form"
    allowed = ALLOWLIST | {
        w.strip().lower() for w in extra_words or () if isinstance(w, str)
    }
    outside = [t for t in _tokens(text) if t not in allowed]
    if outside:
        return False, f"non-small-talk word {outside[0]!r}"
    return True, "small talk only"
