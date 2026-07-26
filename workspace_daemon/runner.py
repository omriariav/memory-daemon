"""The run loop: match → analyze → write note → ledger → triage → ledger outcome.

The ledger entry is written before triage and updated after it, so an item is
never summarized twice and never left with silently unfinished Gmail actions.
"""
import re
from contextlib import ExitStack

from . import actions, config, drive, gmail, llm, notes, state
from .shell import log, utc_now_iso


def _needs_label_catalog(routines):
    return any(
        r.get("analyze", {}).get("pick_label") and r.get("source", {}).get("kind") == "gmail"
        for r in routines
    )


def run(base_dir, routines, dry_run=False):
    """Process every enabled routine. Returns a summary dict.

    In dry-run nothing is mutated: no yoetz call, no Gmail write, no file write,
    no state write. Source reads still happen so the preview is real.
    """
    # A dry run mutates nothing and reads through atomic replaces, so it does
    # not need the lock and must not be blocked by a real run in progress.
    with ExitStack() as stack:
        if not dry_run:
            stack.enter_context(state.RunLock(base_dir))
        return _run_locked(base_dir, routines, dry_run)


def _run_locked(base_dir, routines, dry_run):
    processed = state.Store(base_dir, dry_run=dry_run)
    label_catalog = []
    if _needs_label_catalog(routines):
        label_catalog = gmail.user_labels()
        log(f"fetched {len(label_catalog)} user labels")

    totals = {"matched": 0, "processed": 0, "skipped": 0, "errors": 0,
              "fallbacks": 0, "pending_actions": 0}
    for routine in routines:
        if not routine.get("enabled", True):
            log(f"routine={routine['id']} disabled, skipping")
            continue
        try:
            _run_routine(routine, processed, label_catalog, dry_run, totals)
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


def _gmail_fetch(source, candidate):
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


def _drive_fetch(source, candidate):
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
}


# --- run loop ---------------------------------------------------------------

def _run_routine(routine, processed, label_catalog, dry_run, totals):
    rid = routine["id"]
    problems = config.validate(routine)
    if problems:
        raise config.RoutineError("; ".join(problems))

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
        try:
            _process(routine, candidate, fetch, processed, label_catalog, dry_run, totals)
            totals["processed"] += 1
        except Exception as exc:  # per-item failures are isolated
            totals["errors"] += 1
            log(f"routine={rid} ERROR id={candidate['id']}: {exc}")
    if new == 0:
        log(f"routine={rid} no new matches")


def _process(routine, candidate, fetch, processed, label_catalog, dry_run, totals):
    rid = routine["id"]
    action_list = routine.get("actions", [])
    log(f"routine={rid} new match id={candidate['id']} title={candidate['title']!r}")

    item = fetch(routine["source"], candidate)

    if dry_run:
        path = notes.target_path(routine, item)
        desc = ", ".join(actions.describe(a, "<llm-chosen>") for a in action_list) or "none"
        log(f"routine={rid} [dry-run] would analyze {len(item['body'])} chars via "
            f"provider={routine['analyze']['provider']} model={routine['analyze']['model']}")
        log(f"routine={rid} [dry-run] would write {path}")
        log(f"routine={rid} [dry-run] would apply: {desc}")
        return

    prompt = llm.build_prompt(routine, item, label_catalog)
    content = llm.analyze(routine, prompt)
    summary, label = llm.split_label(content, label_catalog)
    if routine["analyze"].get("pick_label"):
        log(f"routine={rid} id={item['id']} label={label!r}")

    path = notes.write(routine, item, summary, label)
    log(f"routine={rid} wrote {path}")

    record = {
        "rule_id": rid,
        "processed_at": utc_now_iso(),
        "output_file": str(path),
        "gmail_label_applied": label,
    }
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
