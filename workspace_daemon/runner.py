"""The run loop: match → analyze → write note → triage → record state."""
from . import actions, config, gmail, llm, notes, state
from .shell import log, utc_now_iso


def _needs_label_catalog(routines):
    return any(r.get("analyze", {}).get("pick_label") for r in routines)


def run(base_dir, routines, dry_run=False):
    """Process every enabled routine. Returns a summary dict.

    In dry-run nothing is mutated: no yoetz call, no Gmail write, no file write,
    no state write. Gmail reads still happen so the preview is real.
    """
    processed = state.load(base_dir)
    label_catalog = []
    if _needs_label_catalog(routines):
        label_catalog = gmail.user_labels()
        log(f"fetched {len(label_catalog)} user labels")

    totals = {"matched": 0, "processed": 0, "skipped": 0, "errors": 0}
    for routine in routines:
        if not routine.get("enabled", True):
            log(f"routine={routine['id']} disabled, skipping")
            continue
        try:
            _run_routine(routine, processed, label_catalog, dry_run, totals)
        except Exception as exc:  # a broken routine must not abort the rest
            totals["errors"] += 1
            log(f"routine={routine.get('id', '?')} FATAL: {exc}")

    if not dry_run:
        state.save(base_dir, processed)
    return totals


def _run_routine(routine, processed, label_catalog, dry_run, totals):
    rid = routine["id"]
    problems = config.validate(routine)
    if problems:
        raise config.RoutineError("; ".join(problems))

    source = routine["source"]
    log(f"routine={rid} querying gmail: {source['query']}")
    threads = gmail.search(source["query"], source.get("max_results", 20))
    log(f"routine={rid} {len(threads)} message(s) matched")

    new = 0
    for thread in threads:
        message_id = thread["message_id"]
        if message_id in processed:
            totals["skipped"] += 1
            continue
        new += 1
        totals["matched"] += 1
        try:
            _process_message(routine, thread, processed, label_catalog, dry_run)
            totals["processed"] += 1
        except Exception as exc:  # per-message failures are isolated
            totals["errors"] += 1
            log(f"routine={rid} ERROR message_id={message_id}: {exc}")
    if new == 0:
        log(f"routine={rid} no new matches")


def _process_message(routine, thread, processed, label_catalog, dry_run):
    rid = routine["id"]
    message_id = thread["message_id"]
    thread_id = thread.get("thread_id", message_id)
    action_list = routine.get("actions", [])
    log(f"routine={rid} new match message_id={message_id} subject={thread.get('subject', '')!r}")

    msg = gmail.read_message(message_id)
    headers = msg.get("headers", {})
    body = msg.get("body") or ""
    if not body.strip():
        # gws returns the plain-text part only; HTML-only mail comes back empty.
        # Skipping without recording state means a later fix picks it up again.
        raise RuntimeError("message body is empty (HTML-only mail?) — nothing to analyze")

    if dry_run:
        path = notes.target_path(routine, headers, message_id)
        actions_desc = ", ".join(actions.describe(a, "<llm-chosen>") for a in action_list) or "none"
        log(f"routine={rid} [dry-run] would analyze {len(body)} chars via "
            f"provider={routine['analyze']['provider']} model={routine['analyze']['model']}")
        log(f"routine={rid} [dry-run] would write {path}")
        log(f"routine={rid} [dry-run] would apply: {actions_desc}")
        return

    prompt = llm.build_prompt(routine, headers, body, label_catalog)
    content = llm.analyze(routine, prompt)
    summary, label = llm.split_label(content, label_catalog)
    if routine["analyze"].get("pick_label"):
        log(f"routine={rid} message_id={message_id} label={label!r}")

    path = notes.write(routine, headers, message_id, thread_id, summary, label)
    log(f"routine={rid} wrote {path}")

    actions.apply(message_id, action_list, label)

    processed[message_id] = {
        "rule_id": rid,
        "processed_at": utc_now_iso(),
        "output_file": str(path),
        "gmail_label_applied": label,
    }
