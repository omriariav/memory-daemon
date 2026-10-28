"""Optional person resolver hook: map names to slugs and attach facts.

Opt in under a routine's `memory:` block:

    memory:
      person_resolver: [/abs/path/to/executable, optional, args]
      person_resolver_timeout: 5      # seconds, optional

The executable gets `{"names": [...]}` on stdin and prints

    {"resolved": {"<input name>": "<slug>" | null},
     "facts": {"<slug>": {"<key>": "<value>"}}}

Both keys are optional. Any failure (timeout, non-zero exit, bad JSON, a value
of the wrong shape) is logged and treated as "no answer": the daemon behaves as
if the hook were unset. The hook never raises into the capture path and never
mints identities: a resolved slug is still checked against the store.
"""
import json
import os
import re
import subprocess
import time

from .shell import log

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
DEFAULT_TIMEOUT = 5.0
MEMO_TTL = 600  # seconds; the daemon is long-lived, so results age out
MAX_OUTPUT = 1_000_000
MAX_FACT_KEYS = 12
_memo = {}  # (argv, name) -> (stamp, slug | None)
_fact_memo = {}  # (argv, slug) -> (stamp, {key: value})


def reset():
    _memo.clear()
    _fact_memo.clear()


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


def _call(argv, timeout, names):
    """(resolved, facts) from one subprocess call, or None on any failure."""
    try:
        proc = subprocess.run(
            list(argv), input=json.dumps({"names": names}),
            capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise ValueError(f"exit {proc.returncode}")
        if len(proc.stdout) > MAX_OUTPUT:
            raise ValueError("output too large")
        data = json.loads(proc.stdout)
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
        return clean, _clean_facts(data.get("facts", {}))
    except subprocess.TimeoutExpired:
        log(f"person-resolver WARN timed out after {timeout:g}s: {argv[0]}")
    except Exception as exc:  # never raise into the capture path
        log(f"person-resolver WARN {type(exc).__name__}: {exc}")
    return None


def _query(routine, names):
    """Fill the memo for `names`; returns the argv or None when unset."""
    argv = configured(routine)
    if argv is None:
        return None
    now = time.monotonic()
    fresh = lambda stamp: now - stamp < MEMO_TTL  # noqa: E731
    todo = [n for n in dict.fromkeys(names)
            if isinstance(n, str) and n.strip()
            and not ((argv, n) in _memo and fresh(_memo[(argv, n)][0]))]
    if todo:
        got = _call(argv, _timeout(routine), todo)
        resolved, facts = got if got else ({n: None for n in todo}, {})
        for name in todo:
            _memo[(argv, name)] = (now, resolved[name])
        for slug, row in facts.items():
            _fact_memo[(argv, slug)] = (now, row)
    return argv


def canonicalize(routine, people):
    """Map model-supplied people to resolver slugs; unresolved pass through.
    Order and uniqueness are kept. Unset or failing hook: input unchanged."""
    people = list(people)
    argv = _query(routine, people)
    if argv is None:
        return people
    return list(dict.fromkeys(
        (_memo.get((argv, p)) or (0, None))[1] or p for p in people))


def facts_for(routine, slug, names=()):
    """{key: value} the resolver knows about `slug` ({} when none).

    `names` are extra strings (e.g. the display name) to ask about, so the
    resolver can attach facts for a slug it was not asked about earlier.
    """
    argv = _query(routine, [slug, *names])
    if argv is None:
        return {}
    return dict((_fact_memo.get((argv, slug)) or (0, {}))[1])
