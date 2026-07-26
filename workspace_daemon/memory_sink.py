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
- every entry carries a canonical --source-ids, so re-runs update in place
"""
import json
import re
import subprocess

from .shell import log

MEMORY_TYPES = {
    "event", "decision", "todo", "pending-decision", "1on1", "hiring",
    "incident", "achievement", "feedback", "meeting", "note", "summary",
}
AUTO_TAG = "auto-captured"
EXTRACT_PROMPT = """You are filing a distilled work note into a structured personal memory store.

Read the note below and answer with a SINGLE JSON object, no markdown fence, no
other text, with exactly these keys:
  "worthy": boolean — false if the note contains nothing durable (no decision,
            commitment, people signal, incident, or fact worth recalling later)
  "type": one of: event, decision, todo, pending-decision, 1on1, hiring,
          incident, achievement, feedback, meeting, note
  "title": short specific title (<= 90 chars)
  "people": array of kebab-case person slugs, ONLY from this known list: {slugs}
            (leave out anyone not on the list)
  "tags": array of 1-4 short kebab-case topic tags
  "body": the memory entry body in ENGLISH — concrete facts, names, numbers,
          dates, decisions, follow-ups. Compact but complete.

--- NOTE ---
Title: {title}
Date: {date}
{body}
"""


def memory_cfg(routine):
    cfg = routine.get("memory")
    return cfg if isinstance(cfg, dict) else {}


def validate(routine):
    """Config problems for the memory block; empty list means valid."""
    cfg = memory_cfg(routine)
    if not cfg:
        return []
    rid = routine.get("id", "<missing id>")
    problems = []
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
    return problems


def _cli(store, args, stdin_text=None, timeout=120):
    return subprocess.run(
        ["npx", "tsx", "src/cli.ts", *args],
        cwd=store, capture_output=True, text=True, timeout=timeout,
        input=stdin_text,
    )


_slug_cache = {}


def known_person_slugs(store):
    """Person slugs already in the store (cached per run). Empty on any failure."""
    if store in _slug_cache:
        return _slug_cache[store]
    r = _cli(store, ["slugs", "list", "--kind", "person"])
    slugs = set(re.findall(r"^\s*([a-z0-9][a-z0-9-]+)\s", r.stdout, re.M)) if r.returncode == 0 else set()
    _slug_cache[store] = slugs
    return slugs


def source_id_for(item):
    """Canonical scheme:rest id from the item's provenance, or None."""
    meta = item.get("frontmatter", {})
    if str(item.get("id", "")).startswith("slack:"):
        return item["id"]
    if meta.get("gmail_thread_id"):
        return f"gmail:{meta['gmail_thread_id']}"
    if meta.get("drive_file_id"):
        return f"gdrive:{meta['drive_file_id']}"
    return None


def _extract(routine, item, summary, store):
    """Structured second pass over the already-distilled summary."""
    from . import llm  # late import to avoid cycles

    slugs = sorted(known_person_slugs(store))
    prompt = EXTRACT_PROMPT.format(
        slugs=", ".join(slugs) or "(none known yet)",
        title=item.get("title", ""), date=item.get("date", ""), body=summary,
    )
    raw = llm.analyze(routine, prompt).strip()
    raw = re.sub(r"^```(json)?|```$", "", raw, flags=re.M).strip()
    data = json.loads(raw)  # let a malformed answer raise; caller falls back
    if not isinstance(data, dict):
        raise ValueError("extraction did not return a JSON object")
    return data


def capture(routine, item, summary, dry_run=False):
    """Distill `summary` into one memory entry and store it via the CLI.

    Returns a small outcome dict for the ledger, or None when skipped.
    Never raises on model/validation trouble — degrades to a plain note,
    because a failed capture must not fail the routine's Gmail triage.
    """
    cfg = memory_cfg(routine)
    if not cfg:
        return None
    store = cfg["store"]
    rid = routine.get("id")
    source_id = source_id_for(item)

    entry = {"worthy": True, "type": cfg.get("type", "note"),
             "title": item.get("title", ""), "people": [], "tags": [], "body": summary}
    degraded = None
    if cfg.get("extract", True):
        try:
            entry.update(_extract(routine, item, summary, store))
        except Exception as exc:
            degraded = f"extraction failed, stored as plain note: {exc}"
            log(f"routine={rid} memory WARN {degraded}")

    if not entry.get("worthy", True):
        log(f"routine={rid} memory: judged not memory-worthy, skipping")
        return {"memory": "skipped_not_worthy"}

    # --- validate everything the model returned -----------------------------
    etype = entry.get("type") if entry.get("type") in MEMORY_TYPES else cfg.get("type", "note")
    known = known_person_slugs(store)
    people = [p for p in entry.get("people") or [] if p in known]
    dropped = [p for p in entry.get("people") or [] if p not in known]
    tags = [t for t in entry.get("tags") or [] if re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(t))]
    tags += list(cfg.get("tags") or [])
    tags.append(AUTO_TAG)
    if dropped:
        tags.append("people-unmapped")
        log(f"routine={rid} memory: dropped unknown slugs {dropped}")
    if degraded:
        tags.append("extract-degraded")
    title = (entry.get("title") or item.get("title") or "untitled")[:120]
    body = entry.get("body") or summary

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

    if dry_run:
        log(f"routine={rid} [dry-run] would memory-add type={etype} title={title!r} "
            f"people={people} source_id={source_id}")
        return {"memory": "dry_run"}

    r = _cli(store, args, stdin_text=body, timeout=300)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(f"memory add failed: {out.strip()[:300]}")
    m = re.search(r"[✓↻]\s+(created|updated|unchanged)\s+(\S+)", out)
    verdict, entry_id = (m.group(1), m.group(2)) if m else ("unknown", None)
    log(f"routine={rid} memory {verdict} {entry_id or ''}")

    # The store's auto-commit hook only fires inside agent sessions; commit here
    # so daemon writes are versioned too. Best-effort.
    subprocess.run(["git", "-C", f"{store}/memory", "add", "-A", "."],
                   capture_output=True, timeout=30)
    subprocess.run(["git", "-C", f"{store}/memory", "commit", "-q", "-m",
                    f"memory: {rid} auto-capture"], capture_output=True, timeout=30)

    return {"memory": verdict, "memory_entry_id": entry_id, "memory_people": people}
