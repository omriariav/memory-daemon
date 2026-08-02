#!/usr/bin/env python3
"""workspace-daemon — run declarative Gmail → LLM → Obsidian routines.

Every routine is a drop-in file in routines/*.yaml. Adding one is never a code change.

  daemon.py list                       show routines, enabled state, last run
  daemon.py status                     show scheduler and routine health
  daemon.py validate                   check all routine YAML
  daemon.py run [--routine ID] [-n]    process new matches now
  daemon.py tick [-n]                  process only routines whose cadence is due
  daemon.py new                        interactive scaffold for a new routine
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

from workspace_daemon import config, runner, state, status
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


# --- validate ---------------------------------------------------------------

def cmd_validate(args):
    routines = config.discover(BASE_DIR)
    if not routines:
        print("no routines to validate")
        return 0
    all_problems = []
    for r in routines:
        problems = config.validate(r)
        source = Path(r["_source_file"]).name
        if problems:
            all_problems.extend(problems)
            print(f"✗ {source}")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"✓ {source}")
    if all_problems:
        print(f"\n{len(all_problems)} problem(s) found")
        return 1
    print(f"\n{len(routines)} routine(s) valid")
    return 0


# --- run --------------------------------------------------------------------

def _execution_groups(routines, active_ids):
    """Partition work by the lock that protects its mutable state."""
    selected = [
        routine for routine in routines
        if routine.get("enabled", True) and routine["id"] in active_ids
    ]
    return [
        (
            {
                routine["id"] for routine in selected
                if not config.is_maintenance(routine)
            },
            "run",
        ),
        (
            {
                routine["id"] for routine in selected
                if config.is_maintenance(routine)
                and (routine.get("maintenance") or {}).get("kind")
                != "slack_conversation_census"
            },
            "run",
        ),
        (
            {
                routine["id"] for routine in selected
                if config.is_maintenance(routine)
                and (routine.get("maintenance") or {}).get("kind")
                == "slack_conversation_census"
            },
            "slack-census",
        ),
    ]


def _manual_execution_groups(routines, active_ids):
    """Refresh census first, then preserve the runner's maintenance ordering."""
    capture, maintenance, census = _execution_groups(routines, active_ids)
    capture_ids, _ = capture
    maintenance_ids, _ = maintenance
    return [
        census,
        (capture_ids | maintenance_ids, "run"),
    ]


def _empty_totals():
    return {
        "matched": 0, "processed": 0, "skipped": 0, "errors": 0,
        "fallbacks": 0, "pending_actions": 0, "ambiguous": 0,
    }


def _merge_totals(totals, additions):
    for key, value in additions.items():
        if isinstance(value, (int, float)):
            totals[key] = totals.get(key, 0) + value


def cmd_run(args):
    set_log_file(LOG_FILE)
    if not args.dry_run:
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
    problems = [p for r in routines for p in config.validate(r)]
    if problems:
        for p in problems:
            log(f"invalid routine: {p}")
        return 1

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
    log(f"run start: {len(active_ids)} routine(s){mode}")
    totals = _empty_totals()
    for group_ids, lock_name in _manual_execution_groups(
        routines, active_ids
    ):
        if not group_ids:
            continue
        try:
            group_totals = runner.run(
                BASE_DIR, routines, dry_run=args.dry_run,
                refresh_labels=args.refresh_labels, active_ids=group_ids,
                lock_name=lock_name,
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
            continue
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
    return 1 if totals["errors"] else 0


def cmd_tick(args):
    """Run enabled routines whose individual cadence has elapsed."""
    set_log_file(LOG_FILE)
    if not args.dry_run:
        config.secure_routine_files(BASE_DIR)
    tick_id = uuid.uuid4().hex[:12]
    routines = config.discover(BASE_DIR)
    problems = [p for r in routines for p in config.validate(r)]
    if problems:
        for p in problems:
            log(f"invalid routine: {p}")
        return 1

    schedule = state.ScheduleStore(BASE_DIR, dry_run=args.dry_run)
    group = getattr(args, "group", "all")
    tick_name = f"tick[{tick_id}]"
    if group != "all":
        tick_name += f"({group})"
    selected = [
        r for r in routines
        if (
            r.get("enabled", True)
            and (
                group == "all"
                or (group == "capture" and not config.is_maintenance(r))
                or (group == "maintenance" and config.is_maintenance(r))
            )
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
            )
        except state.AlreadyRunning as exc:
            skipped_ids = ", ".join(sorted(group_ids))
            log(
                f"{tick_name} skipped routines={skipped_ids} — "
                f"{exc}{mode}"
            )
            continue
        schedule.mark_attempted(group_ids, now=group_started_epoch)
        _merge_totals(totals, group_totals)
    log(
        f"{tick_name} done: {totals['processed']} processed, "
        f"{totals['skipped']} already-seen, {totals['errors']} error(s)"
        f"{mode}"
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
    p_run.set_defaults(func=cmd_run)

    p_tick = sub.add_parser(
        "tick", help="run only enabled routines whose schedule is due"
    )
    p_tick.add_argument(
        "--group", choices=("all", "capture", "maintenance"), default="all",
        help="run all due routines, capture only, or maintenance only",
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
