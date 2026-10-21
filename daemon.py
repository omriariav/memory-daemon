#!/usr/bin/env python3
"""workspace-daemon — run declarative Gmail → LLM → Obsidian routines.

Every routine is a drop-in file in routines/*.yaml. Adding one is never a code change.

  daemon.py list                       show routines, enabled state, last run
  daemon.py status                     show scheduler and routine health
  daemon.py validate                   check all routine YAML
  daemon.py run [--routine ID] [-n]    process new matches now (--ignore-pause overrides state/PAUSED)
  daemon.py tick [-n]                  process only routines whose cadence is due
  daemon.py new                        interactive scaffold for a new routine
  daemon.py tui                        live terminal UI: status, pause, run, tick
  daemon.py ctl pause|resume|state     non-interactive kill switch and summary
  daemon.py backfill-owner-todos --routine ID --entry ENTRY [-n]
                                       file a stored meeting's owner actions as todos
"""
import argparse
import os
import re
import signal
import sys
import traceback
import uuid
from pathlib import Path
from time import time as current_epoch

import yaml

from workspace_daemon import config, identity, llm, memory_sink, runner, state, status, tui
from workspace_daemon.actions import VALID_ACTIONS
from workspace_daemon.shell import MissingBinary, log, set_log_file

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "logs" / "run.log"

# Scaffold defaults for `daemon.py new`. Override the vault location with
# WORKSPACE_DAEMON_VAULT_DIR so you never have to retype it.
DEFAULT_VAULT_DIR = os.environ.get("WORKSPACE_DAEMON_VAULT_DIR", str(Path.home() / "notes"))
DEFAULT_PROVIDER = os.environ.get("WORKSPACE_DAEMON_PROVIDER", "gemini")
DEFAULT_MODEL = os.environ.get("WORKSPACE_DAEMON_MODEL", "gemini/gemini-3.1-pro-preview")
DEFAULT_ACTIONS = ["apply_label", "mark_read", "unstar", "archive"]


# --- list -------------------------------------------------------------------

def _routine_last_run(routine, schedule):
    """Return the latest meaningful run marker for the routine type."""
    if config.is_maintenance(routine):
        return (
            (schedule.entries.get(routine["id"]) or {})
            .get("last_attempted_at")
        )
    return state.last_run(BASE_DIR, routine["id"])


def cmd_list(args):
    routines = config.discover(BASE_DIR)
    if not routines:
        print("no routines defined — run `daemon.py new` to scaffold one")
        return 0
    display_ids = [str(r.get("id", "")) for r in routines]
    cadence_labels = [config.schedule_label(r) for r in routines]
    schedule = state.ScheduleStore(BASE_DIR)
    width = max(len(rid) for rid in display_ids)
    cadence_width = max(7, *(len(label) for label in cadence_labels))
    print(
        f"{'ROUTINE'.ljust(width)}  {'ENABLED':<8}  "
        f"{'EVERY':<{cadence_width}}  "
        f"{'LAST RUN':<21}  DESCRIPTION"
    )
    for r, display_id, every in zip(
        routines, display_ids, cadence_labels
    ):
        enabled = "yes" if r.get("enabled", True) else "no"
        last = _routine_last_run(r, schedule) or "never"
        desc = r.get("description", "")
        print(
            f"{display_id.ljust(width)}  {enabled:<8}  "
            f"{every:<{cadence_width}}  {last:<21}  {desc}"
        )
    return 0


# --- status -----------------------------------------------------------------

def cmd_status(args):
    routines = config.discover(BASE_DIR)
    text, healthy = status.render(BASE_DIR, routines, label=args.label)
    print(text)
    return 0 if healthy else 1


# --- tui / ctl --------------------------------------------------------------

def cmd_tui(args):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("tui needs a terminal; use `daemon.py status` or `ctl state`",
              file=sys.stderr)
        return 2
    explicit = bool(args.base_dir or os.environ.get("MEMORY_DAEMON_DIR"))
    return tui.run_curses(
        Path(args.base_dir).expanduser() if args.base_dir else BASE_DIR,
        interval=args.interval, label_prefix=args.label_prefix,
        explicit=explicit)


def cmd_ctl(args):
    try:
        if args.action == "pause":
            created = state.pause(BASE_DIR, args.reason)
            print("paused" if created else "already paused")
            return 0
        if args.action == "resume":
            removed = state.resume(BASE_DIR)
            print("resumed" if removed else "was not paused")
            return 0
    except OSError as exc:
        print(f"error: cannot change state/PAUSED: {exc.strerror or exc}",
              file=sys.stderr)
        return 1
    snap = tui.Collector(BASE_DIR, args.label_prefix).snapshot()
    print(tui.summary_line(snap))
    return 0


# --- backfill-owner-todos ---------------------------------------------------

def cmd_backfill_owner_todos(args):
    set_log_file(LOG_FILE)
    routines = {r["id"]: r for r in config.discover(BASE_DIR)}
    routine = routines.get(args.routine)
    if routine is None:
        raise config.RoutineError(f"no routine with id '{args.routine}'")
    problems = config.validate(routine)
    if problems:
        for p in problems:
            log(f"invalid routine: {p}")
        return 1
    llm.configure(BASE_DIR)
    identity.configure(BASE_DIR)
    try:
        result = memory_sink.backfill_owner_todos(
            routine, args.entry, dry_run=args.dry_run
        )
    except RuntimeError as exc:
        log(f"error: {exc}")
        return 1
    verb = "would file" if args.dry_run else "filed"
    count = result.get("memory_owner_todos", 0)
    print(f"{verb} {count} owner todo(s) from {args.entry}")
    problems = result.get("memory_owner_todos_error", 0)
    if problems:
        print(f"{problems} todo(s) failed; see the log", file=sys.stderr)
        return 1
    return 0


# --- triage-report ----------------------------------------------------------

def cmd_triage_report(args):
    from workspace_daemon import local_triage
    entries = [e for _k, e in state.Store(BASE_DIR, dry_run=True).items()]
    matrix = local_triage.report(
        entries, days=args.days, min_confidence=args.min_confidence)
    print(local_triage.render_report(matrix))
    return 0


# --- validate ---------------------------------------------------------------

def cmd_validate(args):
    routines = config.discover(BASE_DIR)
    if not routines:
        print("no routines to validate")
        return 0
    all_problems = []
    cross = config.cross_problems(routines)
    for r in routines:
        problems = config.validate(r) + cross.get(r["id"], [])
        source = Path(r["_source_file"]).name
        if problems:
            all_problems.extend(problems)
            print(f"✗ {source}")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"✓ {source}")
    for warning in config.discovery_warnings(routines):
        print(f"warning: {warning}")
    if all_problems:
        print(f"\n{len(all_problems)} problem(s) found")
        return 1
    print(f"\n{len(routines)} routine(s) valid")
    return 0


# --- run --------------------------------------------------------------------

CENSUS_KIND = "slack_conversation_census"
CALENDAR_KIND = "calendar_cache"


def _maintenance_kind(routine):
    if not config.is_maintenance(routine):
        return None
    return (routine.get("maintenance") or {}).get("kind")


def _execution_groups(routines, active_ids):
    """Partition work by the lock that protects its mutable state.

    Each group runs under its own lock. The calendar cache is only scheduled
    by the `calendar` tick job (or `all`), so a long Slack census in the
    `maintenance` job never delays it; a manual `run` orders census first.
    """
    selected = [
        routine for routine in routines
        if routine.get("enabled", True) and routine["id"] in active_ids
    ]

    def ids(predicate):
        return {r["id"] for r in selected if predicate(_maintenance_kind(r))}

    return [
        (ids(lambda kind: kind is None), "run"),
        (
            ids(lambda kind: kind is not None
                and kind not in (CENSUS_KIND, CALENDAR_KIND)),
            "run",
        ),
        # Within one `all` tick the cheap calendar refresh runs before the census.
        (ids(lambda kind: kind == CALENDAR_KIND), "calendar-cache"),
        (ids(lambda kind: kind == CENSUS_KIND), "slack-census"),
    ]


def _in_tick_group(routine, group):
    """Whether `tick --group <group>` considers this routine."""
    kind = _maintenance_kind(routine)
    if group == "capture":
        return kind is None
    if group == "calendar":
        return kind == CALENDAR_KIND
    if group == "maintenance":
        return kind is not None and kind != CALENDAR_KIND
    return True


def _manual_execution_groups(routines, active_ids):
    """Refresh census and calendar first, then the runner's maintenance order."""
    capture, maintenance, calendar, census = _execution_groups(
        routines, active_ids
    )
    capture_ids, _ = capture
    maintenance_ids, _ = maintenance
    return [
        census,
        calendar,
        (capture_ids | maintenance_ids, "run"),
    ]


def _empty_totals():
    return {
        "matched": 0, "processed": 0, "skipped": 0, "errors": 0,
        "fallbacks": 0, "pending_actions": 0, "ambiguous": 0,
        "trivial": 0, "extract_failed": 0,
    }


def _merge_totals(totals, additions):
    for key, value in additions.items():
        if isinstance(value, (int, float)):
            totals[key] = totals.get(key, 0) + value


def _count_only(routines, active_ids, ignore_pause=False):
    """List-only pass over capture routines: no lock, ledger, cursor or store write."""
    capture = {
        r["id"] for r in routines
        if r["id"] in active_ids and not config.is_maintenance(r)
    }
    totals = runner.run(
        BASE_DIR, routines, dry_run=True, active_ids=capture, count_only=True,
        ignore_pause=ignore_pause,
    )
    return 1 if totals["errors"] else 0


def _pause_reason(args, prefix=""):
    """Log and return the kill-switch reason when this invocation must not run."""
    reason = state.paused(BASE_DIR)
    if reason and not getattr(args, "ignore_pause", False):
        log(f"{prefix}paused: {reason}")
        return reason
    return None


def _retry_unconfirmed(args, routines):
    """One-shot: forget unconfirmed Fibery export attempts, then run on."""
    if not args.routine or args.dry_run:
        raise config.RoutineError(
            "--retry-unconfirmed needs --routine and cannot be a dry run"
        )
    routine = next((r for r in routines if r["id"] == args.routine), None)
    if routine is None:
        raise config.RoutineError(f"no routine with id '{args.routine}'")
    maintenance_cfg = routine.get("maintenance")
    if not (isinstance(maintenance_cfg, dict)
            and maintenance_cfg.get("kind") == "fibery_tasks_outbound"):
        raise config.RoutineError(
            "--retry-unconfirmed only applies to a fibery_tasks_outbound routine"
        )
    from workspace_daemon import fibery_tasks_outbound
    ids = [i.strip() for i in args.retry_unconfirmed.split(",") if i.strip()]
    path = Path(maintenance_cfg.get(
        "checkpoint", fibery_tasks_outbound.DEFAULT_CHECKPOINT)).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    cleared = fibery_tasks_outbound.clear_unconfirmed(path, ids)
    log(f"routine={args.routine} fibery_tasks_outbound: cleared unconfirmed "
        f"for retry: {', '.join(cleared) or 'none'}")


def cmd_run(args):
    set_log_file(LOG_FILE)
    count_only = getattr(args, "count_only", False)
    if count_only and args.dry_run:
        raise config.RoutineError(
            "--count-only and --dry-run cannot be combined: a dry run makes "
            "no connector call, so it has nothing to count"
        )
    if _pause_reason(args):
        return 0
    if not args.dry_run and not count_only:
        config.secure_routine_files(BASE_DIR)
    routines = config.discover(BASE_DIR)
    if args.routine and args.routine not in {r["id"] for r in routines}:
        raise config.RoutineError(f"no routine with id '{args.routine}'")
    if args.include_disabled and not args.routine:
        raise config.RoutineError(
            "--include-disabled requires --routine so a broad manual run "
            "cannot accidentally arm parked routines"
        )
    if args.include_disabled:
        routines = [
            dict(routine, enabled=True)
            if routine["id"] == args.routine else routine
            for routine in routines
        ]
    cross = config.cross_problems(routines)
    problems = [p for r in routines
                for p in config.validate(r) + cross.get(r["id"], [])]
    if problems:
        for p in problems:
            log(f"invalid routine: {p}")
        return 1

    if getattr(args, "retry_unconfirmed", None):
        _retry_unconfirmed(args, routines)
    mode = (
        " (dry-run — no LLM call, source mutation, or data/state write; "
        "operational log only)"
        if args.dry_run else ""
    )
    active_ids = (
        {args.routine}
        if args.routine else {
            routine["id"] for routine in routines
            if routine.get("enabled", True)
        }
    )
    if count_only:
        return _count_only(
            routines, active_ids, getattr(args, "ignore_pause", False)
        )
    log(f"run start: {len(active_ids)} routine(s){mode}")
    totals = _empty_totals()
    attempted = locked = 0
    for group_ids, lock_name in _manual_execution_groups(
        routines, active_ids
    ):
        if not group_ids:
            continue
        attempted += 1
        try:
            group_totals = runner.run(
                BASE_DIR, routines, dry_run=args.dry_run,
                refresh_labels=args.refresh_labels, active_ids=group_ids,
                lock_name=lock_name,
                ignore_pause=getattr(args, "ignore_pause", False),
            )
        except state.AlreadyRunning as exc:
            # Manual and scheduled census runs share slack-census.lock; other
            # work shares run.lock. A busy group is deferred without blocking
            # an independent group in the same manual invocation.
            skipped_ids = ", ".join(sorted(group_ids))
            log(
                f"run group={lock_name} skipped routines={skipped_ids} — "
                f"{exc}"
            )
            locked += 1
            continue
        except state.Paused as exc:
            log(f"run stopped: {exc}")
            return 0
        _merge_totals(totals, group_totals)

    verb = "would process" if args.dry_run else "processed"
    summary = (
        f"{totals['processed']} {verb}, {totals['skipped']} already-seen, "
        f"{totals['errors']} error(s)"
    )
    if totals.get("fallbacks"):
        # A fallback is a silent quality cliff, so it gets its own line in the
        # summary rather than being buried in the per-item log.
        summary += (
            f", {totals['fallbacks']} summarized from a stub "
            f"(grep expand_fallback state/processed.json)"
        )
    if totals.get("pending_actions"):
        summary += (
            f", {totals['pending_actions']} with triage still pending "
            f"(retried automatically next run)"
        )
    log(f"run done: {summary}")
    if attempted and locked == attempted:
        # Nothing ran: every group was busy. Distinct from a clean run.
        return state.EXIT_LOCKED
    return 1 if totals["errors"] else 0


def cmd_tick(args):
    """Run enabled routines whose individual cadence has elapsed."""
    set_log_file(LOG_FILE)
    tick_id = uuid.uuid4().hex[:12]
    group = getattr(args, "group", "all")
    tick_name = f"tick[{tick_id}]"
    if group != "all":
        tick_name += f"({group})"
    # Tick-prefixed so `status` reads a paused tick as a healthy no-op.
    if _pause_reason(args, f"{tick_name}: "):
        return 0
    if not args.dry_run:
        config.secure_routine_files(BASE_DIR)
    routines = config.discover(BASE_DIR)
    cross = config.cross_problems(routines)
    problems = [p for r in routines
                for p in config.validate(r) + cross.get(r["id"], [])]
    if problems:
        for p in problems:
            log(f"invalid routine: {p}")
        return 1

    schedule = state.ScheduleStore(BASE_DIR, dry_run=args.dry_run)
    selected = [
        r for r in routines
        if (
            r.get("enabled", True)
            and _in_tick_group(r, group)
        )
    ]
    due = [
        r for r in selected
        if schedule.due(r)
    ]
    if not due:
        mode = " (dry-run)" if args.dry_run else ""
        log(f"{tick_name}: no routines due{mode}")
        return 0

    due_ids = {r["id"] for r in due}
    mode = " (dry-run)" if args.dry_run else ""
    log(f"{tick_name}: due={', '.join(sorted(due_ids))}{mode}")
    totals = _empty_totals()
    # Run latency-sensitive capture before long maintenance (the Slack census
    # can take tens of minutes). Persist each group's attempt immediately, so
    # the next tick cannot repeat capture merely because maintenance was slow.
    groups = _execution_groups(routines, due_ids)
    paused_mid_tick = False
    for group_ids, lock_name in groups:
        if not group_ids:
            continue
        # Cadence is measured from the attempt's start. A 40-minute census on
        # a one-day schedule must not silently become a 24h40m schedule.
        group_started_epoch = current_epoch()
        try:
            group_totals = runner.run(
                BASE_DIR, routines, dry_run=args.dry_run,
                refresh_labels=args.refresh_labels, active_ids=group_ids,
                lock_name=lock_name,
                ignore_pause=getattr(args, "ignore_pause", False),
            )
        except state.AlreadyRunning as exc:
            skipped_ids = ", ".join(sorted(group_ids))
            log(
                f"{tick_name} skipped routines={skipped_ids} — "
                f"{exc}{mode}"
            )
            continue
        except state.Paused as exc:
            # Stop here, but close the tick normally so status sees a finished
            # (paused) tick rather than an incomplete one.
            log(f"{tick_name} stopped early — {exc}{mode}")
            paused_mid_tick = True
            break
        # A routine held by a cap or the pause switch did not really attempt.
        held = set(group_totals.get("held_ids", ()))
        schedule.mark_attempted(group_ids - held, now=group_started_epoch)
        _merge_totals(totals, group_totals)
    log(
        f"{tick_name} done: {totals['processed']} processed, "
        f"{totals['skipped']} already-seen, {totals['errors']} error(s)"
        f"{' (paused)' if paused_mid_tick else ''}{mode}"
    )
    return 1 if totals["errors"] else 0


# --- new --------------------------------------------------------------------

def _ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default
        print("  (required)")


def _slug(text):
    return re.sub(r"[\s_]+", "-", re.sub(r"[^a-zA-Z0-9\s_-]", "", text).strip().lower())


def cmd_new(args):
    print("Scaffold a new routine. Enter accepts the [default].\n")
    routine_id = _slug(_ask("Routine id (kebab-case)"))
    path = config.routines_dir(BASE_DIR) / f"{routine_id}.yaml"
    if path.exists():
        print(f"error: {path} already exists", file=sys.stderr)
        return 1

    description = _ask("Description", f"Routine {routine_id}")
    query = _ask("Gmail query (e.g. from:x@y.com is:unread)")
    max_results = int(_ask("Max results per run", "20"))
    provider = _ask("LLM provider", DEFAULT_PROVIDER)
    model = _ask("LLM model", DEFAULT_MODEL)
    domains_raw = _ask("Focus domains (comma-separated, or '-' for none)", "-")
    instruction = _ask("Analysis instruction", "Summarize this email for a product leader.")
    pick_label = _ask("Let the LLM pick a Gmail label? (y/n)", "y").lower().startswith("y")
    vault_dir = _ask("Vault output directory", DEFAULT_VAULT_DIR)
    slug_prefix = _ask("Note filename slug prefix", routine_id)
    actions_raw = _ask(
        f"Actions (comma-separated from: {', '.join(sorted(VALID_ACTIONS))}; '-' for none)",
        ", ".join(DEFAULT_ACTIONS) if pick_label else "mark_read, archive",
    )

    domains = [] if domains_raw == "-" else [d.strip() for d in domains_raw.split(",") if d.strip()]
    actions = [] if actions_raw == "-" else [a.strip() for a in actions_raw.split(",") if a.strip()]

    analyze = {"provider": provider, "model": model, "max_output_tokens": 4096}
    if pick_label:
        analyze["pick_label"] = True
    if domains:
        analyze["focus_domains"] = domains
    analyze["instruction"] = instruction

    routine = {
        "id": routine_id,
        "enabled": True,
        "description": description,
        "source": {"kind": "gmail", "query": query, "max_results": max_results},
        "analyze": analyze,
        "output": {"vault_dir": vault_dir, "slug_prefix": slug_prefix},
        "actions": actions,
    }

    problems = config.validate(routine)
    if problems:
        print("\nrefusing to write — the answers produce an invalid routine:", file=sys.stderr)
        for p in problems:
            print(f"    {p}", file=sys.stderr)
        return 1

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    state.write_atomic(
        path,
        yaml.safe_dump(routine, sort_keys=False, allow_unicode=True, width=100),
        mode=0o600,
    )
    print(f"\nwrote {path}")
    print(f"preview it with: ./daemon.py run --routine {routine_id} --dry-run")
    return 0


# --- entrypoint -------------------------------------------------------------

def _on_sigterm(signum, frame):
    raise SystemExit(143)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="daemon.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show routines, enabled state, last run").set_defaults(func=cmd_list)
    p_status = sub.add_parser(
        "status", help="show launchd and per-routine health"
    )
    p_status.add_argument(
        "--label",
        default=os.environ.get(
            "MEMORY_DAEMON_LAUNCHD_LABEL", status.DEFAULT_LAUNCHD_LABEL
        ),
        help="launchd label (default: %(default)s)",
    )
    p_status.set_defaults(func=cmd_status)
    default_prefix = os.environ.get(
        "MEMORY_DAEMON_LAUNCHD_LABEL", status.DEFAULT_LAUNCHD_LABEL
    )
    p_tui = sub.add_parser("tui", help="live terminal UI (status + controls)")
    p_tui.add_argument("--interval", type=float, default=2.0,
                       help="refresh seconds (default: %(default)s)")
    p_tui.add_argument("--label-prefix", default=default_prefix,
                       help="show every loaded launchd job whose label starts "
                            "with this (default: %(default)s)")
    p_tui.add_argument("--base-dir", help="control this checkout instead of "
                       "the one launchd runs (the UI then refuses to act "
                       "if they differ)")
    p_tui.set_defaults(func=cmd_tui)
    p_ctl = sub.add_parser("ctl", help="pause, resume, or summarize state")
    p_ctl.add_argument("action", choices=("pause", "resume", "state"))
    p_ctl.add_argument("--reason", default="paused from the command line",
                       help="first line written to state/PAUSED")
    p_ctl.add_argument("--label-prefix", default=default_prefix,
                       help="launchd label prefix for `state` (default: %(default)s)")
    p_ctl.set_defaults(func=cmd_ctl)
    p_bf = sub.add_parser(
        "backfill-owner-todos",
        help="file the owner's action items of an already-captured meeting "
             "entry as todos (from its stored body)",
    )
    p_bf.add_argument("--routine", required=True,
                      help="capture routine whose memory block to use")
    p_bf.add_argument("--entry", required=True, help="meeting memory entry id")
    p_bf.add_argument("-n", "--dry-run", action="store_true",
                      help="extract and count, but write nothing to the store")
    p_bf.set_defaults(func=cmd_backfill_owner_todos)
    p_tr = sub.add_parser(
        "triage-report",
        help="agreement of the local triage label with the paid outcome",
    )
    p_tr.add_argument("--days", type=int, default=None,
                      help="only ledger rows from the last N days")
    p_tr.add_argument("--min-confidence", type=float, default=0.85,
                      help="split small_talk rows at this confidence "
                           "(default: %(default)s)")
    p_tr.set_defaults(func=cmd_triage_report)
    sub.add_parser("validate", help="check all routine YAML").set_defaults(func=cmd_validate)
    sub.add_parser("new", help="interactive scaffold for a new routine").set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="process new matches")
    p_run.add_argument("--routine", help="run only this routine id")
    p_run.add_argument(
        "--include-disabled",
        action="store_true",
        help="explicitly run the selected disabled routine without arming it",
    )
    p_run.add_argument("-n", "--dry-run", action="store_true",
                       help="preview only: no LLM/source mutation or data/state write; "
                            "operational log only")
    p_run.add_argument("--refresh-labels", action="store_true",
                       help="refetch the Gmail label catalog instead of using the cache")
    p_run.add_argument("--count-only", action="store_true",
                       help="list only: report new/changed items and a cost "
                            "estimate; no ledger, cursor or store write")
    p_run.add_argument("--retry-unconfirmed", metavar="ID[,ID...]|heading",
                       help="one-shot, with --routine on a fibery_tasks_outbound "
                            "routine: forget those unconfirmed export attempts "
                            "(entry ids and/or the word heading) before the run")
    p_run.add_argument("--ignore-pause", action="store_true",
                       help="run even though state/PAUSED exists (manual override)")
    p_run.set_defaults(func=cmd_run)

    p_tick = sub.add_parser(
        "tick", help="run only enabled routines whose schedule is due"
    )
    p_tick.add_argument(
        "--group", choices=("all", "capture", "maintenance", "calendar"), default="all",
        help="run all due routines, capture only, maintenance only (without "
             "the calendar cache), or the calendar cache only",
    )
    p_tick.add_argument(
        "-n", "--dry-run", action="store_true",
        help="preview due routines without LLM/source mutation or data/state "
             "writes; operational log only",
    )
    p_tick.add_argument(
        "--refresh-labels", action="store_true",
        help="refetch the Gmail label catalog instead of using the cache",
    )
    p_tick.set_defaults(func=cmd_tick)

    # launchd sends SIGTERM on unload, logout, or timeout. CPython installs no
    # handler for it, so the process would die without unwinding — skipping the
    # temp-file cleanup in write_atomic. Turning it into SystemExit lets the
    # normal exception paths run.
    signal.signal(signal.SIGTERM, _on_sigterm)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (config.RoutineError, MissingBinary, state.StateError) as exc:
        log(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        log("aborted")
        return 130
    except SystemExit as exc:
        log(f"terminated by signal (exit {exc.code})")
        raise
    except Exception as exc:
        # launchd stdout/stderr intentionally go to /dev/null; unexpected
        # failures must still leave a private, actionable traceback.
        log(f"unhandled ERROR: {exc}\n{traceback.format_exc().rstrip()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
