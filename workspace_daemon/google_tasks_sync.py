"""Deterministic bidirectional Google Tasks ↔ personal-memory todo sync.

The sync is deliberately model-free. Google task ids are stored as canonical
``google-tasks:<list-id>:<task-id>`` source ids on memory entries, while a
small local checkpoint records the last hashes seen on both sides. If both
sides changed since that checkpoint, neither side wins silently: the item is
reported as a conflict and left untouched.
"""
import datetime
import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml

from . import state
from .memory_sink import _commit_store
from .shell import gws_bin
from .time_utils import is_rfc3339_instant


SYNC_VERSION = 1
SOURCE_SCHEME = "google-tasks"
MEMORY_MARKER = "[Google Tasks]"
INITIAL_UPDATED_PREFIX = "Initial Google updated: "
ORIGIN_RE = re.compile(r"\n*Synced from personal memory: [a-z0-9-]+\s*$")
ORIGIN_ID_RE = re.compile(
    r"(?:^|\n)Synced from personal memory: ([a-z0-9-]+)\s*$"
)
SOURCE_RE = re.compile(r"^google-tasks:([^:]+):([^:]+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ENTRY_ID_RE = re.compile(r"(?:created|updated)\s+([a-z0-9][a-z0-9-]*)", re.I)
PERSON_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def _today():
    return datetime.date.today().isoformat()


def _json_hash(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def _source_id(tasklist_id, task_id):
    return f"{SOURCE_SCHEME}:{tasklist_id}:{task_id}"


def _source_parts(source_id):
    match = SOURCE_RE.fullmatch(str(source_id or ""))
    return match.groups() if match else None


def _slug(value):
    rendered = re.sub(r"[^a-z0-9]+", "-", str(value).casefold()).strip("-")
    return rendered or "unnamed"


def _memory_title_slug(value):
    """Mirror personal-memory's ASCII title slug without its empty fallback."""
    return re.sub(
        r"[^a-z0-9]+", "-", str(value).lower()
    ).strip("-")[:60]


def _explicit_memory_id(task, source_id):
    """Return a safe explicit id only when the store's title slug is empty."""
    title = str(task.get("title") or "Untitled task")
    if _memory_title_slug(title):
        return None
    suffix = hashlib.sha256(source_id.encode()).hexdigest()[:12]
    return f"{_memory_entry_date(task)}-google-task-{suffix}"


def _task_from_payload(payload):
    if isinstance(payload, dict) and isinstance(payload.get("task"), dict):
        return payload["task"]
    if isinstance(payload, dict):
        return payload
    raise RuntimeError("Google Tasks returned a non-object task payload")


def _gws(args, timeout=120):
    result = subprocess.run(
        [gws_bin(), "tasks", *args, "--format", "json"],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"gws tasks {args[0] if args else '?'} failed: {detail[:300]}"
        )
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gws tasks returned non-JSON: {result.stdout.strip()[:200]!r}"
        ) from exc


def _memory_cli(store, args, timeout=180):
    return subprocess.run(
        ["npx", "tsx", "src/cli.ts", *args],
        cwd=str(store),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _frontmatter(path):
    try:
        raw = path.read_text()
    except OSError as exc:
        raise RuntimeError(f"cannot read memory entry {path}: {exc}") from exc
    if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
        raise RuntimeError(f"memory entry {path} has no YAML frontmatter")
    header, body = raw[4:].split("\n---\n", 1)
    try:
        data = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"memory entry {path} has invalid YAML: {exc}") from exc
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError(f"memory entry {path} has invalid frontmatter")
    return data, body.strip()


def _load_memory_entries(store):
    root = Path(store) / "memory" / "entries"
    entries = {}
    if not root.exists():
        raise RuntimeError(f"personal-memory entries directory not found: {root}")
    for path in sorted(root.rglob("*.md")):
        data, body = _frontmatter(path)
        entry_id = str(data["id"])
        if entry_id in entries:
            raise RuntimeError(f"duplicate private-memory entry id {entry_id!r}")
        entries[entry_id] = {
            "id": entry_id,
            "date": str(data.get("date") or ""),
            "type": str(data.get("type") or ""),
            "title": str(data.get("title") or ""),
            "body": body,
            "tags": [str(value) for value in (data.get("tags") or [])],
            "people": [str(value) for value in (data.get("people") or [])],
            "source_ids": [
                str(value) for value in (data.get("source_ids") or [])
            ],
            "follows": [str(value) for value in (data.get("follows") or [])],
        }
    resolved = {
        earlier
        for entry in entries.values()
        for earlier in entry["follows"]
    }
    for entry in entries.values():
        entry["resolved"] = entry["id"] in resolved
    return entries


def _shared_entry_ids(store):
    """Return ids materialized in any non-private personal-memory graph."""
    parent = Path(store) / "memory-graphs"
    if not parent.exists():
        return set()
    shared = set()
    for graph in sorted(path for path in parent.iterdir() if path.is_dir()):
        entries = graph / "entries"
        if not entries.exists():
            continue
        for path in sorted(entries.rglob("*.md")):
            data, _body = _frontmatter(path)
            shared.add(str(data["id"]))
    return shared


def _clean_google_notes(notes):
    return ORIGIN_RE.sub("", str(notes or "")).strip()


def _known_people(entries):
    """Return reusable person slugs already present in the private graph."""
    return sorted({
        person
        for entry in entries.values()
        for person in entry.get("people", [])
        if PERSON_SLUG_RE.fullmatch(person)
    })


def _memory_by_google_source(entries):
    """Build a one-to-one canonical Google source map or fail closed."""
    by_source = {}
    for entry in entries.values():
        google_sources = list(dict.fromkeys(
            source_id
            for source_id in entry["source_ids"]
            if _source_parts(source_id)
        ))
        if len(google_sources) > 1:
            raise RuntimeError(
                f"memory entry {entry['id']!r} has multiple Google Tasks "
                f"identities: {', '.join(google_sources)}"
            )
        for source_id in google_sources:
            previous = by_source.get(source_id)
            if previous and previous["id"] != entry["id"]:
                raise RuntimeError(
                    f"Google Tasks source id {source_id!r} is linked to "
                    f"multiple memory entries: {previous['id']}, {entry['id']}"
                )
            by_source[source_id] = entry
    return by_source


def _task_people(task, people, excluded=()):
    """Conservatively match task text to existing person slugs.

    Full names may match case-insensitively. A first name is accepted only when
    it identifies exactly one catalog person, is at least four characters, and
    appears capitalized (or all-caps) in the source text. No new slug is minted.
    """
    text = "\n".join((
        str(task.get("title") or ""),
        _clean_google_notes(task.get("notes")),
    ))
    excluded = set(excluded or [])
    # Exclusions may suppress a result, but must never make an ambiguous first
    # name appear unique by shrinking the catalog used for confidence.
    catalog = list(people)
    matched = set()
    by_first = {}
    full_hits = []
    for slug in catalog:
        parts = slug.split("-")
        by_first.setdefault(parts[0], []).append(slug)
        full_name = r"[\s-]+".join(re.escape(part) for part in parts)
        for hit in re.finditer(
            rf"(?<![A-Za-z0-9]){full_name}(?![A-Za-z0-9])",
            text,
            re.IGNORECASE,
        ):
            full_hits.append((hit.start(), hit.end(), slug))
    overlap_groups = []
    for hit in sorted(full_hits):
        if not overlap_groups or hit[0] >= max(row[1] for row in overlap_groups[-1]):
            overlap_groups.append([hit])
        else:
            overlap_groups[-1].append(hit)
    for group in overlap_groups:
        containers = [
            candidate
            for candidate in group
            if all(
                candidate[0] <= other[0] and other[1] <= candidate[1]
                for other in group
            )
        ]
        if len(containers) != 1:
            continue
        _start, _end, slug = containers[0]
        if slug not in excluded:
            matched.add(slug)
    for first, slugs in by_first.items():
        if len(first) < 4 or len(slugs) != 1:
            continue
        displayed = re.escape(first.capitalize())
        uppercase = re.escape(first.upper())
        first_hits = re.finditer(
            rf"(?<![A-Za-z0-9])(?:{displayed}|{uppercase})(?![A-Za-z0-9])",
            text,
        )
        for hit in first_hits:
            inside_full_name = any(
                other_start <= hit.start()
                and hit.end() <= other_end
                for other_start, other_end, _other_slug in full_hits
            )
            if not inside_full_name and slugs[0] not in excluded:
                matched.add(slugs[0])
                break
    return sorted(matched)


def _origin_memory_id(notes):
    match = ORIGIN_ID_RE.search(str(notes or ""))
    return match.group(1) if match else None


def _due_date(task):
    raw = str(task.get("due") or "")
    return raw[:10] if DATE_RE.fullmatch(raw[:10]) else None


def _task_updated(task):
    updated = str(task.get("updated") or "")
    if not is_rfc3339_instant(updated):
        raise RuntimeError(
            f"Google task {task.get('id')!r} has no valid updated timestamp"
        )
    return updated


def _generated_metadata(metadata):
    """Parse only the exact metadata trailer generated by this module."""
    lines = metadata.splitlines()
    if not lines or not lines[0].startswith("List: "):
        return False, None, None
    due = None
    initial_updated = None
    for line in lines[1:]:
        if line.startswith("Due: ") and due is None:
            candidate = line[5:].strip()
            if not DATE_RE.fullmatch(candidate):
                return False, None, None
            due = candidate
        elif line.startswith(INITIAL_UPDATED_PREFIX) and initial_updated is None:
            candidate = line[len(INITIAL_UPDATED_PREFIX):].strip()
            if not is_rfc3339_instant(candidate):
                return False, None, None
            initial_updated = candidate
        else:
            return False, None, None
    return True, due, initial_updated


def _body_metadata(body):
    if f"\n\n{MEMORY_MARKER}\n" in body:
        _notes, metadata = body.rsplit(f"\n\n{MEMORY_MARKER}\n", 1)
    elif body.startswith(f"{MEMORY_MARKER}\n"):
        metadata = body[len(MEMORY_MARKER) + 1:]
    else:
        return False, None, None
    return _generated_metadata(metadata)


def _memory_body(task, tasklist_title, existing_body=None):
    notes = _clean_google_notes(task.get("notes"))
    metadata = [MEMORY_MARKER, f"List: {tasklist_title}"]
    due = _due_date(task)
    if due:
        metadata.append(f"Due: {due}")
    initial_updated = None
    if existing_body:
        generated, _existing_due, initial_updated = _body_metadata(existing_body)
        if not generated:
            initial_updated = None
    if not initial_updated:
        initial_updated = _task_updated(task)
    if initial_updated:
        metadata.append(f"{INITIAL_UPDATED_PREFIX}{initial_updated}")
    return "\n\n".join(part for part in (notes, "\n".join(metadata)) if part)


def _memory_entry_date(task, existing_entry=None):
    existing_date = str((existing_entry or {}).get("date") or "")
    if DATE_RE.fullmatch(existing_date):
        return existing_date
    return _task_updated(task)[:10]


def _trusted_existing_body(entry, source_id):
    if entry and source_id in entry.get("source_ids", []):
        return entry["body"]
    return None


def _memory_to_google(entry):
    body = entry["body"]
    due = None
    managed = any(
        _source_parts(source_id)
        for source_id in entry.get("source_ids", [])
    )
    notes = body
    if managed and f"\n\n{MEMORY_MARKER}\n" in body:
        candidate_notes, metadata = body.rsplit(f"\n\n{MEMORY_MARKER}\n", 1)
        generated, candidate_due, _initial_updated = _generated_metadata(metadata)
        if generated:
            notes, due = candidate_notes, candidate_due
    elif managed and body.startswith(f"{MEMORY_MARKER}\n"):
        metadata = body[len(MEMORY_MARKER) + 1:]
        generated, candidate_due, _initial_updated = _generated_metadata(metadata)
        if generated:
            notes, due = "", candidate_due
    if notes == body:
        notes = body
        for line in body.splitlines():
            if line.startswith("Due: ") and DATE_RE.fullmatch(line[5:].strip()):
                due = line[5:].strip()
                break
    notes = notes.strip()
    origin = f"Synced from personal memory: {entry['id']}"
    notes = f"{notes}\n\n{origin}" if notes else origin
    return {"title": entry["title"], "notes": notes, "due": due}


def _google_hash(task):
    return _json_hash({
        "title": str(task.get("title") or ""),
        "notes": _clean_google_notes(task.get("notes")),
        "due": _due_date(task),
        "status": str(task.get("status") or "needsAction"),
    })


def _memory_hash(entry):
    return _json_hash({
        "type": entry["type"],
        "title": entry["title"],
        "body": entry["body"],
        "resolved": bool(entry.get("resolved")),
    })


def _memory_guard_hash(entry):
    """Hash every field whose concurrent change can affect write ownership."""
    return _json_hash({
        "id": entry["id"],
        "date": entry["date"],
        "type": entry["type"],
        "title": entry["title"],
        "body": entry["body"],
        "tags": entry["tags"],
        "people": entry.get("people", []),
        "source_ids": entry["source_ids"],
        "follows": entry["follows"],
        "resolved": bool(entry.get("resolved")),
    })


def _assert_memory_unchanged(store, observed):
    current = _load_memory_entries(store).get(observed["id"])
    if not current or _memory_guard_hash(current) != _memory_guard_hash(observed):
        raise RuntimeError(
            f"concurrent memory change detected for {observed['id']!r}; "
            "write aborted"
        )
    return current


def _assert_outbound_eligible(entry, excluded):
    if entry["type"] != "todo" or entry["resolved"]:
        raise RuntimeError(
            f"memory entry {entry['id']!r} is no longer an open todo; "
            "Google task creation aborted"
        )
    if excluded.intersection(entry["tags"]):
        raise RuntimeError(
            f"memory entry {entry['id']!r} became excluded from Google Tasks; "
            "creation aborted"
        )
    if any(_source_parts(source_id) for source_id in entry["source_ids"]):
        raise RuntimeError(
            f"memory entry {entry['id']!r} acquired a Google Tasks identity; "
            "duplicate creation aborted"
        )


def _assert_google_unchanged(tasklist_id, task_id, observed):
    current = _task_from_payload(_gws(["get", tasklist_id, task_id]))
    current["tasklist_id"] = tasklist_id
    if _google_hash(current) != _google_hash(observed):
        raise RuntimeError(
            f"concurrent Google Tasks change detected for "
            f"{tasklist_id}:{task_id}; write aborted"
        )
    return current


def _assert_pair_unchanged(store, entry, task):
    _assert_memory_unchanged(store, entry)
    _assert_google_unchanged(task["tasklist_id"], str(task["id"]), task)


def _content_aligned(task, entry):
    """Whether Google and memory express the same editable task fields."""
    fields = _memory_to_google(entry)
    return (
        str(task.get("title") or "") == fields["title"]
        and _clean_google_notes(task.get("notes"))
            == _clean_google_notes(fields["notes"])
        and _due_date(task) == fields["due"]
    )


def _sides_aligned(task, entry):
    """Whether Google and memory already express the same task state.

    This is also the crash-recovery test. A process can stop after writing one
    side but before advancing the checkpoint; on retry both hashes then look
    changed even though the prior sync made them agree. Only recover the
    checkpoint when content, due date, and completion state all match.
    """
    return (
        _content_aligned(task, entry)
        and (task.get("status") == "completed") == bool(entry.get("resolved"))
    )


def _load_checkpoint(path):
    path = Path(path)
    if not path.exists():
        return {"version": SYNC_VERSION, "mappings": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read Google Tasks sync checkpoint {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or data.get("version") != SYNC_VERSION
        or not isinstance(data.get("mappings"), dict)
    ):
        raise RuntimeError(f"Google Tasks sync checkpoint {path} has an unsupported format")
    return data


def _save_checkpoint(path, checkpoint):
    state.write_atomic(
        Path(path),
        json.dumps(checkpoint, indent=2, sort_keys=True) + "\n",
        mode=0o600,
    )


def _verified_durable_entry(store, source_id, expected):
    matches = [
        entry
        for entry in _load_memory_entries(store).values()
        if source_id in entry["source_ids"]
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one durable memory entry for {source_id!r}; "
            f"found {len(matches)}"
        )
    entry = matches[0]
    for field in ("id", "title", "type", "body"):
        wanted = expected.get(field)
        if wanted is not None and entry[field] != wanted:
            raise RuntimeError(
                f"durable memory entry {entry['id']!r} does not match the "
                f"requested {field}"
            )
    for field in ("follows", "people", "source_ids"):
        wanted = set(expected.get(field) or [])
        if not wanted.issubset(set(entry[field])):
            raise RuntimeError(
                f"durable memory entry {entry['id']!r} is missing requested "
                f"{field}"
            )
    return entry


def _memory_result_id(result, store, source_id, expected):
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    match = ENTRY_ID_RE.search(output)
    if result.returncode == 0 and match:
        entry_id = match.group(1)
        if expected.get("id") and entry_id != expected["id"]:
            raise RuntimeError(
                f"memory add updated {entry_id!r}, expected {expected['id']!r}"
            )
        return entry_id
    # personal-memory may return non-zero after a durable write. Verify by the
    # canonical source id *and exact requested content* before accepting it.
    # Source-id presence alone is insufficient for updates because it already
    # existed before the attempted write.
    try:
        return _verified_durable_entry(store, source_id, expected)["id"]
    except RuntimeError as exc:
        raise RuntimeError(
            f"memory add failed: {output[:300]}; durable verification: {exc}"
        ) from exc


def _memory_upsert(
    store,
    task,
    tasklist,
    source_id,
    update_id=None,
    existing_entry=None,
):
    body = _memory_body(
        task,
        tasklist["title"],
        existing_body=_trusted_existing_body(existing_entry, source_id),
    )
    memory_date = _memory_entry_date(task, existing_entry)
    tags = f"google-tasks,google-tasks-{_slug(tasklist['title'])}"
    explicit_id = None if update_id else _explicit_memory_id(task, source_id)
    args = [
        "add", "--title", str(task.get("title") or "Untitled task"),
        "--type", "todo", "--date", memory_date,
        "--tags", tags, "--source-ids", source_id,
        "--body", body,
    ]
    if update_id:
        args.extend(["--update", update_id])
    else:
        # The canonical Google source id is the dedupe boundary. Two distinct
        # tasks may be semantically similar and must not be collapsed by the
        # store's generic near-duplicate guard.
        if explicit_id:
            args.extend(["--id", explicit_id])
        args.append("--force-new")
    result = _memory_cli(store, args)
    return _memory_result_id(result, store, source_id, {
        "id": update_id or explicit_id,
        "title": str(task.get("title") or "Untitled task"),
        "type": "todo",
        "body": body,
        "source_ids": [source_id],
    })


def _memory_enrich_people(store, entry, source_id, people):
    """Add verified existing person slugs without changing synced task fields."""
    missing = sorted(set(people) - set(entry.get("people", [])))
    if not missing:
        return entry["id"]
    result = _memory_cli(store, [
        "add", "--title", entry["title"],
        "--type", entry["type"], "--date", entry["date"],
        "--people", ",".join(missing),
        "--source-ids", source_id,
        "--body", entry["body"],
        "--update", entry["id"],
    ])
    return _memory_result_id(result, store, source_id, {
        "id": entry["id"],
        "title": entry["title"],
        "type": entry["type"],
        "body": entry["body"],
        "people": missing,
        "source_ids": [source_id],
    })


def _memory_complete(store, entry, task, source_id):
    completed = str(task.get("completed") or task.get("updated") or _today())[:10]
    if not DATE_RE.fullmatch(completed):
        completed = _today()
    completion_source = f"{source_id}:completed"
    result = _memory_cli(store, [
        "add", "--title", f"Completed: {entry['title']}",
        "--type", "note", "--date", completed,
        "--tags", "google-tasks,task-completed",
        "--source-ids", completion_source,
        "--follows", entry["id"],
        "--body", "Google Tasks marked this task completed.",
        "--force-new",
    ])
    return _memory_result_id(result, store, completion_source, {
        "title": f"Completed: {entry['title']}",
        "type": "note",
        "body": "Google Tasks marked this task completed.",
        "source_ids": [completion_source],
        "follows": [entry["id"]],
    })


def _tasklists(cfg):
    payload = _gws(["lists"])
    rows = payload.get("tasklists") or []
    if not isinstance(rows, list):
        raise RuntimeError("gws tasks lists returned no tasklists array")
    wanted = cfg.get("tasklists", "all")
    if wanted == "all":
        selected = rows
    else:
        wanted_set = set(wanted)
        selected = [row for row in rows if row.get("id") in wanted_set]
        missing = sorted(wanted_set - {row.get("id") for row in selected})
        if missing:
            raise RuntimeError(
                "configured Google Task list id(s) not found: " + ", ".join(missing)
            )
    return {
        str(row["id"]): {
            "id": str(row["id"]),
            "title": str(row.get("title") or row["id"]),
        }
        for row in selected
        if row.get("id")
    }


def _open_tasks(tasklists, max_tasks):
    tasks = {}
    for list_id in tasklists:
        payload = _gws(["list", list_id, "--max", str(max_tasks)])
        rows = payload.get("tasks") or []
        if int(payload.get("count") or len(rows)) >= max_tasks:
            raise RuntimeError(
                f"Google Task list {list_id} reached max_tasks={max_tasks}; "
                "increase the cap before syncing"
            )
        for row in rows:
            task_id = row.get("id")
            if not task_id:
                continue
            detail = _task_from_payload(_gws(["get", list_id, str(task_id)]))
            detail["tasklist_id"] = list_id
            tasks[f"{list_id}:{task_id}"] = detail
    return tasks


def _update_google(tasklist_id, task_id, fields):
    args = ["update", tasklist_id, task_id, "--title", fields["title"]]
    args.extend(["--notes", fields["notes"]])
    if fields.get("due"):
        args.extend(["--due", fields["due"]])
    _gws(args)
    return _task_from_payload(_gws(["get", tasklist_id, task_id]))


def _create_google(tasklist_id, entry):
    fields = _memory_to_google(entry)
    args = ["create", "--tasklist", tasklist_id, "--title", fields["title"]]
    args.extend(["--notes", fields["notes"]])
    if fields.get("due"):
        args.extend(["--due", fields["due"]])
    created = _task_from_payload(_gws(args))
    task_id = str(created.get("id") or "")
    if not task_id:
        raise RuntimeError("gws tasks create returned no task id")
    return _task_from_payload(_gws(["get", tasklist_id, task_id]))


def _plan(report, action, **details):
    report["planned"].append({"action": action, **details})
    report[action] = report.get(action, 0) + 1


def run(cfg, checkpoint_path=None, dry_run=False):
    """Run one validated sync and return an audit-friendly report."""
    store = Path(cfg["store"])
    checkpoint = _load_checkpoint(checkpoint_path) if checkpoint_path else {
        "version": SYNC_VERSION,
        "mappings": {},
    }
    mappings = checkpoint["mappings"]
    tasklists = _tasklists(cfg)
    max_tasks = int(cfg.get("max_tasks", 10000))
    google_tasks = _open_tasks(tasklists, max_tasks)
    memory_entries = _load_memory_entries(store)
    known_people = _known_people(memory_entries)
    report = {
        "ok": True,
        "tasklist_count": len(tasklists),
        "open_google_tasks": len(google_tasks),
        "open_memory_todos": sum(
            entry["type"] == "todo" and not entry["resolved"]
            for entry in memory_entries.values()
        ),
        "planned": [],
        "errors": [],
        "conflicts": 0,
    }

    mapped_memory_ids = set()
    mapping_owner_by_memory = {}
    invalid_mapping_keys = set()
    for key, mapping in mappings.items():
        if not isinstance(mapping, dict):
            invalid_mapping_keys.add(key)
            report["errors"].append({
                "task": key,
                "error": "checkpoint mapping is not an object",
            })
            continue
        memory_id = str(mapping.get("memory_id") or "")
        if memory_id:
            mapped_memory_ids.add(memory_id)
            previous_key = mapping_owner_by_memory.get(memory_id)
            if previous_key and previous_key != key:
                invalid_mapping_keys.update((previous_key, key))
                report["conflicts"] += 1
                report["planned"].append({
                    "action": "conflict",
                    "task": key,
                    "memory_id": memory_id,
                    "reason": (
                        "multiple checkpoint mappings claim the same memory entry"
                    ),
                })
            else:
                mapping_owner_by_memory[memory_id] = key
        key_parts = key.split(":")
        if len(key_parts) != 2 or not all(key_parts):
            invalid_mapping_keys.add(key)
            report["errors"].append({
                "task": key,
                "memory_id": memory_id or None,
                "error": "checkpoint mapping key is not <list-id>:<task-id>",
            })
            continue
        list_id, task_id = key_parts
        expected_source = _source_id(list_id, task_id)
        if not memory_id or mapping.get("source_id") != expected_source:
            invalid_mapping_keys.add(key)
            report["errors"].append({
                "task": key,
                "memory_id": memory_id or None,
                "error": (
                    "checkpoint mapping has missing or inconsistent canonical "
                    "identity"
                ),
            })
        if list_id not in tasklists:
            invalid_mapping_keys.add(key)
            report["errors"].append({
                "task": key,
                "memory_id": memory_id or None,
                "error": (
                    "checkpoint identity belongs to a Google Task list "
                    "that is no longer selected or available"
                ),
            })

    memory_by_source = _memory_by_google_source(memory_entries)

    # A checkpoint identity reserves its memory todo even when the remote task
    # is unavailable. Validate the one-to-one graph anchor before any refetch;
    # an invalid mapping must never fall through to outbound creation.
    for key, mapping in mappings.items():
        if key in invalid_mapping_keys or not isinstance(mapping, dict):
            continue
        list_id, task_id = key.split(":", 1)
        source_id = _source_id(list_id, task_id)
        entry = memory_entries.get(str(mapping.get("memory_id") or ""))
        if not entry:
            invalid_mapping_keys.add(key)
            report["errors"].append({
                "task": key,
                "memory_id": mapping.get("memory_id"),
                "error": "mapped memory entry is missing",
            })
            continue
        source_entry = memory_by_source.get(source_id)
        if source_entry and source_entry["id"] != entry["id"]:
            invalid_mapping_keys.add(key)
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": "checkpoint and canonical source identify different memory entries",
            })
            continue
        google_sources = [
            value for value in entry["source_ids"] if _source_parts(value)
        ]
        if not mapping.get("pending_link") and source_id not in google_sources:
            invalid_mapping_keys.add(key)
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": (
                    "checkpoint mapping exists but the canonical Google Tasks "
                    "source identity was removed from memory"
                ),
            })
        elif (
            mapping.get("pending_link")
            and google_sources
            and source_id not in google_sources
        ):
            invalid_mapping_keys.add(key)
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": "pending memory link acquired a different Google Tasks identity",
            })

    # The checkpoint is disposable local state; canonical source ids in the
    # graph are the recovery anchor. Re-fetch linked tasks that are absent from
    # both the checkpoint and the open-task listing (usually completed tasks).
    for source_id in sorted(memory_by_source):
        list_id, task_id = _source_parts(source_id)
        if list_id not in tasklists:
            continue
        key = f"{list_id}:{task_id}"
        if key in invalid_mapping_keys:
            continue
        if key in google_tasks:
            continue
        if (
            mappings.get(key, {}).get("terminal")
            and memory_by_source[source_id]["resolved"]
        ):
            continue
        try:
            detail = _task_from_payload(_gws(["get", list_id, task_id]))
        except Exception as exc:
            report["errors"].append({"task": key, "error": str(exc)})
            continue
        detail["tasklist_id"] = list_id
        google_tasks[key] = detail

    # Rehydrate mapped tasks that disappeared from the open listing. A mapped
    # task may have been completed; an unlinked historical completed task is
    # intentionally ignored on bootstrap.
    for key, mapping in list(mappings.items()):
        if key in google_tasks or key in invalid_mapping_keys:
            continue
        mapped_entry = memory_entries.get(mapping.get("memory_id"))
        if mapping.get("terminal") and mapped_entry and mapped_entry["resolved"]:
            continue
        list_id, task_id = key.split(":", 1)
        if list_id not in tasklists:
            continue
        try:
            detail = _task_from_payload(_gws(["get", list_id, task_id]))
        except Exception as exc:
            report["errors"].append({"task": key, "error": str(exc)})
            continue
        detail["tasklist_id"] = list_id
        google_tasks[key] = detail

    linked_memory_ids = set(mapped_memory_ids)
    google_titles = {}
    excluded = set(cfg.get("exclude_tags") or [])
    identity_deferred_keys = set()

    for key, task in sorted(google_tasks.items()):
        memory_write_this_item = False
        list_id = task["tasklist_id"]
        task_id = str(task["id"])
        source_id = _source_id(list_id, task_id)
        google_titles.setdefault(str(task.get("title") or "").strip().casefold(), []).append(key)
        mapping = mappings.get(key)
        if key in invalid_mapping_keys:
            continue
        source_entry = memory_by_source.get(source_id)
        mapped_entry = (
            memory_entries.get(mapping.get("memory_id")) if mapping else None
        )
        origin_id = _origin_memory_id(task.get("notes"))
        anchored_entry = source_entry or mapped_entry
        if origin_id and anchored_entry and origin_id != anchored_entry["id"]:
            origin_entry = memory_entries.get(origin_id)
            if origin_entry:
                linked_memory_ids.add(origin_entry["id"])
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": anchored_entry["id"],
                "title": anchored_entry["title"],
                "reason": (
                    "Google task canonical and origin markers identify "
                    "different memory entries"
                ),
            })
            continue
        entry = anchored_entry

        if not mapping and not entry:
            # A Google create can succeed remotely before this process records
            # its pending checkpoint. The origin marker in the created notes
            # is therefore a second durable identity anchor, stronger than
            # title matching and safe across title edits.
            if origin_id:
                entry = memory_entries.get(origin_id)
                if not entry:
                    report["conflicts"] += 1
                    report["planned"].append({
                        "action": "conflict",
                        "task": key,
                        "memory_id": origin_id,
                        "title": task.get("title"),
                        "reason": "Google task origin memory entry is missing",
                    })
                    continue
                if entry["id"] in linked_memory_ids:
                    report["conflicts"] += 1
                    report["planned"].append({
                        "action": "conflict",
                        "task": key,
                        "memory_id": entry["id"],
                        "title": entry["title"],
                        "reason": "Google task origin memory entry is already claimed",
                    })
                    continue
                linked_memory_ids.add(entry["id"])
                existing_google_sources = [
                    value
                    for value in entry["source_ids"]
                    if _source_parts(value)
                ]
                if (
                    entry["type"] != "todo"
                    or excluded.intersection(entry["tags"])
                    or existing_google_sources
                    or not _sides_aligned(task, entry)
                ):
                    report["conflicts"] += 1
                    report["planned"].append({
                        "action": "conflict",
                        "task": key,
                        "memory_id": entry["id"],
                        "title": entry["title"],
                        "reason": (
                            "recovered outbound origin differs, is excluded, "
                            "or already has a Google Tasks identity"
                        ),
                    })
                    continue
                _plan(
                    report,
                    "link_memory",
                    task=key,
                    memory_id=entry["id"],
                    tasklist=tasklists[list_id]["title"],
                    title=task.get("title"),
                    source_updated=_task_updated(task),
                    memory_date=_memory_entry_date(task, entry),
                )
                if not dry_run:
                    _assert_pair_unchanged(store, entry, task)
                    _memory_upsert(
                        store,
                        task,
                        tasklists[list_id],
                        source_id,
                        entry["id"],
                        existing_entry=entry,
                    )
                    entry = dict(
                        entry,
                        title=str(task.get("title") or "Untitled task"),
                        body=_memory_body(
                            task,
                            tasklists[list_id]["title"],
                            existing_body=_trusted_existing_body(entry, source_id),
                        ),
                        source_ids=[*entry["source_ids"], source_id],
                    )
                    memory_write_this_item = True

        if not mapping and not entry:
            exact = [
                candidate for candidate in memory_entries.values()
                if candidate["type"] == "todo"
                and not candidate["resolved"]
                and candidate["id"] not in linked_memory_ids
                and not excluded.intersection(candidate["tags"])
                and not any(_source_parts(s) for s in candidate["source_ids"])
                and candidate["title"].strip().casefold()
                    == str(task.get("title") or "").strip().casefold()
            ]
            # A same-title candidate must never also be exported as a new
            # Google task later in this run, even when linking is ambiguous.
            linked_memory_ids.update(candidate["id"] for candidate in exact)
            if len(exact) == 1:
                entry = exact[0]
                if not _sides_aligned(task, entry):
                    report["conflicts"] += 1
                    report["planned"].append({
                        "action": "conflict",
                        "task": key,
                        "memory_id": entry["id"],
                        "title": entry["title"],
                        "reason": "exact title match has different content",
                    })
                    continue
                _plan(
                    report,
                    "link_memory",
                    task=key,
                    memory_id=entry["id"],
                    tasklist=tasklists[list_id]["title"],
                    title=task.get("title"),
                    source_updated=_task_updated(task),
                    memory_date=_memory_entry_date(task, entry),
                )
                if not dry_run:
                    _assert_pair_unchanged(store, entry, task)
                    _memory_upsert(
                        store,
                        task,
                        tasklists[list_id],
                        source_id,
                        entry["id"],
                        existing_entry=entry,
                    )
                    entry = dict(
                        entry,
                        title=str(task.get("title") or "Untitled task"),
                        body=_memory_body(
                            task,
                            tasklists[list_id]["title"],
                            existing_body=_trusted_existing_body(entry, source_id),
                        ),
                        source_ids=[*entry["source_ids"], source_id],
                    )
                    memory_write_this_item = True
            elif len(exact) > 1:
                report["conflicts"] += 1
                report["planned"].append({
                    "action": "conflict", "task": key,
                    "reason": "multiple exact-title memory todos",
                })
                continue
            else:
                explicit_id = _explicit_memory_id(task, source_id)
                people = _task_people(
                    task,
                    known_people,
                    cfg.get("identity_exclude_people") or [],
                )
                _plan(
                    report,
                    "create_memory",
                    task=key,
                    tasklist=tasklists[list_id]["title"],
                    title=task.get("title"),
                    due=_due_date(task),
                    source_updated=_task_updated(task),
                    memory_date=_memory_entry_date(task),
                    **({"memory_id": explicit_id} if explicit_id else {}),
                    **({"people": people} if people else {}),
                )
                if not dry_run:
                    _assert_google_unchanged(list_id, task_id, task)
                    entry_id = _memory_upsert(store, task, tasklists[list_id], source_id)
                    entry = {
                        "id": entry_id,
                        "date": _memory_entry_date(task),
                        "type": "todo",
                        "title": str(task.get("title") or "Untitled task"),
                        "body": _memory_body(task, tasklists[list_id]["title"]),
                        "tags": ["google-tasks"],
                        "people": [],
                        "source_ids": [source_id],
                        "follows": [],
                        "resolved": False,
                    }
                    memory_write_this_item = True
                else:
                    continue

        if not entry:
            report["errors"].append({
                "task": key, "error": "mapped memory entry is missing",
            })
            continue

        linked_memory_ids.add(entry["id"])
        if excluded.intersection(entry["tags"]):
            _plan(
                report,
                "skip_excluded",
                task=key,
                memory_id=entry["id"],
                title=entry["title"],
            )
            continue
        if entry["type"] != "todo":
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": (
                    "linked memory entry is not a todo; automatic "
                    "reclassification is not allowed"
                ),
            })
            continue
        if (
            mapping
            and not mapping.get("pending_link")
            and source_id not in entry["source_ids"]
        ):
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": (
                    "checkpoint mapping exists but the canonical Google Tasks "
                    "source identity was removed from memory"
                ),
            })
            continue
        current_google_hash = _google_hash(task)
        current_memory_hash = _memory_hash(entry)
        if mapping and mapping.get("pending_link"):
            other_google_sources = [
                value
                for value in entry["source_ids"]
                if _source_parts(value) and value != source_id
            ]
            if other_google_sources:
                report["conflicts"] += 1
                report["planned"].append({
                    "action": "conflict",
                    "task": key,
                    "memory_id": entry["id"],
                    "title": entry["title"],
                    "reason": (
                        "pending memory link acquired a different Google Tasks "
                        "identity"
                    ),
                })
                continue
            if not _sides_aligned(task, entry):
                report["conflicts"] += 1
                report["planned"].append({
                    "action": "conflict",
                    "task": key,
                    "memory_id": entry["id"],
                    "title": entry["title"],
                    "reason": (
                        "outbound Google task was created, but the sides "
                        "changed before its memory identity could be linked"
                    ),
                })
                continue
            _plan(
                report,
                "link_memory",
                task=key,
                memory_id=entry["id"],
                tasklist=tasklists[list_id]["title"],
                title=task.get("title"),
                source_updated=_task_updated(task),
                memory_date=_memory_entry_date(task, entry),
            )
            if not dry_run:
                _assert_pair_unchanged(store, entry, task)
                _memory_upsert(
                    store,
                    task,
                    tasklists[list_id],
                    source_id,
                    entry["id"],
                    existing_entry=entry,
                )
                _commit_store(str(store), "memory: sync Google Tasks")
                entry = dict(
                    entry,
                    title=str(task.get("title") or "Untitled task"),
                    body=_memory_body(
                        task,
                        tasklists[list_id]["title"],
                        existing_body=_trusted_existing_body(entry, source_id),
                    ),
                    source_ids=[*entry["source_ids"], source_id],
                )
                mapping.update({
                    "source_id": source_id,
                    "google_hash": current_google_hash,
                    "memory_hash": _memory_hash(entry),
                    "terminal": False,
                    "pending_link": False,
                })
                mappings[key] = mapping
                _save_checkpoint(checkpoint_path, checkpoint)
            continue
        if not mapping and not _content_aligned(task, entry):
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": "linked sides differ but no checkpoint baseline exists",
            })
            continue
        if not mapping and task.get("status") == "completed" and not entry["resolved"]:
            _plan(
                report,
                "complete_memory",
                task=key,
                memory_id=entry["id"],
                title=entry["title"],
            )
            if not dry_run:
                _assert_pair_unchanged(store, entry, task)
                _memory_complete(store, entry, task, source_id)
                entry = dict(entry, resolved=True)
                current_memory_hash = _memory_hash(entry)
                memory_write_this_item = True
        elif not mapping and entry["resolved"] and task.get("status") != "completed":
            _plan(
                report,
                "complete_google",
                task=key,
                memory_id=entry["id"],
                title=entry["title"],
            )
            if dry_run:
                identity_deferred_keys.add(key)
            if not dry_run:
                _assert_pair_unchanged(store, entry, task)
                _gws(["complete", list_id, task_id])
                task = _task_from_payload(_gws(["get", list_id, task_id]))
                task["tasklist_id"] = list_id
                google_tasks[key] = task
                current_google_hash = _google_hash(task)

        if not mapping:
            mapping = {
                "memory_id": entry["id"],
                "source_id": source_id,
                "google_hash": current_google_hash,
                "memory_hash": current_memory_hash,
                "terminal": bool(
                    task.get("status") == "completed" and entry["resolved"]
                ),
            }
            if not dry_run:
                if memory_write_this_item or source_id in memory_by_source:
                    _commit_store(str(store), "memory: sync Google Tasks")
                mappings[key] = mapping
                _save_checkpoint(checkpoint_path, checkpoint)
            continue

        google_changed = current_google_hash != mapping.get("google_hash")
        local_changed = current_memory_hash != mapping.get("memory_hash")
        if google_changed and local_changed:
            if _sides_aligned(task, entry):
                _plan(
                    report,
                    "recover_checkpoint",
                    task=key,
                    memory_id=entry["id"],
                    title=entry["title"],
                )
                if not dry_run:
                    _commit_store(str(store), "memory: sync Google Tasks")
                    mapping.update({
                        "google_hash": current_google_hash,
                        "memory_hash": current_memory_hash,
                        "terminal": bool(
                            task.get("status") == "completed"
                            and entry["resolved"]
                        ),
                    })
                    mappings[key] = mapping
                    _save_checkpoint(checkpoint_path, checkpoint)
            else:
                report["conflicts"] += 1
                report["planned"].append({
                    "action": "conflict", "task": key,
                    "memory_id": entry["id"], "title": entry["title"],
                    "reason": "both sides changed",
                })
            continue

        google_completed = task.get("status") == "completed"
        completion_forward = (
            google_changed and google_completed and not entry["resolved"]
        ) or (
            local_changed and entry["resolved"] and not google_completed
        )
        if google_completed != entry["resolved"] and not completion_forward:
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": (
                    "task was reopened on one side; automatic reopening is "
                    "not supported"
                ),
            })
            continue
        if completion_forward and not _content_aligned(task, entry):
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict",
                "task": key,
                "memory_id": entry["id"],
                "title": entry["title"],
                "reason": (
                    "content and completion changed together; neither change "
                    "was applied automatically"
                ),
            })
            continue
        if google_changed:
            if google_completed and not entry["resolved"]:
                _plan(
                    report,
                    "complete_memory",
                    task=key,
                    memory_id=entry["id"],
                    title=entry["title"],
                )
                if not dry_run:
                    _assert_pair_unchanged(store, entry, task)
                    _memory_complete(store, entry, task, source_id)
                    entry = dict(entry, resolved=True)
                    current_memory_hash = _memory_hash(entry)
                    memory_write_this_item = True
            elif not google_completed:
                _plan(
                    report,
                    "update_memory",
                    task=key,
                    memory_id=entry["id"],
                    title=task.get("title"),
                    due=_due_date(task),
                    source_updated=_task_updated(task),
                    memory_date=_memory_entry_date(task, entry),
                )
                if not dry_run:
                    _assert_pair_unchanged(store, entry, task)
                    _memory_upsert(
                        store,
                        task,
                        tasklists[list_id],
                        source_id,
                        entry["id"],
                        existing_entry=entry,
                    )
                    entry = dict(
                        entry,
                        title=str(task.get("title") or "Untitled task"),
                        body=_memory_body(
                            task,
                            tasklists[list_id]["title"],
                            existing_body=_trusted_existing_body(entry, source_id),
                        ),
                    )
                    current_memory_hash = _memory_hash(entry)
                    memory_write_this_item = True
        elif local_changed:
            if entry["resolved"] and not google_completed:
                _plan(
                    report,
                    "complete_google",
                    task=key,
                    memory_id=entry["id"],
                    title=entry["title"],
                )
                if dry_run:
                    identity_deferred_keys.add(key)
                if not dry_run:
                    _assert_pair_unchanged(store, entry, task)
                    _gws(["complete", list_id, task_id])
                    task = _task_from_payload(_gws(["get", list_id, task_id]))
                    task["tasklist_id"] = list_id
                    google_tasks[key] = task
                    current_google_hash = _google_hash(task)
            elif not entry["resolved"] and not google_completed:
                fields = _memory_to_google(entry)
                if _due_date(task) and not fields["due"]:
                    report["conflicts"] += 1
                    report["planned"].append({
                        "action": "conflict",
                        "task": key,
                        "memory_id": entry["id"],
                        "title": entry["title"],
                        "reason": (
                            "clearing a Google Tasks due date is not supported "
                            "by the configured CLI"
                        ),
                    })
                    continue
                _plan(
                    report,
                    "update_google",
                    task=key,
                    memory_id=entry["id"],
                    title=entry["title"],
                    due=fields["due"],
                )
                if dry_run:
                    identity_deferred_keys.add(key)
                if not dry_run:
                    _assert_pair_unchanged(store, entry, task)
                    task = _update_google(list_id, task_id, fields)
                    task["tasklist_id"] = list_id
                    google_tasks[key] = task
                    current_google_hash = _google_hash(task)

        if not dry_run:
            if memory_write_this_item:
                _commit_store(str(store), "memory: sync Google Tasks")
            mapping.update({
                "memory_id": entry["id"],
                "source_id": source_id,
                "google_hash": current_google_hash,
                "memory_hash": current_memory_hash,
                "terminal": bool(
                    task.get("status") == "completed" and entry["resolved"]
                ),
            })
            mappings[key] = mapping
            _save_checkpoint(checkpoint_path, checkpoint)

    outbound_since = cfg.get("outbound_since")
    outbound_list = cfg["outbound_tasklist"]
    if outbound_list not in tasklists:
        raise RuntimeError(
            f"outbound_tasklist {outbound_list!r} is outside the selected tasklists"
        )
    for entry in sorted(memory_entries.values(), key=lambda row: (row["date"], row["id"])):
        if (
            entry["type"] != "todo"
            or entry["resolved"]
            or entry["id"] in linked_memory_ids
            or any(_source_parts(s) for s in entry["source_ids"])
            or (outbound_since and entry["date"] < outbound_since)
            or excluded.intersection(entry["tags"])
        ):
            continue
        title_key = entry["title"].strip().casefold()
        if title_key in google_titles:
            report["conflicts"] += 1
            report["planned"].append({
                "action": "conflict", "memory_id": entry["id"],
                "reason": "unlinked Google task has the same title",
            })
            continue
        fields = _memory_to_google(entry)
        _plan(
            report,
            "create_google",
            memory_id=entry["id"],
            tasklist=tasklists[outbound_list]["title"],
            title=entry["title"],
            due=fields["due"],
        )
        if dry_run:
            continue
        current_entry = _assert_memory_unchanged(store, entry)
        _assert_outbound_eligible(current_entry, excluded)
        task = _create_google(outbound_list, entry)
        # Raw ``gws tasks create/get`` payloads do not carry the list id. Keep
        # the same synthetic shape as ``_open_tasks`` for the later identity
        # enrichment pass and for any checkpoint bookkeeping in this run.
        task["tasklist_id"] = outbound_list
        _task_updated(task)
        task_id = str(task.get("id") or "")
        if not task_id:
            raise RuntimeError("gws tasks create returned no task id")
        source_id = _source_id(outbound_list, task_id)
        key = f"{outbound_list}:{task_id}"
        # Persist identity immediately. If a concurrent edit or crash prevents
        # the memory link below, the next run can fail closed on this exact
        # pair instead of importing/exporting duplicate tasks.
        mappings[key] = {
            "memory_id": entry["id"],
            "source_id": source_id,
            "google_hash": _google_hash(task),
            "memory_hash": _memory_hash(entry),
            "terminal": False,
            "pending_link": True,
        }
        _save_checkpoint(checkpoint_path, checkpoint)
        _assert_memory_unchanged(store, entry)
        _assert_google_unchanged(outbound_list, task_id, task)
        _memory_upsert(
            store,
            task,
            tasklists[outbound_list],
            source_id,
            entry["id"],
            existing_entry=entry,
        )
        _commit_store(str(store), "memory: sync Google Tasks")
        synced_entry = dict(
            entry,
            title=str(task.get("title") or entry["title"]),
            body=_memory_body(
                task,
                tasklists[outbound_list]["title"],
                existing_body=_trusted_existing_body(entry, source_id),
            ),
            source_ids=[*entry["source_ids"], source_id],
        )
        google_tasks[key] = task
        mappings[key] = {
            "memory_id": entry["id"],
            "source_id": source_id,
            "google_hash": _google_hash(task),
            "memory_hash": _memory_hash(synced_entry),
            "terminal": False,
            "pending_link": False,
        }
        _save_checkpoint(checkpoint_path, checkpoint)

    # Person links are memory-only enrichment. They are deliberately outside
    # the bidirectional content hash, so adding a person can never trigger a
    # Google Tasks write. Run this pass only after a conflict-free sync plan;
    # partial or disputed task state must remain entirely untouched.
    if not report["errors"] and report["conflicts"] == 0:
        durable_entries = (
            _load_memory_entries(store) if not dry_run else memory_entries
        )
        durable_people = _known_people(durable_entries)
        durable_by_source = _memory_by_google_source(durable_entries)
        shared_entry_ids = _shared_entry_ids(store)
        excluded_people = cfg.get("identity_exclude_people") or []
        for key, task in sorted(google_tasks.items()):
            if key in identity_deferred_keys:
                continue
            source_id = _source_id(task["tasklist_id"], str(task["id"]))
            entry = durable_by_source.get(source_id)
            if (
                not entry
                or entry["type"] != "todo"
                or excluded.intersection(entry["tags"])
            ):
                continue
            mapping = mappings.get(key)
            if mapping and mapping.get("memory_id") != entry["id"]:
                raise RuntimeError(
                    f"concurrent Google Tasks source reassignment detected "
                    f"for {source_id!r}; people enrichment aborted"
                )
            people = _task_people(task, durable_people, excluded_people)
            missing = sorted(set(people) - set(entry.get("people", [])))
            if not missing:
                continue
            if entry["id"] in shared_entry_ids:
                _plan(
                    report,
                    "skip_shared_people_enrichment",
                    task=key,
                    memory_id=entry["id"],
                    title=entry["title"],
                    reason="entry is materialized in a non-private graph",
                )
                continue
            _plan(
                report,
                "enrich_memory_people",
                task=key,
                memory_id=entry["id"],
                title=entry["title"],
                people=missing,
            )
            if not dry_run:
                _assert_google_unchanged(
                    task["tasklist_id"], str(task["id"]), task
                )
                current_entries = _load_memory_entries(store)
                current_by_source = _memory_by_google_source(current_entries)
                current_entry = current_by_source.get(source_id)
                if not current_entry or current_entry["id"] != entry["id"]:
                    raise RuntimeError(
                        f"concurrent Google Tasks source reassignment detected "
                        f"for {source_id!r}; people enrichment aborted"
                    )
                if _memory_guard_hash(current_entry) != _memory_guard_hash(entry):
                    raise RuntimeError(
                        f"concurrent memory change detected for {entry['id']!r}; "
                        "people enrichment aborted"
                    )
                current_people = set(_known_people(current_entries))
                if not set(missing).issubset(current_people):
                    raise RuntimeError(
                        "person catalog changed before Google Tasks people "
                        "enrichment; write aborted"
                    )
                if entry["id"] in _shared_entry_ids(store):
                    raise RuntimeError(
                        f"memory entry {entry['id']!r} gained shared graph "
                        "membership; people enrichment aborted"
                    )
                _memory_enrich_people(store, entry, source_id, missing)
                _commit_store(str(store), "memory: enrich Google Task people")
                entry["people"] = sorted(set(entry["people"]) | set(missing))

    report["ok"] = not report["errors"] and report["conflicts"] == 0
    if not dry_run and report["ok"]:
        checkpoint["last_successful_sync_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_checkpoint(checkpoint_path, checkpoint)

    return report
