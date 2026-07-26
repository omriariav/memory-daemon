"""Processed-message state, keyed by Gmail message id."""
import json
from pathlib import Path


def state_file(base_dir):
    return Path(base_dir) / "state" / "processed.json"


def load(base_dir):
    path = state_file(base_dir)
    if path.exists():
        return json.loads(path.read_text())
    return {}


def save(base_dir, state):
    path = state_file(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def last_run(base_dir, routine_id):
    """Most recent processed_at for a routine, or None if it never processed anything."""
    stamps = [
        entry.get("processed_at")
        for entry in load(base_dir).values()
        if entry.get("rule_id") == routine_id and entry.get("processed_at")
    ]
    return max(stamps) if stamps else None
