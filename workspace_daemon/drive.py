"""Google Drive / Docs adapter over the workspace-cli (`gws`).

Used by `source.kind: drive_docs`. Gemini meeting notes are the motivating case:
the notification email carries only the "Quick notes" tab, while the Doc holds
Full notes and a speaker-attributed Transcript that the email never links to in
a machine-readable way.
"""
import re
from functools import lru_cache

from .shell import gws_bin, log, run_json

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
ACCOUNT_TIMEOUT_SECONDS = 20

# "Meeting Title – 2026/07/22 16:59 IDT – Notes by Gemini". The separator is
# sometimes an en dash and sometimes a hyphen, so match either.
_NAME_DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")

# What must follow the meeting title in a doc name for the match to be a real
# title boundary rather than a longer, different meeting.
_BOUNDARY = re.compile(r"^\s*[-–—]\s")


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


def _escape(value):
    """Escape a literal for a Drive query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def search_by_name(fragment, mime_type=GOOGLE_DOC_MIME, max_results=25, date_fragment=None):
    """Search on the file NAME via a raw Drive query.

    Plain `drive search <text>` is a full-text search, and it silently misses
    documents whose name matches perfectly — notably notes docs owned by a
    meeting organiser and only shared with you. Querying `name contains`
    directly is both more precise and more complete.

    `date_fragment` ("2026/07/20") is ANDed into the query. That matters for
    recurring meetings: the result set is capped and unordered, so a weekly
    sync with more docs than the cap can push the one you want off the end.
    """
    clauses = [f"name contains '{_escape(fragment)}'", "trashed = false"]
    if date_fragment:
        clauses.append(f"name contains '{_escape(date_fragment)}'")
    if mime_type:
        clauses.append(f"mimeType = '{_escape(mime_type)}'")
    result = run_json([
        gws_bin(), "drive", "search", " and ".join(clauses),
        "--raw", "--max", str(max_results), "--format", "json",
    ])
    return result.get("files", [])


def _name_matches(name, needle, name_contains):
    """The doc name must START with the meeting title, at a title boundary.

    Drive returns plenty of near-misses, and summarizing the wrong meeting into
    the vault is worse than summarizing none. A bare startswith() is not enough:
    it accepts "Roadmap review extended" for "Roadmap review". The remainder
    therefore has to be empty or begin with the " – " separator.
    """
    norm = " ".join((name or "").split())
    if not norm.lower().startswith(needle):
        return False
    remainder = norm[len(needle):]
    if remainder and not _BOUNDARY.match(remainder):
        return False
    if name_contains and name_contains.lower() not in norm.lower():
        return False
    return True


def find_doc(title, name_contains=None, on_date=None, max_results=25):
    """Locate the Drive doc for a meeting title, or None.

    When a date is supplied it is REQUIRED, not merely preferred: a recurring
    meeting has one doc per occurrence, and quietly substituting a different
    week's document is the worst thing this function could do.
    """
    needle = " ".join((title or "").split()).lower()
    if not needle:
        return None
    date_fragment = on_date.replace("-", "/") if on_date else None

    def matching(files):
        return [f for f in files if _name_matches(f.get("name"), needle, name_contains)]

    # Date-scoped query first — precise, and immune to the result cap.
    candidates = []
    if date_fragment:
        candidates = matching(search_by_name(
            title, max_results=max_results, date_fragment=date_fragment))

    if not candidates:
        pool = matching(search_by_name(title, max_results=max_results))
        if not pool:
            # Full-text backstop: `name contains` can miss on indexing lag, so
            # retry the broader search. The name still has to match.
            pool = matching(search(title, max_results=max_results, name_contains=name_contains))
        if on_date:
            pool = [f for f in pool if date_from_name(f.get("name")) == on_date]
        candidates = pool

    if not candidates:
        return None
    if len(candidates) > 1:
        log(f"ambiguous doc lookup for {title!r} on {on_date}: "
            f"{len(candidates)} matches, using {candidates[0].get('name')!r}")
    return candidates[0]


def info(doc_id):
    """Document metadata, including title and ordered tab metadata."""
    return run_json([gws_bin(), "docs", "info", doc_id, "--format", "json"])


def file_info(doc_id):
    """Drive file metadata, including owner email addresses."""
    return run_json([gws_bin(), "drive", "info", doc_id, "--format", "json"])


@lru_cache(maxsize=1)
def _current_user_email_result():
    """Return the authenticated Drive email and a cached lookup error."""
    try:
        result = run_json(
            [gws_bin(), "drive", "about", "--format", "json"],
            timeout=ACCOUNT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return None, str(exc)
    email = str((result.get("user") or {}).get("email") or "").strip().casefold()
    if not email or "@" not in email:
        return None, "Drive account metadata did not include a valid user email"
    return email, None


def current_user_email():
    """Authenticated Workspace email, cached for one daemon run."""
    email, error = _current_user_email_result()
    if error:
        raise RuntimeError(error)
    return email


def clear_identity_cache():
    """Allow the next daemon run to retry the account metadata lookup."""
    _current_user_email_result.cache_clear()


def tabs(doc_id, document=None):
    """Tab titles in document order. Single-tab docs report one entry."""
    document = document or info(doc_id)
    return [
        tab.get("title")
        for tab in document.get("tabs", [])
        if tab.get("title")
    ]


def read_tab(doc_id, tab):
    """Plain text of one tab, addressed by title."""
    result = run_json(
        [gws_bin(), "docs", "read", doc_id, "--tab", tab, "--format", "json"], timeout=180
    )
    return result.get("text") or ""


def read_tabs(doc_id, wanted=None, document=None):
    """Concatenate the requested tabs into one labelled document.

    `wanted` is a list of tab titles; missing ones are skipped rather than
    failing, since not every notes doc has every tab. Matching ignores case and
    repeated whitespace because Gemini has emitted both "Full notes" and
    "Full Notes". None reads all tabs.
    """
    available = tabs(doc_id, document=document)
    by_normalized = {
        " ".join(title.split()).casefold(): title
        for title in available
    }
    selected = available if not wanted else [
        by_normalized[key]
        for requested in wanted
        if (key := " ".join(requested.split()).casefold()) in by_normalized
    ]
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
