"""Processed-item state, keyed by source item id.

The ledger is the only thing standing between a re-run and a duplicate note, so
it is written atomically and flushed after every item rather than once at the
end of a run: an hourly LaunchAgent on a laptop gets interrupted (sleep, reboot,
launchd timeout), and a run that dies halfway must not forget what it just did.
"""
import json
import os
from pathlib import Path


class StateError(Exception):
    pass


def state_file(base_dir):
    return Path(base_dir) / "state" / "processed.json"


def load(base_dir):
    path = state_file(base_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        # Loading happens before the per-routine exception boundary, so a
        # corrupt ledger would otherwise abort every routine with a bare
        # traceback. Say what is wrong and what deleting it would cost.
        raise StateError(
            f"{path} is not valid JSON ({exc}). Repair it by hand if you can — "
            f"deleting it makes every item look unprocessed, so the next run "
            f"will re-summarize and re-triage everything its queries still match."
        ) from exc


def _write_atomic(path, entries):
    """Write via a temp file + os.replace so a crash cannot truncate the ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(entries, indent=2, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save(base_dir, entries):
    _write_atomic(state_file(base_dir), entries)


def last_run(base_dir, routine_id):
    """Most recent processed_at for a routine, or None if it never processed anything."""
    stamps = [
        entry.get("processed_at")
        for entry in load(base_dir).values()
        if entry.get("rule_id") == routine_id and entry.get("processed_at")
    ]
    return max(stamps) if stamps else None


class Store:
    """The processed ledger, persisted as each item completes."""

    def __init__(self, base_dir, dry_run=False):
        self.path = state_file(base_dir)
        self.dry_run = dry_run
        self.entries = load(base_dir)

    def __contains__(self, item_id):
        return item_id in self.entries

    def __len__(self):
        return len(self.entries)

    def record(self, item_id, entry):
        self.entries[item_id] = entry
        if not self.dry_run:
            _write_atomic(self.path, self.entries)
