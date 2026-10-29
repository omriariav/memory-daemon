"""Optional person resolver hook: map names to slugs and attach facts.

Opt in under a routine's `memory:` block:

    memory:
      person_resolver: [/abs/path/to/executable, optional, args]
      person_resolver_timeout: 5      # seconds, optional

The executable gets `{"names": [...]}` on stdin and prints

    {"resolved": {"<input name>": "<slug>" | null},
     "facts": {"<slug>": {"<key>": "<value>"}},
     "first_names": {"<slug>": ["<given name>", "<nickname>", ...]},
     "stale": true}

Names are stripped, and empty or duplicate ones dropped, before they are sent;
the keys of `resolved` must equal the sent strings exactly. `facts` is keyed by
the slug the resolver returns and is accepted for any well-formed slug, even one
no sent name resolved to. `first_names` lists the names a person goes by
(preferred name, nicknames), used only to match a bare first name against a
meeting's attendees. `stale: true` means the resolver's data is past its
refresh date: answers are still used and one warning is logged per run. All
keys but `resolved` are optional. Any failure (timeout, non-zero
exit, bad JSON, a value of the wrong shape) is logged and treated as "no
answer": the daemon behaves as if the hook were unset. Failures are never
cached, so the next capture retries. The hook never raises into the capture
path and never mints identities: a resolved slug is still checked against the
store.

The resolver runs in its own process group with a minimal environment (PATH,
HOME, LANG, LC_ALL, TMPDIR) and the executable's directory as cwd; on timeout
the whole group is killed. Names go to it in plaintext on stdin.

Bare first names in a meeting capture ("Ada said...") are matched only
against that meeting's calendar attendees, never the whole directory: a name
maps to an attendee only when exactly one attendee goes by it.
"""
import json
import os
import re
import signal
import subprocess
import time
import unicodedata

from .shell import log

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
DEFAULT_TIMEOUT = 5.0
MEMO_TTL = 600  # seconds; the daemon is long-lived, so results age out
ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
MAX_FACT_KEYS = 12
_memo = {}  # (argv, name) -> (stamp, slug | None)
_fact_memo = {}  # (argv, slug) -> (stamp, {key: value})
_first_memo = {}  # (argv, slug) -> (stamp, [first name, ...])
MAX_FIRST_NAMES = 8
_stale_warned = [False]


def new_run():
    """Start of a routine run: the stale-data warning may fire again."""
    _stale_warned[0] = False


def reset():
    _memo.clear()
    _fact_memo.clear()
    _first_memo.clear()
    _stale_warned[0] = False


def configured(routine):
    mem = routine.get("memory")
    argv = mem.get("person_resolver") if isinstance(mem, dict) else None
    if isinstance(argv, list) and argv and all(
        isinstance(a, str) and a for a in argv
    ):
        return tuple(argv)
    return None


def _timeout(routine):
    value = (routine.get("memory") or {}).get("person_resolver_timeout")
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and value > 0:
        return float(value)
    return DEFAULT_TIMEOUT


def validate(memory_cfg, rid):
    """Config problems for the resolver keys; empty list means valid."""
    problems = []
    if "person_resolver_timeout" in memory_cfg:
        t = memory_cfg["person_resolver_timeout"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
            problems.append(
                f"{rid}: memory.person_resolver_timeout must be a positive number")
    domains = memory_cfg.get("internal_domains")
    if domains is not None and (
        not isinstance(domains, list)
        or any(not isinstance(d, str) or not d.strip() or "@" in d
               for d in domains)
    ):
        problems.append(
            f"{rid}: memory.internal_domains must be a list of "
            "domains (no @)")
    if "person_resolver" not in memory_cfg:
        return problems
    argv = memory_cfg["person_resolver"]
    if not isinstance(argv, list) or not argv or any(
        not isinstance(a, str) or not a for a in argv
    ):
        problems.append(
            f"{rid}: memory.person_resolver must be a list "
            "[absolute_path_to_executable, ...args]")
        return problems
    exe = argv[0]
    if not exe.startswith("/"):
        problems.append(f"{rid}: memory.person_resolver[0] must be an absolute path")
    elif not os.path.isfile(exe):
        problems.append(f"{rid}: memory.person_resolver[0] {exe} does not exist")
    elif not os.access(exe, os.X_OK):
        problems.append(f"{rid}: memory.person_resolver[0] {exe} is not executable")
    return problems


def _clean_facts(raw):
    """{slug: {key: value}} with only well-formed strings, else raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("facts must be an object")
    out = {}
    for slug, row in raw.items():
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise ValueError("facts key is not a slug")
        if not isinstance(row, dict):
            raise ValueError("facts value must be an object")
        clean = {}
        for key, value in row.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("fact keys and values must be strings")
            key, value = key.strip()[:40], value.strip()[:200]
            if key and value and len(clean) < MAX_FACT_KEYS:
                clean[key] = value
        out[slug] = clean
    return out


def _clean_first_names(raw):
    """{slug: [name, ...]} with only well-formed strings, else raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("first_names must be an object")
    out = {}
    for slug, names in raw.items():
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            raise ValueError("first_names key is not a slug")
        if not isinstance(names, list) or any(
                not isinstance(n, str) for n in names):
            raise ValueError("first_names value must be a list of strings")
        out[slug] = [n.strip()[:40] for n in names if n.strip()][:MAX_FIRST_NAMES]
    return out


def _env():
    return {k: os.environ[k] for k in ENV_KEYS if k in os.environ}


def _run(argv, timeout, payload):
    """stdout of one resolver run; kills the whole process group on timeout."""
    proc = subprocess.Popen(
        list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_env(),
        cwd=os.path.dirname(os.path.realpath(argv[0])), start_new_session=True)
    try:
        out, err = proc.communicate(payload, timeout=timeout)
    finally:
        # Always kill the session: on timeout or a decode error the resolver
        # may still run; after success this reaps background children it left.
        # The group id is the leader's pid and stays reserved while any member
        # lives, so this cannot hit an unrelated process; once the group is
        # empty killpg fails with ESRCH, which is ignored.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)  # reap (instant if already reaped)
        except Exception:
            pass
    if proc.returncode != 0:
        raise ValueError(f"exit {proc.returncode}: {(err or '').strip()[:200]}")
    return out


def _call(argv, timeout, names):
    """(resolved, facts, first_names) from one subprocess call, or None on
    any failure."""
    try:
        data = json.loads(_run(argv, timeout, json.dumps({"names": names})))
        if not isinstance(data, dict):
            raise ValueError("output must be an object")
        resolved = data.get("resolved", {})
        if not isinstance(resolved, dict):
            raise ValueError("resolved must be an object")
        clean = {}
        for name in names:
            slug = resolved.get(name)
            if slug is None:
                clean[name] = None
            elif isinstance(slug, str) and SLUG_RE.fullmatch(slug):
                clean[name] = slug
            else:
                raise ValueError("resolved value is not a slug")
        facts = _clean_facts(data.get("facts", {}))
        first_names = _clean_first_names(data.get("first_names", {}))
        if data.get("stale") is True and not _stale_warned[0]:
            _stale_warned[0] = True
            log(f"person-resolver WARN directory data is stale: {argv[0]}")
        return clean, facts, first_names
    except subprocess.TimeoutExpired:
        log(f"person-resolver WARN timed out after {timeout:g}s: {argv[0]}")
    except Exception as exc:  # never raise into the capture path
        log(f"person-resolver WARN {type(exc).__name__}: {exc}")
    return None


def _norm(names):
    """Stripped, non-empty, unique strings, in order."""
    return list(dict.fromkeys(
        n.strip() for n in names if isinstance(n, str) and n.strip()))


def _query(routine, names):
    """Fill the memo for `names` (already normalized); argv or None if unset.
    A failed call memoizes nothing."""
    argv = configured(routine)
    if argv is None:
        return None
    now = time.monotonic()
    todo = [n for n in names
            if not ((argv, n) in _memo and now - _memo[(argv, n)][0] < MEMO_TTL)]
    if todo:
        got = _call(argv, _timeout(routine), todo)
        if got:
            resolved, facts, first_names = got
            # Single-threaded daemon: no locking. Drop expired entries on
            # insert so the memos cannot grow without bound.
            for memo in (_memo, _fact_memo, _first_memo):
                for key in [k for k, v in memo.items()
                            if now - v[0] >= MEMO_TTL]:
                    del memo[key]
            for name in todo:
                _memo[(argv, name)] = (now, resolved[name])
            for slug, row in facts.items():
                _fact_memo[(argv, slug)] = (now, row)
            for slug, row in first_names.items():
                _first_memo[(argv, slug)] = (now, row)
    return argv


def canonicalize(routine, people, keep_single=False, keep=()):
    """Map model-supplied people to resolver slugs; unresolved pass through.
    Order and uniqueness are kept. Unset or failing hook: input unchanged.

    `keep_single` leaves bare single-word names alone (they may only resolve
    through a meeting's attendees); `keep` names are never sent or mapped.
    """
    people = list(people)

    def skip(p):
        return not isinstance(p, str) or p in keep or (
            keep_single and len(p.split()) == 1 and " " not in fold(p))

    argv = _query(routine, _norm([p for p in people if not skip(p)]))
    if argv is None:
        return people
    return list(dict.fromkeys(
        (None if skip(p) else (_memo.get((argv, p.strip())) or (0, None))[1])
        or p for p in people))


def facts_for(routine, slug, names=()):
    """{key: value} the resolver knows about `slug` ({} when none).

    `names` are display names to ask about (never the slug itself); facts
    learned for `slug` earlier, e.g. during capture, are reused without a call.
    """
    argv = configured(routine)
    if argv is None:
        return {}
    now = time.monotonic()
    cached = _fact_memo.get((argv, slug))
    if not (cached and now - cached[0] < MEMO_TTL):
        _query(routine, _norm(names))
    row = _fact_memo.get((argv, slug))
    return dict(row[1]) if row else {}


_FOLD_EXTRA = {"ł": "l", "ø": "o", "đ": "d", "ß": "ss", "æ": "ae", "œ": "oe"}


def fold(value):
    """Case- and accent-folded words of a name, joined by single spaces."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    for src, dst in _FOLD_EXTRA.items():
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _local_part(email):
    return email.split("@", 1)[0] if "@" in email else ""


def _domain(email):
    return email.rsplit("@", 1)[1] if "@" in email else ""


def first_name_map(routine, attendees, verified=(), internal=(),
                   owner_email=""):
    """{folded first name: {slug, ...}} for a meeting's attendees.

    `attendees` are {"email", "name"} rows; `verified` are directory-verified
    people ({"email", "slug"}); `internal` are the owner's email domains. An
    attendee's slug is an exact-email `verified` slug, else (only when the
    email's domain is in `internal`) the resolver's answer for the email's
    local part, then the display name. External attendees never get a
    resolver slug: a guest named like an employee is not that employee.
    Its first names are the resolver's `first_names` plus the first word of
    the display name and of the local part. An attendee with no slug still
    counts, under a `?<email>` placeholder, so a first name it shares stays
    ambiguous. Several emails of one slug count once. The `owner_email` row
    is kept as a `?owner` placeholder: it counts toward ambiguity.
    """
    owner_email = str(owner_email or "").strip().casefold()
    internal = {str(d).strip().casefold() for d in internal}
    by_email = {
        str(p.get("email") or "").strip().casefold(): p.get("slug")
        for p in verified if isinstance(p, dict) and p.get("slug")
    }
    rows = []
    for row in attendees:
        if not isinstance(row, dict):
            continue
        email = str(row.get("email") or "").strip().casefold()
        name = str(row.get("name") or "").strip()
        rows.append((email, _local_part(email), name))

    def lookup(email):
        return email != owner_email and (not by_email.get(email)) and _domain(email) in internal

    argv = _query(routine, _norm(
        [key for email, local, name in rows if lookup(email)
         for key in (local, name)]))
    found = {}
    for email, local, name in rows:
        slug = "?owner" if owner_email and email == owner_email \
            else by_email.get(email)
        if not slug and argv is not None and lookup(email):
            for key in (local, name):
                hit = _memo.get((argv, key.strip())) if key.strip() else None
                if hit and hit[1]:
                    slug = hit[1]
                    break
        slug = slug or f"?{email or name}"
        given = [name.split()[0] if name.split() else "",
                 re.split(r"[._+-]", local)[0] if local else ""]
        known = _first_memo.get((argv, slug)) if argv is not None else None
        if known:
            given += known[1]
        for key in map(fold, given):
            if key and " " not in key:
                found.setdefault(key, set()).add(slug)
    return found


def first_name_matches(routine, names, attendees, verified=(), exclude=(),
                       internal=(), owner_email=""):
    """{name: slug} for the bare first names in `names` that exactly one
    attendee goes by.

    A name shared by several attendees, or by none, is left out. So is a
    match on a slug in `exclude` (the memory owner). `internal` are the
    owner's email domains (see `first_name_map`).
    """
    bare = [
        n for n in names
        if isinstance(n, str) and fold(n) and " " not in fold(n)
    ]
    if not attendees or not bare:
        return {}
    first = first_name_map(routine, attendees, verified, internal,
                           owner_email)
    matches, ambiguous = {}, 0
    for name in bare:
        slugs = first.get(fold(name)) or set()
        if len(slugs) > 1:
            ambiguous += 1
        elif len(slugs) == 1:
            slug = next(iter(slugs))
            if slug not in exclude and not slug.startswith("?"):
                matches[name] = slug
    if ambiguous:
        log(f"person-resolver: {ambiguous} first name(s) shared by several "
            "attendees left unresolved")
    return matches
