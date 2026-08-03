"""Obsidian note rendering and writing.

Sources hand the writer a normalized `item`, so a routine's output shape does
not depend on where its content came from:

    {"id", "title", "date", "body", "frontmatter"}

`frontmatter` holds the source-specific keys (gmail_message_id, drive_file_id,
...) and is spliced in after the common header fields.
"""
import datetime
import re
from pathlib import Path

import yaml

from . import state
from .shell import utc_now_iso

DEFAULT_FILENAME_TEMPLATE = "{slug_prefix}-{date}"
MAX_TITLE_SLUG = 70

# {subject} and {message_id} are the original Gmail-era spellings, kept as
# aliases so existing routines keep working.
FILENAME_FIELDS = {"slug_prefix", "date", "title", "subject", "id", "message_id"}


def slugify(text):
    text = re.sub(r"[^a-zA-Z0-9\s-]", "", text or "").strip().lower()
    return re.sub(r"[\s-]+", "-", text)


def title_slug(title):
    """Slugified title, trimmed at a word boundary so filenames stay readable."""
    slug = slugify(title)
    if len(slug) > MAX_TITLE_SLUG:
        slug = slug[:MAX_TITLE_SLUG].rsplit("-", 1)[0]
    return slug or "untitled"


def email_date(headers):
    """RFC 2822 header date → YYYY-MM-DD, falling back to today."""
    raw = headers.get("date", "")
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%d %b %Y %H:%M:%S %z"):
        try:
            return datetime.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.datetime.strptime(raw[:31], "%a, %d %b %Y %H:%M:%S %z").date().isoformat()
    except ValueError:
        return datetime.date.today().isoformat()


ID_KEYS = ("item_id", "gmail_message_id", "drive_file_id")


def note_owner(path):
    """The item id recorded in an existing note's frontmatter, or None."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        front = yaml.safe_load(text[3:end])
    except yaml.YAMLError:
        return None
    if not isinstance(front, dict):
        return None
    for key in ID_KEYS:
        if front.get(key):
            return str(front[key])
    return None


def _contained_note_path(directory, candidate):
    """Return candidate only when its resolved location stays in directory."""
    resolved_directory = directory.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_directory)
    except ValueError as exc:
        raise ValueError(
            f"refusing note path outside output.vault_dir: {candidate}"
        ) from exc
    return candidate


def target_path(routine, item):
    """Deterministic note path; suffixed with a short item id on collision.

    output.filename_template accepts {slug_prefix}, {date}, {title} and {id}.
    Routines whose matches share a date (several meetings in one day) want
    {title} in there, or every note collides onto the same stem.

    Collision is judged by *ownership*, not mere existence. A crash between
    writing the note and recording the ledger entry leaves an unledgered note
    behind; the retry must overwrite its own note rather than treat it as a
    different item colliding on the same stem and write a second copy.
    """
    output = routine["output"]
    template = output.get("filename_template", DEFAULT_FILENAME_TEMPLATE)
    item_id = str(item["id"])
    short_id = item_id[:8]
    slug = title_slug(item.get("title"))
    stem = template.format(
        slug_prefix=output["slug_prefix"],
        date=item["date"],
        title=slug,
        subject=slug,
        id=short_id,
        message_id=short_id,
    )
    stem = re.sub(r"-{2,}", "-", stem).strip("-")
    directory = Path(output["vault_dir"])
    for candidate in (directory / f"{stem}.md", directory / f"{stem}-{short_id}.md"):
        candidate = _contained_note_path(directory, candidate)
        if not candidate.exists() or note_owner(candidate) == item_id:
            return candidate
    # Both taken by other items: two ids sharing a stem and an 8-char prefix.
    return _contained_note_path(directory, directory / f"{stem}-{item_id}.md")


def render(routine, item, summary, label):
    frontmatter = {
        "kind": routine["output"].get("kind", "email-scoop-summary"),
        "rule_id": routine["id"],
        "source": item.get(
            "source_kind",
            (routine.get("source") or {}).get("kind", "unknown"),
        ),
        # Source-agnostic identity. target_path() reads this back to tell "my own
        # note from an interrupted run" apart from "a different item, same stem".
        "item_id": str(item["id"]),
    }
    if routine.get("_handler_id"):
        frontmatter["handler_id"] = routine["_handler_id"]
    frontmatter.update(item.get("frontmatter", {}))
    frontmatter["focus_domains"] = routine["analyze"].get("focus_domains") or []
    if frontmatter["source"] == "gmail":
        # Kept unconditionally for Gmail routines (including as an explicit null)
        # so the existing vault frontmatter shape does not change.
        frontmatter["gmail_label_applied"] = label
    frontmatter.update({
        "generated_by": f"workspace-daemon (yoetz + {routine['analyze']['model']})",
        "generated_at": utc_now_iso(),
        "tags": routine["output"].get(
            "tags", ["kind/email-scoop-summary", "status/inbox"]
        ),
    })
    out = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True) + "---\n\n"
    out += f"# {item.get('title') or '(untitled)'}\n\n"
    out += summary.strip() + "\n"
    return out


def write(routine, item, summary, label):
    """Write the note atomically and durably.

    The ledger entry that follows is fsynced, so the note must be too — a plain
    write_text() can leave the ledger pointing at a note that a crash never
    flushed, and the item is then skipped forever. The temp file is dot-prefixed
    so a vault watcher never indexes a half-written note.
    """
    path = target_path(routine, item)
    state.write_atomic(path, render(routine, item, summary, label), mode=0o600)
    return path
