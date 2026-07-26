#!/usr/bin/env python3
"""workspace-daemon — run declarative Gmail → LLM → Obsidian routines.

Every routine is a drop-in file in routines/*.yaml. Adding one is never a code change.

  daemon.py list                       show routines, enabled state, last run
  daemon.py validate                   check all routine YAML
  daemon.py run [--routine ID] [-n]    process new matches (-n / --dry-run: no side effects)
  daemon.py new                        interactive scaffold for a new routine
"""
import argparse
import os
import re
import sys
from pathlib import Path

import yaml

from workspace_daemon import config, runner, state
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

def cmd_list(args):
    routines = config.discover(BASE_DIR)
    if not routines:
        print("no routines defined — run `daemon.py new` to scaffold one")
        return 0
    width = max(len(r["id"]) for r in routines)
    print(f"{'ROUTINE'.ljust(width)}  {'ENABLED':<8}  {'LAST RUN':<21}  DESCRIPTION")
    for r in routines:
        enabled = "yes" if r.get("enabled", True) else "no"
        last = state.last_run(BASE_DIR, r["id"]) or "never"
        desc = r.get("description", "")
        print(f"{r['id'].ljust(width)}  {enabled:<8}  {last:<21}  {desc}")
    return 0


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

def cmd_run(args):
    set_log_file(LOG_FILE)
    routines = config.discover(BASE_DIR, args.routine)
    problems = [p for r in routines for p in config.validate(r)]
    if problems:
        for p in problems:
            print(f"invalid routine: {p}", file=sys.stderr)
        return 1

    mode = " (dry-run — no LLM call, no Gmail mutation, no file write)" if args.dry_run else ""
    log(f"run start: {len(routines)} routine(s){mode}")
    try:
        totals = runner.run(BASE_DIR, routines, dry_run=args.dry_run)
    except state.AlreadyRunning as exc:
        # Not an error: launchd firing while a long run is still going is
        # expected, and the next interval will pick the work up.
        log(f"run skipped — {exc}")
        return 0

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

    path.write_text(yaml.safe_dump(routine, sort_keys=False, allow_unicode=True, width=100))
    print(f"\nwrote {path}")
    print(f"preview it with: ./daemon.py run --routine {routine_id} --dry-run")
    return 0


# --- entrypoint -------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="daemon.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show routines, enabled state, last run").set_defaults(func=cmd_list)
    sub.add_parser("validate", help="check all routine YAML").set_defaults(func=cmd_validate)
    sub.add_parser("new", help="interactive scaffold for a new routine").set_defaults(func=cmd_new)

    p_run = sub.add_parser("run", help="process new matches")
    p_run.add_argument("--routine", help="run only this routine id")
    p_run.add_argument("-n", "--dry-run", action="store_true",
                       help="preview only: no LLM call, no Gmail mutation, no file write")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (config.RoutineError, MissingBinary, state.StateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
