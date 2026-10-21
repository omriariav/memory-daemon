"""Owner action items from captured meetings, filed as separate todo entries.

A meeting source yields ONE `meeting` memory entry whose action items are only
body text. With `memory.owner_todos.enabled` the extraction step also returns
the memory owner's own actions, and the sink files each as its own `todo` entry
so the task-inbox export can see it:

    memory:
      owner_todos:
        enabled: true
        max_per_item: 5
        tag: from-meeting
        owner_slug: me-example   # optional; default: the verified owner slug

Pure helpers live here (config, prompt text, strict validation, stable keys);
writing goes through memory_sink so the store CLI stays the only writer.
"""
import hashlib
import re

MEETING_KINDS = {"mila", "whisper", "transcripts", "meeting-notes"}
DEFAULT_MAX_PER_ITEM = 5
MAX_ALLOWED = 20
DEFAULT_TAG = "from-meeting"
EVIDENCE_REASON = "meeting-owner-action"
TEXT_LIMIT = 140
TITLE_LIMIT = 120
ACTION_ID_PREFIX = "#action-"
NOISE_TAGS = {
    "meeting", "meetings", "transcription", "transcript", "transcripts",
    "mila", "whisper", "meeting-notes", "recording",
}
_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TS_RE = re.compile(r"[0-9][0-9:.,\-\[\] ]{0,24}")
_KEYS = {"text", "due", "evidence_ts"}

PROMPT_SECTION = """  "owner_actions": array (at most {max_per_item}) of {owner}'s OWN action items
            from this meeting, each {{"text": imperative sentence <= {text_limit}
            chars, "due": "YYYY-MM-DD" (optional), "evidence_ts": transcript
            timestamp (optional)}}. Only what the owner explicitly committed to,
            or was explicitly asked to do and accepted, grounded in the
            transcript. Never other people's actions, vague intentions, or app
            hints the transcript does not support (hints are candidates only).
            Use [] when none.
"""

BACKFILL_PROMPT = """You are reading a stored meeting memory entry. Answer with a SINGLE JSON
object, no markdown fence, no other text, with exactly one key:
{section}
--- MEETING ENTRY ---
Title: {title}
Date: {date}
{body}
"""


def cfg(routine):
    memory = routine.get("memory")
    block = memory.get("owner_todos") if isinstance(memory, dict) else None
    return block if isinstance(block, dict) else {}


def enabled(routine):
    return cfg(routine).get("enabled") is True


def max_per_item(routine):
    value = cfg(routine).get("max_per_item", DEFAULT_MAX_PER_ITEM)
    return value if type(value) is int and 1 <= value <= MAX_ALLOWED \
        else DEFAULT_MAX_PER_ITEM


def tag(routine):
    value = cfg(routine).get("tag", DEFAULT_TAG)
    return value if isinstance(value, str) and _SLUG_RE.fullmatch(value) \
        else DEFAULT_TAG


def is_meeting_item(item):
    meta = item.get("frontmatter") or {}
    return item.get("source_kind") in MEETING_KINDS or bool(
        meta.get("mila_recording_id")
    )


def validate(memory_cfg, rid):
    """Config problems for `memory.owner_todos`; empty list means valid."""
    if "owner_todos" not in memory_cfg:
        return []
    block = memory_cfg["owner_todos"]
    if not isinstance(block, dict):
        return [f"{rid}: memory.owner_todos must be a mapping"]
    problems = []
    unknown = set(block) - {"enabled", "max_per_item", "tag", "owner_slug"}
    if unknown:
        problems.append(
            f"{rid}: memory.owner_todos has unknown key(s) "
            f"{', '.join(sorted(unknown))}"
        )
    if "enabled" in block and type(block["enabled"]) is not bool:
        problems.append(f"{rid}: memory.owner_todos.enabled must be true or false")
    if "max_per_item" in block and not (
        type(block["max_per_item"]) is int
        and 1 <= block["max_per_item"] <= MAX_ALLOWED
    ):
        problems.append(
            f"{rid}: memory.owner_todos.max_per_item must be an integer "
            f"from 1 to {MAX_ALLOWED}"
        )
    for key in ("tag", "owner_slug"):
        if key in block and not (
            isinstance(block[key], str) and _SLUG_RE.fullmatch(block[key])
        ):
            problems.append(
                f"{rid}: memory.owner_todos.{key} must be a kebab-case slug"
            )
    return problems


def prompt_section(routine, owner_name=None):
    return PROMPT_SECTION.format(
        max_per_item=max_per_item(routine), text_limit=TEXT_LIMIT,
        owner=owner_name or "the memory owner (identified by the speaker mapping)",
    )


def normalize(text):
    """Case/whitespace/punctuation-insensitive form used for the stable key."""
    words = re.sub(r"[^\w\s]", " ", str(text or "").casefold()).split()
    return " ".join(words)


def action_key(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:10]


def action_source_id(meeting_source_id, text):
    return f"{meeting_source_id}{ACTION_ID_PREFIX}{action_key(text)}"


def validate_actions(raw):
    """Strictly validate the model's `owner_actions`.

    Returns a list of clean dicts (possibly empty), or None when the value is
    present but invalid in any way: one bad element voids the whole list.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    clean = []
    for row in raw:
        if not isinstance(row, dict) or set(row) - _KEYS:
            return None
        text = row.get("text")
        if not isinstance(text, str) or not " ".join(text.split()):
            return None
        text = " ".join(text.split())
        if len(text) > TEXT_LIMIT:
            return None
        out = {"text": text}
        due = row.get("due")
        if due is not None:
            if not isinstance(due, str) or not _DATE_RE.fullmatch(due):
                return None
            out["due"] = due
        ts = row.get("evidence_ts")
        if ts is not None:
            if not isinstance(ts, str) or not _TS_RE.fullmatch(ts.strip()):
                return None
            out["evidence_ts"] = ts.strip()
        clean.append(out)
    seen, unique = set(), []
    for row in clean:
        key = action_key(row["text"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def todo_title(text):
    line = " ".join(str(text).split())
    return line if len(line) <= TITLE_LIMIT else line[:TITLE_LIMIT - 3].rstrip() + "..."


def todo_body(meeting_entry_id, action):
    parts = [f"From meeting {meeting_entry_id}"]
    if action.get("evidence_ts"):
        parts.append(f"at {action['evidence_ts']}")
    line = " ".join(parts) + f" (evidence: {EVIDENCE_REASON})"
    if action.get("due"):
        line += f"; due {action['due']}"
    return line + "."


def todo_tags(routine, routine_tags, personal_tag=None, personal=False):
    tags = [tag(routine)]
    tags += [t for t in routine_tags if t not in NOISE_TAGS]
    if personal and personal_tag:
        tags.append(personal_tag)
    return list(dict.fromkeys(tags))
