"""Scheduled deterministic maintenance and cross-system sync routines."""
import json
from pathlib import Path

from . import google_tasks_sync, slack_census, slack_cli
from .shell import log


def _path(base_dir, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(base_dir) / path


def run(base_dir, routine, dry_run=False):
    """Execute one validated maintenance routine and return its report."""
    routine_id = routine["id"]
    cfg = routine["maintenance"]
    kind = cfg["kind"]
    if kind == "google_tasks_sync":
        # A preview needs the real baseline to distinguish which side changed;
        # google_tasks_sync.run guarantees it never saves that checkpoint in
        # dry-run mode.
        checkpoint = _path(base_dir, cfg["checkpoint"])
        mode = "previewing" if dry_run else "syncing"
        log(f"routine={routine_id} maintenance={kind} {mode}")
        report = google_tasks_sync.run(
            cfg,
            checkpoint_path=checkpoint,
            dry_run=dry_run,
        )
        log(
            f"routine={routine_id} maintenance={kind} "
            f"tasklists={report['tasklist_count']} "
            f"google_open={report['open_google_tasks']} "
            f"memory_open={report['open_memory_todos']} "
            f"planned={len(report['planned'])} "
            f"conflicts={report['conflicts']} errors={len(report['errors'])}"
        )
        for row in report["planned"]:
            log(
                f"routine={routine_id} maintenance={kind} plan="
                + json.dumps(row, ensure_ascii=False, sort_keys=True)
            )
        for row in report["errors"]:
            log(
                f"routine={routine_id} maintenance={kind} error="
                + json.dumps(row, ensure_ascii=False, sort_keys=True)
            )
        if not report["ok"]:
            raise RuntimeError(
                "Google Tasks sync requires attention: "
                f"{report['conflicts']} conflict(s), "
                f"{len(report['errors'])} error(s)"
            )
        return report
    if kind != "slack_conversation_census":
        raise RuntimeError(f"unsupported maintenance kind {kind!r}")

    checkpoint = None
    if not dry_run:
        checkpoint = _path(base_dir, cfg["checkpoint"])
    mode = "previewing" if dry_run else "refreshing"
    log(
        f"routine={routine_id} maintenance={kind} {mode} "
        f"{float(cfg.get('hours', 48)):g}h fixed-window census"
    )
    report = slack_cli.run_census(
        hours=float(cfg.get("hours", 48)),
        requests_per_minute=int(cfg.get("requests_per_minute", 40)),
        checkpoint=checkpoint,
        progress=lambda message: log(f"routine={routine_id} {message}"),
    )
    log(
        f"routine={routine_id} maintenance={kind} "
        f"considered={report['considered']} active={report['active_count']} "
        f"errors={report['error_count']} fatal={report['fatal_error_count']}"
    )
    if not report["ok"]:
        fatal = slack_census.fatal_errors(report["errors"])
        raise RuntimeError(
            "Slack census has fatal coverage errors: "
            + ", ".join(
                f"{row.get('id', '?')}={row.get('error', 'unknown')}"
                for row in fatal[:10]
            )
        )
    return report
