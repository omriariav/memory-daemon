"""Google Drive / Docs adapter over the workspace-cli (`gws`).

Used by `source.kind: drive_docs`. Gemini meeting notes are the motivating case:
the notification email carries only the "Quick notes" tab, while the Doc holds
Full notes and a speaker-attributed Transcript that the email never links to in
a machine-readable way.
"""
import re

from .shell import gws_bin, run_json

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"

# "Meeting Title – 2026/07/22 16:59 IDT – Notes by Gemini". The separator is
# sometimes an en dash and sometimes a hyphen, so match either.
_NAME_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def search(query, max_results=50, mime_type=GOOGLE_DOC_MIME, name_contains=None):
    """Drive search, narrowed to one mime type and optionally a name substring.

    Drive's full-text search also matches document *bodies*, so a name filter is
    what keeps 'Notes by Gemini' from dragging in every doc that mentions it.
    """
    result = run_json(
        [gws_bin(), "drive", "search", query, "--max", str(max_results), "--format", "json"]
    )
    files = result.get("files", [])
    if mime_type:
        files = [f for f in files if f.get("mime_type") == mime_type]
    if name_contains:
        needle = name_contains.lower()
        files = [f for f in files if needle in (f.get("name") or "").lower()]
    return files


def find_doc(title, name_contains=None, on_date=None, max_results=10):
    """Locate the Drive doc for a meeting title, or None.

    Requires the doc name to *start with* the title — Drive's full-text search
    happily returns docs that merely mention it, and summarizing the wrong
    meeting is worse than summarizing none. When several recurring meetings
    share a title, the date disambiguates.
    """
    needle = " ".join((title or "").split()).lower()
    if not needle:
        return None
    matches = [
        f for f in search(title, max_results=max_results, name_contains=name_contains)
        if " ".join((f.get("name") or "").split()).lower().startswith(needle)
    ]
    if not matches:
        return None
    if on_date:
        dated = [f for f in matches if date_from_name(f.get("name")) == on_date]
        if dated:
            return dated[0]
    return matches[0]


def tabs(doc_id):
    """Tab titles in document order. Single-tab docs report one entry."""
    info = run_json([gws_bin(), "docs", "info", doc_id, "--format", "json"])
    return [t.get("title") for t in info.get("tabs", []) if t.get("title")]


def read_tab(doc_id, tab):
    """Plain text of one tab, addressed by title."""
    result = run_json(
        [gws_bin(), "docs", "read", doc_id, "--tab", tab, "--format", "json"], timeout=180
    )
    return result.get("text") or ""


def read_tabs(doc_id, wanted=None):
    """Concatenate the requested tabs into one labelled document.

    `wanted` is a list of tab titles; missing ones are skipped rather than
    failing, since not every notes doc has every tab. None reads all tabs.
    """
    available = tabs(doc_id)
    selected = available if not wanted else [t for t in available if t in wanted]
    parts = []
    for title in selected:
        text = read_tab(doc_id, title).strip()
        if text:
            parts.append(f"### {title}\n\n{text}")
    return "\n\n".join(parts), selected


def date_from_name(name):
    """YYYY-MM-DD parsed out of the doc title, or None.

    Preferred over Drive's modifiedTime: a doc edited later still belongs to the
    day the meeting happened.
    """
    match = _NAME_DATE.search(name or "")
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def meeting_title(name):
    """Strip the ' – <date> – Notes by Gemini' suffix back to the meeting name."""
    return re.split(r"\s+[–-]\s+\d{4}/\d{2}/\d{2}", name or "", maxsplit=1)[0].strip() or name
