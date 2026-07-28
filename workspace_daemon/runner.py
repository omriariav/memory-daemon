"""The run loop: match → analyze → write note → ledger → triage → ledger outcome.

The ledger entry is written before triage and updated after it, so an item is
never summarized twice and never left with silently unfinished Gmail actions.
"""
import datetime
import re
from contextlib import ExitStack
from email.utils import getaddresses

from . import actions, config, contacts, drive, gchat_source, gmail, labels, llm, memory_sink, notes, slack_source, state, time_utils
from .shell import log, utc_now_iso

MAX_GMAIL_SOURCE_PEOPLE = 20


def _needs_label_catalog(routines):
    """Both LLM-chosen and configured labels are validated against the catalog."""
    return any(
        (r.get("analyze", {}).get("pick_label") or config.configured_labels(r))
        and any(source.get("kind") == "gmail" for source in config.sources(r))
        for r in routines
    )


def run(base_dir, routines, dry_run=False, refresh_labels=False, active_ids=None):
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
        lock = stack.enter_context(state.RunLock(base_dir)) if not dry_run else None
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
        for vault in {r.get("output", {}).get("vault_dir") for r in active}:
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
            if catalog is not None:
                for name in config.configured_labels(routine):
                    _validated_label(name, label_catalog, routine["id"], catalog)
            _retry_pending_actions(routine, processed, dry_run, totals)
            valid.append(routine)
        except Exception as exc:  # a broken routine must not abort the rest
            totals["errors"] += 1
            log(f"routine={routine.get('id', '?')} FATAL: {exc}")
            routing_failures.extend(_routine_failures(routine))

    active_source_kinds = {
        source["kind"]
        for routine in valid
        if routine["id"] in active_ids
        for source in config.sources(routine)
    }
    source_overrides = {}
    catch_up_sources = []
    for routine in valid:
        if routine["id"] not in active_ids:
            continue
        for source_index, source in enumerate(config.sources(routine)):
            if source.get("catch_up") is not True:
                continue
            kind = source["kind"]
            cursor_id = f"{kind}:all-spaces"
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
                since = source.get("batch_messages_after")
            if since:
                source_overrides[(routine["id"], source_index)] = {
                    "_since": since,
                }
                log(
                    f"routine={routine['id']} source={kind} catch-up since={since}"
                )
            catch_up_sources.append((routine["id"], cursor_id, kind))

    claims, listing_failures = _collect_claims(
        valid, totals, source_kinds=active_source_kinds,
        routing_context=routines,
        source_overrides=source_overrides,
    )
    owned = _route_claims(
        claims, totals, failures=[*routing_failures, *listing_failures]
    )
    valid_ids = {routine["id"] for routine in valid}

    for routine in active:
        if routine["id"] not in valid_ids:
            continue
        rid = routine["id"]
        routine_claims = owned.get(rid, [])
        _run_owned(
            routine, routine_claims, processed, label_catalog, dry_run,
            totals, lock, catalog,
        )

    if catch_up_sources:
        if totals["errors"] == 0:
            cursors.mark_successful(catch_up_sources, scan_started_at)
            mode = "[dry-run] would advance" if dry_run else "advanced"
            log(
                f"catch-up cursor {mode} to {scan_started_at} for "
                f"{len(catch_up_sources)} source(s)"
            )
        else:
            log(
                f"catch-up cursor held at prior checkpoint due to "
                f"{totals['errors']} error(s)"
            )

    return totals


# --- sources ----------------------------------------------------------------
# A source lists candidates cheaply (id + title only), then fetches one item's
# full content on demand. Listing stays cheap so dedupe can skip most work.

def _gmail_candidates(source):
    return [
        {"id": t["message_id"], "title": t.get("subject", ""), "raw": t}
        for t in gmail.search(source["query"], source.get("max_results", 20))
    ]


def _email_source_people(headers):
    """Verified-identity candidates from structured Gmail address headers.

    These addresses are not trusted as person slugs by themselves.  The memory
    sink resolves each exact address through the Workspace directory before it
    lets the extraction model link the person.
    """
    people = []
    seen = set()
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


def _gmail_fetch(routine, source, candidate):
    message_id = candidate["id"]
    msg = gmail.read_message(message_id)
    headers = msg.get("headers", {})
    body = msg.get("body") or ""
    thread_id = candidate["raw"].get("thread_id", message_id)
    subject = headers.get("subject", "")
    date = notes.email_date(headers)
    source_people, source_people_truncated = _email_source_people(headers)

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
            "email_subject": subject,
            "email_date": headers.get("date", ""),
            "source_people": source_people,
            "source_people_truncated": source_people_truncated,
        },
    }

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
              lambda routine, source, candidate: slack_source.fetch(routine, candidate)),
    "gchat": (gchat_source.candidates,
              lambda routine, source, candidate: gchat_source.fetch(routine, candidate)),
}


# --- candidate ownership and run loop --------------------------------------

def _scope(source):
    if source.get("kind") == "gchat" and source.get("all_spaces"):
        return "all active Google Chat conversations"
    slack_channels = [
        channel
        for key in ("channels", "ada_channels", "private_channels")
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
    return candidate["id"]


_SOURCE_DEFAULT_LIMITS = {
    "gmail": 20,
    "drive_docs": 50,
    "slack": 30,
    "gchat": 50,
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
            for key in ("channels", "ada_channels", "private_channels")
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


def _failure(routine, source=None, known_ids=None):
    return {
        "routine_id": str(routine.get("id", "?")),
        "rank": _safe_routing_rank(routine),
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

    failures = [
        _failure(routine, source)
        for source in source_values
        if isinstance(source, dict) and source.get("kind") in config.VALID_SOURCE_KINDS
    ]
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


def _collect_claims(routines, totals, source_kinds=None, routing_context=None,
                    source_overrides=None):
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
        if kind == "gchat" and source.get("all_spaces"):
            # A configured explicit space remains owned even while its domain
            # routine is disabled or not due. The broad fallback must not race
            # it now and create memories with the wrong prompt before that
            # routine is armed.
            listing_source = dict(
                listing_source,
                _exclude_spaces=sorted(claimed_gchat_spaces),
            )
        log(f"routine={rid} querying {kind}: {_scope(source)}")
        try:
            candidates = list_candidates(listing_source)
        except Exception as exc:
            totals["errors"] += 1
            log(f"routine={rid} source={kind} FATAL: {exc}")
            failures.append(_failure(routine, source))
            continue

        log(f"routine={rid} source={kind} {len(candidates)} item(s) matched")
        normal_ids = {_routing_id(candidate) for candidate in candidates}

        discovery = []
        discovery_limit = _ownership_limit(source, all_sources)
        source_limit = _source_limit(source)
        if source_limit and discovery_limit > source_limit:
            expanded_source = dict(
                listing_source, max_results=discovery_limit
            )
            log(
                f"routine={rid} source={kind} expanding ownership scan "
                f"to {discovery_limit}"
            )
            try:
                discovery = list_candidates(expanded_source)
            except Exception as exc:
                totals["errors"] += 1
                log(
                    f"routine={rid} source={kind} ownership scan FATAL: {exc}"
                )
                failures.append(
                    _failure(routine, source, known_ids=normal_ids)
                )

        candidates_with_budget = [(candidate, True) for candidate in candidates]
        candidates_with_budget.extend(
            (candidate, False)
            for candidate in discovery
            if _routing_id(candidate) not in normal_ids
        )
        for candidate, processable in candidates_with_budget:
            key = (kind, _routing_id(candidate))
            claims.setdefault(key, []).append({
                "routine": routine,
                "source": source,
                "source_index": source_index,
                "candidate": candidate,
                "fetch": fetch,
                "processable": processable,
            })
    return claims, failures


def _route_claims(claims, totals, failures=()):
    """Choose exactly one routine for every source candidate.

    A specific routine always beats a fallback. Explicit lower priority wins
    within either class. Equal-ranked distinct owners are ambiguous and skipped
    rather than letting routine file order choose the extraction prompt.
    """
    owned = {}
    for (kind, item_id), candidates in claims.items():
        # Multiple source blocks in one routine may match the same item. That is
        # one owner; keep the first declared block for deterministic actions.
        by_routine = {}
        for claim in candidates:
            rid = claim["routine"]["id"]
            if (
                rid not in by_routine
                or (
                    not by_routine[rid].get("processable", True)
                    and claim.get("processable", True)
                )
            ):
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
        blockers = {
            failure["routine_id"]
            for failure in failures
            if failure["rank"] <= best_rank
            and item_id not in failure["known_ids"]
            and _scopes_overlap(
                failure["scopes"],
                _candidate_scopes(claim["source"], claim["candidate"]),
            )
        }
        if blockers:
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
        owned.setdefault(claim["routine"]["id"], []).append(claim)
    return owned


def _run_owned(routine, claims, processed, label_catalog, dry_run, totals,
               lock=None, catalog=None):
    rid = routine["id"]
    log(f"routine={rid} {len(claims)} owned item(s)")
    new = 0
    for claim in claims:
        candidate = claim["candidate"]
        existing = processed.get(candidate["id"])
        retry_memory = (
            existing is not None
            and claim["source"].get("catch_up") is True
            and "memory_error" in existing
        )
        if existing is not None and not retry_memory:
            totals["skipped"] += 1
            continue
        if retry_memory:
            log(
                f"routine={rid} retrying id={candidate['id']} after memory error"
            )
        new += 1
        totals["matched"] += 1
        if lock:
            # Cheap per-item guard: if the lock file vanished we may no longer
            # be the only run, and continuing risks double-processing.
            lock.check()
        try:
            _process(
                routine, claim["source"], candidate, claim["fetch"], processed,
                label_catalog, dry_run, totals, catalog,
            )
            totals["processed"] += 1
        except state.AlreadyRunning:
            raise
        except Exception as exc:  # per-item failures are isolated
            totals["errors"] += 1
            log(f"routine={rid} ERROR id={candidate['id']}: {exc}")
    if new == 0:
        log(f"routine={rid} no new matches")


def _process(routine, source, candidate, fetch, processed, label_catalog,
             dry_run, totals, catalog=None):
    rid = routine["id"]
    action_list = config.source_actions(routine, source)
    log(f"routine={rid} new match id={candidate['id']} title={candidate['title']!r}")

    item = fetch(routine, source, candidate)
    item.setdefault("source_kind", source["kind"])
    static = _static_label(routine, item) if source["kind"] == "gmail" else None

    if dry_run:
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
        return

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
