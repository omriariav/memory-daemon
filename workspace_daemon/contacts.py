"""Google Workspace directory identity resolution over ``gws contacts``.

Drive file metadata gives us authoritative owner email addresses.  Resolve
those exact addresses through the Workspace directory before minting a memory
person slug; model-invented identities never enter this path.
"""
import re
import unicodedata
from functools import lru_cache

from .shell import gws_bin, run_json

DIRECTORY_TIMEOUT_SECONDS = 20


def _email(value):
    return str(value or "").strip().casefold()


def _slug(name):
    ascii_name = (
        unicodedata.normalize("NFKD", str(name or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9]+", "-", ascii_name))


@lru_cache(maxsize=512)
def _resolve_normalized_email(wanted):
    """Cached directory outcome: ``(person, error)``.

    Exceptions are converted to values so they are cached too. Otherwise an
    outage is retried once per item, and each subprocess may consume the full
    timeout before the daemon can continue.
    """
    try:
        result = run_json([
            gws_bin(), "contacts", "directory-search",
            "--query", wanted, "--max", "10", "--format", "json",
        ], timeout=DIRECTORY_TIMEOUT_SECONDS)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    exact = [
        contact
        for contact in result.get("contacts", [])
        if wanted in {_email(value) for value in contact.get("emails", [])}
    ]
    if len(exact) != 1:
        return None, None

    name = " ".join(str(exact[0].get("name") or "").split())
    slug = _slug(name)
    if not name or not slug:
        return None, None
    return {"email": wanted, "name": name, "slug": slug}, None


def resolve_email(email):
    """Return a verified ``{email, name, slug}`` for one directory email.

    Directory search is fuzzy, so merely getting one result is insufficient.
    Accept exactly one contact that contains the requested email address.
    """
    wanted = _email(email)
    if not wanted or "@" not in wanted:
        return None
    person, error = _resolve_normalized_email(wanted)
    if error:
        raise RuntimeError(error)
    return person


def clear_cache():
    """Forget directory outcomes at the boundary between daemon runs."""
    _resolve_normalized_email.cache_clear()
