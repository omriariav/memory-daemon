"""Exact Google Workspace identity resolution over ``gws contacts``.

Structured Gmail, Google Chat, and Drive metadata gives us authoritative email
addresses. Resolve those exact addresses through the Workspace directory before
minting a memory person slug; model-invented identities never enter this path.
"""
import re
import unicodedata
from functools import lru_cache

from .shell import gws_bin, run_json

DIRECTORY_TIMEOUT_SECONDS = 20
_directory_failure = None


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
    resource_name = str(exact[0].get("resource_name") or "").strip()
    if not name or not slug or not resource_name:
        return None, None
    return {
        "email": wanted,
        "name": name,
        "slug": slug,
        "resource_name": resource_name,
    }, None


def resolve_email(email):
    """Return verified ``{email, name, slug, resource_name}`` for one address.

    Directory search is fuzzy, so merely getting one result is insufficient.
    Accept exactly one contact that contains the requested email address and a
    stable Workspace ``resource_name``.
    """
    global _directory_failure
    wanted = _email(email)
    if not wanted or "@" not in wanted:
        return None
    if _directory_failure is not None:
        raise RuntimeError(_directory_failure)
    person, error = _resolve_normalized_email(wanted)
    if error:
        _directory_failure = error
        raise RuntimeError(error)
    return person


def clear_cache():
    """Forget directory outcomes at the boundary between daemon runs."""
    global _directory_failure
    _directory_failure = None
    _resolve_normalized_email.cache_clear()
