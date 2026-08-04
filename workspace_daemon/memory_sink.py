"""Sink routine output into a personal-memory store (github.com/vladimanaev/personal-memory).

A routine opts in with a `memory:` block:

    memory:
      store: /absolute/path/to/personal-memory-instance
      type: note              # default entry type when extraction doesn't pick one
      tags: [business]        # extra tags; `auto-captured` is always added
      extract: true           # structured second pass over the summary (default true)

The sink NEVER hand-writes entry files — everything goes through the store's own
CLI (`npx tsx src/cli.ts add`), which owns index sync, source-id dedup, and the
nested-git versioning. See the store's MEMORY-GUARDRAILS.md.

Validation discipline (nothing model-invented reaches the store unchecked):
- entry type is checked against the store's known types, else falls back
- people slugs are checked against `slugs list --kind person`; unknown ones are
  dropped to a `people-unmapped` tag rather than minting a new identity
- structured Gmail, Google Chat, and Drive emails are resolved exactly through
  the Workspace directory; verified identities may safely mint a new person
  slug, while the authenticated user and aliases resolving to that same person
  remain source metadata rather than self-links
- every entry carries a canonical --source-ids, so re-runs update in place
"""
import datetime
import json
import re
import subprocess
from pathlib import Path

import yaml

from . import contacts, drive
from .shell import log, npx_bin

MEMORY_TYPES = {
    "event", "decision", "todo", "pending-decision", "1on1", "hiring",
    "incident", "achievement", "feedback", "meeting", "note", "summary",
}
AUTO_TAG = "auto-captured"
# Marks a capture that a model classified as the owner's action but that was
# downgraded to a note for lack of ownership evidence. Briefing and triage
# flows must treat entries carrying it as FYI, never as "waiting on me".
NO_OWNER_ACTION_TAG = "no-owner-action"
MAX_SOURCE_PEOPLE = 20
RELATED_CONTEXT_DAYS = 14
RELATED_CONTEXT_LIMIT = 8
NO_OWNER_ACTION_MARKER = "FYI: no action assigned to the memory owner."
EXTRACT_PROMPT = """You are filing a distilled work note into a structured personal memory store.

Read the note below and answer with a SINGLE JSON object, no markdown fence, no
other text, with exactly these keys:
  "worthy": boolean — false if the note contains nothing durable (no decision,
            commitment, concrete pending action/request, open decision, blocker,
            people signal, incident, or fact worth recalling later). A concrete
            request with a named or clearly identified owner and a specific
            deliverable is worthy as a todo while it remains unresolved; it
            need not already have been accepted or started. Preserve any stated
            deadline, but do not require one. A short acknowledgement
            (e.g. "done", "approved", a positive reaction) that resolves or
            confirms one of the related memory entries listed below is worthy
            even though the text alone is terse.
  "owner_attention": boolean — true only when the memory owner explicitly owns
            or accepted an action, must make a decision, or is expected to
            follow up. A third party's deadline, commitment, or unresolved work
            can still be worthy FYI context, but set this false and use "note".
            If the note contains the standalone line
            "{no_owner_action_marker}", set this false.
  "type": one of: event, decision, todo, pending-decision, 1on1, hiring,
          incident, achievement, feedback, meeting, note, summary
          (use "meeting" only for an actual meeting record; an email report or
          channel discussion is a "note", "decision", or "event"; use "todo"
          or "pending-decision" only when owner_attention is true)
  "title": short specific title (<= 90 chars)
  "people": array of kebab-case person slugs, ONLY from the allowed identities
            below (leave out anyone not listed, and include only people
            materially involved in the memory)
  "tags": array of 1-4 short kebab-case topic tags
  "follows": array of entry ids copied EXACTLY from the related memory entries
          below that this note directly resolves, confirms, or updates —
          usually empty; never invent an id. When the note records the
          resolution or completion of a related entry, list it here and
          prefer type "achievement" or "decision" over a standalone note.
  "body": the memory entry body in ENGLISH — concrete facts, names, numbers,
          dates, decisions, follow-ups. Compact but complete.

Known memory person slugs: {slugs}
Verified source identities: {verified_people}
Related memory entries already captured from this conversation: {related_entries}

--- NOTE ---
Title: {title}
Date: {date}
{body}
"""

OPERATOR_CONFIRMED_PROMPT = """The memory owner explicitly confirmed that this exact source is durable
and must be retained. Do not decide whether it is memory-worthy and do not
output NOT MEMORY-WORTHY. Write a compact, self-contained work-memory note in
English that preserves the source's concrete product facts, questions, current
behavior, constraints, decisions, and actions. Do not invent missing context.
Never reproduce credentials, secrets, or unrelated sensitive personal data.

--- SOURCE ---
{source_header}

{body}
"""

OPERATOR_CONFIRMED_SOURCE_ID_RE = re.compile(
    r"^(?:gmail|gchat|slack|gdrive|mila):[^\s]+$"
)


def memory_cfg(routine):
    cfg = routine.get("memory")
    return cfg if isinstance(cfg, dict) else {}


def mark_connector_pulled(routine, at, dry_run=False):
    """Record a successful connector-backed sweep in the memory store.

    The daemon owns source enumeration and its durable catch-up cursor. The
    connector state is the store-facing health signal, so update it only after
    the full run succeeds. A failure must remain visible to the runner rather
    than letting captures land while the store reports stale coverage.
    """
    analyze = routine.get("analyze") or {}
    if analyze.get("connector_sweep") is not True:
        return False
    name = analyze.get("instruction_from_connector")
    cfg = memory_cfg(routine)
    if not name or not cfg:
        return False
    rid = routine.get("id")
    if dry_run:
        log(
            f"routine={rid} [dry-run] would mark connector "
            f"{name!r} pulled at {at}"
        )
        return True
    r = _cli(
        cfg["store"],
        ["connectors", "mark-pulled", name, "--at", at],
    )
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        raise RuntimeError(
            f"connector {name!r} mark-pulled failed: {out[:300]}"
        )
    log(f"routine={rid} connector {name!r} marked pulled at {at}")
    return True


def validate(routine):
    """Config problems for the memory block; empty list means valid."""
    raw = routine.get("memory")
    if raw is not None and (not isinstance(raw, dict) or not raw):
        return [
            f"{routine.get('id', '<missing id>')}: `memory` must be a "
            "non-empty mapping"
        ]
    cfg = memory_cfg(routine)
    if not cfg:
        return []
    rid = routine.get("id", "<missing id>")
    problems = []
    unknown = set(cfg) - {
        "store", "type", "tags", "extract", "operator_confirmed_source_ids",
    }
    if unknown:
        problems.append(
            f"{rid}: memory has unknown key(s) {', '.join(sorted(unknown))}"
        )
    store = cfg.get("store")
    if not store or not str(store).startswith("/"):
        problems.append(f"{rid}: memory.store must be an absolute path to the store instance")
    etype = cfg.get("type", "note")
    if etype not in MEMORY_TYPES:
        problems.append(
            f"{rid}: memory.type {etype!r} is not a known entry type "
            f"({', '.join(sorted(MEMORY_TYPES))})"
        )
    tags = cfg.get("tags")
    if tags is not None and not isinstance(tags, list):
        problems.append(f"{rid}: memory.tags must be a list")
    confirmed = cfg.get("operator_confirmed_source_ids")
    if confirmed is not None and (
        not isinstance(confirmed, list)
        or any(
            not isinstance(value, str)
            or value != value.strip()
            or not OPERATOR_CONFIRMED_SOURCE_ID_RE.fullmatch(value)
            for value in confirmed
        )
    ):
        problems.append(
            f"{rid}: memory.operator_confirmed_source_ids must be a list "
            "of canonical source ids (gmail:, gchat:, slack:, gdrive:, or mila:)"
        )
    return problems


def _cli(store, args, stdin_text=None, timeout=120):
    return subprocess.run(
        [npx_bin(), "tsx", "src/cli.ts", *args],
        cwd=store, capture_output=True, text=True, timeout=timeout,
        input=stdin_text,
    )


def _verify_written_entry(store, source_id, etype, title, people, tags, body):
    """Return the exact persisted entry id after an ambiguous CLI result.

    personal-memory can exit non-zero after its durable write (for example if
    a later indexing or rule step fails). Trusting only the exit status causes
    an endless retry, while trusting any file carrying the source id could hide
    a failed update. Verify the complete payload in every materialization of
    the one matching id before treating the write as successful.
    """
    if not source_id:
        return None
    root = Path(store)
    entry_roots = [root / "memory" / "entries"]
    graphs = root / "memory-graphs"
    if graphs.is_dir():
        entry_roots.extend(
            path / "entries"
            for path in graphs.iterdir()
            if path.is_dir()
        )

    expected_people = set(people)
    expected_tags = set(tags)
    matches = {}
    try:
        paths = [
            path
            for entry_root in entry_roots
            if entry_root.is_dir()
            for path in entry_root.rglob("*.md")
            if path.is_file()
        ]
        for path in paths:
            raw = path.read_text(encoding="utf-8")
            if not raw.startswith("---"):
                continue
            parts = raw.split("---", 2)
            if len(parts) != 3:
                continue
            frontmatter = yaml.safe_load(parts[1]) or {}
            if not isinstance(frontmatter, dict):
                continue
            source_ids = frontmatter.get("source_ids") or []
            if not isinstance(source_ids, list) or any(
                not isinstance(value, str) for value in source_ids
            ):
                continue
            if source_id not in source_ids:
                continue
            stored_people = frontmatter.get("people") or []
            stored_tags = frontmatter.get("tags") or []
            if not isinstance(stored_people, list) or not isinstance(
                stored_tags, list
            ):
                continue
            entry_id = frontmatter.get("id")
            valid = bool(
                isinstance(entry_id, str)
                and frontmatter.get("type") == etype
                and frontmatter.get("title") == title
                and expected_people.issubset(set(stored_people))
                and expected_tags.issubset(set(stored_tags))
                and parts[2].strip() == body.strip()
            )
            matches.setdefault(entry_id, []).append(valid)
    except (OSError, UnicodeError, yaml.YAMLError, TypeError):
        return None
    if len(matches) != 1:
        return None
    entry_id, materializations = next(iter(matches.items()))
    return entry_id if entry_id and all(materializations) else None


def recent_entries_for_prefix(store, prefix, exclude_source_id=None,
                              days=RELATED_CONTEXT_DAYS,
                              limit=RELATED_CONTEXT_LIMIT):
    """Recent default-graph entries whose source ids share a source prefix.

    This is the conversation's durable history: with it in the prompt, a terse
    acknowledgement ("done", a reaction) can be recognized and linked as the
    resolution of an already-captured request instead of discarded as noise.
    Only the default graph is scanned — every capture lands there, and shared
    graphs must not leak extra context into prompts. Empty on any failure.
    """
    entries_root = Path(store) / "memory" / "entries"
    cutoff = (
        datetime.date.today() - datetime.timedelta(days=days)
    ).isoformat()
    found = {}
    try:
        paths = (
            [path for path in entries_root.rglob("*.md") if path.is_file()]
            if entries_root.is_dir() else []
        )
        for path in paths:
            raw = path.read_text(encoding="utf-8")
            if not raw.startswith("---"):
                continue
            parts = raw.split("---", 2)
            if len(parts) != 3:
                continue
            frontmatter = yaml.safe_load(parts[1]) or {}
            if not isinstance(frontmatter, dict):
                continue
            source_ids = frontmatter.get("source_ids") or []
            if not isinstance(source_ids, list):
                continue
            if exclude_source_id and exclude_source_id in source_ids:
                continue
            if not any(
                isinstance(value, str) and value.startswith(prefix)
                for value in source_ids
            ):
                continue
            entry_id = frontmatter.get("id")
            date = str(frontmatter.get("date") or "")
            if not isinstance(entry_id, str) or not entry_id or date < cutoff:
                continue
            found[entry_id] = {
                "id": entry_id,
                "type": str(frontmatter.get("type") or ""),
                "date": date,
                "title": str(frontmatter.get("title") or ""),
            }
    except (OSError, UnicodeError, yaml.YAMLError, TypeError):
        return []
    ordered = sorted(
        found.values(), key=lambda entry: (entry["date"], entry["id"]),
        reverse=True,
    )
    return ordered[:limit]


_slug_cache = {}


def known_person_slugs(store):
    """Person slugs already in the store (cached per run). Empty on any failure."""
    if store in _slug_cache:
        return _slug_cache[store]
    r = _cli(store, ["slugs", "list", "--kind", "person"])
    # Output rows look like "   3  jane-doe  (last seen 2026-07-01)" — count first;
    # the "(last seen" suffix distinguishes rows from the trailing summary line.
    slugs = set(re.findall(r"^\s*\d+\s+([a-z0-9][a-z0-9-]*)\s+\(last seen ", r.stdout, re.M)) if r.returncode == 0 else set()
    _slug_cache[store] = slugs
    return slugs


def source_id_for(item):
    """Canonical scheme:rest id from the item's provenance, or None.

    Sources that separate ledger identity from memory identity (slack, gchat:
    version-aware candidate ids) set item["source_id"] explicitly — that stable
    anchor always wins. Legacy fallbacks derive from provenance frontmatter.
    """
    if item.get("source_id"):
        return item["source_id"]
    meta = item.get("frontmatter", {})
    if str(item.get("id", "")).startswith("slack:"):
        return item["id"].split("@")[0]
    if meta.get("gmail_thread_id"):
        return f"gmail:{meta['gmail_thread_id']}"
    if meta.get("drive_file_id"):
        return f"gdrive:{meta['drive_file_id']}"
    return None


def followup_source_id_for(item):
    """Stable id for the actionable record created from a Gmail Chat forward.

    A forwarded Chat message can already have a durable Gmail memory under the
    canonical thread source id.  Reusing that id would update the durable entry
    in place, even with ``--force-new``.  The follow-up is a separate fact about
    the owner's intent, so it needs a separate id and lifecycle.
    """
    thread_id = (item.get("frontmatter") or {}).get("gmail_thread_id")
    return f"gmail:{thread_id}:followup-open" if thread_id else None


def _authenticated_identity(rid):
    """Verified authenticated person used to enforce self-exclusion."""
    try:
        email = drive.current_user_email()
    except Exception as exc:
        log(
            f"routine={rid} memory WARN authenticated Workspace identity "
            f"unavailable: {exc}"
        )
        return {"email": None, "person": None, "safe": False}
    try:
        person = contacts.resolve_email(email)
    except Exception as exc:
        log(
            f"routine={rid} memory WARN authenticated person lookup failed "
            f"for {email}: {exc}"
        )
        return {"email": email, "person": None, "safe": False}
    if not person:
        log(
            f"routine={rid} memory WARN authenticated person not found in "
            f"Workspace directory: {email}"
        )
        return {"email": email, "person": None, "safe": False}
    return {"email": email, "person": person, "safe": True}


def _is_authenticated_person(email, person, identity):
    """True for the login address or alias with the same directory identity."""
    if email == identity.get("email"):
        return True
    authenticated = identity.get("person") or {}
    return bool(
        authenticated.get("resource_name")
        and person
        and person.get("resource_name") == authenticated["resource_name"]
    )


def _collides_with_authenticated_slug(person, identity):
    """A different directory person cannot reuse the memory owner's slug."""
    authenticated = identity.get("person") or {}
    return bool(
        person
        and authenticated.get("slug")
        and person.get("slug") == authenticated["slug"]
        and person.get("resource_name") != authenticated.get("resource_name")
    )


def _known_slugs_for_identity(store, identity):
    """Known store slugs with self removed; fail closed if self is unknown."""
    if not identity.get("safe"):
        return set()
    slugs = set(known_person_slugs(store))
    slugs.discard(identity["person"]["slug"])
    return slugs


def _verified_owner_people(item, rid, identity):
    """Resolve exact Drive owner emails; return (slugs, unresolved emails)."""
    owner_emails = item.get("frontmatter", {}).get("drive_owner_emails") or []
    if not owner_emails:
        return [], []
    if not identity.get("safe"):
        log(
            f"routine={rid} memory WARN Drive owner enrichment skipped: "
            "authenticated person could not be verified"
        )
        return [], list(dict.fromkeys(owner_emails))

    slugs = []
    unresolved = []
    for email in owner_emails:
        normalized = str(email).strip().casefold()
        if normalized == identity["email"]:
            log(f"routine={rid} memory: skipped authenticated user as Drive owner")
            continue
        try:
            person = contacts.resolve_email(normalized)
        except Exception as exc:
            log(f"routine={rid} memory WARN directory lookup failed for {normalized}: {exc}")
            unresolved.append(normalized)
            continue
        if not person:
            unresolved.append(normalized)
            continue
        if _is_authenticated_person(normalized, person, identity):
            log(f"routine={rid} memory: skipped authenticated user alias as Drive owner")
            continue
        if _collides_with_authenticated_slug(person, identity):
            log(
                f"routine={rid} memory WARN Drive owner {normalized} has a "
                "person-slug collision with the authenticated user"
            )
            unresolved.append(normalized)
            continue
        slugs.append(person["slug"])
        log(
            f"routine={rid} memory: verified Drive owner "
            f"{person['name']} <{person['email']}> as {person['slug']}"
        )
    return list(dict.fromkeys(slugs)), unresolved


def _verified_source_people(item, rid, identity):
    """Resolve exact Gmail/Chat participant emails for safe model attribution."""
    candidates = item.get("frontmatter", {}).get("source_people") or []
    candidates = [
        candidate for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("email")
    ]
    if not candidates:
        return [], []
    candidates = candidates[:MAX_SOURCE_PEOPLE]
    if not identity.get("safe"):
        unresolved = list(dict.fromkeys(
            str(candidate["email"]).strip().casefold() for candidate in candidates
        ))
        log(
            f"routine={rid} memory WARN source identity enrichment skipped: "
            "authenticated person could not be verified"
        )
        return [], unresolved

    verified = []
    unresolved = []
    seen = set()
    for candidate in candidates:
        email = str(candidate["email"]).strip().casefold()
        if not email or email in seen:
            continue
        seen.add(email)
        if email == identity["email"]:
            log(f"routine={rid} memory: skipped authenticated user as source participant")
            continue
        try:
            person = contacts.resolve_email(email)
        except Exception as exc:
            log(f"routine={rid} memory WARN directory lookup failed for {email}: {exc}")
            unresolved.append(email)
            continue
        if not person:
            unresolved.append(email)
            continue
        if _is_authenticated_person(email, person, identity):
            log(
                f"routine={rid} memory: skipped authenticated user alias "
                "as source participant"
            )
            continue
        if _collides_with_authenticated_slug(person, identity):
            log(
                f"routine={rid} memory WARN source participant {email} has a "
                "person-slug collision with the authenticated user"
            )
            unresolved.append(email)
            continue
        verified.append(person)
        log(
            f"routine={rid} memory: verified source participant "
            f"{person['name']} <{person['email']}> as {person['slug']}"
        )
    return verified, unresolved


def _extract(routine, item, summary, store, verified_people=(), identity=None):
    """Structured second pass over the already-distilled summary."""
    from . import llm  # late import to avoid cycles

    identity = identity or {"safe": False}
    slugs = sorted(
        _known_slugs_for_identity(store, identity)
        | {person["slug"] for person in verified_people}
    )
    verified_catalog = ", ".join(
        f"{person['name']} -> {person['slug']}"
        for person in verified_people
    )
    related = [
        entry for entry in
        (item.get("frontmatter") or {}).get("related_memory_entries") or []
        if isinstance(entry, dict) and entry.get("id")
    ]
    related_catalog = "; ".join(
        f"{entry['id']} ({entry.get('type', '')}, {entry.get('date', '')}: "
        f"{entry.get('title', '')})"
        for entry in related
    )
    prompt = EXTRACT_PROMPT.format(
        slugs=", ".join(slugs) or "(none known yet)",
        verified_people=verified_catalog or "(none)",
        no_owner_action_marker=NO_OWNER_ACTION_MARKER,
        related_entries=related_catalog or "(none)",
        title=item.get("title", ""), date=item.get("date", ""), body=summary,
    )
    raw = llm.analyze(routine, prompt).strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)  # let a malformed answer raise; caller falls back
    if not isinstance(data, dict):
        raise ValueError("extraction did not return a JSON object")
    expected = {
        "worthy", "owner_attention", "type", "title", "people", "tags",
        "follows", "body",
    }
    if set(data) != expected:
        missing = sorted(expected - set(data))
        unknown = sorted(set(data) - expected)
        details = []
        if missing:
            details.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown keys: {', '.join(unknown)}")
        raise ValueError("invalid extraction schema (" + "; ".join(details) + ")")
    if type(data["worthy"]) is not bool or type(data["owner_attention"]) is not bool:
        raise ValueError("worthy and owner_attention must be booleans")
    if data["type"] not in MEMORY_TYPES:
        raise ValueError(f"unknown memory type {data['type']!r}")
    if not isinstance(data["title"], str) or not data["title"].strip():
        raise ValueError("title must be a non-empty string")
    if len(data["title"]) > 120:
        raise ValueError("title must be at most 120 characters")
    if not isinstance(data["body"], str) or not data["body"].strip():
        raise ValueError("body must be a non-empty string")
    for key, limit in (("people", None), ("tags", 4)):
        values = data[key]
        if not isinstance(values, list) or any(
            not isinstance(value, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value)
            for value in values
        ):
            raise ValueError(f"{key} must be a list of kebab-case strings")
        if limit is not None and not 1 <= len(values) <= limit:
            raise ValueError(f"{key} must contain 1-{limit} values")
    follows = data["follows"]
    if not isinstance(follows, list) or any(
        not isinstance(value, str) or not value.strip() for value in follows
    ):
        raise ValueError("follows must be a list of entry-id strings")
    return data


def _has_no_owner_action_marker(summary):
    """True only for the canonical standalone FYI marker, never quoted prose."""
    marker = NO_OWNER_ACTION_MARKER.casefold()
    return any(line.strip().casefold() == marker for line in summary.splitlines())


def _operator_confirmed(cfg, source_id):
    return bool(
        source_id
        and source_id in (cfg.get("operator_confirmed_source_ids") or [])
    )


def is_operator_confirmed(routine, item):
    """Whether the owner explicitly approved this exact source for memory."""
    return is_operator_confirmed_source_id(routine, source_id_for(item))


def is_operator_confirmed_source_id(routine, source_id):
    """Whether an exact canonical source id has an explicit owner override."""
    return _operator_confirmed(memory_cfg(routine), source_id)


def _operator_confirmed_summary(routine, item):
    """Re-summarize one exact source after an explicit owner override."""
    from . import llm  # late import to avoid cycles

    source_header = llm.source_header_lines(item) + [
        f"Title: {item.get('title', '')}",
        f"Date: {item.get('date', '')}",
    ]
    prompt = OPERATOR_CONFIRMED_PROMPT.format(
        source_header="\n".join(source_header),
        body=item.get("body", ""),
    )
    summary = llm.analyze(routine, prompt).strip()
    if not summary or summary == "NOT MEMORY-WORTHY":
        raise RuntimeError(
            "operator-confirmed source did not produce a usable summary"
        )
    return summary


def capture(routine, item, summary, dry_run=False):
    """Distill `summary` into one memory entry and store it via the CLI.

    Returns a small outcome dict for the ledger, or None when skipped.
    Model extraction trouble degrades to a validated plain note. Persistence
    uncertainty raises so callers can retry and must not perform source-side
    actions (such as Gmail archive) after an unverified memory write.
    """
    cfg = memory_cfg(routine)
    if not cfg:
        return None
    store = cfg["store"]
    rid = routine.get("id")
    meta = item.get("frontmatter") or {}
    active_chat_followup = (
        meta.get("gmail_chat_followup_managed") is True
        and meta.get("gmail_manual_chat_followup") is True
        and meta.get("gmail_chat_followup_active") is True
    )
    canonical_source_id = source_id_for(item)
    source_id = (
        followup_source_id_for(item)
        if active_chat_followup
        else canonical_source_id
    )
    if active_chat_followup and not source_id:
        raise RuntimeError(
            "active Gmail Chat follow-up is missing gmail_thread_id"
        )
    operator_confirmed = is_operator_confirmed_source_id(
        routine, canonical_source_id
    )

    if dry_run:
        # Honor the daemon's dry-run contract: no LLM call, no store write. The
        # extraction verdict can't be previewed without the real summary anyway.
        if active_chat_followup:
            log(
                f"routine={rid} [dry-run] would extract + force-new "
                f"memory-add (type=todo; active Chat follow-up) "
                f"source_id={source_id}"
            )
        else:
            log(f"routine={rid} [dry-run] would extract + memory-add "
                f"(fallback type={cfg.get('type', 'note')}) source_id={source_id}")
        return {"memory": "dry_run"}

    if summary.strip() == "NOT MEMORY-WORTHY" and active_chat_followup:
        summary = (
            "The memory owner manually forwarded this Chat message to their "
            "Gmail Inbox for follow-up. Review the source-linked conversation."
        )
    if (
        summary.strip() == "NOT MEMORY-WORTHY"
        and operator_confirmed
        and not active_chat_followup
    ):
        log(
            f"routine={rid} memory source_id={source_id}: "
            "operator-confirmed, forcing durable summarization"
        )
        summary = _operator_confirmed_summary(routine, item)
    if summary.strip() == "NOT MEMORY-WORTHY" and not active_chat_followup:
        log(
            f"routine={rid} memory source_id={source_id}: "
            "source analysis judged not memory-worthy, skipping extraction"
        )
        return {"memory": "skipped_not_worthy"}

    entry = {"worthy": True, "type": cfg.get("type", "note"),
             "title": item.get("title", ""), "people": [], "tags": [], "body": summary}
    degraded = None
    extraction_succeeded = False
    extract = cfg.get("extract", True)
    owner_emails = item.get("frontmatter", {}).get("drive_owner_emails") or []
    identity = (
        _authenticated_identity(rid)
        if extract or owner_emails
        else {"email": None, "person": None, "safe": False}
    )
    if extract:
        verified_source, unresolved_source = _verified_source_people(
            item, rid, identity
        )
    else:
        verified_source, unresolved_source = [], []
    verified_owners, unresolved_owners = _verified_owner_people(
        item, rid, identity
    )
    if extract:
        try:
            entry.update(_extract(
                routine, item, summary, store,
                verified_people=verified_source,
                identity=identity,
            ))
            extraction_succeeded = True
        except Exception as exc:
            degraded = f"extraction failed, stored as plain note: {exc}"
            log(f"routine={rid} memory WARN {degraded}")

    if (
        not entry.get("worthy", True)
        and operator_confirmed
        and not active_chat_followup
    ):
        log(
            f"routine={rid} memory source_id={source_id}: "
            "operator confirmation overrode extraction veto"
        )
        entry["worthy"] = True
    if not entry.get("worthy", True) and not active_chat_followup:
        log(
            f"routine={rid} memory source_id={source_id}: "
            "judged not memory-worthy, skipping"
        )
        return {"memory": "skipped_not_worthy"}

    # --- validate everything the model returned -----------------------------
    etype = entry.get("type") if entry.get("type") in MEMORY_TYPES else cfg.get("type", "note")
    has_no_owner_marker = _has_no_owner_action_marker(summary)
    owner_attention_denied = has_no_owner_marker or (
        extraction_succeeded and entry.get("owner_attention") is not True
    )
    downgraded_no_owner = False
    if etype in {"todo", "pending-decision"} and owner_attention_denied:
        reason = (
            "the source contains the standalone no-owner-action marker"
            if has_no_owner_marker
            else "structured extraction did not affirm owner attention"
        )
        log(
            f"routine={rid} memory: downgraded {etype} to note because {reason}"
        )
        etype = "note"
        downgraded_no_owner = True
    # Slack items carry deterministic ownership evidence computed from the
    # source itself (self mention, own message, direct conversation, or an
    # owner-action channel rule). A model's owner_attention judgment alone
    # cannot mint the owner a task from other people's discussion — topical
    # relevance is not ownership.
    if etype in {"todo", "pending-decision"} and "slack_owner_evidence" in meta:
        evidence = [
            value for value in (meta.get("slack_owner_evidence") or [])
            if isinstance(value, str) and value
        ]
        if not evidence:
            log(
                f"routine={rid} memory: downgraded {etype} to note because "
                "the Slack source carries no ownership evidence (no mention "
                "of the owner, no owner message, not a direct conversation, "
                "no owner-action channel rule)"
            )
            etype = "note"
            downgraded_no_owner = True
    if active_chat_followup and etype != "todo":
        log(
            f"routine={rid} memory: classified active self-forwarded Chat "
            f"follow-up as todo instead of {etype}"
        )
        etype = "todo"
    known = _known_slugs_for_identity(store, identity)
    source_slugs = {person["slug"] for person in verified_source}
    allowed = known | source_slugs | set(verified_owners)
    model_people = entry.get("people") or []
    people = list(dict.fromkeys(
        verified_owners + [p for p in model_people if p in allowed]
    ))
    dropped = [p for p in model_people if p not in allowed]
    source_people_truncated = bool(
        item.get("frontmatter", {}).get("source_people_truncated")
        or len(item.get("frontmatter", {}).get("source_people") or [])
        > MAX_SOURCE_PEOPLE
    )
    tags = [t for t in entry.get("tags") or [] if re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(t))]
    tags += list(cfg.get("tags") or [])
    if active_chat_followup:
        tags.append("gmail-followup")
    if operator_confirmed:
        tags.append("operator-confirmed")
    if downgraded_no_owner:
        tags.append(NO_OWNER_ACTION_TAG)
    tags.append(AUTO_TAG)
    if (
        dropped or unresolved_source or unresolved_owners
        or source_people_truncated or (extract and not identity.get("safe"))
    ):
        tags.append("people-unmapped")
        if dropped:
            log(f"routine={rid} memory: dropped unknown slugs {dropped}")
        if unresolved_source:
            log(
                f"routine={rid} memory: unresolved source identities "
                f"{unresolved_source}"
            )
        if unresolved_owners:
            log(
                f"routine={rid} memory: unresolved Drive owners "
                f"{unresolved_owners}"
            )
        if source_people_truncated:
            log(
                f"routine={rid} memory: source identity candidates truncated "
                f"to {MAX_SOURCE_PEOPLE}"
            )
    if degraded:
        tags.append("extract-degraded")
    title = (entry.get("title") or item.get("title") or "untitled")[:120]
    body = entry.get("body") or summary
    if active_chat_followup:
        followup_line = (
            "The memory owner manually forwarded this Chat conversation to "
            "Gmail Inbox for follow-up."
        )
        if followup_line not in body:
            body = f"{followup_line}\n\n{body}"

    args = ["add", "--type", etype, "--title", title, "--date", item.get("date", "")]
    if people:
        args += ["--people", ",".join(people)]
    args += ["--tags", ",".join(dict.fromkeys(tags))]
    if source_id:
        args += ["--source-ids", source_id]
    else:
        # No canonical id -> the store's near-dup guard may interject; force-new
        # would risk duplicates, so let the guard win and report it.
        log(f"routine={rid} memory WARN: no source id derived; relying on near-dup guard")
    if not active_chat_followup:
        # Only ids that were offered as related context are linkable — a
        # model-invented id must never reach the store. The store merges
        # repeated links, so re-runs stay idempotent.
        related_ids = {
            related_entry.get("id")
            for related_entry in meta.get("related_memory_entries") or []
            if isinstance(related_entry, dict)
        }
        model_follows = list(dict.fromkeys(entry.get("follows") or []))
        follows = [value for value in model_follows if value in related_ids]
        dropped_follows = [
            value for value in model_follows if value not in related_ids
        ]
        if dropped_follows:
            log(
                f"routine={rid} memory: dropped unoffered follows targets "
                f"{dropped_follows}"
            )
        if follows:
            args += ["--follows", ",".join(follows)]
            log(
                f"routine={rid} memory: linking resolution to "
                f"{', '.join(follows)}"
            )
    if active_chat_followup:
        # The underlying Chat fact may already exist in memory, but the user's
        # explicit follow-up intent is a separate actionable record. Its
        # dedicated source id makes retries idempotent without overwriting the
        # durable source entry.
        predecessor = meta.get("gmail_followup_predecessor_entry_id")
        if isinstance(predecessor, str) and predecessor.strip():
            args += ["--follows", predecessor.strip()]
        args.append("--force-new")

    if dry_run:
        log(f"routine={rid} [dry-run] would memory-add type={etype} title={title!r} "
            f"people={people} source_id={source_id}")
        return {"memory": "dry_run"}

    r = _cli(store, args, stdin_text=body, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    # Some store failures happen after the entry file was written. Preserve
    # any such write before deciding whether the result is an error.
    _commit_store(store, f"memory: {rid} auto-capture")
    m = re.search(r"[✓↻]\s+(created|updated|unchanged)\s+(\S+)", out)
    if r.returncode == 0 and m:
        verdict, entry_id = m.group(1), m.group(2)
    else:
        entry_id = _verify_written_entry(
            store, source_id, etype, title, people,
            list(dict.fromkeys(tags)), body,
        )
        if entry_id:
            verdict = "verified"
            log(
                f"routine={rid} memory WARN ambiguous CLI result was "
                f"verified on disk for source_id={source_id}"
            )
        elif r.returncode != 0:
            raise RuntimeError(f"memory add failed: {out.strip()[:300]}")
        else:
            raise RuntimeError(
                "memory add returned no entry id or recognized verdict: "
                f"{out.strip()[:300]}"
            )
    log(
        f"routine={rid} memory {verdict} {entry_id or ''} "
        f"source_id={source_id}"
    )

    return {
        "memory": verdict,
        "memory_entry_id": entry_id,
        "memory_people": people,
        "memory_source_id": source_id,
    }


def resolve_followup(routine, memory_entry_id, thread_id, title,
                     completed_on=None):
    """Resolve an archived Gmail follow-up with a timeline successor note."""
    cfg = memory_cfg(routine)
    if not cfg:
        raise RuntimeError("Gmail follow-up reconciliation requires a memory sink")
    rid = routine.get("id")
    completed_on = completed_on or datetime.date.today().isoformat()
    clean_title = re.sub(r"^Fwd:\s*", "", title or "Chat follow-up", flags=re.I)
    resolution_title = f"Completed follow-up: {clean_title}"[:120]
    source_id = f"gmail:{thread_id}:followup-completed"
    body = (
        "The manually forwarded Chat message is no longer in the Gmail Inbox, "
        "which is the configured completion signal for this follow-up. Read or "
        "star state alone did not resolve it."
    )
    args = [
        "add",
        "--type", "note",
        "--title", resolution_title,
        "--date", completed_on,
        "--tags", f"gmail,gmail-followup,follow-up-completed,{AUTO_TAG}",
        "--source-ids", source_id,
        "--follows", memory_entry_id,
        "--force-new",
    ]
    result = _cli(cfg["store"], args, stdin_text=body, timeout=300)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(
            f"memory follow-up resolution failed: {output.strip()[:300]}"
        )
    match = re.search(r"[✓↻]\s+(created|updated|unchanged)\s+(\S+)", output)
    if not match:
        raise RuntimeError(
            "memory follow-up resolution returned no entry id: "
            f"{output.strip()[:300]}"
        )
    verdict, entry_id = match.group(1), match.group(2)
    log(
        f"routine={rid} memory follow-up {verdict} {entry_id or ''} "
        f"source_id={source_id} follows={memory_entry_id}"
    )
    _commit_store(cfg["store"], f"memory: {rid} follow-up completed")
    return {"memory": verdict, "memory_entry_id": entry_id}


def _commit_store(store, message):
    """Commit unattended store writes and fail if versioning is unhealthy."""
    # The store's auto-commit hook only fires inside agent sessions; commit here
    # so daemon writes are versioned too.
    repo = f"{store}/memory"
    added = subprocess.run(
        ["git", "-C", repo, "add", "-A", "."],
        capture_output=True, text=True, timeout=30,
    )
    if added.returncode != 0:
        raise RuntimeError(
            f"memory git add failed: {(added.stderr or added.stdout).strip()[:300]}"
        )
    committed = subprocess.run(
        ["git", "-C", repo, "commit", "-q", "-m", message],
        capture_output=True, text=True, timeout=30,
    )
    if committed.returncode == 0:
        return
    status = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain"],
        capture_output=True, text=True, timeout=30,
    )
    if status.returncode == 0 and not status.stdout.strip():
        return
    detail = (committed.stderr or committed.stdout).strip()[:300]
    raise RuntimeError(f"memory git commit failed: {detail or 'working tree still dirty'}")
