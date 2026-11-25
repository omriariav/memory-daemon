"""Contract test: `linkable_extra_source_ids` agrees with the real store CLI.

The simulation in `memory_sink._simulated_target` mirrors how `memory add`
picks its target from the source ids it is given. This runs the real CLI
against a scratch store and checks that an extra id is kept exactly when the
CLI's target is the same with and without it.

Optional: skipped unless a sibling checkout of the store repo (with
node_modules), Node 20+ and npx are available. The store code is COPIED into a
scratch directory (only node_modules is symlinked), so the real store is never
written; the test asserts its entry count (store and graph stores) and the HEAD
and status of the code repo and the nested store repos are unchanged. CI has no
sibling store checkout, so CI always skips this test: a green CI run does not
mean the contract passed.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from workspace_daemon import memory_sink

SID = "mila:X"
EXTRA = "mila-gcal:evt1_20260801T100000Z"
REPO = Path(__file__).resolve().parent.parent


def _find_store_repo():
    """A sibling directory holding the store CLI with its dependencies."""
    for path in sorted(REPO.parent.iterdir()):
        try:
            if (path != REPO
                    and (path / "src" / "cli.ts").is_file()
                    and (path / "tsconfig.json").is_file()
                    and (path / "node_modules" / "tsx").is_dir()
                    and '"name": "personal-memory"' in (
                        path / "package.json").read_text(encoding="utf-8")):
                return path
        except OSError:
            continue
    return None


def _node_major():
    node = shutil.which("node")
    if not node or not shutil.which("npx"):
        return 0
    try:
        out = subprocess.run([node, "--version"], capture_output=True,
                             text=True, timeout=20).stdout
        return int(out.strip().lstrip("v").split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


STORE_REPO = _find_store_repo()
SKIP_REASON = (
    "store repo (sibling checkout with node_modules) not found"
    if STORE_REPO is None else
    "node 20+ and npx are required" if _node_major() < 20 else None
)


def _entry_count(repo):
    """Entry files in the store and in every graph store."""
    count = 0
    for folder in (repo / "memory", repo / "memory-graphs"):
        if folder.is_dir():
            count += sum(1 for _ in folder.rglob("*.md"))
    return count


def _git_status(repo):
    """HEAD and porcelain status of the code repo and of the nested store
    repos (`memory/` and each graph store keep their own .git)."""
    out = []
    repos = [repo, repo / "memory"]
    graphs = repo / "memory-graphs"
    if graphs.is_dir():
        repos += sorted(p for p in graphs.iterdir() if (p / ".git").exists())
    for path in repos:
        if not (path / ".git").exists():
            continue
        for args in (["rev-parse", "HEAD"], ["status", "--porcelain"]):
            out.append(subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True, text=True, timeout=60).stdout)
    return "".join(out)


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class CliContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name)
        shutil.copytree(STORE_REPO / "src", cls.root / "src")
        for name in ("package.json", "tsconfig.json"):
            shutil.copy2(STORE_REPO / name, cls.root / name)
        (cls.root / "node_modules").symlink_to(STORE_REPO / "node_modules")
        patch = mock.patch.dict(os.environ, {
            "MEMORY_EMBEDDINGS": "fake", "MEMORY_QUIET": "1"})
        patch.start()
        cls.addClassCleanup(patch.stop)

    def setUp(self):
        before = (_entry_count(STORE_REPO), _git_status(STORE_REPO))
        self.addCleanup(lambda: self.assertEqual(
            (_entry_count(STORE_REPO), _git_status(STORE_REPO)), before,
            "the real store was modified"))

    def seed(self, entries):
        """Reset the scratch store to exactly `entries`."""
        for name in ("memory", ".index"):
            shutil.rmtree(self.root / name, ignore_errors=True)
        (self.root / "memory" / "entries").mkdir(parents=True)
        for name, etype, source_ids, extra in entries:
            front = {
                "id": name, "date": extra.get("date", "2026-08-01"),
                "type": etype, "title": f"Seeded {name}", "people": [],
                "teams": [], "tags": extra.get("tags", ["manual"]),
                "source_ids": list(source_ids),
            }
            if extra.get("follows"):
                front["follows"] = list(extra["follows"])
            directory = self.root / "memory" / "entries" / "2026" / "08"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{name}.md").write_text(
                f"---\n{yaml.safe_dump(front)}---\n\n"
                f"Notes about the {name} topic, zebra {name}.\n")

    def cli_target(self, source_ids):
        """What the real `add` does: ("new",), ("write", id) or ("error", id)."""
        result = memory_sink._cli(self.root, [
            "add", "--type", "meeting", "--title", "Quantum kitchen capture",
            "--date", "2026-08-05", "--source-ids", source_ids,
            "--body", "Unrelated words about quantum kitchens."],
            timeout=180)
        out = (result.stdout or "") + (result.stderr or "")
        refusal = re.search(r"already anchors the open \S+ (\S+) —", out)
        if result.returncode != 0:
            self.assertTrue(refusal, f"unexpected CLI failure: {out}")
            return ("error", refusal.group(1))
        matched = re.search(r"↻ matches existing (\S+) via", out)
        if matched:
            return ("write", matched.group(1))
        self.assertIn("✓ created", out)
        return ("new",)

    def check(self, entries, expect_kept, expect_without, expect_with,
              extra=EXTRA):
        self.seed(entries)
        with mock.patch.object(memory_sink, "log"):
            kept = memory_sink.linkable_extra_source_ids(
                str(self.root), SID, [extra], "meeting")
        without = self.cli_target(SID)
        self.seed(entries)
        with_extra = self.cli_target(f"{SID},{extra}")
        self.assertEqual(without, expect_without)
        self.assertEqual(with_extra, expect_with)
        self.assertEqual(kept == [extra], expect_kept)
        # The contract: kept exactly when the CLI's target does not change.
        self.assertEqual(kept == [extra], without == with_extra)

    def test_second_mila_recording_of_the_event_drops_the_link(self):
        self.check(
            [("other-recording", "meeting", ["mila:Y", EXTRA],
              {"tags": ["auto-captured"]})],
            expect_kept=False, expect_without=("new",),
            expect_with=("write", "other-recording"))

    def test_event_entry_carrying_the_id_is_a_split_either_way(self):
        self.check(
            [("hand-event", "event", [EXTRA], {})],
            expect_kept=True, expect_without=("new",), expect_with=("new",))

    def test_open_todo_carrying_the_id_drops_the_link(self):
        self.check(
            [("hand-todo", "todo", [EXTRA], {})],
            expect_kept=False, expect_without=("new",),
            expect_with=("error", "hand-todo"))

    def test_followed_todo_keeps_the_link(self):
        self.check(
            [("hand-todo", "todo", [EXTRA], {}),
             ("outcome", "note", ["slack:C1:1.0"],
              {"follows": ["hand-todo"]})],
            expect_kept=True, expect_without=("new",), expect_with=("new",))

    def test_lone_note_carrying_the_id_drops_the_link(self):
        self.check(
            [("hand-note", "note", [EXTRA], {})],
            expect_kept=False, expect_without=("new",),
            expect_with=("write", "hand-note"))

    def test_recapture_of_our_own_entry_keeps_the_link(self):
        self.check(
            [("ours", "meeting", [SID, EXTRA],
              {"tags": ["auto-captured"], "date": "2026-08-02"}),
             ("hand-note", "note", [EXTRA], {"date": "2026-07-01"})],
            expect_kept=True, expect_without=("write", "ours"),
            expect_with=("write", "ours"))

    def test_plain_gcal_entry_is_unrelated_to_the_link(self):
        self.check(
            [("hand-note", "note", ["gcal:evt1_20260801T100000Z"], {})],
            expect_kept=True, expect_without=("new",), expect_with=("new",))


if __name__ == "__main__":
    unittest.main()
