"""The run loop: match → analyze → write note → ledger → triage → ledger outcome.

The ledger entry is written before triage and updated after it, so an item is
never summarized twice and never left with silently unfinished Gmail actions.
"""
import datetime
import hashlib
import json
import math
import re
from contextlib import ExitStack
from email.utils import getaddresses

from . import actions, chat_text, config, contacts, drive, gchat_source, gmail, labels, llm, maintenance, memory_sink, mila_source, notes, slack_source, state, time_utils
from .shell import log, utc_now_iso

MAX_GMAIL_SOURCE_PEOPLE = 20
MAX_GMAIL_THREAD_MESSAGES = 50
MAX_GMAIL_THREAD_CHARS = 120_000
GMAIL_CHAT_FOLLOWUP_QUERY = 'in:inbox from:me to:me subject:"Fwd: Chat"'
_SHARED_DEPENDENCY_ERRORS = (
    "name or service not known", "temporary failure in name resolution",
    "could not resolve host", "nodename nor servname provided",
    "network is unreachable", "connection refused", "connection reset",
    "connection timed out", "timed out", "too many requests", "rate limit",
    "quota exceeded", "unauthenticated", "invalid api key",
)


def _is_shared_dependency_failure(value):
    text = str(value).casefold()
    return any(marker in text for marker in _SHARED_DEPENDENCY_ERRORS)


def _catch_up_cursor_id(source):
    """Stable cursor namespace for each supported catch-up source shape."""
    kind = source["kind"]
    if kind == "gchat":
        # Preserve the original namespace for legacy unbatched configurations.
        # Once batching is configured, fingerprint the construction contract:
        # a later cutover/session change must not inherit a cursor newer than
        # the messages that need to be rebuilt under the new semantics.
        batching_scope = {
            "batch_messages": source.get("batch_messages"),
            "batch_messages_after": source.get("batch_messages_after"),
            "session_gap_minutes": source.get("session_gap_minutes"),
            "catch_up_after": source.get("catch_up_after"),
            "candidate_schema": gchat_source.CATCH_UP_SCHEMA,
        }
        if not any(
            batching_scope[key] is not None
            for key in (
                "batch_messages", "batch_messages_after",
                "session_gap_minutes",
            )
        ):
            return "gchat:all-spaces"
        digest = hashlib.sha256(
            json.dumps(
                batching_scope, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()[:16]
        return f"gchat:all-spaces:{digest}"
    if kind == "slack":
        # A cursor is valid only for the exact configured coverage. If a
        # channel or mentions are added later, the new scope bootstraps from
        # catch_up_after instead of inheriting a checkpoint that predates it.
        scope = {
            key: sorted(source.get(key, []))
            for key in (
                "channels", "ada_channels", "direct_channels", "private_channels"
            )
        }
        active_conversations = source.get("active_conversations")
        if isinstance(active_conversations, dict):
            active_conversations = dict(active_conversations)
            # Whether a stale cache may be refreshed inline changes execution,
            # not source coverage. Keep the pre-split cursor namespace so the
            # first consume-only run still validates its new census against
            # the prior discovery watermark instead of silently bootstrapping.
            active_conversations.pop("refresh_if_stale", None)
        scope.update({
            "active_conversations": active_conversations,
            "include_mentions": source.get("include_mentions") is True,
            "catch_up_after": source.get("catch_up_after"),
            "reply_roots_after": source.get("reply_roots_after"),
            # Candidate/enrichment migrations need a fresh replay from the
            # declared cutover, even when the prior cursor is already newer
            # than the affected daily entries.
            "candidate_schema": slack_source.CATCH_UP_SCHEMA,
        })
        digest = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return f"slack:configured-scope:{digest}"
    if kind == "gmail":
        # The cursor belongs to the stable semantic query, not its runtime
        # `after:` boundary. Changing mailbox scope intentionally bootstraps a
        # new cursor from catch_up_after.
        scope = {
            "query": source.get("query"),
            "queue_query": source.get("queue_query"),
            "exclude_query": source.get("exclude_query"),
            "read_thread": source.get("read_thread") is True,
            "self_forwarded_chat_followups": (
                source.get("self_forwarded_chat_followups") is True
            ),
        }
        digest = hashlib.sha256(
            json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return f"gmail:configured-query:{digest}"
    raise config.RoutineError(f"catch-up is not supported for source kind {kind!r}")


def _active_conversation_cursor_id(source):
    """Durable upper boundary of the last consumed Slack census snapshot."""
    return f"{_catch_up_cursor_id(source)}:discovery"


def _coverage_cursor_id(source_index, source):
    """Stable success checkpoint for one configured connector source scope."""
    scope = {
        key: value
        for key, value in source.items()
        if not str(key).startswith("_")
    }
    active_conversations = scope.get("active_conversations")
    if isinstance(active_conversations, dict):
        active_conversations = dict(active_conversations)
        active_conversations.pop("refresh_if_stale", None)
        scope["active_conversations"] = active_conversations
    digest = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return f"connector-coverage:{source_index}:{digest}"


def _needs_label_catalog(routines):
    """Both LLM-chosen and configured labels are validated against the catalog."""
    return any(
        (
            effective.get("analyze", {}).get("pick_label")
            or config.configured_labels(effective)
        )
        and source.get("kind") == "gmail"
        for r in routines
        for source, effective in zip(
            config.sources(r), config.execution_routines(r)
        )
    )


def run(base_dir, routines, dry_run=False, refresh_labels=False, active_ids=None,
        lock_name="run"):
    """Process every enabled routine. Returns a summary dict.

    In dry-run no source or data state is mutated: no yoetz call, Gmail write,
    vault/store write, or state write. Source reads still happen so the preview
    is real; the CLI may append to its operational run log.

    `routines` is the complete routing context. `active_ids` optionally limits
    which owners may process in this invocation while still letting inactive
    domain routines protect their candidates from a due fallback sweep.
    """
    contacts.clear_cache()
    drive.clear_identity_cache()
    # A dry run mutates nothing and reads through atomic replaces, so it does
    # not need the lock and must not be blocked by a real run in progress.
    with ExitStack() as stack:
        lock = (
            stack.enter_context(state.RunLock(base_dir, name=lock_name))
            if not dry_run else None
        )
        return _run_locked(
            base_dir, routines, dry_run, lock, refresh_labels, active_ids=active_ids
        )


def _run_locked(base_dir, routines, dry_run, lock=None, refresh_labels=False,
                active_ids=None):
    scan_started_at = utc_now_iso()
    active_ids = set(active_ids) if active_ids is not None else {
        r["id"] for r in routines if r.get("enabled", True)
    }
    active = [
        r for r in routines
        if r.get("enabled", True) and r["id"] in active_ids
    ]
    processed = state.Store(base_dir, dry_run=dry_run)
    cursors = state.CursorStore(base_dir, dry_run=dry_run)
    catalog = None
    label_catalog = []
    if _needs_label_catalog(active):
        catalog = labels.Catalog(base_dir, force_refresh=refresh_labels,
                                 read_only=dry_run)
        label_catalog = catalog.names()

    if not dry_run:
        state.sweep_temp_files(state.state_file(base_dir).parent)
        vaults = {
            effective.get("output", {}).get("vault_dir")
            for routine in active
            for effective in config.execution_routines(routine)
            if isinstance(effective.get("output"), dict)
        }
        for vault in vaults:
            if vault:
                state.sweep_temp_files(vault)

    totals = {"matched": 0, "processed": 0, "skipped": 0, "errors": 0,
              "fallbacks": 0, "pending_actions": 0, "ambiguous": 0}

    valid = []
    routing_failures = []
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
            problems = config.validate(routine)
            if problems:
                raise config.RoutineError("; ".join(problems))
            if routine["id"] not in active_ids:
                valid.append(routine)
                continue
            if config.is_maintenance(routine):
                valid.append(routine)
                continue
            if catalog is not None:
                for effective in config.execution_routines(routine):
                    for name in config.configured_labels(effective):
                        _validated_label(
                            name, label_catalog, routine["id"], catalog
                        )
            _retry_pending_actions(routine, processed, dry_run, totals)
            valid.append(routine)
        except Exception as exc:  # a broken routine must not abort the rest
            totals["errors"] += 1
            log(f"routine={routine.get('id', '?')} FATAL: {exc}")
            routing_failures.extend(_routine_failures(routine))

    # Within one lock group, maintenance executes before capture sources. The
    # coordinator refreshes Slack census separately under slack-census.lock:
    # manual all-routine runs do that first, while scheduled capture consumes
    # the last completed snapshot without waiting for a long census.
    for routine in valid:
        if (
            routine["id"] not in active_ids
            or not config.is_maintenance(routine)
        ):
            continue
        try:
            maintenance.run(base_dir, routine, dry_run=dry_run)
        except Exception as exc:
            totals["errors"] += 1
            log(
                f"routine={routine['id']} maintenance FATAL: {exc}"
            )

    active_source_kinds = {
        source["kind"]
        for routine in valid
        if routine["id"] in active_ids
        for source in config.sources(routine)
    }
    source_overrides = {}
    catch_up_sources = []
    active_conversation_sources = []
    for routine in valid:
        routine_active = routine["id"] in active_ids
        for source_index, source in enumerate(config.sources(routine)):
            if (
                source.get("catch_up") is not True
                or source.get("kind") not in active_source_kinds
            ):
                continue
            kind = source["kind"]
            cursor_id = _catch_up_cursor_id(source)
            checkpoint = cursors.checkpoint(
                routine["id"], cursor_id, kind,
            )
            if checkpoint:
                second, _ = time_utils.rfc3339_key(checkpoint)
                overlap = config.duration_seconds(
                    source.get("catch_up_overlap", "1h")
                )
                since = (
                    second - datetime.timedelta(seconds=overlap)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                since = (
                    source.get("catch_up_after")
                    or source.get("batch_messages_after")
                )
            if since:
                override = {"_since": since}
                if kind == "slack" and source.get("catch_up_after"):
                    override["_catch_up_boundary"] = source["catch_up_after"]
                if (
                    routine_active
                    and kind == "slack"
                    and source.get("active_conversations")
                ):
                    discovery_cursor_id = _active_conversation_cursor_id(source)
                    runtime = {
                        "previous_until": cursors.checkpoint(
                            routine["id"], discovery_cursor_id, kind,
                        ),
                    }
                    override["_active_conversation_runtime"] = runtime
                    active_conversation_sources.append(
                        (
                            routine["id"],
                            source_index,
                            discovery_cursor_id,
                            kind,
                            runtime,
                        )
                    )
                source_overrides[(routine["id"], source_index)] = override
                log(
                    f"routine={routine['id']} source={kind} "
                    f"{'catch-up' if routine_active else 'routing catch-up'} "
                    f"since={since}"
                )
            if routine_active:
                catch_up_sources.append(
                    (routine["id"], source_index, cursor_id, kind)
                )

    # Source adapters may need to distinguish a real run from a read-only
    # preview. Keep this runtime-only flag out of routine files and cursor
    # fingerprints (underscore-prefixed fields are deliberately ignored).
    for routine in valid:
        for source_index, source in enumerate(config.sources(routine)):
            if source.get("kind") not in active_source_kinds:
                continue
            override = source_overrides.setdefault(
                (routine["id"], source_index), {}
            )
            override["_dry_run"] = dry_run

    source_coverage = {}
    claims, listing_failures = _collect_claims(
        valid, totals, source_kinds=active_source_kinds,
        routing_context=routines,
        source_overrides=source_overrides,
        source_coverage=source_coverage,
        processed=processed,
    )
    active_source_stamps = {}
    for (
        routine_id,
        source_index,
        _cursor_id,
        kind,
        runtime,
    ) in active_conversation_sources:
        coverage_key = (routine_id, source_index)
        until_at = runtime.get("until_at")
        if until_at:
            active_source_stamps[coverage_key] = until_at
        elif (
            coverage_key in source_coverage
            and not source_coverage[coverage_key]
        ):
            totals["errors"] += 1
            source_coverage[coverage_key] = (
                "Slack census did not report its discovery boundary"
            )
            log(
                f"routine={routine_id} source={kind} FATAL: "
                f"{source_coverage[coverage_key]}"
            )
    routing_errors_before = totals["errors"]
    routing_holds = set()
    owned = _route_claims(
        claims, totals, failures=[*routing_failures, *listing_failures],
        held_source_keys=routing_holds,
    )
    failed_source_keys = {
        key for key, problem in source_coverage.items() if problem
    }
    failed_source_keys.update(routing_holds)
    if totals["errors"] > routing_errors_before:
        # Ownership ambiguity cannot safely be assigned to just one claimant.
        failed_source_keys.update(
            (routine["id"], source_index)
            for routine in active
            for source_index, _source in enumerate(config.sources(routine))
        )
    valid_ids = {routine["id"] for routine in valid}
    shared_circuit = {}

    for routine in active:
        if (
            routine["id"] not in valid_ids
            or config.is_maintenance(routine)
        ):
            continue
        rid = routine["id"]
        routine_claims = owned.get(rid, [])
        failed_source_keys.update(_run_owned(
            routine, routine_claims, processed, label_catalog, dry_run,
            totals, lock, catalog, base_dir, shared_circuit,
        ))

    # A manually self-forwarded Chat message uses Gmail's Inbox as an explicit
    # follow-up queue. Reconcile after capture so an archived item whose prior
    # sink attempt failed can first recreate its todo, then close it in this
    # same run. This still runs when the broad source query had no new items.
    for routine in valid:
        if (
            routine["id"] not in active_ids
            or config.is_maintenance(routine)
        ):
            continue
        for source in config.sources(routine):
            if source.get("self_forwarded_chat_followups") is not True:
                continue
            try:
                _reconcile_gmail_chat_followups(
                    routine, processed, dry_run=dry_run,
                )
            except Exception as exc:
                totals["errors"] += 1
                log(
                    f"routine={routine['id']} Gmail follow-up reconciliation "
                    f"FATAL: {exc}"
                )

    for key in failed_source_keys:
        if key in source_coverage and not source_coverage[key]:
            source_coverage[key] = "item processing failed"

    coverage_points = _mark_connector_sweeps(
        routines,
        valid_ids,
        active_ids,
        scan_started_at,
        dry_run,
        totals,
        cursors,
        source_coverage,
        active_source_stamps,
    )

    successful_points = [
        (routine_id, cursor_id, kind, scan_started_at)
        for routine_id, source_index, cursor_id, kind in catch_up_sources
        if not source_coverage.get((routine_id, source_index))
    ]
    successful_points.extend(coverage_points)
    for (
        routine_id,
        _source_index,
        cursor_id,
        kind,
        runtime,
    ) in active_conversation_sources:
        until_at = runtime.get("until_at")
        if until_at and not source_coverage.get((routine_id, _source_index)):
            successful_points.append(
                (routine_id, cursor_id, kind, until_at)
            )
        elif not source_coverage.get((routine_id, _source_index)):
            totals["errors"] += 1
            log(
                f"routine={routine_id} source={kind} FATAL: "
                "Slack census did not report its discovery boundary"
            )

    if successful_points:
        cursors.mark_successful_at(successful_points)
        advanced_catch_up = sum(
            1 for rid, index, _cursor, _kind in catch_up_sources
            if not source_coverage.get((rid, index))
        )
        if advanced_catch_up:
            mode = "[dry-run] would advance" if dry_run else "advanced"
            log(
                f"catch-up cursor {mode} to {scan_started_at} for "
                f"{advanced_catch_up} source(s)"
            )
    held = len(catch_up_sources) - sum(
        1 for rid, index, _cursor, _kind in catch_up_sources
        if not source_coverage.get((rid, index))
    )
    if held:
        log(f"catch-up cursor held for {held} failed source(s)")

    return totals


def _mark_connector_sweeps(routines, valid_ids, active_ids, scan_started_at,
                           dry_run, totals, cursors, source_coverage,
                           active_source_stamps=None):
    """Advance connector health only to the oldest fully covered scope.

    A connector prompt is reusable by partial routines, so merely reading one
    does not prove connector-wide coverage.  ``connector_sweep: true`` names
    the routine that publishes health for the configured source.  Every
    enabled routine source of that kind participates in the safe watermark:
    active sources contribute this run's start (or their actual fixed-snapshot
    boundary), while inactive owners retain their prior successful checkpoint.
    """
    active_source_stamps = active_source_stamps or {}
    active_source_kinds = {
        source.get("kind")
        for routine in routines
        if (
            routine.get("enabled", True)
            and routine.get("id") in valid_ids
            and routine.get("id") in active_ids
        )
        for source in config.sources(routine)
    }
    declarations = {}
    duplicate_keys = set()
    connector_kinds = set()
    for routine in routines:
        if not routine.get("enabled", True) or routine["id"] not in valid_ids:
            continue
        analyze = routine.get("analyze") or {}
        if analyze.get("connector_sweep") is not True:
            continue
        connector = analyze.get("instruction_from_connector")
        # A targeted run of an unrelated source must not publish connector
        # health from old checkpoints. This could otherwise advance
        # `last_pulled` even though that connector was never queried.
        if connector not in active_source_kinds:
            continue
        store = memory_sink.memory_cfg(routine).get("store")
        key = (store, connector)
        if not store or not connector:
            continue
        if key in declarations:
            duplicate_keys.add(key)
            totals["errors"] += 1
            log(
                f"routine={routine['id']} connector state FATAL: duplicate "
                f"{connector!r} sweep publisher also declared by "
                f"{declarations[key]['id']}"
            )
            continue
        declarations[key] = routine
        connector_kinds.add(connector)

    for routine in routines:
        rid = routine.get("id", "?")
        if (
            not routine.get("enabled", True)
            or rid not in valid_ids
            or rid not in active_ids
        ):
            continue
        for source_index, source in enumerate(config.sources(routine)):
            if source.get("kind") not in connector_kinds:
                continue
            coverage_key = (rid, source_index)
            if (
                coverage_key not in source_coverage
                or source_coverage[coverage_key]
            ):
                continue
            window_seconds = _fixed_window_seconds(source)
            if window_seconds is None:
                continue
            cursor_id = _coverage_cursor_id(source_index, source)
            previous = cursors.checkpoint(
                rid, cursor_id, source["kind"]
            )
            if not previous:
                continue  # first successful run establishes the bootstrap
            stamp = active_source_stamps.get(
                coverage_key, scan_started_at
            )
            stamp_second, _ = time_utils.rfc3339_key(stamp)
            boundary = stamp_second - datetime.timedelta(
                seconds=window_seconds
            )
            if time_utils.rfc3339_key(previous) < (boundary, 0):
                source_coverage[coverage_key] = (
                    "fixed-window gap since prior successful scan"
                )

    coverage_points = []
    for routine in routines:
        if not (
            routine.get("enabled", True)
            and routine["id"] in valid_ids
            and routine["id"] in active_ids
        ):
            continue
        for source_index, source in enumerate(config.sources(routine)):
            coverage_key = (routine["id"], source_index)
            if not (
                source.get("kind") in connector_kinds
                and coverage_key in source_coverage
                and not source_coverage.get(coverage_key)
            ):
                continue
            coverage_points.append((
                routine["id"],
                _coverage_cursor_id(source_index, source),
                source["kind"],
                active_source_stamps.get(coverage_key, scan_started_at),
            ))

    for (store, connector), sweep in declarations.items():
        if (store, connector) in duplicate_keys:
            continue
        stamps = []
        blockers = []
        for routine in routines:
            rid = routine.get("id", "?")
            for source_index, source in enumerate(config.sources(routine)):
                if source.get("kind") != connector:
                    continue
                cursor_id = _coverage_cursor_id(source_index, source)
                if not routine.get("enabled", True):
                    blockers.append(f"{rid}[{source_index}] disabled")
                    continue
                if rid not in valid_ids:
                    blockers.append(f"{rid}[{source_index}] invalid")
                    continue
                if rid in active_ids:
                    coverage_key = (rid, source_index)
                    if coverage_key not in source_coverage:
                        blockers.append(
                            f"{rid}[{source_index}] was not listed"
                        )
                    elif source_coverage[coverage_key]:
                        blockers.append(
                            f"{rid}[{source_index}] "
                            f"{source_coverage[coverage_key]}"
                        )
                    else:
                        stamps.append(
                            active_source_stamps.get(
                                coverage_key, scan_started_at
                            )
                        )
                    continue
                checkpoint = cursors.checkpoint(rid, cursor_id, connector)
                if checkpoint:
                    stamps.append(checkpoint)
                else:
                    blockers.append(f"{rid}[{source_index}] never completed")

        if blockers or not stamps:
            detail = ", ".join(blockers) if blockers else "no covered sources"
            log(
                f"routine={sweep['id']} connector {connector!r} not marked "
                f"pulled: incomplete configured coverage ({detail})"
            )
            continue
        watermark = min(stamps, key=time_utils.rfc3339_key)
        try:
            memory_sink.mark_connector_pulled(
                sweep, watermark, dry_run=dry_run
            )
        except Exception as exc:
            totals["errors"] += 1
            for routine in routines:
                if routine.get("id") not in active_ids:
                    continue
                for source_index, source in enumerate(config.sources(routine)):
                    if source.get("kind") == connector:
                        source_coverage[(routine["id"], source_index)] = (
                            f"connector mark-pulled failed: {exc}"
                        )
            log(
                f"routine={sweep['id']} connector state FATAL: {exc}"
            )
    return [
        point for point in coverage_points
        if not any(
            routine.get("id") == point[0]
            and _coverage_cursor_id(source_index, source) == point[1]
            and source_coverage.get((point[0], source_index))
            for routine in routines
            for source_index, source in enumerate(config.sources(routine))
        )
    ]


# --- sources ----------------------------------------------------------------
# A source lists candidates cheaply (id + title only), then fetches one item's
# full content on demand. Listing stays cheap so dedupe can skip most work.

def _gmail_candidates(source):
    query = source["query"]
    exclude_query = source.get("exclude_query")
    if exclude_query:
        query = f"({query}) ({exclude_query})"
    since = source.get("_since")
    if since:
        second, _ = time_utils.rfc3339_key(since)
        windowed = f"({query}) after:{int(second.timestamp())}"
        queue_query = source.get("queue_query")
        if queue_query and exclude_query:
            queue_query = f"({queue_query}) ({exclude_query})"
        query = (
            f"({queue_query}) OR ({windowed})"
            if queue_query else windowed
        )
    threads = gmail.search(query, source.get("max_results", 20))
    candidates = []
    seen = set()
    for thread in threads:
        thread = dict(thread)
        if source.get("_gmail_chat_followup_listing") is True:
            thread["_gmail_chat_followup_candidate"] = True
        message_id = thread["message_id"]
        if message_id in seen:
            continue
        seen.add(message_id)
        candidates.append({
            "id": message_id,
            "title": chat_text.redact_secrets(thread.get("subject", "")),
            "raw": thread,
        })
    return candidates


def _address_set(value):
    return {
        email.strip().casefold()
        for _, email in getaddresses([value or ""])
        if "@" in email
    }


def _gmail_chat_followup_state(headers, labels):
    """Identify Gmail's manually self-forwarded Google Chat queue item."""
    subject = (headers.get("subject") or "").strip().casefold()
    normalized_labels = {
        str(label).strip().upper() for label in labels or [] if str(label).strip()
    }
    senders = _address_set(headers.get("from"))
    recipients = _address_set(headers.get("to"))
    manual = (
        (subject == "fwd: chat" or subject.startswith("fwd: chat "))
        and bool(senders & recipients)
        and "SENT" in normalized_labels
    )
    return manual, manual and "INBOX" in normalized_labels


def _email_source_people(*header_sets):
    """Verified-identity candidates from structured Gmail address headers.

    These addresses are not trusted as person slugs by themselves.  The memory
    sink resolves each exact address through the Workspace directory before it
    lets the extraction model link the person.
    """
    people = []
    seen = set()
    for headers in header_sets:
        for field in ("from", "to", "cc"):
            value = headers.get(field) or ""
            for name, email in getaddresses([value]):
                normalized = email.strip().casefold()
                if not normalized or "@" not in normalized or normalized in seen:
                    continue
                seen.add(normalized)
                if len(people) >= MAX_GMAIL_SOURCE_PEOPLE:
                    return people, True
                people.append({
                    "email": normalized,
                    "name": " ".join(name.split()),
                    "role": field,
                })
    return people, False


def _gmail_thread_body(messages):
    """Render bounded chronological thread context, favoring recent messages.

    Gmail reply bodies commonly quote the entire history. Strip only the
    corroborated quote markers we already trust, then keep the newest messages
    that fit below the prompt-safety ceiling. The coverage metadata prevents
    the model from treating a bounded thread as complete.
    """
    rendered_newest_first = []
    chars = 0
    body_truncated = False
    total = len(messages)

    for index in range(total - 1, max(-1, total - MAX_GMAIL_THREAD_MESSAGES - 1), -1):
        message = messages[index]
        headers = message.get("headers") or {}
        message_body, _ = _strip_quoted_history(message.get("body") or "")
        lines = [f"--- Message {index + 1} of {total} ---"]
        for label, field in (
            ("From", "from"),
            ("To", "to"),
            ("Cc", "cc"),
            ("Date", "date"),
            ("Subject", "subject"),
        ):
            value = headers.get(field)
            if value:
                lines.append(f"{label}: {value}")
        segment = "\n".join(lines) + "\n\n" + message_body.strip()
        separator_cost = 2 if rendered_newest_first else 0
        remaining = MAX_GMAIL_THREAD_CHARS - chars - separator_cost
        if remaining <= 0:
            break
        if len(segment) > remaining:
            if rendered_newest_first:
                break
            marker = "\n\n[message body truncated by Gmail thread safety limit]"
            segment = segment[:max(0, remaining - len(marker))].rstrip() + marker
            body_truncated = True
        rendered_newest_first.append(segment)
        chars += len(segment) + separator_cost

    rendered = list(reversed(rendered_newest_first))
    included = len(rendered)
    truncated = body_truncated or included < total
    prefix = ""
    if truncated:
        prefix = (
            f"[Thread coverage: supplied {included} of {total} messages; "
            "older content may be omitted.]\n\n"
        )
    return prefix + "\n\n".join(rendered), included, truncated


def _gmail_fetch(routine, source, candidate):
    message_id = candidate["id"]
    thread_id = candidate["raw"].get("thread_id", message_id)
    thread_messages = None
    routed_thread = candidate["raw"].get("_gmail_routed_thread") is True
    if source.get("read_thread") or routed_thread:
        thread = gmail.read_thread(thread_id)
        thread_messages = thread.get("messages") or []
        if not thread_messages:
            raise RuntimeError(f"no content: Gmail thread {thread_id} has no messages")
        if routed_thread:
            # Routing is thread-scoped, but the ledger and Gmail actions are
            # message-scoped. Always process the thread's actual newest message
            # so an older specialized match cannot hide or mutate the wrong
            # reply. The complete bounded thread remains the analysis body.
            msg = thread_messages[-1]
            message_id = msg.get("id") or message_id
        else:
            msg = next(
                (
                    entry for entry in thread_messages
                    if entry.get("id") == message_id
                ),
                thread_messages[-1],
            )
    else:
        msg = gmail.read_message(message_id)
    headers = msg.get("headers", {})
    if thread_messages is not None:
        body, included, thread_truncated = _gmail_thread_body(thread_messages)
        # Identity enrichment is capped. Favor the latest participants so a
        # long-lived thread cannot crowd out the people involved in its current
        # request with an old distribution list.
        people_headers = [
            entry.get("headers") or {} for entry in reversed(thread_messages)
        ]
    else:
        body = msg.get("body") or ""
        included = None
        thread_truncated = False
        people_headers = [headers]
    # Gmail can carry credentials in ordinary text (HR onboarding emails are a
    # common example). Redact before the body or subject can reach a model,
    # logs, notes, or the memory store. The original message remains in Gmail.
    body = chat_text.redact_secrets(body)
    subject = chat_text.redact_secrets(headers.get("subject", ""))
    date = notes.email_date(headers)
    gmail_labels = sorted({str(label) for label in msg.get("labels") or []})
    manual_chat_followup, chat_followup_active = _gmail_chat_followup_state(
        headers, gmail_labels,
    )
    queue_candidate = (
        (candidate.get("raw") or {}).get("_gmail_chat_followup_candidate")
        is True
    )
    if source.get("self_forwarded_chat_followups") is True and queue_candidate:
        # The dedicated Gmail query already established from:me, to:me,
        # subject and Inbox state. Trust it when aliases prevent raw From/To
        # headers from intersecting exactly.
        manual_chat_followup = True
        chat_followup_active = True
    managed_chat_followup = (
        source.get("self_forwarded_chat_followups") is True
        and manual_chat_followup
    )
    source_people, source_people_truncated = _email_source_people(*people_headers)

    item = {
        "id": message_id,
        "source_kind": "gmail",
        "title": subject,
        "date": date,
        "body": body,
        "frontmatter": {
            "gmail_message_id": message_id,
            "gmail_thread_id": thread_id,
            "gmail_link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
            "email_from": headers.get("from", ""),
            "email_to": headers.get("to", ""),
            "email_cc": headers.get("cc", ""),
            "email_subject": subject,
            "email_date": headers.get("date", ""),
            "gmail_labels": gmail_labels,
            "gmail_manual_chat_followup": manual_chat_followup,
            "gmail_chat_followup_managed": managed_chat_followup,
            "gmail_chat_followup_active": chat_followup_active,
            "source_people": source_people,
            "source_people_truncated": source_people_truncated,
        },
    }
    if thread_messages is not None:
        item["frontmatter"].update({
            "gmail_thread_message_count": len(thread_messages),
            "gmail_thread_messages_included": included,
            "gmail_thread_truncated": thread_truncated,
        })

    stream = _stream_for(routine, item)
    message_updates = bool(stream.get("message_updates"))
    if message_updates:
        # Some recurring reports deliberately reuse one Gmail thread: every
        # reply is a new report, while its body still contains the quoted
        # history. Keep the new message as the ledger/memory identity and never
        # let prior weeks leak into the analysis.
        item["source_id"] = f"gmail:{message_id}"
        item["body"], removed = _strip_quoted_history(item["body"])
        if removed:
            item["frontmatter"]["quoted_history_removed"] = True

    report_date = _report_date_from_subject(subject) if stream else None
    # Gmail's root message id equals its thread id. Comparing those identities
    # recognizes replies without guessing from localized or gateway-modified
    # subject prefixes such as AW:, SV:, Re[2]:, or [EXTERNAL] Re:.
    reply_is_new_report = message_updates and message_id != thread_id
    if reply_is_new_report:
        # A reused subject can still carry the first report's date weeks later.
        # For an explicitly message-oriented stream, the reply itself is the
        # new report, so its Gmail date is the truthful event date.
        item["frontmatter"]["report_date"] = date
        if report_date and report_date != date:
            item["frontmatter"]["subject_report_date"] = report_date
    elif report_date:
        # A reply can make Gmail's message date weeks newer than the report it
        # contains. Recurring streams with an explicit subject date should be
        # filed and stored under that report period, while the raw email header
        # remains available in `email_date`.
        item["date"] = report_date
        item["frontmatter"]["report_date"] = report_date
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
    that the plain-text read omits. Resolve that HTML link directly when the
    installed gws supports it, with precise title/date search as a fallback.
    """
    match = re.search(expand["title_from_subject"], subject)
    title = match.group("title").strip() if match else None
    if not title:
        _missing(expand, item, f"subject {subject!r} did not match title_from_subject")
        return

    # Apply the clean title even if the doc lookup fails, so a fallback note is
    # still filed as meeting-notes-<date>-<meeting> rather than the raw subject.
    item["title"] = title

    doc = _linked_google_doc(item["id"])
    lookup = "gmail-link" if doc else "title-date-search"
    if not doc:
        doc = drive.find_doc(
            title,
            name_contains=expand.get("name_contains"),
            on_date=date,
        )
    if not doc:
        _missing(expand, item, f"no Drive doc found for {title!r}")
        return

    document = drive.info(doc["id"])
    doc_title = document.get("title") or doc.get("name", "")
    meeting_date = drive.date_from_name(doc_title)
    if meeting_date:
        # Notification delivery can lag the meeting by a day. The generated
        # document name records the actual occurrence date and is the same
        # authority used by standalone Drive-document sources.
        item["date"] = meeting_date
        item["frontmatter"]["meeting_date"] = meeting_date
    body, read = drive.read_tabs(
        doc["id"],
        expand.get("tabs"),
        document=document,
    )
    if not body.strip():
        _missing(expand, item, f"doc {doc['id']} had no text in the requested tabs")
        return

    owner_emails = _drive_owner_emails(doc["id"])
    item["body"] = body
    item["frontmatter"].update({
        "expanded": True,
        "drive_file_id": doc["id"],
        "drive_link": doc.get("web_link", ""),
        "doc_title": doc_title,
        "doc_tabs": read,
        "doc_lookup": lookup,
    })
    if owner_emails:
        item["frontmatter"]["drive_owner_emails"] = owner_emails
    if doc.get("linked_tab_ids"):
        item["frontmatter"]["doc_linked_tab_ids"] = doc["linked_tab_ids"]
    normalized_read = {" ".join(tab.split()).casefold() for tab in read}
    missing_tabs = [
        tab
        for tab in (expand.get("tabs") or [])
        if " ".join(tab.split()).casefold() not in normalized_read
    ]
    if missing_tabs:
        log(f"doc={doc['id']} tabs not present, skipped: {', '.join(missing_tabs)}")


def _drive_owner_emails(doc_id):
    """Best-effort owner metadata; identity enrichment must not lose the note."""
    try:
        metadata = drive.file_info(doc_id)
    except Exception as exc:
        log(f"WARN doc={doc_id} Drive owner lookup failed: {exc}")
        return []
    return list(dict.fromkeys(
        str(owner).strip().casefold()
        for owner in metadata.get("owners", [])
        if isinstance(owner, str) and "@" in owner
    ))


def _linked_google_doc(message_id):
    """Resolve the meeting Doc directly from the notification's HTML links.

    workspace-cli v1.40.1 added this read-only path. Search remains a fallback
    for old notifications or older CLI installs, but a link is authoritative
    when present and avoids title punctuation/indexing failures.
    """
    try:
        links = [
            link
            for link in gmail.links(message_id)
            if link.get("google_docs_id")
        ]
    except Exception as exc:
        log(
            f"WARN message_id={message_id} direct Gmail link lookup failed; "
            f"falling back to Drive search: {exc}"
        )
        return None
    if not links:
        return None

    preferred = [
        link
        for link in links
        if (link.get("text") or "").strip().casefold()
        in {"open meeting notes", "notes by gemini"}
    ]
    chosen = (preferred or links)[0]
    doc_id = chosen["google_docs_id"]
    distinct = {link["google_docs_id"] for link in links}
    if len(distinct) > 1:
        log(
            f"WARN message_id={message_id} contains {len(distinct)} Google Docs; "
            f"using meeting-notes link {doc_id}"
        )
    return {
        "id": doc_id,
        "web_link": chosen.get("href", ""),
        "linked_tab_ids": sorted({
            link["tab_id"]
            for link in links
            if link.get("google_docs_id") == doc_id and link.get("tab_id")
        }),
    }


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


def _drive_fetch(routine, source, candidate):
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
    owner_emails = _drive_owner_emails(doc_id)
    frontmatter = {
        "drive_file_id": doc_id,
        "drive_link": meta.get("web_link", ""),
        "doc_title": name,
        "doc_tabs": read,
        "doc_modified": meta.get("modified", ""),
    }
    if owner_emails:
        frontmatter["drive_owner_emails"] = owner_emails
    return {
        "id": doc_id,
        "source_kind": "drive_docs",
        "title": drive.meeting_title(name),
        "date": drive.date_from_name(name) or (meta.get("modified") or "")[:10],
        "body": body,
        "frontmatter": frontmatter,
    }


SOURCES = {
    "gmail": (_gmail_candidates, _gmail_fetch),
    "drive_docs": (_drive_candidates, _drive_fetch),
    "slack": (slack_source.candidates,
              lambda routine, source, candidate: slack_source.fetch(
                  routine, candidate, source)),
    "gchat": (gchat_source.candidates,
              lambda routine, source, candidate: gchat_source.fetch(routine, candidate)),
    "mila": (mila_source.candidates, mila_source.fetch),
}


# --- candidate ownership and run loop --------------------------------------

def _scope(source):
    if source.get("kind") == "gchat" and source.get("all_spaces"):
        return "all active Google Chat conversations"
    if source.get("kind") == "slack" and source.get("active_conversations"):
        return "recently active Slack conversations"
    if source.get("kind") == "mila":
        return source.get("recordings_file") or "Mila recordings"
    slack_channels = [
        channel
        for key in (
            "channels", "ada_channels", "direct_channels", "private_channels"
        )
        for channel in source.get(key, [])
    ]
    return (
        source.get("query")
        or ", ".join(slack_channels)
        or ", ".join(source.get("spaces", []))
        or "mentions"
    )


def _routing_id(candidate):
    """Stable ownership key, even when a chat candidate is version-aware."""
    raw = candidate.get("raw")
    if isinstance(raw, dict) and raw.get("source_id"):
        return raw["source_id"]
    if isinstance(raw, dict) and raw.get("thread_id"):
        # Gmail queries may match different messages in the same thread. For
        # example, a Privacy query can match an earlier counsel reply while a
        # general Inbox query returns a newer non-Privacy reply. Memory is
        # thread-anchored, so ownership must be thread-anchored too or the
        # fallback can race the specialized routine and create a duplicate
        # with the wrong prompt.
        return raw["thread_id"]
    return candidate["id"]


_SOURCE_DEFAULT_LIMITS = {
    "gmail": 20,
    "drive_docs": 50,
    "slack": 30,
    "gchat": 50,
    "mila": 0,
}


def _source_limit(source):
    return source.get(
        "max_results", _SOURCE_DEFAULT_LIMITS.get(source.get("kind"), 20)
    )


def _source_scopes(source):
    """Conservative static scopes used for overlap and failure isolation."""
    kind = source.get("kind")
    if kind in {"gmail", "drive_docs"}:
        # Query intersection cannot be proven statically.
        return {(kind, "*")}
    if kind == "gchat":
        values = {
            (kind, space)
            for space in source.get("spaces", [])
            if isinstance(space, str)
        }
        return values or {(kind, "*")}
    if kind == "slack":
        if source.get("include_mentions"):
            # A workspace-wide mention can originate in any channel.
            return {(kind, "*")}
        values = {
            (kind, channel)
            for key in (
                "channels", "ada_channels", "direct_channels", "private_channels"
            )
            for channel in source.get(key, [])
            if isinstance(channel, str)
        }
        return values or {(kind, "*")}
    return {(kind, "*")} if kind else {
        (known, "*") for known in config.VALID_SOURCE_KINDS
    }


def _scopes_overlap(left, right):
    return any(
        left_kind == right_kind
        and (left_scope == "*" or right_scope == "*" or left_scope == right_scope)
        for left_kind, left_scope in left
        for right_kind, right_scope in right
    )


def _candidate_scopes(source, candidate):
    """Narrow a listed chat candidate to its actual channel or space."""
    raw = candidate.get("raw")
    raw = raw if isinstance(raw, dict) else {}
    kind = source.get("kind")
    if kind == "slack" and isinstance(raw.get("channel"), str):
        return {(kind, raw["channel"])}
    if kind == "gchat" and isinstance(raw.get("space"), str):
        return {(kind, raw["space"])}
    return _source_scopes(source)


def _safe_routing_rank(routine):
    """A conservative rank for malformed routines that failed validation."""
    routing = routine.get("routing")
    if not isinstance(routing, dict):
        return (0, 100)
    fallback = routing.get("fallback")
    priority = routing.get("priority", 100)
    if not isinstance(fallback, bool):
        fallback = False
    if not isinstance(priority, int) or isinstance(priority, bool):
        priority = 100
    return (1 if fallback else 0, priority)


def _failure(routine, source=None, known_ids=None, ownership_class="ordinary"):
    return {
        "routine_id": str(routine.get("id", "?")),
        "rank": _safe_routing_rank(routine),
        "ownership_class": ownership_class,
        "scopes": _source_scopes(source) if isinstance(source, dict) else {
            (kind, "*") for kind in config.VALID_SOURCE_KINDS
        },
        # An expanded ownership scan may fail after the routine's normal,
        # capped scan succeeded. Those normal ids are still proven claims.
        "known_ids": set(known_ids or ()),
    }


def _routine_failures(routine):
    """Turn an invalid routine into conservative ownership barriers."""
    source_values = []
    unknown = False
    if "source" in routine:
        source_values.append(routine.get("source"))
    if "sources" in routine:
        values = routine.get("sources")
        if isinstance(values, list):
            source_values.extend(values)
        else:
            unknown = True

    failures = []
    for source in source_values:
        if not (
            isinstance(source, dict)
            and source.get("kind") in config.VALID_SOURCE_KINDS
        ):
            continue
        failures.append(_failure(routine, source))
        if source.get("self_forwarded_chat_followups") is True:
            failures.append(
                _failure(
                    routine, source, ownership_class="gmail_followup",
                )
            )
    if (
        unknown
        or not failures
        or any(not isinstance(source, dict) for source in source_values)
        or any(
            isinstance(source, dict)
            and source.get("kind") not in config.VALID_SOURCE_KINDS
            for source in source_values
        )
    ):
        failures.append(_failure(routine))
    return failures


def _ownership_limit(source, sources):
    """Largest processing cap among source blocks that may overlap."""
    scopes = _source_scopes(source)
    return max(
        (
            _source_limit(other)
            for other in sources
            if _scopes_overlap(scopes, _source_scopes(other))
        ),
        default=_source_limit(source),
    )


def _source_coverage_problem(source, candidates):
    """Why a successful listing still cannot prove the whole scope was read."""
    if source.get("max_results") != 0:
        return f"bounded max_results={source.get('max_results')!r}"
    if (
        source.get("kind") == "gchat"
        and source.get("all_spaces") is True
        and source.get("max_per_space") != 0
    ):
        return (
            f"bounded max_per_space={source.get('max_per_space')!r}"
        )
    if source.get("kind") == "slack":
        capped_ada = sorted({
            candidate.get("raw", {}).get("channel")
            for candidate in candidates
            if (
                candidate.get("raw", {}).get("mode") == "ada_digest"
                and int(
                    candidate.get("raw", {})
                    .get("summary", {})
                    .get("message_count") or 0
                ) >= 100
            )
        } - {None})
        if capped_ada:
            return (
                "Ada 100-message cap reached for "
                + ", ".join(capped_ada)
            )
    return None


def _latest_memory_record(processed, source_id):
    """Newest ledger outcome for one stable memory source id, if any."""
    if processed is None:
        return None
    matching = [
        (entry.get("processed_at") or "", item_id, entry)
        for item_id, entry in processed.items()
        if entry.get("memory_source_id") == source_id
    ]
    return max(matching, default=(None, None, None))[-1]


def _fixed_window_seconds(source):
    """Smallest non-cursor window a source relies on, or None for catch-up."""
    if source.get("catch_up") is True:
        if source.get("kind") == "slack" and source.get("active_conversations"):
            # Content reads are cursor-backed, but discovering which joined
            # conversations to read still depends on a fixed census window.
            return (
                float(source["active_conversations"].get("hours", 48))
                * 60 * 60
            )
        return None
    kind = source.get("kind")
    hours = float(source.get("hours", 26))
    if kind == "gchat":
        return hours * 60 * 60
    if kind != "slack":
        return None

    windows = []
    if any(source.get(key) for key in (
        "channels", "direct_channels", "private_channels"
    )):
        windows.append(hours * 60 * 60)
    default_days = max(1, min(90, math.ceil(hours / 24)))
    if source.get("ada_channels"):
        windows.append(int(source.get("ada_days", default_days)) * 24 * 60 * 60)
    if source.get("include_mentions"):
        windows.append(default_days * 24 * 60 * 60)
    return min(windows) if windows else None


def _collect_claims(routines, totals, source_kinds=None, routing_context=None,
                    source_overrides=None, source_coverage=None,
                    processed=None):
    """List candidates for the full routing context.

    Every enabled routine participates for source kinds used by the active
    routines, even when it is not due. Otherwise a daily fallback could steal
    an item from a domain routine whose four-hour cadence had not elapsed yet.
    Unrelated source kinds cannot claim the same ownership key, so querying them
    only adds latency and lets an irrelevant outage fail a targeted run.
    Candidate processing caps remain per source, but lower caps are expanded
    for ownership discovery so overflow items wait for their specific owner
    instead of leaking into a broader fallback.
    """
    source_kinds = set(source_kinds) if source_kinds is not None else None
    routing_context = routines if routing_context is None else routing_context
    source_overrides = source_overrides or {}
    source_coverage = {} if source_coverage is None else source_coverage
    claims = {}
    failures = []
    entries = [
        (routine, source_index, source)
        for routine in routines
        for source_index, source in enumerate(config.sources(routine))
        if source_kinds is None or source.get("kind") in source_kinds
    ]
    all_sources = [source for _, _, source in entries]
    context_sources = [
        source
        for routine in routing_context
        for source in config.sources(routine)
        if source_kinds is None or source.get("kind") in source_kinds
    ]
    claimed_slack_channels = {
        channel
        for source in context_sources
        if source.get("kind") == "slack"
        for channel in slack_source.configured_channels(source)
    }
    claimed_gchat_spaces = {
        space
        for source in context_sources
        if source.get("kind") == "gchat"
        for space in source.get("spaces", [])
        if isinstance(space, str)
    }
    gmail_sources = {
        routine["id"]: [
            (index, source)
            for index, source in enumerate(config.sources(routine))
            if source.get("kind") == "gmail"
        ]
        for routine in routines
    }
    gmail_listed_ids = {routine["id"]: set() for routine in routines}

    for routine, source_index, source in entries:
        rid = routine["id"]
        kind = source["kind"]
        list_candidates, fetch = SOURCES[kind]
        override = source_overrides.get((rid, source_index))
        listing_source = dict(source, **override) if override else source
        if kind == "slack" and source.get("include_mentions"):
            # Mentions are workspace-wide. Exclude every explicitly owned
            # channel, including channels owned by another routine whose digest
            # source id differs from the mention thread's source id.
            listing_source = dict(
                listing_source,
                _exclude_mention_channels=sorted(claimed_slack_channels),
            )
        if kind == "slack" and source.get("active_conversations"):
            # The census-fed general sweep is workspace-wide. Explicit domain
            # and specialized channels remain owned by their declared routine.
            listing_source = dict(
                listing_source,
                _exclude_channels=sorted(claimed_slack_channels),
            )
        if kind == "gchat" and source.get("all_spaces"):
            # A configured explicit space remains owned even while its domain
            # routine is disabled or not due. The broad fallback must not race
            # it now and create memories with the wrong prompt before that
            # routine is armed.
            listing_source = dict(
                listing_source,
                _exclude_spaces=sorted(claimed_gchat_spaces),
            )
        listing_specs = [{
            "label": kind,
            "listing_source": listing_source,
            "claim_source": source,
            "ownership_class": "ordinary",
            "expand_ownership": True,
        }]
        if (
            kind == "gmail"
            and source.get("self_forwarded_chat_followups") is True
        ):
            # Keep the normal Gmail sweep and the explicit Inbox queue as two
            # independent reads. A failure in either one must not erase the
            # successful candidates from the other.
            followup_listing = dict(
                listing_source,
                query=GMAIL_CHAT_FOLLOWUP_QUERY,
                max_results=0,
                _gmail_chat_followup_listing=True,
            )
            # This is an exact lifecycle queue, not another catch-up source.
            # Inheriting the broad Gmail queue or its cursor causes every
            # unread/starred thread to masquerade as an explicit Chat forward.
            for inherited in ("queue_query", "exclude_query", "_since"):
                followup_listing.pop(inherited, None)
            listing_specs = [
                {
                    "label": "gmail",
                    "listing_source": dict(
                        listing_source,
                        self_forwarded_chat_followups=False,
                    ),
                    "claim_source": dict(
                        source,
                        self_forwarded_chat_followups=False,
                    ),
                    "ownership_class": "ordinary",
                    "expand_ownership": True,
                },
                {
                    "label": "gmail follow-up queue",
                    "listing_source": followup_listing,
                    "claim_source": source,
                    "ownership_class": "gmail_followup",
                    "expand_ownership": False,
                },
            ]

        coverage_problems = []
        listed_routing_ids = set()
        for spec in listing_specs:
            spec_label = spec["label"]
            spec_source = spec["listing_source"]
            claim_source = spec["claim_source"]
            ownership_class = spec["ownership_class"]
            log(f"routine={rid} querying {spec_label}: {_scope(spec_source)}")
            try:
                candidates = list_candidates(spec_source)
            except Exception as exc:
                coverage_problems.append(f"{spec_label} listing failed: {exc}")
                totals["errors"] += 1
                log(f"routine={rid} source={spec_label} FATAL: {exc}")
                failures.append(
                    _failure(
                        routine, source,
                        ownership_class=ownership_class,
                    )
                )
                continue

            if ownership_class == "ordinary":
                problem = _source_coverage_problem(source, candidates)
                if problem:
                    coverage_problems.append(problem)
            log(
                f"routine={rid} source={spec_label} "
                f"{len(candidates)} item(s) matched"
            )
            normal_ids = {_routing_id(candidate) for candidate in candidates}
            listed_routing_ids.update(normal_ids)
            if kind == "gmail":
                gmail_listed_ids[rid].update(normal_ids)

            discovery = []
            discovery_limit = _ownership_limit(source, all_sources)
            source_limit = _source_limit(source)
            if (
                spec["expand_ownership"]
                and source_limit
                and discovery_limit > source_limit
            ):
                expanded_source = dict(
                    spec_source, max_results=discovery_limit
                )
                log(
                    f"routine={rid} source={spec_label} expanding ownership "
                    f"scan to {discovery_limit}"
                )
                try:
                    discovery = list_candidates(expanded_source)
                except Exception as exc:
                    totals["errors"] += 1
                    log(
                        f"routine={rid} source={spec_label} ownership scan "
                        f"FATAL: {exc}"
                    )
                    failures.append(
                        _failure(
                            routine, source, known_ids=normal_ids,
                            ownership_class=ownership_class,
                        )
                    )

            candidates_with_budget = [
                (candidate, True) for candidate in candidates
            ]
            candidates_with_budget.extend(
                (candidate, False)
                for candidate in discovery
                if _routing_id(candidate) not in normal_ids
            )
            for candidate, processable in candidates_with_budget:
                key = (kind, _routing_id(candidate))
                claims.setdefault(key, []).append({
                    "routine": routine,
                    "source": claim_source,
                    "source_index": source_index,
                    "candidate": candidate,
                    "fetch": fetch,
                    "processable": processable,
                })

        source_coverage[(rid, source_index)] = (
            "; ".join(dict.fromkeys(coverage_problems))
            if coverage_problems else None
        )

    # Query results are not a recovery mechanism: older daemon versions could
    # archive a Gmail thread even when its memory sink failed.  Re-open those
    # exact threads from the ledger and retry the sink without repeating Gmail
    # actions.  The same mechanism supports explicit operator overrides after
    # an item has fallen out of its original query.
    for routine in routines:
        rid = routine["id"]
        available = gmail_sources.get(rid) or []
        if not available:
            continue

        def replay_source_for(handler_id):
            if handler_id:
                for index, candidate_source in available:
                    if candidate_source.get("handler") == handler_id:
                        return index, candidate_source
            for index, candidate_source in available:
                if not candidate_source.get("handler"):
                    return index, candidate_source
            return available[0]

        requested = {}
        for source_id in (
            memory_sink.memory_cfg(routine)
            .get("operator_confirmed_source_ids")
            or []
        ):
            if not source_id.startswith("gmail:"):
                continue
            latest = _latest_memory_record(processed, source_id)
            already_complete = bool(
                latest
                and latest.get("memory") not in (None, "skipped_not_worthy")
                and "memory_error" not in latest
            )
            if already_complete:
                continue
            requested[source_id] = {
                "operator_confirmed": True,
                "handler_id": (latest or {}).get("handler_id"),
            }

        latest_by_source = {}
        for item_id, entry in processed.items() if processed is not None else []:
            if (
                entry.get("rule_id") != rid
                or entry.get("source_kind") != "gmail"
            ):
                continue
            managed_followup = (
                entry.get("gmail_manual_chat_followup") is True
            )
            source_id = (
                entry.get("gmail_canonical_memory_source_id")
                if managed_followup else None
            )
            if not source_id and managed_followup and entry.get("gmail_thread_id"):
                source_id = f"gmail:{entry['gmail_thread_id']}"
            source_id = (
                source_id
                or entry.get("memory_source_id")
                or entry.get("source_id")
            )
            if not isinstance(source_id, str) or not source_id.startswith("gmail:"):
                continue
            marker = (entry.get("processed_at") or "", item_id)
            if marker > latest_by_source.get(source_id, (("", ""), None))[0]:
                latest_by_source[source_id] = (marker, entry)
        for source_id, (_, entry) in latest_by_source.items():
            if "memory_error" not in entry:
                continue
            replay = requested.setdefault(source_id, {})
            replay["memory_error"] = True
            replay.setdefault("handler_id", entry.get("handler_id"))
            if entry.get("gmail_manual_chat_followup") is True:
                replay["managed_followup"] = True
                replay["predecessor_entry_id"] = entry.get(
                    "gmail_followup_predecessor_entry_id"
                )

        replayed = 0
        for source_id, replay in requested.items():
            thread_id = source_id.split(":", 1)[1]
            if not thread_id or thread_id in gmail_listed_ids[rid]:
                continue
            source_index, source = replay_source_for(replay.get("handler_id"))
            raw = {
                "thread_id": thread_id,
                "_gmail_routed_thread": True,
            }
            title = "failed Gmail memory capture"
            if replay.get("operator_confirmed"):
                raw["_operator_confirmed_replay"] = True
                title = "operator-confirmed Gmail thread"
            if replay.get("memory_error"):
                raw["_memory_error_replay"] = True
            if replay.get("managed_followup"):
                raw["_gmail_chat_followup_candidate"] = True
                raw["_gmail_followup_replay"] = True
                if replay.get("predecessor_entry_id"):
                    raw["_gmail_followup_predecessor_entry_id"] = (
                        replay["predecessor_entry_id"]
                    )
            candidate = {"id": thread_id, "title": title, "raw": raw}
            replay_source = dict(source, read_thread=True, actions=[])
            if replay.get("managed_followup"):
                replay_source["self_forwarded_chat_followups"] = True
            claims.setdefault(("gmail", thread_id), []).append({
                "routine": routine,
                "source": replay_source,
                "source_index": source_index,
                "candidate": candidate,
                "fetch": SOURCES["gmail"][1],
                "processable": True,
            })
            replayed += 1
        if replayed:
            log(
                f"routine={rid} source=gmail added {replayed} durable replay "
                "candidate(s)"
            )
    return claims, failures


def _is_managed_followup_claim(claim):
    return (
        claim["source"].get("self_forwarded_chat_followups") is True
        and (claim["candidate"].get("raw") or {}).get(
            "_gmail_chat_followup_candidate"
        ) is True
    )


def _claim_source_id(claim):
    """Canonical source id available from a cheap, unfetched candidate."""
    raw = claim["candidate"].get("raw") or {}
    if raw.get("source_id"):
        return raw["source_id"]
    if claim["source"].get("kind") == "gmail" and raw.get("thread_id"):
        return f"gmail:{raw['thread_id']}"
    return None


def _gmail_followup_predecessor(processed, claim, existing=None):
    """Durable memory entry that an actionable Gmail follow-up succeeds.

    Gmail ledger identity is message-scoped while memory identity is
    thread-scoped.  Prefer a predecessor persisted by an earlier failed
    attempt, then find the newest ordinary memory record for the stable thread
    source id.  Managed rows are excluded so a retry never follows itself.
    """
    if existing is not None:
        persisted = existing.get("gmail_followup_predecessor_entry_id")
        if isinstance(persisted, str) and persisted.strip():
            return persisted.strip()
        if (
            existing.get("gmail_manual_chat_followup") is not True
            and existing.get("memory_entry_id")
        ):
            return existing["memory_entry_id"]
    candidate_predecessor = (
        (claim["candidate"].get("raw") or {}).get(
            "_gmail_followup_predecessor_entry_id"
        )
    )
    if (
        isinstance(candidate_predecessor, str)
        and candidate_predecessor.strip()
    ):
        return candidate_predecessor.strip()
    source_id = _claim_source_id(claim)
    if processed is None or not source_id:
        return None
    matching = [
        (entry.get("processed_at") or "", item_id, entry["memory_entry_id"])
        for item_id, entry in processed.items()
        if (
            entry.get("memory_source_id") == source_id
            and entry.get("gmail_manual_chat_followup") is not True
            and entry.get("memory_entry_id")
        )
    ]
    return max(matching, default=(None, None, None))[-1]


def _could_be_gmail_followup(claim):
    return (
        claim["source"].get("kind") == "gmail"
        and str(claim["candidate"].get("title") or "")
        .strip().casefold().startswith("fwd: chat")
    )


def _failure_blocks_claim(failure, claim, best_rank, managed):
    failure_class = failure.get("ownership_class", "ordinary")
    if managed:
        # Ordinary Gmail ownership is irrelevant once the explicit queue has
        # proved this item is a managed follow-up.
        return (
            failure_class == "gmail_followup"
            and failure["rank"] <= best_rank
        )
    if failure_class == "gmail_followup":
        # When queue discovery failed, never let an ordinary Gmail listing
        # ledger a potential self-forward first. Managed ownership outranks
        # ordinary ownership regardless of routine rank.
        return _could_be_gmail_followup(claim)
    return failure["rank"] <= best_rank


def _route_claims(claims, totals, failures=(), held_source_keys=None):
    """Choose exactly one routine for every source candidate.

    A specific routine always beats a fallback. Explicit lower priority wins
    within either class. Equal-ranked distinct owners are ambiguous and skipped
    rather than letting routine file order choose the extraction prompt.
    """
    owned = {}
    held_source_keys = (
        held_source_keys if held_source_keys is not None else set()
    )
    for (kind, item_id), candidates in claims.items():
        managed_followups = [
            claim for claim in candidates if _is_managed_followup_claim(claim)
        ]
        if managed_followups:
            # A self-forward is an explicit queue action, not merely another
            # copy of the underlying conversation. Its lifecycle-aware source
            # owns the Gmail item even if another source also matched it.
            candidates = managed_followups
        # Multiple source blocks in one routine may match the same item. That is
        # one owner, and declaration order is deterministic handler precedence.
        # Never replace an earlier ownership-only claim with a later broad
        # processable one: doing so would let a capped specialized query leak
        # overflow into the general prompt. The item waits for its first owner.
        by_routine = {}
        for claim in candidates:
            rid = claim["routine"]["id"]
            if rid not in by_routine:
                by_routine[rid] = claim
        unique = list(by_routine.values())
        best_rank = min(config.routing_rank(c["routine"]) for c in unique)
        winners = [
            c for c in unique if config.routing_rank(c["routine"]) == best_rank
        ]
        if len(winners) != 1:
            ids = ", ".join(sorted(c["routine"]["id"] for c in winners))
            totals["errors"] += 1
            totals["ambiguous"] += 1
            log(
                f"ownership ERROR source={kind} id={item_id}: equal-ranked "
                f"routines {ids}; skipped"
            )
            continue
        claim = winners[0]
        claimant_source_keys = {
            (candidate["routine"]["id"], candidate["source_index"])
            for candidate in candidates
        }
        blockers = {
            failure["routine_id"]
            for failure in failures
            if _failure_blocks_claim(
                failure, claim, best_rank, bool(managed_followups)
            )
            and item_id not in failure["known_ids"]
            and _scopes_overlap(
                failure["scopes"],
                _candidate_scopes(claim["source"], claim["candidate"]),
            )
        }
        if blockers:
            # The fallback correctly refuses to steal this item, but its
            # successful listing must not advance past it. Hold every claimant
            # cursor until the failed higher-priority owner recovers.
            held_source_keys.update(claimant_source_keys)
            ids = ", ".join(sorted(blockers))
            log(
                f"ownership blocked source={kind} id={item_id}: "
                f"candidate listing failed for {ids}"
            )
            continue
        if not claim.get("processable", True):
            log(
                f"ownership held source={kind} id={item_id}: "
                f"owner {claim['routine']['id']} reached its processing cap"
            )
            continue
        if kind == "gmail" and len(candidates) > 1:
            # Different Gmail queries can match different messages from the
            # same thread. Keep the winning routine and prompt, but make fetch
            # resolve the actual newest message and render the bounded thread.
            # This also makes message-scoped actions target the current reply.
            # Multiple source blocks inside one routine need the same treatment
            # as claims from separate routines.
            claim = dict(claim)
            candidate = dict(claim["candidate"])
            raw = dict(candidate.get("raw") or {})
            raw["_gmail_routed_thread"] = True
            candidate["raw"] = raw
            claim["candidate"] = candidate
        # Cursor safety follows the item, not only its selected owner. Every
        # source that listed this ownership key stays behind it until the
        # winning prompt and sinks complete successfully.
        claim["_claimant_source_keys"] = claimant_source_keys
        owned.setdefault(claim["routine"]["id"], []).append(claim)
    return owned


def _run_owned(routine, claims, processed, label_catalog, dry_run, totals,
               lock=None, catalog=None, base_dir=None, shared_circuit=None):
    rid = routine["id"]
    shared_circuit = {} if shared_circuit is None else shared_circuit
    failed_source_keys = set()
    circuit_reported = False

    def hold_claim(claim):
        keys = claim.get("_claimant_source_keys")
        if keys:
            failed_source_keys.update(keys)
        else:
            failed_source_keys.add((rid, claim["source_index"]))

    log(f"routine={rid} {len(claims)} owned item(s)")
    new = 0
    for claim in claims:
        effective = config.routine_for_source(routine, claim["source"])
        handler_id = effective.get("_handler_id")
        if handler_id:
            log(
                f"routine={rid} deterministic handler={handler_id} "
                f"source={claim['source'].get('kind')}"
            )
        candidate = claim["candidate"]
        prefetched_item = None
        if (
            claim["source"].get("kind") == "gmail"
            and (candidate.get("raw") or {}).get("_gmail_routed_thread") is True
        ):
            try:
                prefetched_item = claim["fetch"](
                    effective, claim["source"], candidate
                )
            except Exception as exc:
                totals["errors"] += 1
                hold_claim(claim)
                log(f"routine={rid} ERROR id={candidate['id']}: {exc}")
                continue
            candidate = dict(
                candidate,
                id=prefetched_item["id"],
                title=prefetched_item.get("title", candidate.get("title", "")),
            )
            claim = dict(claim, candidate=candidate)
        existing = processed.get(candidate["id"])
        managed_followup_claim = _is_managed_followup_claim(claim)
        followup_predecessor = (
            _gmail_followup_predecessor(processed, claim, existing)
            if managed_followup_claim
            else None
        )
        upgrade_followup = (
            existing is not None
            and _is_managed_followup_claim(claim)
            and existing.get("gmail_manual_chat_followup") is not True
        )
        upgrade_operator_confirmation = (
            existing is not None
            and existing.get("memory") == "skipped_not_worthy"
            and memory_sink.is_operator_confirmed_source_id(
                effective,
                (
                    memory_sink.source_id_for(prefetched_item)
                    if prefetched_item is not None
                    else _claim_source_id(claim)
                ),
            )
        )
        # A sink failure is never a completed item.  This applies equally to
        # bounded Gmail queries and catch-up connectors: otherwise Gmail can
        # be mutated while its durable memory is missing, and the ledger then
        # prevents the sink from ever being retried.
        retry_memory = (
            existing is not None
            and "memory_error" in existing
        )
        retry_expansion = (
            existing is not None
            and "expand_fallback" in existing
        )
        retry_calendar = (
            existing is not None
            and claim["source"].get("kind") == "mila"
            and existing.get("calendar_match_rejected") is True
        )
        if (
            existing is not None
            and not retry_memory
            and not retry_expansion
            and not retry_calendar
            and not upgrade_followup
            and not upgrade_operator_confirmation
        ):
            totals["skipped"] += 1
            continue
        if shared_circuit and not dry_run:
            hold_claim(claim)
            totals["skipped"] += 1
            if not circuit_reported:
                log(
                    f"routine={rid} dependency circuit open after: "
                    f"{shared_circuit['error']}; deferring remaining candidates"
                )
                circuit_reported = True
            continue
        if upgrade_followup:
            log(
                f"routine={rid} upgrading id={candidate['id']} to managed "
                "Gmail follow-up lifecycle"
            )
        elif upgrade_operator_confirmation:
            log(
                f"routine={rid} replaying id={candidate['id']} after "
                "explicit memory confirmation"
            )
        elif retry_memory:
            log(
                f"routine={rid} retrying id={candidate['id']} after memory error"
            )
        elif retry_expansion:
            log(
                f"routine={rid} retrying id={candidate['id']} after "
                "incomplete source expansion"
            )
        elif retry_calendar:
            log(
                f"routine={rid} retrying id={candidate['id']} after "
                "inconclusive Calendar match"
            )
        new += 1
        totals["matched"] += 1
        if lock:
            # Cheap per-item guard: if the lock file vanished we may no longer
            # be the only run, and continuing risks double-processing.
            lock.check()
        try:
            errors_before = totals["errors"]
            outcome_record = _process(
                effective, claim["source"], candidate, claim["fetch"], processed,
                label_catalog, dry_run, totals, catalog, base_dir,
                prefetched_item,
                followup_predecessor_entry_id=followup_predecessor,
                managed_followup_claim=managed_followup_claim,
            )
            if totals["errors"] > errors_before:
                hold_claim(claim)
            if (
                isinstance(outcome_record, dict)
                and outcome_record.get("memory_error")
                and _is_shared_dependency_failure(outcome_record["memory_error"])
            ):
                shared_circuit["error"] = outcome_record["memory_error"]
            totals["processed"] += 1
        except state.AlreadyRunning:
            raise
        except Exception as exc:  # per-item failures are isolated
            totals["errors"] += 1
            hold_claim(claim)
            log(f"routine={rid} ERROR id={candidate['id']}: {exc}")
            if _is_shared_dependency_failure(exc):
                shared_circuit["error"] = str(exc)[:300]
            if (
                claim["source"].get("kind") == "mila"
                and not dry_run
                and base_dir is not None
            ):
                raw = candidate.get("raw") or {}
                placeholder = {
                    "id": candidate["id"],
                    "source_id": raw.get("source_id"),
                    "frontmatter": {
                        "mila_recording_id": (
                            raw.get("recording") or {}
                        ).get("id"),
                        "mila_content_hash": raw.get("content_hash"),
                        "mila_recording_start": raw.get("recording_start"),
                    },
                }
                try:
                    mila_source.write_receipt(
                        base_dir, "failed", placeholder,
                        {
                            "failure_kind": "transient-error",
                            "error": str(exc)[:500],
                        },
                    )
                except Exception as receipt_exc:
                    # Receipts are observability, not a reason to let one item
                    # escape its isolation boundary and abort every routine.
                    log(
                        f"routine={rid} receipt ERROR id={candidate['id']}: "
                        f"{receipt_exc}"
                    )
    if new == 0:
        log(f"routine={rid} no new matches")
    return failed_source_keys


def _attach_related_memories(routine, source, item):
    """Give Chat items their conversation's recent durable memories as context.

    Without this, a terse acknowledgement ("done") is judged in isolation and
    discarded, losing the state transition of an already-captured request.
    Enrichment only: any lookup failure leaves the item unchanged.
    """
    if source.get("kind") != "gchat":
        return
    cfg = memory_sink.memory_cfg(routine)
    space = (item.get("frontmatter") or {}).get("gchat_space")
    if not cfg or not space:
        return
    prefix = f"gchat:{space.split('/')[-1]}:"
    try:
        related = memory_sink.recent_entries_for_prefix(
            cfg["store"], prefix, exclude_source_id=item.get("source_id"),
        )
    except Exception as exc:
        log(
            f"routine={routine['id']} WARN related-memory lookup failed "
            f"for {space}: {exc}"
        )
        return
    if related:
        item.setdefault("frontmatter", {})["related_memory_entries"] = related
        log(
            f"routine={routine['id']} id={item['id']} attached "
            f"{len(related)} related memory entr"
            f"{'y' if len(related) == 1 else 'ies'} for {space}"
        )


def _process(routine, source, candidate, fetch, processed, label_catalog,
             dry_run, totals, catalog=None, base_dir=None,
             prefetched_item=None, followup_predecessor_entry_id=None,
             managed_followup_claim=False):
    rid = routine["id"]
    handler_id = routine.get("_handler_id")
    action_list = config.source_actions(routine, source)
    handler_note = f" handler={handler_id}" if handler_id else ""
    log(
        f"routine={rid}{handler_note} new match id={candidate['id']} "
        f"title={candidate['title']!r}"
    )

    item = (
        prefetched_item
        if prefetched_item is not None
        else fetch(routine, source, candidate)
    )
    if followup_predecessor_entry_id or managed_followup_claim:
        item = dict(item)
        meta = dict(item.get("frontmatter") or {})
        if followup_predecessor_entry_id:
            meta["gmail_followup_predecessor_entry_id"] = (
                followup_predecessor_entry_id
            )
        if managed_followup_claim:
            meta.update({
                "gmail_manual_chat_followup": True,
                "gmail_chat_followup_managed": True,
                "gmail_chat_followup_active": True,
            })
            meta.setdefault(
                "gmail_thread_id",
                (candidate.get("raw") or {}).get("thread_id"),
            )
        item["frontmatter"] = meta
    meta = item.get("frontmatter") or {}
    item.setdefault("source_kind", source["kind"])
    static = _static_label(routine, item) if source["kind"] == "gmail" else None

    if dry_run:
        if source["kind"] == "mila":
            log(
                f"routine={rid} [dry-run] "
                f"{mila_source.dry_run_description(item)}"
            )
        dry_label = (
            static
            or ("<llm-chosen>" if routine["analyze"].get("pick_label") else None)
        )
        desc = ", ".join(
            actions.describe(action, dry_label) for action in action_list
        ) or "none"
        log(f"routine={rid} [dry-run] would analyze {len(item['body'])} chars via "
            f"provider={routine['analyze']['provider']} model={routine['analyze']['model']}")
        if routine.get("output"):
            log(f"routine={rid} [dry-run] would write {notes.target_path(routine, item)}")
        if memory_sink.memory_cfg(routine):
            memory_sink.capture(routine, item, "<summary>", dry_run=True)
        log(f"routine={rid} [dry-run] would apply: {desc}")
        return None

    calendar_match = None
    if source["kind"] == "mila":
        accepted, calendar_match = mila_source.match_calendar(
            routine, source, item
        )
        if not accepted:
            record = {
                "rule_id": rid,
                "source_kind": source["kind"],
                "source_id": item["source_id"],
                "processed_at": utc_now_iso(),
                "calendar_match_rejected": True,
                "calendar_match": calendar_match,
            }
            if handler_id:
                record["handler_id"] = handler_id
            if base_dir is not None:
                mila_source.write_receipt(
                    base_dir, "failed", item,
                    {
                        "failure_kind": "calendar-match",
                        "calendar_match": calendar_match,
                    },
                )
            processed.record(item["id"], record)
            log(
                f"routine={rid} id={item['id']} not captured: "
                f"Calendar match confidence={calendar_match['confidence']} "
                f"reason={calendar_match['reason']}"
            )
            return
        log(
            f"routine={rid} id={item['id']} Calendar match accepted "
            f"event={calendar_match['event_id']}"
        )

    _attach_related_memories(routine, source, item)
    prompt = llm.build_prompt(routine, item, label_catalog)
    content = llm.analyze(routine, prompt)
    summary, label = llm.split_label(content, label_catalog)
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
        "source_kind": source["kind"],
        "source_id": item.get("source_id"),
        "processed_at": utc_now_iso(),
        "output_file": str(path) if path else None,
        "gmail_label_applied": label,
    }
    if handler_id:
        record["handler_id"] = handler_id
    canonical_memory_source_id = memory_sink.source_id_for(item)
    active_followup = (
        meta.get("gmail_chat_followup_managed") is True
        and meta.get("gmail_chat_followup_active") is True
    )
    memory_source_id = (
        memory_sink.followup_source_id_for(item)
        if active_followup else canonical_memory_source_id
    )
    if memory_source_id:
        record["memory_source_id"] = memory_source_id
    meta = item.get("frontmatter") or {}
    operator_confirmed = memory_sink.is_operator_confirmed(routine, item)
    if operator_confirmed:
        record["memory_operator_confirmed"] = True
    if (
        meta.get("gmail_chat_followup_managed") is True
        and meta.get("gmail_chat_followup_active") is True
    ):
        record.update({
            "gmail_manual_chat_followup": True,
            "gmail_followup_open": True,
            "gmail_followup_title": item.get("title", ""),
            "gmail_thread_id": meta.get("gmail_thread_id"),
        })
        if canonical_memory_source_id:
            record["gmail_canonical_memory_source_id"] = (
                canonical_memory_source_id
            )
        predecessor = meta.get("gmail_followup_predecessor_entry_id")
        if isinstance(predecessor, str) and predecessor.strip():
            record["gmail_followup_predecessor_entry_id"] = (
                predecessor.strip()
            )

    # Memory sink runs after the vault note: the note is the expensive half and
    # the memory add is idempotent by source id, so a crash between the two is
    # healed by the next run re-capturing into the same entry.
    memory_failed = False
    if memory_sink.memory_cfg(routine):
        try:
            outcome = memory_sink.capture(routine, item, summary)
            if outcome:
                record.update(outcome)
            if (
                meta.get("gmail_chat_followup_managed") is True
                and meta.get("gmail_chat_followup_active") is True
                and (outcome or {}).get("memory") == "skipped_not_worthy"
            ):
                raise RuntimeError(
                    "an active self-forwarded Chat follow-up was rejected "
                    "as not memory-worthy"
                )
        except Exception as exc:
            record["memory_error"] = str(exc)[:300]
            memory_failed = True
            totals["errors"] += 1
            log(f"routine={rid} memory ERROR: {exc}")
            if action_list:
                log(
                    f"routine={rid} id={item['id']} withholding Gmail actions "
                    "until memory capture succeeds"
                )
    if item.get("expand_fallback"):
        # Queryable: `grep expand_fallback state/processed.json` lists every
        # item summarized from a stub, so they can be deleted and re-run once
        # the underlying document shows up.
        record["expand_fallback"] = item["expand_fallback"]
        totals["fallbacks"] += 1
        if action_list:
            log(
                f"routine={rid} id={item['id']} withholding Gmail actions "
                "until source expansion succeeds"
            )

    # Two-phase. The note on disk is the expensive, irreversible half, so it is
    # ledgered immediately — with the whole action list marked pending, so that
    # dying here leaves the triage recoverable rather than lost. Recording only
    # after triage would instead risk a duplicate note on the next run.
    actions_to_apply = [] if (memory_failed or item.get("expand_fallback")) else action_list
    if actions_to_apply:
        record["actions_pending"] = list(actions_to_apply)
    if source["kind"] == "mila" and base_dir is not None:
        receipt_status = "failed" if record.get("memory_error") else "processed"
        details = {
            "calendar_match": calendar_match,
            "memory": record.get("memory"),
            "memory_entry_id": record.get("memory_entry_id"),
        }
        if record.get("memory_error"):
            details.update({
                "failure_kind": "memory-error",
                "error": record["memory_error"],
            })
        mila_source.write_receipt(base_dir, receipt_status, item, details)
        processed.record_resolving(
            item["id"], record, item["source_id"]
        )
    elif not record.get("memory_error") and not record.get("expand_fallback"):
        processed.record_resolving(
            item["id"], record, record.get("memory_source_id") or item.get("source_id")
        )
    else:
        processed.record(item["id"], record)

    if actions_to_apply:
        applied, pending = actions.apply(item["id"], actions_to_apply, label)
        processed.record(item["id"], _with_action_outcome(record, applied, pending))
        if pending:
            totals["pending_actions"] += 1
    return record


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


_SUBJECT_REPORT_DATE = re.compile(
    r"(?<!\d)(?P<day>[0-3]?\d)[./-](?P<month>[01]?\d)[./-]"
    r"(?P<year>20\d{2}|\d{2})(?!\d)"
)
_SUBJECT_TEXT_REPORT_DATE = re.compile(
    r"(?<!\d)(?P<day>[0-3]?\d)(?:st|nd|rd|th)?\s+"
    r"(?P<month>january|february|march|april|may|june|july|august|"
    r"september|october|november|december)\s+"
    r"(?P<year>20\d{2}|\d{2})(?!\d)",
    re.IGNORECASE,
)
_MONTH_NUMBER = {
    name: number
    for number, name in enumerate(
        (
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november",
            "december",
        ),
        start=1,
    )
}


def _strip_quoted_history(body):
    """Return only the newest plain-text reply and whether history was removed.

    Gmail's plain-text body includes the complete quoted conversation. That is
    useful for ordinary correspondence but wrong when each reply is itself a
    fresh recurring report: the model would merge several weeks. This helper is
    intentionally opt-in through ``streams.*.message_updates``.
    """
    lines = (body or "").splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        lower = stripped.casefold()
        if re.match(r"^-{2,}\s*original message\s*-{2,}$", stripped, re.I):
            return "\n".join(lines[:index]).strip(), True

        # Outlook-style quoted headers are strong evidence. A naked `From:`
        # line in a report is not enough: require the adjacent Sent/Subject
        # fields as corroboration.
        if lower.startswith("from:") and _looks_like_quoted_header(lines, index):
            return "\n".join(lines[:index]).strip(), True

        # Some clients place an underscore rule before the same header block.
        # The rule alone may be legitimate report formatting, so only remove it
        # when the following non-empty line begins a corroborated header.
        if re.match(r"^_{5,}$", stripped):
            header_index = _next_nonempty_line(lines, index + 1)
            if (
                header_index is not None
                and lines[header_index].strip().casefold().startswith("from:")
                and _looks_like_quoted_header(lines, header_index)
            ):
                return "\n".join(lines[:index]).strip(), True

        # Gmail's "On <date>, <person> wrote:" attribution may wrap across a
        # few lines. Require the attribution *and* a following quoted `>` line;
        # `>10% growth` in fresh report content must never be a cutoff by itself.
        if lower.startswith("on "):
            for end in range(index, min(len(lines), index + 6)):
                attribution = " ".join(
                    part.strip() for part in lines[index:end + 1]
                )
                quote_index = _next_nonempty_line(lines, end + 1)
                if (
                    re.match(r"^On .+\bwrote:\s*$", attribution, re.I)
                    and quote_index is not None
                    and lines[quote_index].lstrip().startswith(">")
                ):
                    return "\n".join(lines[:index]).strip(), True

    return (body or "").strip(), False


def _next_nonempty_line(lines, start):
    for index in range(start, len(lines)):
        if lines[index].strip():
            return index
    return None


def _looks_like_quoted_header(lines, start):
    header = " ".join(
        candidate.strip().casefold()
        for candidate in lines[start:start + 8]
    )
    return " sent:" in f" {header}" and " subject:" in f" {header}"


def _report_date_from_subject(subject):
    """Return a valid day-first report date embedded in a subject, if any.

    Supports numeric D/M/YYYY and textual forms such as ``1st July 2026``.
    Invalid or vague periods are deliberately ignored so the normalized Gmail
    date remains the safe fallback.
    """
    matches = [
        (match, int(match.group("month")))
        for match in _SUBJECT_REPORT_DATE.finditer(subject or "")
    ]
    matches.extend(
        (match, _MONTH_NUMBER[match.group("month").casefold()])
        for match in _SUBJECT_TEXT_REPORT_DATE.finditer(subject or "")
    )
    for match, month in sorted(matches, key=lambda value: value[0].start()):
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        try:
            return datetime.date(
                year,
                month,
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            continue
    return None


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
    if not any(source.get("kind") == "gmail" for source in config.sources(routine)):
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


def _reconcile_gmail_chat_followups(routine, processed, dry_run=False):
    """Resolve tracked Chat follow-ups once their Gmail queue item is archived."""
    rid = routine["id"]
    tracked = [
        (item_id, entry)
        for item_id, entry in processed.items()
        if (
            entry.get("rule_id") == rid
            and entry.get("gmail_followup_open") is True
        )
    ]
    if not tracked:
        return 0

    active_threads = {
        item.get("thread_id") or item.get("message_id")
        for item in gmail.search(GMAIL_CHAT_FOLLOWUP_QUERY, 0)
    }
    resolved = 0
    failures = []
    for item_id, entry in tracked:
        thread_id = entry.get("gmail_thread_id") or item_id
        if thread_id in active_threads:
            continue
        memory_entry_id = entry.get("memory_entry_id")
        if not memory_entry_id:
            failures.append(
                f"tracked follow-up {item_id} left Inbox before its memory "
                "todo was created"
            )
            continue
        if dry_run:
            log(
                f"routine={rid} [dry-run] would resolve archived Gmail "
                f"follow-up {item_id} (memory={memory_entry_id})"
            )
            resolved += 1
            continue

        try:
            outcome = memory_sink.resolve_followup(
                routine,
                memory_entry_id=memory_entry_id,
                thread_id=thread_id,
                title=entry.get("gmail_followup_title") or "Chat follow-up",
            )
        except Exception as exc:
            failures.append(f"{item_id}: {exc}")
            continue
        updated = dict(entry)
        updated.update({
            "gmail_followup_open": False,
            "gmail_followup_resolved_at": utc_now_iso(),
            "gmail_followup_resolution_entry_id": outcome.get(
                "memory_entry_id"
            ),
        })
        processed.record(item_id, updated)
        resolved += 1
        log(
            f"routine={rid} resolved archived Gmail follow-up {item_id} "
            f"via {outcome.get('memory_entry_id') or 'memory'}"
        )
    if failures:
        raise RuntimeError(
            f"{len(failures)} follow-up(s) remain unresolved: "
            + "; ".join(failures[:3])
        )
    return resolved
