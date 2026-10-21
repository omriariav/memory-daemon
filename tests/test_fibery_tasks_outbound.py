"""Outbound memory todo -> Fibery inbox export, and inbound coordination."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_fibery_tasks_inbound import Doc, FakeMemory, bid, q
from workspace_daemon import claude_bridge, config
from workspace_daemon import fibery_tasks_inbound as fti
from workspace_daemon import fibery_tasks_outbound as fto

RTOOL = "mcp__claude_ai_Example_Fibery__read_document"
WTOOL = "mcp__claude_ai_Example_Fibery__insert_document_blocks"
SECRET = "00000000-0000-0000-0000-000000000000"
OWNER = "example-owner"


def toon(doc, suggestions=None, wrap=True):
    text = doc.text(wrap=False)
    if suggestions is not None:
        text += f"\nsuggestions[{len(suggestions)}]:\n" + "\n".join(
            f"  - {s}" for s in suggestions)
    return json.dumps({"success": True, "document": text}) if wrap else text


def write_entry(store, entry_id, title="Send the report", date="2026-08-02",
                etype="todo", people=(OWNER,), tags=(), sources=("slack:C1:1",),
                follows=()):
    folder = Path(store) / "memory" / "entries"
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["---", f"id: {entry_id}", f"date: '{date}'", f"type: {etype}",
             f"title: {json.dumps(title)}",
             "people: " + json.dumps(list(people)), "teams: []",
             "tags: " + json.dumps(list(tags)),
             "source_ids: " + json.dumps(list(sources)),
             "follows: " + json.dumps(list(follows)), "---", "", "Body", ""]
    (folder / f"{entry_id}.md").write_text("\n".join(lines))


class OutboundBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / "store"
        self.checkpoint = self.dir / "state" / "fibery-outbound.json"
        self.cfg = {
            "document_secret": SECRET, "read_tool": RTOOL, "write_tool": WTOOL,
            "store": str(self.store), "owner_slug": OWNER,
            "outbound_since": "2026-08-01",
        }
        self.calls = []
        self.doc_markers = []     # accepted markers
        self.pending_markers = []  # pending suggestions
        self.heading = False
        self.pending_heading = False
        self.accepted_items = []
        self.land = True          # whether a write actually lands
        patcher = mock.patch.object(
            fto.claude_bridge, "call_tools", self._call)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(fti.claude_bridge, "call_tools", self._call)
        patcher.start()
        self.addCleanup(patcher.stop)

    def document(self):
        doc = Doc()
        if self.pending_heading:
            doc.heading("<ins>Inbox</ins>")
        elif self.heading or self.accepted_items:
            doc.heading("Inbox")
            if self.accepted_items:
                doc.bullets([(bid(i + 1), "TODO", [q(t)])
                             for i, t in enumerate(self.accepted_items)])
        suggestions = [q(f"insert (mem:{m})") for m in self.pending_markers]
        return toon(doc, suggestions or None)

    def _call(self, prompt, tools, **kwargs):
        self.calls.append((prompt, tools, kwargs))
        (name,) = tools
        if name == RTOOL:
            return [{"name": RTOOL, "input": {"secret": SECRET},
                     "text": self.document(), "is_error": False}]
        pins = tools[name]
        if self.land:
            for insert in pins["inserts"]:
                for line in insert["content"].split("\n"):
                    if line.startswith("- [ ] "):
                        if pins["mode"] == "suggest":
                            self.pending_markers.extend(fto.MEM_RE.findall(line))
                        else:
                            self.accepted_items.append(line[6:])
                    elif line.startswith("## "):
                        self.heading = True
        return [{"name": name, "input": dict(pins), "text": "ok",
                 "is_error": False}]

    def writes(self):
        return [c for c in self.calls if WTOOL in c[1]]

    def reads(self):
        return [c for c in self.calls if RTOOL in c[1]]

    def run_once(self, **cfg):
        return fto.run({**self.cfg, **cfg}, self.dir, self.checkpoint)

    def items(self):
        return json.loads(self.checkpoint.read_text())["items"]


class SelectionTest(OutboundBase):
    def test_filters(self):
        s = self.store
        write_entry(s, "good")
        write_entry(s, "personal", tags=["personal"])
        write_entry(s, "nofib", tags=["no-fibery"])
        write_entry(s, "fib", sources=["slack:C1:2", "fibery:tasks:x"])
        write_entry(s, "notowner", people=["someone-else"])
        write_entry(s, "downgraded", tags=["no-owner-action"])
        write_entry(s, "note", etype="note")
        write_entry(s, "old", date="2026-07-01")
        write_entry(s, "badsrc", sources=["drive:abc"])
        write_entry(s, "mila-ok", sources=["mila:rec1"], date="2026-08-03")
        write_entry(s, "resolved")
        write_entry(s, "done-note", etype="note", follows=["resolved"])
        entries = fto._load_memory_entries(s)
        ids = [e["id"] for e in fto.select_candidates(entries, self.cfg)]
        self.assertEqual(ids, ["good", "mila-ok"])
        ids = [e["id"] for e in
               fto.select_candidates(entries, self.cfg, exported={"good"})]
        self.assertEqual(ids, ["mila-ok"])

    def test_cap_and_checkpoint(self):
        for n in range(7):
            write_entry(self.store, f"e{n}", date=f"2026-08-0{n + 1}")
        report = self.run_once(max_per_run=3)
        self.assertEqual(report["exported"], 3)
        self.assertEqual(sorted(self.items()), ["e0", "e1", "e2"])
        self.run_once(max_per_run=3)
        self.assertEqual(len(self.items()), 6)


class ExportTest(OutboundBase):
    def test_suggest_mode_pinned_and_verified(self):
        write_entry(self.store, "a1", title="Do the thing")
        report = self.run_once()
        self.assertEqual(report["exported"], 1)
        (prompt, tools, kwargs), = self.writes()
        pins = tools[WTOOL]
        self.assertEqual(pins["mode"], "suggest")
        self.assertEqual(pins["secret"], SECRET)
        self.assertEqual(list(tools), [WTOOL])
        self.assertEqual(kwargs["attempts"], 1)
        # No existing list: heading created once, at the document end.
        self.assertEqual(pins["inserts"], [{
            "content": "## Inbox\n- [ ] Do the thing (mem:a1)",
            "parent": {"blockId": None, "position": "end"}}])
        self.assertIn("Do the thing (mem:a1)", prompt)
        self.assertEqual(self.items()["a1"]["state"], "exported")
        self.assertEqual(len(self.reads()), 2)

    def test_apply_mode_only_when_configured(self):
        write_entry(self.store, "a1")
        self.run_once(write_mode="apply")
        self.assertEqual(self.writes()[0][1][WTOOL]["mode"], "apply")

    def test_idempotent_via_accepted_markers(self):
        write_entry(self.store, "a1")
        write_entry(self.store, "a2")
        self.accepted_items = ["old (mem:a1)"]
        report = self.run_once()
        self.assertEqual(report["already_present"], 1)
        self.assertEqual(report["exported"], 1)
        pins = self.writes()[0][1][WTOOL]
        self.assertNotIn("mem:a1", json.dumps(pins["inserts"]))
        self.assertEqual(sorted(self.items()), ["a1", "a2"])

    def test_idempotent_via_pending_suggestions(self):
        write_entry(self.store, "a1")
        self.pending_markers = ["a1"]
        report = self.run_once()
        self.assertEqual(self.writes(), [])
        self.assertEqual(report["already_present"], 1)
        self.assertEqual(self.items()["a1"]["state"], "exported")

    def test_prefix_marker_does_not_hide_other_entry(self):
        write_entry(self.store, "a")
        self.accepted_items = ["x (mem:a-b)"]
        self.assertEqual(self.run_once()["exported"], 1)

    def test_inserts_after_last_inbox_item(self):
        write_entry(self.store, "a2")
        self.accepted_items = ["first", "second"]
        self.run_once()
        pins = self.writes()[0][1][WTOOL]
        self.assertEqual(pins["inserts"][0]["after"], bid(2))
        self.assertEqual(pins["inserts"][0]["content"],
                         "- [ ] Send the report (mem:a2)")

    def test_empty_inbox_inserts_after_heading(self):
        write_entry(self.store, "a2")
        self.run_once()  # creates heading + item (suggestion)
        self.assertTrue(self.heading)
        write_entry(self.store, "a3")
        self.run_once()
        inserts = self.writes()[1][1][WTOOL]["inserts"]
        self.assertIn("after", inserts[0])
        self.assertNotIn("Inbox", inserts[0]["content"])

    def test_pending_heading_holds_the_run(self):
        write_entry(self.store, "a1")
        self.pending_heading = True
        report = self.run_once()
        self.assertTrue(report["held_pending_heading"])
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.checkpoint.exists())

    def test_heading_only_in_suggestions_section_holds(self):
        write_entry(self.store, "a1")
        self.heading = False
        self.pending_markers = ["other"]
        original = self.document
        self.document = lambda: toon(Doc(), ["insert heading Inbox"])
        report = self.run_once()
        self.document = original
        self.assertTrue(report["held_pending_heading"])
        self.assertEqual(self.writes(), [])

    def test_no_candidates_makes_no_bridge_call(self):
        write_entry(self.store, "a1", tags=["personal"])
        self.assertEqual(self.run_once()["exported"], 0)
        self.assertEqual(self.calls, [])

    def test_verification_failure_records_nothing(self):
        write_entry(self.store, "a1")
        self.land = False
        with self.assertRaises(RuntimeError) as ctx:
            self.run_once()
        self.assertIn("verification failed", str(ctx.exception))
        self.assertNotIn("a1", str(ctx.exception).replace("1 of 1", ""))
        self.assertFalse(self.checkpoint.exists())

    def test_next_run_adopts_landed_write(self):
        write_entry(self.store, "a1")
        self.land = True
        orig = self._call

        def flaky(prompt, tools, **kw):
            if WTOOL in tools and not hasattr(self, "failed"):
                self.failed = True
                orig(prompt, tools, **kw)  # lands...
                raise claude_bridge.BridgeError("timeout")  # ...outcome unknown
            return orig(prompt, tools, **kw)

        with mock.patch.object(fto.claude_bridge, "call_tools", flaky):
            with self.assertRaises(claude_bridge.BridgeError):
                self.run_once()
        self.assertFalse(self.checkpoint.exists())
        report = self.run_once()
        self.assertEqual(report["already_present"], 1)
        self.assertEqual(len(self.writes()), 1)

    def test_write_with_mismatched_input_is_rejected(self):
        write_entry(self.store, "a1")
        orig = self._call

        def tamper(prompt, tools, **kw):
            rows = orig(prompt, tools, **kw)
            if WTOOL in tools:
                rows[0]["input"] = dict(rows[0]["input"], mode="apply")
            return rows

        with mock.patch.object(fto.claude_bridge, "call_tools", tamper):
            with self.assertRaises(claude_bridge.BridgeError):
                self.run_once()
        self.assertFalse(self.checkpoint.exists())

    def test_dry_run_makes_no_bridge_call(self):
        write_entry(self.store, "a1")
        report = fto.run(self.cfg, self.dir, self.checkpoint, dry_run=True)
        self.assertEqual(report["candidates"], 1)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.checkpoint.exists())

    def test_logs_carry_ids_not_titles_or_secret(self):
        write_entry(self.store, "a1", title="Very private title")
        with mock.patch.object(fto, "log") as log:
            self.run_once()
        blob = " ".join(str(c) for c in log.call_args_list)
        self.assertIn("a1", blob)
        self.assertNotIn("Very private", blob)
        self.assertNotIn(SECRET, blob)

    def test_rejects_bad_tools_and_mode(self):
        for override in ({"write_tool": "mcp__x__replace_block_text"},
                         {"write_tool": WTOOL + "_extra"},
                         {"read_tool": "mcp__x__insert_document_blocks"},
                         {"write_mode": "overwrite"},
                         {"outbound_since": None}):
            with self.assertRaises(RuntimeError):
                self.run_once(**override)
        self.assertEqual(self.calls, [])

    def test_entry_changed_before_write_is_dropped(self):
        write_entry(self.store, "a1")
        orig = fto.inspect

        def inspect_then_tag(text, heading):
            info = orig(text, heading)
            write_entry(self.store, "a1", tags=["no-fibery"])
            return info

        with mock.patch.object(fto, "inspect", inspect_then_tag):
            report = self.run_once()
        self.assertEqual(report["exported"], 0)
        self.assertEqual(self.writes(), [])


class EscapingTest(unittest.TestCase):
    def test_title_is_escaped_bounded_single_line_redacted(self):
        title = "# *bold* _it_ [link](x)\n`code` <ins>x</ins> mem:other-id " \
                "IL 123456782 " + "z" * 300
        out = fto.safe_title(title)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), fto.TITLE_LIMIT)
        self.assertTrue(out.startswith("\\#"))
        for raw in ("*bold*", "_it_", "[link]", "`code`", "<ins>"):
            self.assertNotIn(raw, out)
        self.assertNotIn("mem:", out)
        self.assertIn("\\*bold\\*", out)

    def test_redaction_and_leading_dash(self):
        out = fto.safe_title("- Pay card 4111 1111 1111 1111 today")
        self.assertTrue(out.startswith("\\-"))
        self.assertNotIn("4111", out)
        self.assertIn("\\[ID\\]", out)

    def test_no_dangling_escape_after_truncation(self):
        out = fto.safe_title("*" * 200)
        self.assertLessEqual(len(out), fto.TITLE_LIMIT)
        self.assertFalse(out.rstrip("…").endswith("\\") and
                         len(out.rstrip("…")) - len(out.rstrip("…").rstrip("\\")) % 2 == 1)

    def test_empty_title(self):
        self.assertEqual(fto.safe_title("  \n "), "Untitled")


class InboundCoordinationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / "store"
        self.memory = FakeMemory(self.store)
        self.checkpoint = self.dir / "state" / "fibery-tasks.json"
        self.cfg = {"document_secret": SECRET, "read_tool": RTOOL,
                    "store": str(self.store), "owner_slug": OWNER}
        self.text = ""
        for p in (mock.patch.object(fti, "_memory_cli", self.memory),
                  mock.patch.object(fti, "_commit_store"),
                  mock.patch.object(fti.claude_bridge, "call_tools", self._call)):
            p.start()
            self.addCleanup(p.stop)

    def _call(self, prompt, tools, **kw):
        return [{"name": RTOOL, "input": {"secret": SECRET},
                 "text": self.text, "is_error": False}]

    def run_once(self, text, **cfg):
        self.text = text
        return fti.run({**self.cfg, **cfg}, self.dir, self.checkpoint)

    def items(self):
        return json.loads(self.checkpoint.read_text())["items"]

    def doc(self, state="TODO", text="Send report (mem:a1)"):
        doc = Doc()
        doc.heading("Inbox")
        doc.bullets([(bid(1), state, [q(text)])])
        return toon(doc)

    def test_marker_links_instead_of_creating(self):
        write_entry(self.store, "a1")
        report = self.run_once(self.doc())
        self.assertEqual(report["created"], 0)
        self.assertEqual(report["linked"], 1)
        self.assertEqual(self.memory.calls, [])
        row = self.items()[bid(1)]
        self.assertEqual(row["entry_id"], "a1")
        self.assertTrue(row["linked"])

    def test_linked_done_adds_note_but_edit_never_rewrites(self):
        write_entry(self.store, "a1")
        self.run_once(self.doc())
        self.run_once(self.doc(text="Reworded (mem:a1)"))
        self.assertEqual(self.memory.calls, [])
        self.assertTrue(self.items()[bid(1)]["linked"])
        self.run_once(self.doc("DONE", "Reworded (mem:a1)"))
        notes = self.memory.of("note")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][notes[0].index("--follows") + 1], "a1")
        self.assertEqual(self.memory.of("todo"), [])

    def test_unknown_marker_never_creates_a_todo(self):
        write_entry(self.store, "other")
        self.run_once(self.doc(text="X (mem:missing)"))
        self.assertEqual(self.memory.calls, [])
        self.assertIsNone(self.items()[bid(1)]["entry_id"])
        # Reopen path must not mint a todo either.
        self.run_once(self.doc("DONE", "X (mem:missing)"))
        self.run_once(self.doc("TODO", "X (mem:missing)"))
        self.assertEqual(self.memory.calls, [])

    def test_unmarked_tasks_still_create(self):
        write_entry(self.store, "a1")
        report = self.run_once(self.doc(text="Plain task"))
        self.assertEqual(report["created"], 1)

    def test_pending_suggestion_blocks_are_ignored(self):
        doc = Doc()
        doc.heading("Inbox")
        doc.bullets([
            (bid(1), "TODO", [q("<ins>Brand new (mem:a1)</ins>")]),
            (bid(2), "TODO", [q("Accepted task")]),
            (bid(3), "TODO", [q("Kept <ins>pending add</ins> text <del>gone</del>")]),
        ])
        text = toon(doc, [q("insert (mem:a1)"), q("another")])
        tasks = fti.parse_tasks(text)
        self.assertEqual([t["block_id"] for t in tasks], [bid(2), bid(3)])
        self.assertEqual(tasks[1]["text"], "Kept  text gone")
        report = self.run_once(text)
        self.assertEqual(report["created"], 2)
        self.assertNotIn(bid(1), self.items())

    def test_block_level_pending_attr_is_ignored(self):
        text = toon(Doc())
        doc = Doc()
        doc.bullets([(bid(1), "TODO", [q("x")])])
        raw = doc.text(wrap=False).replace(
            "    attrs:\n      state: TODO",
            "    attrs:\n      state: TODO\n      suggestion: insert")
        self.assertEqual(fti.parse_tasks(raw), [])
        del text

    def test_suggestions_section_is_not_parsed_as_blocks(self):
        doc = Doc()
        doc.heading("Inbox")
        text = toon(doc, ["id: zzz", "type: bogus"], wrap=False)
        roots, blocks = fti.parse_blocks(text)
        self.assertEqual(len(blocks), 1)


class ConfigTest(unittest.TestCase):
    def routine(self, **over):
        maintenance = {
            "kind": "fibery_tasks_outbound", "document_secret": SECRET,
            "read_tool": RTOOL, "write_tool": WTOOL, "store": "/tmp/store",
            "owner_slug": OWNER, "outbound_since": "2026-08-01",
        }
        maintenance.update(over)
        return {"id": "fib-out", "role": "maintenance",
                "maintenance": maintenance,
                "budget": {"daily_usd": 0.5}}

    def test_valid(self):
        self.assertEqual(config.validate(self.routine()), [])
        self.assertTrue(config.uses_claude_bridge(self.routine()))

    def test_rejections(self):
        for over in (
            {"write_tool": "mcp__claude_ai_Example_Fibery__replace_block_text"},
            {"write_tool": "mcp__claude_ai_Other__insert_document_blocks"},
            {"read_tool": "mcp__claude_ai_Example_Fibery__insert_document_blocks"},
            {"write_mode": "overwrite"},
            {"outbound_since": None},
            {"outbound_since": "2026-13-45"},
            {"owner_slug": None},
            {"from_sources": []},
            {"inbox_heading": "a\nb"},
            {"max_per_run": 0},
            {"bogus": 1},
        ):
            self.assertTrue(config.validate(self.routine(**over)), over)

    def test_dispatch(self):
        from workspace_daemon import maintenance
        routine = self.routine()
        routine["maintenance"]["store"] = "/tmp/store"
        with mock.patch.object(fto, "run", return_value={
                "candidates": 0, "exported": 0, "already_present": 0,
                "mode": "suggest", "dry_run": True}) as run:
            maintenance.run(Path("/tmp/base"), routine, dry_run=True)
        self.assertTrue(run.call_args[1]["dry_run"])
        self.assertEqual(run.call_args[0][2],
                         Path("/tmp/base/state/fibery-outbound.json"))


if __name__ == "__main__":
    unittest.main()
