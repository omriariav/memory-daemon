#!/usr/bin/env python3
"""Re-baseline one Google Tasks <-> memory pair that both sides agree is done.

When a linked task is completed in Google and its memory entry is resolved,
but content drifted on both sides since the last checkpoint, the sync refuses
to auto-apply ("both sides changed"). Nothing remains to sync for a finished
pair, so record the current hashes and mark the mapping terminal.

Usage:
  python3 tools/rebaseline_terminal_pair.py --key <tasklist_id>:<task_id> \
      [--checkpoint state/google-tasks-sync.json] [--store /path/to/omri-mem]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace_daemon import google_tasks_sync as gts  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="<tasklist_id>:<task_id>")
    parser.add_argument("--checkpoint", default="state/google-tasks-sync.json")
    parser.add_argument("--store", default="/Users/omri.a/Code/omri-mem")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    checkpoint = gts._load_checkpoint(args.checkpoint)
    mapping = checkpoint["mappings"].get(args.key)
    if not mapping:
        sys.exit(f"no checkpoint mapping for {args.key}")
    list_id, task_id = args.key.split(":", 1)
    task = gts._task_from_payload(gts._gws(["get", list_id, task_id]))
    task["tasklist_id"] = list_id
    entries = gts._load_memory_entries(Path(args.store))
    entry = entries.get(mapping["memory_id"])
    if not entry:
        sys.exit(f"memory entry {mapping['memory_id']} not found")

    print(f"task     : {task.get('title')!r} status={task.get('status')}")
    print(f"memory   : {entry['title']!r} type={entry['type']} resolved={entry['resolved']}")
    if task.get("status") != "completed" or not entry["resolved"]:
        sys.exit("refusing: both sides must already be done (completed + resolved)")

    before = dict(mapping)
    mapping.update({
        "google_hash": gts._google_hash(task),
        "memory_hash": gts._memory_hash(entry),
        "terminal": True,
        "pending_link": False,
    })
    for field in ("google_hash", "memory_hash", "terminal", "pending_link"):
        print(f"{field:12}: {before.get(field)} -> {mapping[field]}")
    if args.dry_run:
        print("dry-run: checkpoint not written")
        return
    gts._save_checkpoint(args.checkpoint, checkpoint)
    print("checkpoint updated")


if __name__ == "__main__":
    main()
