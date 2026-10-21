"""Inbound Fibery task document -> memory todo import."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from workspace_daemon import claude_bridge, config, fibery_tasks_inbound as fti
from workspace_daemon import maintenance

TOOL = "mcp__claude_ai_Example_Fibery__read_document"
SECRET = "00000000-0000-0000-0000-000000000000"


def bid(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def q(value):
    return json.dumps(value, ensure_ascii=False)


class Doc:
    """Builds a synthetic TOON document."""

    def __init__(self):
        self.blocks = []
        self.roots = []
        self.n = 1000

    def _id(self):
        self.n += 1
        return bid(self.n)

    def heading(self, text):
        block_id = self._id()
        self.roots.append(block_id)
        self.blocks.append([
            f"id: {block_id}", "type: heading", "parentId: null",
            "attrs:", "  level: 2", f"content: {q(text)}",
        ])

    def bullets(self, items):
        """items: list of (block_id, state, [paragraph texts])."""
        list_id = self._id()
        self.roots.append(list_id)
        self.blocks.append([
            f"id: {list_id}", "type: bullet_list", "parentId: null",
            f"childIds[{len(items)}]: " + ",".join(i[0] for i in items),
        ])
        for item_id, state, paragraphs in items:
            pids = [self._id() for _ in paragraphs]
            self.blocks.append([
                f"id: {item_id}", "type: list_item", f"parentId: {list_id}",
                "attrs:", f"  state: {state}",
                f"childIds[{len(pids)}]: " + ",".join(pids),
            ])
            for pid, text in zip(pids, paragraphs):
                self.blocks.append([
                    f"id: {pid}", "type: paragraph", f"parentId: {item_id}",
                    f"content: {text}" if not text.startswith('"')
                    else f"content: {text}",
                ])

    def text(self, wrap=True):
        lines = ["version: 0",
                 f"rootBlockIds[{len(self.roots)}]: " + ",".join(self.roots),
                 f"blocks[{len(self.blocks)}]:"]
        for block in self.blocks:
            lines.append("  - " + block[0])
            lines.extend("    " + ln for ln in block[1:])
        toon = "\n".join(lines)
        return json.dumps({"success": True, "document": toon}) if wrap else toon


def simple_doc(*specs):
    """specs: (n, state, text, section)."""
    doc = Doc()
    by_section = {}
    for n, state, text, section in specs:
        by_section.setdefault(section, []).append((bid(n), state, [q(text)]))
    for section, items in by_section.items():
        if section:
            doc.heading(section)
        doc.bullets(items)
    return doc.text()


class FakeMemory:
    """Fake memory CLI that persists entries as files like the real store."""

    def __init__(self, store):
        self.store = Path(store)
        self.entries = self.store / "memory" / "entries"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.calls = []
        self.count = 0
        self.timeout_when = None  # predicate(args) -> write, then time out

    @staticmethod
    def _opt(args, flag):
        return args[args.index(flag) + 1] if flag in args else None

    def __call__(self, store, args, timeout=180):
        self.calls.append(args)
        if "--update" in args:
            entry_id = self._opt(args, "--update")
            out = f"updated {entry_id}"
        else:
            self.count += 1
            entry_id = f"entry-{self.count}"
            out = f"created {entry_id}"
        follows = self._opt(args, "--follows")
        lines = [
            "---", f"id: {entry_id}", "date: '2026-08-02'",
            f"type: {self._opt(args, '--type')}",
            f"title: {json.dumps(self._opt(args, '--title'))}",
            "people: []", "teams: []", "tags: []",
            "source_ids:", f"  - '{self._opt(args, '--source-ids')}'",
        ]
        if follows:
            lines.extend(["follows:", f"  - {follows}"])
        lines.extend(["---", "", self._opt(args, "--body"), ""])
        (self.entries / f"{entry_id}.md").write_text("\n".join(lines))
        if self.timeout_when and self.timeout_when(args):
            raise subprocess.TimeoutExpired(args, 1)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def of(self, kind):
        return [c for c in self.calls if c[c.index("--type") + 1] == kind]


class RunBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.checkpoint = self.dir / "state" / "fibery-tasks.json"
        self.cfg = {
            "document_secret": SECRET, "read_tool": TOOL,
            "store": str(self.dir / "store"), "owner_slug": "example-owner",
            "tags": ["fibery"],
        }
        self.memory = FakeMemory(self.dir / "store")
        self.calls = []
        self.text = simple_doc()
        for patcher in (
            mock.patch.object(fti, "_memory_cli", self.memory),
            mock.patch.object(fti, "_commit_store"),
            mock.patch.object(fti.claude_bridge, "call_tools", self._call),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _call(self, prompt, tools, **kwargs):
        self.calls.append((prompt, tools, kwargs))
        return [{"name": TOOL, "input": {"secret": SECRET},
                 "text": self.text, "is_error": False}]

    def run_once(self, text=None, **cfg):
        if text is not None:
            self.text = text
        return fti.run({**self.cfg, **cfg}, self.dir, self.checkpoint)

    def items(self):
        return json.loads(self.checkpoint.read_text())["items"]


class ParserTest(unittest.TestCase):
    def test_tasks_sections_and_alerts(self):
        doc = Doc()
        doc.bullets([(bid(1), "TODO", ['"Before any heading"'])])
        doc.heading("Work stuff")
        doc.bullets([
            (bid(2), "TODO", ['"Say \\"hi\\" \\\\~ — ₪ alert:abc123def456"']),
            (bid(3), "DONE", ["bare text: with colon"]),
            (bid(4), "null", ['"plain bullet"']),
            (bid(5), "TODO", ['"first"', '""', '"second"']),
            (bid(6), "TODO", ['""']),
        ])
        tasks = {t["block_id"]: t for t in fti.parse_tasks(doc.text())}
        self.assertEqual(set(tasks), {bid(1), bid(2), bid(3), bid(5), bid(6)})
        self.assertIsNone(tasks[bid(1)]["section"])
        self.assertEqual(tasks[bid(2)]["section"], "Work stuff")
        self.assertEqual(tasks[bid(2)]["text"], 'Say "hi" \\~ — ₪')
        self.assertEqual(tasks[bid(2)]["alert_id"], "abc123def456")
        self.assertEqual(tasks[bid(3)]["state"], "DONE")
        self.assertEqual(tasks[bid(3)]["text"], "bare text: with colon")
        self.assertEqual(tasks[bid(5)]["text"], "first\nsecond")
        self.assertEqual(tasks[bid(6)]["text"], "")
        self.assertNotIn("alert_id", tasks[bid(3)])

    def test_bare_document_without_json_wrapper(self):
        text = simple_doc((1, "TODO", "x", "S"))
        document = json.loads(text)["document"]
        self.assertEqual(len(fti.parse_tasks(document)), 1)

    def test_empty_document(self):
        self.assertEqual(fti.parse_tasks(simple_doc()), [])

    def test_unknown_shapes_fail(self):
        good = json.loads(simple_doc((1, "TODO", "x", "S")))["document"]
        bad = [
            "nonsense",
            good.replace("version: 0", "version: 7"),
            good.replace("blocks[4]", "blocks[9]"),
            good.replace("state: TODO", "state: MAYBE"),
            good.replace("type: list_item", "type: list_item\n    bogus line"),
            good + "\ngarbage",
            good.replace('content: "x"', 'content: "unterminated'),
            json.dumps({"success": False, "error": "nope"}),
            json.dumps({"success": True}),
        ]
        for text in bad:
            with self.assertRaises(fti.FiberyParseError, msg=text):
                fti.parse_tasks(text)

    def test_missing_child_fails(self):
        good = json.loads(simple_doc((1, "TODO", "x", "S")))["document"]
        broken = good.replace("childIds[1]: " + bid(1003),
                              "childIds[1]: " + bid(9999))
        with self.assertRaises(fti.FiberyParseError):
            fti.parse_tasks(broken)


class DiffTest(RunBase):
    def test_new_todo_created_with_source_tags_and_owner(self):
        report = self.run_once(simple_doc((1, "TODO", "Write the report", "Big Rocks!")))
        self.assertEqual(report["created"], 1)
        args = self.memory.of("todo")[0]
        self.assertEqual(args[args.index("--source-ids") + 1], f"fibery:tasks:{bid(1)}")
        self.assertEqual(args[args.index("--tags") + 1], "fibery,big-rocks")
        self.assertEqual(args[args.index("--people") + 1], "example-owner")
        self.assertEqual(args[args.index("--title") + 1], "Write the report")
        self.assertIn("Section: Big Rocks!", args[args.index("--body") + 1])
        self.assertEqual(self.items()[bid(1)]["entry_id"], "entry-1")
        self.assertEqual(oct(self.checkpoint.stat().st_mode & 0o777), "0o600")

    def test_title_is_bounded(self):
        self.run_once(simple_doc((1, "TODO", "x" * 400, None)))
        title = self.memory.calls[0][self.memory.calls[0].index("--title") + 1]
        self.assertLessEqual(len(title), fti.TITLE_LIMIT)

    def test_unchanged_run_is_a_noop(self):
        text = simple_doc((1, "TODO", "A", "S"))
        self.run_once(text)
        n = len(self.memory.calls)
        report = self.run_once(text)
        self.assertEqual(len(self.memory.calls), n)
        self.assertEqual(report["created"], 0)

    def test_new_done_skipped_by_default(self):
        report = self.run_once(simple_doc((1, "DONE", "Old", "S")))
        self.assertEqual(self.memory.calls, [])
        self.assertEqual(report["skipped_done"], 1)
        self.assertEqual(self.items()[bid(1)]["state"], "DONE")

    def test_new_done_imported_when_enabled(self):
        self.run_once(simple_doc((1, "DONE", "Old", "S")), import_done=True)
        self.assertEqual(len(self.memory.of("todo")), 1)
        note = self.memory.of("note")[0]
        self.assertEqual(note[note.index("--title") + 1], "done: Old")
        self.assertEqual(note[note.index("--follows") + 1], "entry-1")

    def test_todo_to_done_adds_note_following_todo(self):
        self.run_once(simple_doc((1, "TODO", "Ship it", "S")))
        report = self.run_once(simple_doc((1, "DONE", "Ship it", "S")))
        self.assertEqual(report["done"], 1)
        note = self.memory.of("note")[0]
        self.assertEqual(note[note.index("--title") + 1], "done: Ship it")
        self.assertEqual(note[note.index("--follows") + 1], "entry-1")
        self.assertEqual(len(self.memory.of("todo")), 1)
        self.assertNotIn("--update", note)

    def test_edit_updates_todo_in_place(self):
        self.run_once(simple_doc((1, "TODO", "Ship it", "S")))
        report = self.run_once(simple_doc((1, "TODO", "Ship it today", "S")))
        self.assertEqual(report["edited"], 1)
        args = self.memory.calls[-1]
        self.assertEqual(args[args.index("--update") + 1], "entry-1")
        self.assertEqual(args[args.index("--source-ids") + 1], f"fibery:tasks:{bid(1)}")
        self.assertEqual(args[args.index("--title") + 1], "Ship it today")

    def test_section_move_updates_todo(self):
        self.run_once(simple_doc((1, "TODO", "Ship it", "A")))
        report = self.run_once(simple_doc((1, "TODO", "Ship it", "B")))
        self.assertEqual(report["edited"], 1)
        self.assertIn("b", self.memory.calls[-1][self.memory.calls[-1].index("--tags") + 1])

    def test_reopen_adds_note_and_second_done_gets_new_source(self):
        self.run_once(simple_doc((1, "TODO", "Ship it", "S")))
        self.run_once(simple_doc((1, "DONE", "Ship it", "S")))
        report = self.run_once(simple_doc((1, "TODO", "Ship it", "S")))
        self.assertEqual(report["reopened"], 1)
        note = self.memory.of("note")[-1]
        self.assertEqual(note[note.index("--title") + 1], "reopened: Ship it")
        self.assertEqual(note[note.index("--follows") + 1], "entry-1")
        self.run_once(simple_doc((1, "DONE", "Ship it", "S")))
        sources = [c[c.index("--source-ids") + 1] for c in self.memory.of("note")]
        self.assertEqual(len(sources), len(set(sources)))

    def test_reopen_of_skipped_done_creates_todo(self):
        self.run_once(simple_doc((1, "DONE", "Old", "S")))
        report = self.run_once(simple_doc((1, "TODO", "Old", "S")))
        self.assertEqual(report["created"], 1)
        self.assertEqual(self.items()[bid(1)]["entry_id"], "entry-1")

    def test_disappeared_item_is_marked_gone_without_memory_change(self):
        self.run_once(simple_doc((1, "TODO", "A", "S"), (2, "TODO", "B", "S")))
        n = len(self.memory.calls)
        report = self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.assertEqual(report["gone"], 1)
        self.assertEqual(len(self.memory.calls), n)
        self.assertTrue(self.items()[bid(2)]["gone"])
        # Returning clears the flag without a write.
        self.run_once(simple_doc((1, "TODO", "A", "S"), (2, "TODO", "B", "S")))
        self.assertNotIn("gone", self.items()[bid(2)])
        self.assertEqual(len(self.memory.calls), n)

    def test_bootstrap_cap_leaves_rest_pending(self):
        specs = [(n, "TODO", f"T{n}", "S") for n in range(1, 8)]
        text = simple_doc(*specs)
        report = self.run_once(text, max_new_per_run=3)
        self.assertEqual((report["created"], report["pending"]), (3, 4))
        self.assertEqual(len(self.items()), 3)
        report = self.run_once(text, max_new_per_run=3)
        self.assertEqual((report["created"], report["pending"]), (3, 1))
        report = self.run_once(text, max_new_per_run=3)
        self.assertEqual((report["created"], report["pending"]), (1, 0))
        self.assertEqual(len(self.memory.of("todo")), 7)

    def test_mass_change_guard_writes_nothing(self):
        specs = [(n, "TODO", f"T{n}", "S") for n in range(1, 6)]
        self.run_once(simple_doc(*specs))
        before = self.checkpoint.read_text()
        n = len(self.memory.calls)
        done = [(n_, "DONE", f"T{n_}", "S") for n_ in range(1, 6)]
        with self.assertRaises(fti.FiberyHeld) as ctx:
            self.run_once(simple_doc(*done), max_changes_per_run=4)
        self.assertIn("max_changes_per_run", str(ctx.exception))
        self.assertEqual(len(self.memory.calls), n)
        self.assertEqual(self.checkpoint.read_text(), before)
        report = self.run_once(max_changes_per_run=5)
        self.assertEqual(report["done"], 5)

    def test_mass_disappearance_counts_toward_guard(self):
        specs = [(n, "TODO", f"T{n}", "S") for n in range(1, 6)]
        self.run_once(simple_doc(*specs))
        with self.assertRaises(fti.FiberyHeld):
            self.run_once(simple_doc(), max_changes_per_run=2)
        self.assertFalse(any(r.get("gone") for r in self.items().values()))

    def test_new_items_do_not_count_toward_guard(self):
        specs = [(n, "TODO", f"T{n}", "S") for n in range(1, 6)]
        report = self.run_once(simple_doc(*specs), max_changes_per_run=1)
        self.assertEqual(report["created"], 5)


class SafetyTest(RunBase):
    def test_pinned_call_uses_only_the_read_tool(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        prompt, tools, _ = self.calls[0]
        self.assertEqual(tools, {TOOL: {"secret": SECRET}})
        self.assertIn(TOOL, prompt)

    def test_wrong_tool_name_rejected(self):
        for name in (
            "mcp__claude_ai_Example_Fibery__replace_block_text",
            "mcp__claude_ai_Example_Fibery__read_document_x",
            "read_document", "",
        ):
            with self.assertRaises(RuntimeError):
                self.run_once(read_tool=name)
        self.assertEqual(self.calls, [])

    def test_dry_run_makes_no_call_or_write(self):
        report = fti.run(self.cfg, self.dir, self.checkpoint, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.memory.calls, [])
        self.assertFalse(self.checkpoint.exists())

    def test_connector_error_writes_nothing(self):
        def failing(prompt, tools, **kwargs):
            raise claude_bridge.BridgeError(f"boom {SECRET}")
        with mock.patch.object(fti.claude_bridge, "call_tools", failing):
            with self.assertRaises(claude_bridge.BridgeError) as ctx:
                self.run_once()
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertEqual(self.memory.calls, [])
        self.assertFalse(self.checkpoint.exists())

    def test_error_result_or_wrong_input_writes_nothing(self):
        for row in (
            {"name": TOOL, "input": {"secret": SECRET}, "text": "x", "is_error": True},
            {"name": TOOL, "input": {"secret": "other"}, "text": "x", "is_error": False},
        ):
            with mock.patch.object(fti.claude_bridge, "call_tools",
                                   return_value=[row]):
                with self.assertRaises(claude_bridge.BridgeError):
                    self.run_once()
        self.assertEqual(self.memory.calls, [])

    def test_unparseable_document_never_partially_applies(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        n = len(self.memory.calls)
        before = self.checkpoint.read_text()
        with self.assertRaises(fti.FiberyParseError):
            self.run_once("version: 0\nrootBlockIds[0]:\nblocks[2]:\n  - id: x")
        self.assertEqual(len(self.memory.calls), n)
        self.assertEqual(self.checkpoint.read_text(), before)

    def test_memory_failure_keeps_earlier_progress(self):
        calls = {"n": 0}

        def flaky(store, args, timeout=180):
            calls["n"] += 1
            if calls["n"] == 2:
                return SimpleNamespace(returncode=1, stdout="", stderr="bad")
            return self.memory(store, args)

        with mock.patch.object(fti, "_memory_cli", flaky), mock.patch.object(
            fti, "_memory_result_id",
            side_effect=lambda r, *a: (_ for _ in ()).throw(RuntimeError("x"))
            if r.returncode else r.stdout.split()[-1],
        ):
            with self.assertRaises(RuntimeError):
                self.run_once(simple_doc((1, "TODO", "A", "S"), (2, "TODO", "B", "S")))
        self.assertEqual(list(self.items()), [bid(1)])

    def test_logs_never_carry_text_or_secret(self):
        logged = []
        with mock.patch.object(fti, "log", logged.append):
            self.run_once(simple_doc((1, "TODO", "Private words here", "Sec")))
            self.run_once(simple_doc())
        blob = "\n".join(logged)
        self.assertNotIn("Private words", blob)
        self.assertNotIn(SECRET, blob)
        self.assertIn(bid(1), blob)

    def test_bad_checkpoint_fails_before_the_call(self):
        self.checkpoint.parent.mkdir(parents=True)
        self.checkpoint.write_text("{not json")
        with self.assertRaises(RuntimeError):
            self.run_once()
        self.assertEqual(self.calls, [])


class DuplicateSafetyTest(RunBase):
    def entry_count(self):
        return len(list(self.memory.entries.glob("*.md")))

    def test_timeout_after_create_does_not_duplicate_todo(self):
        self.memory.timeout_when = lambda a: True
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.assertEqual(self.entry_count(), 1)
        self.assertEqual(self.items()[bid(1)]["entry_id"], "entry-1")
        self.memory.timeout_when = None
        self.run_once()
        self.assertEqual(self.entry_count(), 1)

    def test_killed_before_checkpoint_adopts_existing_todo(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.checkpoint.unlink()
        report = self.run_once()
        self.assertEqual(self.entry_count(), 1)
        self.assertEqual(len(self.memory.calls), 1)
        self.assertEqual(self.items()[bid(1)]["entry_id"], "entry-1")
        self.assertEqual(report["created"], 1)

    def test_timeout_after_done_note_does_not_duplicate(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.memory.timeout_when = lambda a: "--follows" in a
        self.run_once(simple_doc((1, "DONE", "A", "S")))
        self.assertEqual(self.entry_count(), 2)
        self.memory.timeout_when = None
        self.run_once()
        self.assertEqual(self.entry_count(), 2)

    def test_unrecorded_done_note_is_adopted_on_rerun(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.run_once(simple_doc((1, "DONE", "A", "S")))
        rows = json.loads(self.checkpoint.read_text())
        rows["items"][bid(1)].update(state="TODO", transitions=0)
        self.checkpoint.write_text(json.dumps(rows))
        self.run_once()
        self.assertEqual(self.entry_count(), 2)

    def test_timeout_after_reopened_note_does_not_duplicate(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.run_once(simple_doc((1, "DONE", "A", "S")))
        self.memory.timeout_when = lambda a: "--follows" in a
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.assertEqual(self.entry_count(), 3)
        self.memory.timeout_when = None
        self.run_once()
        self.assertEqual(self.entry_count(), 3)

    def test_reopen_note_persisted_before_failed_update(self):
        self.run_once(simple_doc((1, "TODO", "A", "S")))
        self.run_once(simple_doc((1, "DONE", "A", "S")))

        def fail_update(store, args, timeout=180):
            if "--update" in args:
                return SimpleNamespace(returncode=1, stdout="", stderr="bad")
            return self.memory(store, args)

        with mock.patch.object(fti, "_memory_cli", fail_update):
            with self.assertRaises(RuntimeError):
                self.run_once(simple_doc((1, "TODO", "A edited", "S")))
        self.assertEqual(self.entry_count(), 3)
        self.assertEqual(self.items()[bid(1)]["state"], "TODO")
        report = self.run_once()
        self.assertEqual(self.entry_count(), 3)
        self.assertEqual(report["edited"], 1)

    def test_import_done_failed_note_retries_via_done_path(self):
        def fail_note(store, args, timeout=180):
            if "--follows" in args:
                return SimpleNamespace(returncode=1, stdout="", stderr="bad")
            return self.memory(store, args)

        text = simple_doc((1, "DONE", "Old", "S"))
        with mock.patch.object(fti, "_memory_cli", fail_note):
            with self.assertRaises(RuntimeError):
                self.run_once(text, import_done=True)
        self.assertEqual(self.items()[bid(1)]["state"], "TODO")
        report = self.run_once(import_done=True)
        self.assertEqual(report["done"], 1)
        self.assertEqual(self.items()[bid(1)]["state"], "DONE")
        self.assertEqual(self.entry_count(), 2)


class WiringTest(unittest.TestCase):
    def routine(self, **extra):
        maint = {
            "kind": "fibery_tasks_inbound", "document_secret": SECRET,
            "read_tool": TOOL, "store": "/tmp/personal-memory",
        }
        maint.update(extra)
        return {"id": "fibery-in", "enabled": True, "description": "d",
                "role": "maintenance", "schedule": {"every": "1h"},
                "maintenance": maint}

    def test_valid_with_defaults_and_uses_bridge(self):
        routine = self.routine()
        self.assertEqual(config.validate(routine), [])
        self.assertTrue(config.uses_claude_bridge(routine))

    def test_validation_rejects_bad_values(self):
        for extra in (
            {"read_tool": "mcp__x__replace_block_text"},
            {"read_tool": "mcp__x__read_document__write"},
            {"document_secret": "has space"},
            {"store": "relative"},
            {"tags": ["Bad Tag"]},
            {"import_done": "yes"},
            {"max_new_per_run": 0},
            {"max_changes_per_run": True},
            {"unknown_key": 1},
        ):
            self.assertTrue(config.validate(self.routine(**extra)), extra)

    def test_maintenance_dispatch_uses_default_checkpoint(self):
        report = {"ok": True, "dry_run": True, "tasks": 0, "created": 0,
                  "done": 0, "reopened": 0, "edited": 0, "gone": 0,
                  "pending": 0}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            maintenance.fibery_tasks_inbound, "run", return_value=report
        ) as run:
            maintenance.run(Path(tmp), self.routine(), dry_run=True)
        self.assertEqual(run.call_args.args[2],
                         Path(tmp) / "state" / "fibery-tasks.json")
        self.assertTrue(run.call_args.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
