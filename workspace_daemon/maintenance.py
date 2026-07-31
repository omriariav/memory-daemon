"""Scheduled source-maintenance routines that do not create memories."""
from pathlib import Path

from . import slack_census, slack_cli
from .shell import log


def _path(base_dir, value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path(base_dir) / path


def run(base_dir, routine, dry_run=False):
    """Execute one validated maintenance routine and return its report."""
    routine_id = routine["id"]
    cfg = routine["maintenance"]
    kind = cfg["kind"]
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
