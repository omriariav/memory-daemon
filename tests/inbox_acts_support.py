"""Shared helpers for the acts tests: the fake command source and a
`LocalActions` wired to it. Synthetic data only."""
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "inbox_fake_source.py"
CHANNEL, THREAD, MESSAGE = "C0EXAMPLE01", "1893492000.000100", "1893492060.000200"
ALL_ACTS = ["close", "snooze", "mute", "waits_on", "relay"]
TASK_ACTS = ["close", "drop", "due"]


class FakeSource:
    """The fake command source in a temp directory: `.argv` runs it, `.mode`
    changes how `act` answers, `.requests()` lists what it received."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.argv = [sys.executable, str(SCRIPT), str(self.root)]
        self.configure()

    def cleanup(self):
        self.tmp.cleanup()

    def configure(self, mode="ok", modes=None, sleep=0.0):
        (self.root / "config.json").write_text(json.dumps(
            {"mode": mode, "modes": modes or {}, "sleep": sleep}))

    def _lines(self, name):
        path = self.root / name
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text().splitlines() if x]

    def requests(self):
        return self._lines("requests.jsonl")

    def applied(self):
        return self._lines("applied.jsonl")

    def set_items(self, items):
        """The items `list` returns (and that task acts change)."""
        (self.root / "items.json").write_text(json.dumps(items))

    def items(self):
        return json.loads((self.root / "items.json").read_text())


def acts_cfg(source, name="src", acts=None, timeout_s=15.0):
    return {name: {"argv": source.argv, "timeout_s": timeout_s,
                   "acts": list(ALL_ACTS if acts is None else acts)}}


def thread(*who):
    return [{"who": w, "ts": time.time() - 100 + n, "text": f"msg {n}"}
            for n, w in enumerate(who)]


def task_item(n, task_type="todo", due=None, date="2030-01-01", people=(),
              evidence=None, link=None, source="memory", name=None, **meta):
    """A synthetic task item (v1 dict, JSON-able) for a task source."""
    base = {"kind": "task", "task_type": task_type, "due": due, "date": date,
            "people": list(people), "evidence": evidence,
            "source_ids": [f"src-{n}"]}
    base.update(meta)
    return {"v": 1, "id": f"task:{n}", "source": source,
            "section": "fyi", "urgency": "digest",
            "sender": (people[0] if people else "me"), "where": "tasks",
            "gist": name or f"Task {n}", "ts": 1893492000.0 + n,
            "link": link, "thread": [], "draft": None,
            "actions": ["done"] + (["open"] if link else []), "meta": base}
