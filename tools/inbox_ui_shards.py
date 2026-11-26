#!/usr/bin/env python3
"""The inbox-ui CI job's module list, and how it is split into shards.

    python3 tools/inbox_ui_shards.py --shard 2 --of 4   # modules of shard 2
    python3 tools/inbox_ui_shards.py --check            # fail on a forgotten module

MODULES is the one place the list lives (module -> seconds on a quiet laptop,
used only to balance the shards). `--check` fails when a tests/test_inbox_*.py
file is in neither MODULES nor PLAIN_ONLY, so a new test module cannot be left
out of CI. The workflow runs the check in its always-on `changes` job.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Run by the inbox-ui job (with Textual installed). Weights: seconds measured
# by running each module alone on a laptop (2026-11-18); refresh them when a
# module grows a lot. They only balance the shards.
MODULES = {
    "tests.test_inbox_help_scroll": 10.3,
    "tests.test_inbox_view_scroll": 40.0,
    "tests.test_inbox_dup_rows": 3.0,
    "tests.test_inbox_ui": 51.2,
    "tests.test_inbox_items": 0.1,
    "tests.test_inbox_sources": 7.6,
    "tests.test_inbox_store": 0.2,
    "tests.test_inbox_cli": 0.2,
    "tests.test_inbox_writes": 51.0,
    "tests.test_inbox_context": 42.8,
    "tests.test_inbox_actions": 49.1,
    "tests.test_inbox_thread_order": 8.4,
    "tests.test_inbox_send": 37.3,
    "tests.test_inbox_react": 16.1,
    "tests.test_inbox_polish": 11.7,
    "tests.test_inbox_draft": 62.8,
    "tests.test_inbox_memory": 8.6,
    "tests.test_inbox_views": 0.1,
    "tests.test_inbox_views_ui": 36.7,
    "tests.test_inbox_health": 0.1,
    "tests.test_inbox_acts": 4.7,
    "tests.test_inbox_acts_ui": 67.4,
    "tests.test_inbox_invite_twins": 0.5,
    "tests.test_inbox_open_with": 0.8,
    "tests.test_inbox_tasks": 0.6,
    "tests.test_inbox_tasks_ui": 30.1,
    "tests.test_inbox_word_keys": 3.0,
    "tests.test_inbox_scrollback": 11.0,
    "tests.test_inbox_owe": 12.0,
    "tests.test_inbox_remind": 8.0,
    "tests.test_inbox_rowmenu": 90.0,
    "tests.test_inbox_outcome": 22.0,
    "tests.test_inbox_agents": 0.2,
    "tests.test_inbox_agents_ui": 25.0,
    "tests.test_inbox_start_task_ui": 30.0,
    "tests.test_inbox_away": 30.0,
}

# Inbox test modules the plain `test` job already runs (they need no Textual).
PLAIN_ONLY = {
    "tests.test_inbox_alert_active",
    "tests.test_inbox_gaps",
    "tests.test_inbox_launcher",
    "tests.test_inbox_readside",
    "tests.test_inbox_review",
    "tests.test_inbox_version",
    "tests.test_inbox_watch",
}


def on_disk():
    return {f"tests.{p.stem}" for p in (ROOT / "tests").glob("test_inbox_*.py")}


def missing():
    return sorted(on_disk() - set(MODULES) - PLAIN_ONLY)


def textual_in_plain():
    """PLAIN_ONLY modules that skip without Textual (they reference
    HAVE_TEXTUAL): the plain job would skip those tests, so they belong in
    MODULES."""
    return sorted(
        m for m in PLAIN_ONLY
        if "HAVE_TEXTUAL" in (ROOT / "tests" / (m.split(".")[1] + ".py"))
        .read_text(encoding="utf-8"))


def stale():
    return sorted((set(MODULES) | PLAIN_ONLY) - on_disk())


def shards(count):
    """Greedy balance: heaviest module first, onto the lightest shard."""
    out = [[] for _ in range(count)]
    load = [0.0] * count
    for name, weight in sorted(MODULES.items(), key=lambda kv: (-kv[1], kv[0])):
        i = load.index(min(load))
        out[i].append(name)
        load[i] += weight
    return [sorted(s) for s in out]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shard", type=int, help="0-based shard to print")
    ap.add_argument("--of", type=int, default=4, help="number of shards")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        bad = missing()
        gone = stale()
        needs_textual = [m for m in textual_in_plain() if m not in gone]
        if bad:
            print("inbox_ui_shards: not run by any CI job (add to MODULES in "
                  "tools/inbox_ui_shards.py, or to PLAIN_ONLY if it needs no "
                  "Textual): " + ", ".join(bad))
        if gone:
            print("inbox_ui_shards: listed but missing on disk: "
                  + ", ".join(gone))
        if needs_textual:
            print("inbox_ui_shards: in PLAIN_ONLY but they use Textual (their "
                  "UI tests would skip in the plain job); move to MODULES: "
                  + ", ".join(needs_textual))
        if bad or gone or needs_textual:
            return 1
        print(f"inbox_ui_shards: ok ({len(MODULES)} UI modules, "
              f"{len(PLAIN_ONLY)} plain-only)")
        return 0
    if args.shard is None or not 0 <= args.shard < args.of:
        ap.error("--shard N (0 <= N < --of) or --check")
    print(" ".join(shards(args.of)[args.shard]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
