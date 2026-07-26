"""The run loop: match → analyze → write note → ledger → triage → ledger outcome.

The ledger entry is written before triage and updated after it, so an item is
never summarized twice and never left with silently unfinished Gmail actions.
"""
import re
from contextlib import ExitStack

from . import actions, config, drive, gmail, labels, llm, memory_sink, notes, slack_source, state
from .shell import log, utc_now_iso


def _needs_label_catalog(routines):
    """Both LLM-chosen and configured labels are validated against the catalog."""
    return any(
        (r.get("analyze", {}).get("pick_label") or config.configured_labels(r))
        and r.get("source", {}).get("kind") == "gmail"
        for r in routines
    )


def run(base_dir, routines, dry_run=False, refresh_labels=False):
    """Process every enabled routine. Returns a summary dict.

    In dry-run nothing is mutated: no yoetz call, no Gmail write, no file write,
    no state write. Source reads still happen so the preview is real.
    """
    # A dry run mutates nothing and reads through atomic replaces, so it does
    # not need the lock and must not be blocked by a real run in progress.
    with ExitStack() as stack:
        lock = stack.enter_context(state.RunLock(base_dir)) if not dry_run else None
        return _run_locked(base_dir, routines, dry_run, lock, refresh_labels)


def _run_locked(base_dir, routines, dry_run, lock=None, refresh_labels=False):
    processed = state.Store(base_dir, dry_run=dry_run)
    catalog = None
    label_catalog = []
    if _needs_label_catalog(routines):
        catalog = labels.Catalog(base_dir, force_refresh=refresh_labels,
                                 read_only=dry_run)
        label_catalog = catalog.names()

    if not dry_run:
        state.sweep_temp_files(state.state_file(base_dir).parent)
        for vault in {r.get("output", {}).get("vault_dir") for r in routines}:
            if vault:
                state.sweep_temp_files(vault)

    totals = {"matched": 0, "processed": 0, "skipped": 0, "errors": 0,
              "fallbacks": 0, "pending_actions": 0}
    for routine in routines:
        if not routine.get("enabled", True):
            parked = sum(
                1 for _, e in processed.items()
                if e.get("rule_id") == routine["id"] and e.get("actions_pending")
            )
            note = f" ({parked} item(s) with triage still parked)" if parked else ""
            log(f"routine={routine['id']} disabled, skipping{note}")
            continue
        try:
            _run_routine(routine, processed, label_catalog, dry_run, totals, lock, catalog)
        except state.AlreadyRunning:
            # Losing the lock is a whole-run condition, not one routine's bug:
            # every remaining routine would hit it too. Let it reach the CLI,
            # which reports it as a clean skip rather than N fatal errors.
            raise
        except Exception as exc:  # a broken routine must not abort the rest
            totals["errors"] += 1
            log(f"routine={routine.get('id', '?')} FATAL: {exc}")

    return totals


# --- sources ----------------------------------------------------------------
# A source lists candidates cheaply (id + title only), then fetches one item's
# full content on demand. Listing stays cheap so dedupe can skip most work.

def _gmail_candidates(source):
    return [
        {"id": t["message_id"], "title": t.get("subject", ""), "raw": t}
        for t in gmail.search(source["query"], source.get("max_results", 20))
    ]


def _gmail_fetch(routine, candidate):
    source = routine["source"]
    message_id = candidate["id"]
    msg = gmail.read_message(message_id)
    headers = msg.get("headers", {})
    body = msg.get("body") or ""
    thread_id = candidate["raw"].get("thread_id", message_id)
    subject = headers.get("subject", "")
    date = notes.email_date(headers)

    item = {
        "id": message_id,
        "title": subject,
        "date": date,
        "body": body,
        "frontmatter": {
            "gmail_message_id": message_id,
            "gmail_thread_id": thread_id,
            "gmail_link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
            "email_from": headers.get("from", ""),
            "email_subject": subject,
            "email_date": headers.get("date", ""),
        },
    }

    stream = _stream_for(routine, item)
    if stream.get("title"):
        # A stable stream name beats the raw subject, which carries RE:/FW:
        # prefixes and its own embedded date — both of which make for noisy,
        # unsortable filenames.
        item["title"] = stream["title"]
        item["frontmatter"]["stream"] = stream["title"]

    expand = source.get("expand")
    if expand:
        item["frontmatter"]["expanded"] = False
        _expand_from_drive(expand, item, subject, date)

    if not item["body"].strip():
        # gws returns the plain-text part only; HTML-only mail comes back empty.
        # Skipping without recording state means a later fix picks it up again.
        raise RuntimeError("no content: empty message body and no expanded document")
    return item


def _expand_from_drive(expand, item, subject, date):
    """Replace the email body with the linked Drive doc's tabs, in place.

    The notification email is a stub — for Gemini notes it carries only the
    "Quick notes" tab, and its link to the doc lives solely in the HTML part
    that gws strips. So the doc is located by title instead of by link.
    """
    match = re.search(expand["title_from_subject"], subject)
    title = match.group("title").strip() if match else None
    if not title:
        _missing(expand, item, f"subject {subject!r} did not match title_from_subject")
        return

    # Apply the clean title even if the doc lookup fails, so a fallback note is
    # still filed as meeting-notes-<date>-<meeting> rather than the raw subject.
    item["title"] = title

    doc = drive.find_doc(title, name_contains=expand.get("name_contains"), on_date=date)
    if not doc:
        _missing(expand, item, f"no Drive doc found for {title!r}")
        return

    body, read = drive.read_tabs(doc["id"], expand.get("tabs"))
    if not body.strip():
        _missing(expand, item, f"doc {doc['id']} had no text in the requested tabs")
        return

    item["body"] = body
    item["frontmatter"].update({
        "expanded": True,
        "drive_file_id": doc["id"],
        "drive_link": doc.get("web_link", ""),
        "doc_title": doc.get("name", ""),
        "doc_tabs": read,
    })
    missing_tabs = [t for t in (expand.get("tabs") or []) if t not in read]
    if missing_tabs:
        log(f"doc={doc['id']} tabs not present, skipped: {', '.join(missing_tabs)}")


def _missing(expand, item, reason):
    """Handle a failed expansion per on_missing: fall back to the stub, or fail.

    A fallback is a quiet quality cliff — the stub is a fraction of the document
    — so it is recorded on the note, in state, and in the run summary rather
    than being indistinguishable from a full expansion. `on_missing: error`
    skips the item instead, leaving it in the queue to retry.
    """
    if expand.get("on_missing", "body") == "error":
        raise RuntimeError(f"expand failed: {reason}")
    item["expand_fallback"] = reason
    log(f"WARN routine fell back to the email stub — {reason}")


def _drive_candidates(source):
    files = drive.search(
        source["query"],
        max_results=source.get("max_results", 50),
        mime_type=source.get("mime_type", drive.GOOGLE_DOC_MIME),
        name_contains=source.get("name_contains"),
    )
    return [{"id": f["id"], "title": f.get("name", ""), "raw": f} for f in files]


def _drive_fetch(routine, candidate):
    source = routine["source"]
    doc_id = candidate["id"]
    meta = candidate["raw"]
    name = meta.get("name", "")
    wanted = source.get("tabs")
    body, read = drive.read_tabs(doc_id, wanted)
    if not body.strip():
        raise RuntimeError(f"no text in tabs {wanted or 'all'} (available: {drive.tabs(doc_id)})")
    if wanted:
        missing = [t for t in wanted if t not in read]
        if missing:
            log(f"doc={doc_id} tabs not present, skipped: {', '.join(missing)}")
    return {
        "id": doc_id,
        "title": drive.meeting_title(name),
        "date": drive.date_from_name(name) or (meta.get("modified") or "")[:10],
        "body": body,
        "frontmatter": {
            "drive_file_id": doc_id,
            "drive_link": meta.get("web_link", ""),
            "doc_title": name,
            "doc_tabs": read,
            "doc_modified": meta.get("modified", ""),
        },
    }


SOURCES = {
    "gmail": (_gmail_candidates, _gmail_fetch),
    "drive_docs": (_drive_candidates, _drive_fetch),
    "slack": (slack_source.candidates,
              lambda routine, candidate: slack_source.fetch(routine, candidate)),
}


# --- run loop ---------------------------------------------------------------

def _run_routine(routine, processed, label_catalog, dry_run, totals, lock=None, catalog=None):
    rid = routine["id"]
    problems = config.validate(routine)
    if problems:
        raise config.RoutineError("; ".join(problems))

    # Fail fast on a mistyped configured label: it is a config error affecting
    # every item, so surfacing it once per routine beats failing each item
    # individually and leaving them all to retry forever.
    # `label_catalog` is only ever populated from `catalog`, so testing the
    # object alone covers both — and unlike the list it stays truthy when the
    # catalog comes back empty, which is exactly when one routine-level failure
    # beats a refetch per item.
    if catalog is not None:
        for name in config.configured_labels(routine):
            _validated_label(name, label_catalog, rid, catalog)

    _retry_pending_actions(routine, processed, dry_run, totals)

    source = routine["source"]
    list_candidates, fetch = SOURCES[source["kind"]]
    log(f"routine={rid} querying {source['kind']}: {source['query']}")
    candidates = list_candidates(source)
    log(f"routine={rid} {len(candidates)} item(s) matched")

    new = 0
    for candidate in candidates:
        if candidate["id"] in processed:
            totals["skipped"] += 1
            continue
        new += 1
        totals["matched"] += 1
        if lock:
            # Cheap per-item guard: if the lock file vanished we may no longer
            # be the only run, and continuing risks double-processing.
            lock.check()
        try:
            _process(routine, candidate, fetch, processed, label_catalog, dry_run,
                     totals, catalog)
            totals["processed"] += 1
        except Exception as exc:  # per-item failures are isolated
            totals["errors"] += 1
            log(f"routine={rid} ERROR id={candidate['id']}: {exc}")
    if new == 0:
        log(f"routine={rid} no new matches")


def _process(routine, candidate, fetch, processed, label_catalog, dry_run, totals,
             catalog=None):
    rid = routine["id"]
    action_list = routine.get("actions", [])
    log(f"routine={rid} new match id={candidate['id']} title={candidate['title']!r}")

    item = fetch(routine, candidate)

    if dry_run:
        desc = ", ".join(actions.describe(a, "<llm-chosen>") for a in action_list) or "none"
        log(f"routine={rid} [dry-run] would analyze {len(item['body'])} chars via "
            f"provider={routine['analyze']['provider']} model={routine['analyze']['model']}")
        if routine.get("output"):
            log(f"routine={rid} [dry-run] would write {notes.target_path(routine, item)}")
        if memory_sink.memory_cfg(routine):
            memory_sink.capture(routine, item, "<summary>", dry_run=True)
        log(f"routine={rid} [dry-run] would apply: {desc}")
        return

    prompt = llm.build_prompt(routine, item, label_catalog)
    content = llm.analyze(routine, prompt)
    summary, label = llm.split_label(content, label_catalog)
    static = _static_label(routine, item)
    if static:
        # A configured label still goes through the catalog so a typo in the YAML
        # fails loudly here rather than creating a stray Gmail label.
        label = _validated_label(static, label_catalog, rid, catalog)
        log(f"routine={rid} id={item['id']} label={label!r} (from config)")
    elif routine["analyze"].get("pick_label"):
        log(f"routine={rid} id={item['id']} label={label!r}")

    path = None
    if routine.get("output"):
        path = notes.write(routine, item, summary, label)
        log(f"routine={rid} wrote {path}")

    record = {
        "rule_id": rid,
        "processed_at": utc_now_iso(),
        "output_file": str(path) if path else None,
        "gmail_label_applied": label,
    }

    # Memory sink runs after the vault note: the note is the expensive half and
    # the memory add is idempotent by source id, so a crash between the two is
    # healed by the next run re-capturing into the same entry.
    if memory_sink.memory_cfg(routine):
        try:
            outcome = memory_sink.capture(routine, item, summary)
            if outcome:
                record.update(outcome)
        except Exception as exc:
            # Memory failure must not abort Gmail triage; the ledger records it
            # for a manual re-run.
            record["memory_error"] = str(exc)[:300]
            totals["errors"] += 1
            log(f"routine={rid} memory ERROR: {exc}")
    if item.get("expand_fallback"):
        # Queryable: `grep expand_fallback state/processed.json` lists every
        # item summarized from a stub, so they can be deleted and re-run once
        # the underlying document shows up.
        record["expand_fallback"] = item["expand_fallback"]
        totals["fallbacks"] += 1

    # Two-phase. The note on disk is the expensive, irreversible half, so it is
    # ledgered immediately — with the whole action list marked pending, so that
    # dying here leaves the triage recoverable rather than lost. Recording only
    # after triage would instead risk a duplicate note on the next run.
    if action_list:
        record["actions_pending"] = list(action_list)
    processed.record(item["id"], record)

    if action_list:
        applied, pending = actions.apply(item["id"], action_list, label)
        processed.record(item["id"], _with_action_outcome(record, applied, pending))
        if pending:
            totals["pending_actions"] += 1


def _stream_for(routine, item):
    """The `streams:` entry matching this item, or {}.

    One routine often covers several recurring report streams that differ only
    in what they should be called and which label they belong under. That is a
    pure lookup, so it is declared rather than left to the model to judge.

    Keys match against the subject by default, which is the stable identity of a
    recurring report — a colleague replying into the thread does not change it,
    where the sender would. `from:` prefixes a key to match the sender instead.
    """
    meta = item.get("frontmatter", {})
    subject = (meta.get("email_subject") or "").lower()
    sender = (meta.get("email_from") or "").lower()
    for needle, cfg in (routine.get("streams") or {}).items():
        key = needle.lower()
        haystack, key = (sender, key[5:].strip()) if key.startswith("from:") else (subject, key)
        if key in haystack:
            return cfg or {}
    return {}


def _static_label(routine, item):
    """The configured label for this item: per-stream first, then routine-wide."""
    return _stream_for(routine, item).get("label") or routine.get("label")


def _validated_label(name, label_catalog, rid, catalog=None):
    """Resolve a configured label, case-insensitively.

    Goes through the Catalog when there is one so a miss refetches before being
    reported — a cached catalog must never turn a real label into a false error.
    """
    match = (catalog.resolve(name) if catalog
             else {n.lower(): n for n in label_catalog}.get(name.lower()))
    if not match:
        raise RuntimeError(
            f"routine={rid} label {name!r} does not exist in Gmail; "
            f"create it first or fix the routine"
        )
    return match


def _with_action_outcome(record, applied, pending):
    """Fold an action result into a ledger entry."""
    updated = dict(record)
    updated["actions_applied"] = sorted(set(record.get("actions_applied", [])) | set(applied))
    if pending:
        updated["actions_pending"] = pending
    else:
        updated.pop("actions_pending", None)
    return updated


def _retry_pending_actions(routine, processed, dry_run, totals):
    """Re-apply triage that failed on an earlier run.

    Retried by ledger id rather than by re-querying: once `archive` succeeds the
    item no longer matches an `in:inbox` query, so a query-driven retry could
    never reach the leftovers. Actions are idempotent, so replaying a partially
    applied sequence is safe.
    """
    rid = routine["id"]
    if routine["source"]["kind"] != "gmail":
        return
    for item_id, entry in processed.items():
        if entry.get("rule_id") != rid:
            continue
        pending = entry.get("actions_pending")
        if not pending:
            continue
        if dry_run:
            log(f"routine={rid} [dry-run] would retry pending actions on {item_id}: "
                f"{', '.join(pending)}")
            continue
        log(f"routine={rid} retrying pending actions on {item_id}: {', '.join(pending)}")
        applied, still_pending = actions.apply(item_id, pending, entry.get("gmail_label_applied"))
        processed.record(item_id, _with_action_outcome(entry, applied, still_pending))
        if still_pending:
            totals["pending_actions"] += 1
        else:
            log(f"routine={rid} pending actions cleared for {item_id}")
