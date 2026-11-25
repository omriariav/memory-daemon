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
        owner_slug: me-example
        owner_name: Me Example         # fail closed without a known name
        owner_names: [Me E.]           # optional aliases / other scripts
        mode: file                     # or review: one review todo + queue row
        review_queue: /path/queue.jsonl  # review mode; default state dir

A meeting is recognized by its source (`is_meeting_item`), never by the entry
type the model chose. In `review` mode the actions are not filed one by one:
one `needs-review` todo lists every candidate (verified or not) and one JSON
line per meeting goes to a candidates queue (see `action_review_queue.py`).

The model's `evidence_ts` is a hint, not a requirement: an unusable one (0,
"0", "00:00", free text, ...) is dropped and the row is kept, grounded by its
text against the owner's own transcript lines (`ground_without_ts`). A usable
timestamp that matches no owner line gets the same text check before the row
is given up.

Pure helpers live here (config, prompt text, strict validation, deterministic
grounding, stable keys); writing goes through memory_sink so the store CLI
stays the only writer.
"""
import hashlib
import os
import re

from . import due_phrases

MEETING_KINDS = {
    "mila", "whisper", "transcripts", "meeting-notes", "zoom", "mi",
}
DEFAULT_MAX_PER_ITEM = 5
MAX_ALLOWED = 20
DEFAULT_TAG = "from-meeting"
EVIDENCE_REASON = "meeting-owner-action"
TEXT_LIMIT = 140
TITLE_LIMIT = 120
ACTION_ID_PREFIX = "#action-"
REVIEW_ID_SUFFIX = ":review"
REVIEW_TAG = "needs-review"
REVIEW_EVIDENCE = "meeting-owner-action-review"
MODES = ("file", "review")
DEFAULT_MODE = "file"
DEFAULT_QUEUE_NAME = "meeting-action-candidates.jsonl"
NO_TS_REASON = "no timestamp and no owner-spoken line matches it"
TEXT_MATCH_REASON = "matched by text"
MAX_TS_SECONDS = 86400
TEXT_MATCH_MIN = 0.75
QUOTE_LIMIT = 240
CANDIDATE_TEXT_LIMIT = 200
MEETING_SOURCE_PREFIXES = tuple(f"{k}:" for k in sorted(MEETING_KINDS))
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
            chars, "due": "YYYY-MM-DD" (optional){ts_schema}}}. Only what the
            owner explicitly committed to, or was explicitly asked to do and
            accepted, grounded in the transcript. Never other people's actions,
            vague intentions, or app hints the transcript does not support
            (hints are candidates only). Use [] when none.{existing}
"""
TS_SCHEMA = (', "evidence_ts": the timestamp in brackets of the transcript '
             'line where it was committed to, copied exactly; leave the key '
             'out when no transcript line carries one (never 0)')

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


def mode(routine):
    value = cfg(routine).get("mode", DEFAULT_MODE)
    return value if value in MODES else DEFAULT_MODE


def review_mode(routine):
    return mode(routine) == "review"


def review_queue_path(routine, base_dir):
    """Candidates queue file: `memory.owner_todos.review_queue` (relative
    paths resolve against the daemon base dir) or `<base>/state/<default>`;
    None when neither is known."""
    value = cfg(routine).get("review_queue")
    if isinstance(value, str) and value.strip():
        path = os.path.expanduser(value.strip())
        if os.path.isabs(path):
            return path
        return os.path.join(base_dir, path) if base_dir else None
    if not base_dir:
        return None
    return os.path.join(base_dir, "state", DEFAULT_QUEUE_NAME)


def is_meeting_item(item):
    meta = item.get("frontmatter") or {}
    return item.get("source_kind") in MEETING_KINDS or bool(
        meta.get("mila_recording_id")
    )


def _name_ok(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 80


def validate(memory_cfg, rid):
    """Config problems for `memory.owner_todos`; empty list means valid."""
    if "owner_todos" not in memory_cfg:
        return []
    block = memory_cfg["owner_todos"]
    if not isinstance(block, dict):
        return [f"{rid}: memory.owner_todos must be a mapping"]
    problems = []
    unknown = set(block) - {
        "enabled", "max_per_item", "tag", "owner_slug", "owner_name",
        "owner_names", "mode", "review_queue",
    }
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
    if "owner_name" in block and not _name_ok(block["owner_name"]):
        problems.append(
            f"{rid}: memory.owner_todos.owner_name must be a non-empty string"
        )
    if "mode" in block and block["mode"] not in MODES:
        problems.append(
            f"{rid}: memory.owner_todos.mode must be one of "
            f"{', '.join(MODES)}"
        )
    if "review_queue" in block and not (
        isinstance(block["review_queue"], str)
        and block["review_queue"].strip()
    ):
        problems.append(
            f"{rid}: memory.owner_todos.review_queue must be a non-empty path"
        )
    names = block.get("owner_names")
    if "owner_names" in block and not (
        isinstance(names, list) and all(_name_ok(n) for n in names)
    ):
        problems.append(
            f"{rid}: memory.owner_todos.owner_names must be a list of "
            "non-empty strings"
        )
    return problems


def owner_names(routine, verified_name=None):
    """Display names that identify the owner; empty means fail closed."""
    block = cfg(routine)
    names = [block.get("owner_name"), *(block.get("owner_names") or []),
             verified_name]
    return list(dict.fromkeys(
        " ".join(n.split()) for n in names if _name_ok(n)
    ))


def prompt_section(routine, names, existing_titles=(), require_ts=True):
    owner = "the memory owner (" + " / ".join(f'"{n}"' for n in names) + ")"
    existing = ""
    if existing_titles:
        existing = (
            "\n            Already filed for this meeting; when an action is the "
            "same one, reuse that title verbatim as its text: "
            + "; ".join(f'"{t}"' for t in list(existing_titles)[:10])
        )
    return PROMPT_SECTION.format(
        max_per_item=max_per_item(routine), text_limit=TEXT_LIMIT,
        owner=owner, ts_schema=TS_SCHEMA if require_ts else "",
        existing=existing,
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

    Returns (clean list, dropped count), or (None, 0) when the value itself is
    not a list. An invalid element is dropped on its own; the rest are kept.
    A row without a usable `evidence_ts` is kept without one (see
    `usable_ts`); the caller grounds it by its text instead.
    """
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return None, 0
    clean, dropped = [], 0
    for row in raw:
        out = _check_row(row)[0]
        if out is None:
            dropped += 1
        else:
            clean.append(out)
    seen, unique = set(), []
    for row in clean:
        key = action_key(row["text"])
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique, dropped


def usable_ts(value):
    """A comparable timestamp string, or None when `value` is not one.

    Usable: a string that fits `_TS_RE` and names a real, non-zero time of day
    (`00:12:03`, `12:03`, `[00:12:03]`; surrounding brackets are stripped), or
    a positive number of seconds below 24 h (int/float, converted to
    `HH:MM:SS`, fraction dropped). Everything else
    (a missing value, 0, 0.0, "0", "00:00", "0:00:00", a bool, NaN, a bare
    digit string, free text) is None: a recorder's own action items carry
    `timestamp_seconds: 0` and the model copies it. Never invents a time.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        if value >= MAX_TS_SECONDS:
            return None
        secs = int(value)
        if secs <= 0:
            return None
        return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if not _TS_RE.fullmatch(text):
        return None
    norm = _norm_ts(text)
    return text if norm and norm != "00:00:00" else None


def _check_row(row):
    """(clean row, None) or (None, reason); a reason never quotes the row.

    An unusable `evidence_ts` is dropped, never a reason to refuse the row."""
    if not isinstance(row, dict):
        return None, "not an object"
    if set(row) - _KEYS:
        return None, "unexpected field"
    text = row.get("text")
    if not isinstance(text, str) or not " ".join(text.split()):
        return None, "missing text"
    text = " ".join(text.split())
    if len(text) > TEXT_LIMIT:
        return None, "text too long"
    out = {"text": text}
    due = row.get("due")
    if due is not None:
        if not isinstance(due, str) or not _DATE_RE.fullmatch(due):
            return None, "bad due date"
        out["due"] = due
    ts = usable_ts(row.get("evidence_ts"))
    if ts:
        out["evidence_ts"] = ts
    return out, None


def classify_actions(raw):
    """Every candidate the model returned, valid or not.

    Returns None when `raw` is not a list, else a list of
    `{"text", "status": "valid"|"invalid", "reason", ...}`; valid rows keep
    `due` and a usable `evidence_ts` (a row without one is still valid) and
    are de-duplicated like `validate_actions`
    (a repeat is not a candidate). An invalid row keeps a readable, capped
    `text` when it has one; its reason never quotes it."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    rows, seen = [], set()
    for row in raw:
        out, reason = _check_row(row)
        if out is None:
            text = ""
            if isinstance(row, dict) and isinstance(row.get("text"), str):
                text = " ".join(row["text"].split())[:CANDIDATE_TEXT_LIMIT]
            bad = {"text": text, "status": "invalid", "reason": reason}
            ts = usable_ts(row.get("evidence_ts")) \
                if isinstance(row, dict) else None
            if ts:
                bad["evidence_ts"] = ts
            rows.append(bad)
            continue
        key = action_key(out["text"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(dict(out, status="valid", reason=None))
    return rows


# --- deterministic grounding -------------------------------------------------

_LINE_RE = re.compile(r"^\[(?P<ts>[^\]\n]{1,40})\]\s*(?P<rest>.*)$")
_SPEAKER_RE = re.compile(r"^(?P<who>[^:\n]{1,60}):\s")
_GENERIC_SPEAKER_RE = re.compile(
    r"^(speaker|spk|unknown|guest|participant|voice)[\s_-]*\w{0,3}$", re.I
)


def _norm_ts(ts):
    """Comparable HH:MM:SS form; folds SRT commas, ranges and short forms."""
    ts = ts.strip().strip("[]").split("-->", 1)[0].strip()
    ts = ts.replace(",", ".").split(".", 1)[0]
    parts = [p.strip() for p in ts.split(":")]
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    parts = ["0"] * (3 - len(parts)) + parts
    return ":".join(f"{int(p):02d}" for p in parts)


def _name_match(label, names):
    label = label.casefold().strip()
    return any(
        label == n.casefold() or n.casefold() in label for n in names
    )


def ground_in_transcript(body, evidence_ts, names, mapped_names=()):
    """(grounded, owner line). `grounded` is True when `evidence_ts` heads a
    transcript line (rendered as `[ts] Speaker: text`) spoken by the owner.
    The line is the MATCHED line (speaker kept, capped), never another line
    that shares the second; None when not grounded or the line is empty.

    When the owner is mapped to a speaker in this recording (a line in the body
    carries an owner name, or `mapped_names` lists one) only the owner's own
    lines ground an action: lines from any other key, named or raw
    `SPEAKER_NN`, fail. When the owner is not mapped at all, a named
    non-owner fails and an unnamed or missing label passes."""
    want = _norm_ts(evidence_ts or "")
    if want is None:
        return False, None
    rows = []
    for line in str(body or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        who = _SPEAKER_RE.match(m.group("rest"))
        rows.append((_norm_ts(m.group("ts")),
                     who.group("who").strip() if who else None,
                     " ".join(m.group("rest").split())[:QUOTE_LIMIT] or None))
    owner_mapped = any(_name_match(n, names) for n in mapped_names) or any(
        label and _name_match(label, names) for _, label, _ in rows
    )
    for ts, label, rest in rows:
        if ts != want:
            continue
        if label and _name_match(label, names):
            return True, rest
        if not owner_mapped and (not label or _GENERIC_SPEAKER_RE.match(label)):
            return True, rest
    return False, None


def _label_match(label, names):
    """True when the speaker `label` IS an owner name: its whole words contain
    all the words of a name, in order (case-insensitive, any script). "Dan"
    and "Dan Cohen" match the name "Dan"; "Daniel" does not."""
    got = re.findall(r"\w+", label.casefold())
    for name in names:
        want = re.findall(r"\w+", name.casefold())
        if want and any(got[i:i + len(want)] == want
                        for i in range(len(got) - len(want) + 1)):
            return True
    return False


def ground_without_ts(body, text, names):
    """(grounded, owner line) for an action that has no usable timestamp, or
    whose timestamp matched no owner line.

    Weaker than `ground_in_transcript`, but still owner-only: grounded when a
    transcript line whose speaker label is an owner name (whole words, see
    `_label_match`) shares at least `TEXT_MATCH_MIN` of the action's
    significant words (4-letter stems, as `body_quote`). An unnamed or
    non-owner line never grounds it. The line returned is the best-scoring
    owner line (the first on a tie), speaker kept, capped. An action with
    fewer than two significant words is never grounded this way."""
    words = {w[:4] for w in normalize(text).split() if len(w) >= 4}
    if len(words) < 2:   # one shared word is no evidence
        return False, None
    best, best_score = None, 0.0
    for line in str(body or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        who = _SPEAKER_RE.match(m.group("rest"))
        if not who or not _label_match(who.group("who"), names):
            continue
        spoken = normalize(m.group("rest")[who.end():])
        score = len(words & {w[:4] for w in spoken.split()}) / len(words)
        if score > best_score:
            best, best_score = m.group("rest"), score
    if best is not None and best_score >= TEXT_MATCH_MIN:
        return True, " ".join(best.split())[:QUOTE_LIMIT] or None
    return False, None


def grounded_in_transcript(body, evidence_ts, names, mapped_names=()):
    return ground_in_transcript(body, evidence_ts, names, mapped_names)[0]


def transcript_quote(body, evidence_ts):
    """The first transcript line (`Speaker: text`, speaker kept) at
    `evidence_ts`, capped, whoever spoke it; None when no line carries that
    timestamp. Context for a candidate that did not ground, never a quote."""
    want = _norm_ts(evidence_ts or "")
    if want is None:
        return None
    for line in str(body or "").splitlines():
        m = _LINE_RE.match(line.strip())
        if m and _norm_ts(m.group("ts")) == want:
            rest = " ".join(m.group("rest").split())
            return rest[:QUOTE_LIMIT] or None
    return None


def body_quote(body, text, names):
    """Backfill grounding: the sentence or bullet of the stored body where an
    owner name shares most of the action's significant words (capped), or
    None. Weaker than the transcript check: the stored body is a model
    summary, not the source."""
    # Compare 4-letter stems so "send" matches "sends".
    words = {w[:4] for w in normalize(text).split() if len(w) >= 4}
    if not words:
        return None
    for seg in re.split(r"[\n.;!?]+", str(body or "")):
        seg_norm = normalize(seg)
        if not any(normalize(n) and normalize(n) in seg_norm for n in names):
            continue
        if len(words & {w[:4] for w in seg_norm.split()}) >= 0.6 * len(words):
            return " ".join(seg.split())[:QUOTE_LIMIT]
    return None


def grounded_in_body(body, text, names):
    """Backfill check: an owner name shares a sentence or bullet with the
    action (most of its significant words appear there)."""
    return body_quote(body, text, names) is not None


def apply_due_phrase(action, anchor, week, line=None):
    """`action` with its `due` replaced by the code-resolved date of a
    relative phrase ("early next week", "by Thursday", ...). The action text
    is checked first and decides whenever it holds a phrase (an ambiguous one
    leaves the model's `due`); only when it has none is the owner's spoken
    `line`, used when it holds exactly one phrase. The model's `due` stays
    when nothing resolves. Never mutates `action`."""
    count, due = due_phrases.analyse(action.get("text"), anchor, week)
    if not count:
        count, due = due_phrases.analyse(
            line or action.get("line"), anchor, week)
        if count != 1:
            due = None
    return dict(action, due=due) if due else action


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


# --- review mode -------------------------------------------------------------

def review_source_id(meeting_source_id):
    return f"{meeting_source_id}{REVIEW_ID_SUFFIX}"


def review_title(meeting_title):
    return todo_title(f"Review action items: {meeting_title or 'meeting'}")


def review_tags(routine, routine_tags, personal_tag=None, personal=False):
    return list(dict.fromkeys(
        todo_tags(routine, routine_tags, personal_tag, personal)
        + [REVIEW_TAG]))


def _flag(candidate):
    status = candidate["status"]
    if status == "valid" and candidate.get("reason") == TEXT_MATCH_REASON:
        return " [matched by text]"
    if status == "ungrounded":
        if candidate.get("reason") == NO_TS_REASON:
            return " [unverified: no timestamp]"
        return " [unverified: ungrounded]"
    if status == "invalid":
        return f" [invalid: {candidate.get('reason') or 'unknown'}]"
    return ""


def review_body(meeting_entry_id, date, candidates, redact=None):
    """Body of the one review todo: the meeting link and one bullet per
    candidate, its grounding quote if any, a marker when unverified."""
    redact = redact or (lambda text: text)
    head = (f"From meeting {meeting_entry_id or 'unknown'} "
            f"(evidence: {REVIEW_EVIDENCE}); due {date}.")
    lines = [head, "Review each candidate; file the real ones, then close "
             "this todo.", ""]
    for c in candidates:
        text = redact(c.get("text") or "") or "(unreadable candidate)"
        line = f"- {text}{_flag(c)}"
        if c.get("due"):
            line += f" (due {c['due']})"
        if c.get("quote"):
            line += f'\n  quote: "{redact(c["quote"])}"'
        elif c.get("line_at_ts"):
            line += f'\n  line at timestamp: "{redact(c["line_at_ts"])}"'
        lines.append(line)
    return "\n".join(lines)


def candidate_row(candidate, owner_slug, redact=None):
    redact = redact or (lambda text: text)
    row = {
        "text": redact(candidate.get("text") or ""),
        "quote": redact(candidate["quote"]) if candidate.get("quote") else None,
    }
    if candidate.get("line_at_ts"):
        row["line_at_ts"] = redact(candidate["line_at_ts"])
    if candidate.get("due"):
        row["due"] = candidate["due"]
    row.update(owner=owner_slug, status=candidate["status"],
               reason=candidate.get("reason"))
    return row


def cap_candidates(candidates, limit):
    """Order valid, then ungrounded, then invalid, and cap at `limit` without
    ever dropping a valid candidate: only the flagged tail is trimmed (to the
    slots the valid ones leave). Returns (kept, dropped count)."""
    rank = {"valid": 0, "ungrounded": 1, "invalid": 2}
    ordered = sorted(candidates, key=lambda c: rank[c["status"]])
    valid = sum(1 for c in ordered if c["status"] == "valid")
    keep = max(limit, valid)
    return ordered[:keep], len(ordered) - keep if len(ordered) > keep else 0
